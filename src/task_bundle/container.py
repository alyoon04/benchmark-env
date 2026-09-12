"""Thin Docker wrapper: image build/inspect, hardened detached containers, exec, cp.

Uses the docker CLI via subprocess rather than the docker SDK: one fewer heavy
dependency, identical capability for our needs, and failures surface the exact
command for debugging. All predictable failures raise DockerError with an
actionable message.

Container hardening defaults (DESIGN.md §4): no network, non-root uid, memory/CPU/pid
limits, all capabilities dropped, no-new-privileges. Hidden tests are staged with
``cp_in`` into *running* containers only — they never enter an image layer.
"""

import io
import json
import secrets
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from task_bundle.errors import DockerError

WORKDIR = "/workspace"
"""Default in-container repo path for native bundles (prebuilt images keep their own)."""
SANDBOX_UID = "1000:1000"
MEMORY_LIMIT = "4g"
CPU_LIMIT = "2"
PIDS_LIMIT = "512"

_DAEMON_HINT = "Is Docker running? Start Docker Desktop (or the docker daemon) and retry."


@dataclass(frozen=True)
class ExecResult:
    """Outcome of one command executed inside a container."""

    exit_code: int
    output: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _docker(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise DockerError("docker CLI not found on PATH. Install Docker and retry.") from e


def _docker_bytes(
    args: list[str], timeout: int | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Like ``_docker`` but with binary stdout/stderr (for tar streams)."""
    cmd = ["docker", *args]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise DockerError("docker CLI not found on PATH. Install Docker and retry.") from e


# docker exec passes argv through its API as JSON, so the practical bound is the
# container's ARG_MAX; chunking keeps every batch comfortably inside it.
ARGV_BATCH = 500


class Docker:
    """Stateless wrapper over the docker CLI."""

    runtime_name = "docker"

    def ensure_available(self) -> None:
        """Raise DockerError unless the daemon is reachable."""
        try:
            proc = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=30)
        except subprocess.TimeoutExpired:
            return  # daemon reachable but busy (e.g. a large pull in flight)
        if proc.returncode != 0:
            raise DockerError(f"Docker daemon unreachable. {_DAEMON_HINT}")

    def version(self) -> str:
        proc = _docker(["version", "--format", "{{.Server.Version}}"], timeout=30)
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"

    # -- images ------------------------------------------------------------

    def image_exists(self, tag: str) -> bool:
        return _docker(["image", "inspect", tag], timeout=60).returncode == 0

    def image_id(self, tag: str) -> str | None:
        proc = _docker(["image", "inspect", "--format", "{{.Id}}", tag], timeout=60)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def image_workdir(self, tag: str) -> str:
        """The image's configured WorkingDir ("" if unset or the image is absent)."""
        proc = _docker(["image", "inspect", "--format", "{{.Config.WorkingDir}}", tag], timeout=60)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def pull(self, image: str, *, timeout: int = 1800) -> None:
        """Pull ``image`` from its registry (network on)."""
        try:
            proc = _docker(["pull", image], timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"Pulling {image} timed out after {timeout}s.") from e
        if proc.returncode != 0:
            raise DockerError(f"Could not pull {image}: {proc.stderr.strip()}")

    def build(self, context: Path, tag: str, *, network: bool = True, timeout: int = 1800) -> str:
        """Build ``context`` (containing a Dockerfile) into ``tag``; return the build log."""
        args = ["build", "-t", tag]
        if not network:
            args += ["--network", "none"]
        args.append(str(context))
        try:
            proc = _docker(args, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DockerError(
                f"Image build for {tag} timed out after {timeout}s. "
                "Slow network or a hanging setup command? Check setup_commands in task.json."
            ) from e
        log = proc.stdout + proc.stderr
        if proc.returncode != 0:
            tail = "\n".join(log.splitlines()[-25:])
            raise DockerError(
                f"Image build failed for {tag} (exit {proc.returncode}). "
                f"Last lines of the build log:\n{tail}"
            )
        return log

    def rmi(self, tag: str) -> None:
        _docker(["rmi", "-f", tag], timeout=120)

    def tag(self, source: str, target: str) -> None:
        proc = _docker(["tag", source, target], timeout=120)
        if proc.returncode != 0:
            raise DockerError(f"Could not tag {source} as {target}: {proc.stderr.strip()}")

    def push(self, tag: str, *, timeout: int = 1800) -> None:
        try:
            proc = _docker(["push", tag], timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"Pushing {tag} timed out after {timeout}s.") from e
        if proc.returncode != 0:
            raise DockerError(f"Could not push {tag}: {proc.stderr.strip()}")

    def list_images(self, repo_prefix: str) -> list[str]:
        """Return ``repository:tag`` for every image whose ref starts with ``repo_prefix``."""
        proc = _docker(["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=60)
        if proc.returncode != 0:
            return []
        return sorted(line for line in proc.stdout.splitlines() if line.startswith(repo_prefix))

    # -- containers ----------------------------------------------------------

    def run_detached(
        self, image: str, *, network_off: bool = True, workdir: str | None = None
    ) -> str:
        """Start a hardened, idle container and return its id.

        The container runs ``sleep infinity`` so we can ``exec`` repeatedly; callers
        must ``rm_force`` it when done. Assumes ``sleep`` exists in the image (true
        for any practical dev base image). ``workdir`` defaults to the image's own
        WorkingDir, which task images set to the repo directory.
        """
        args = [
            "run", "-d", "--rm",
            "--user", SANDBOX_UID,
            "--memory", MEMORY_LIMIT,
            "--cpus", CPU_LIMIT,
            "--pids-limit", PIDS_LIMIT,
            # Drop everything, then re-add only what root-user *orchestrator* execs
            # need to stage hidden tests (chown/mkdir in dirs owned by the sandbox
            # uid). The solver itself runs as SANDBOX_UID and gains nothing from
            # these, and no-new-privileges blocks escalation.
            "--cap-drop", "ALL",
            "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE",
            "--cap-add", "FOWNER",
            "--security-opt", "no-new-privileges",
            # Override any image ENTRYPOINT (e.g. SWE-bench Pro images set
            # ENTRYPOINT ["/bin/bash"], which would mangle the idle command into
            # `bash sleep infinity` — bash reading a binary as a script).
            "--entrypoint", "sleep",
        ]  # fmt: skip
        if workdir:
            args += ["-w", workdir]
        if network_off:
            args += ["--network", "none"]
        args += [image, "infinity"]
        proc = _docker(args, timeout=120)
        if proc.returncode != 0:
            raise DockerError(
                f"Could not start container from {image}: {proc.stderr.strip()}\n{_DAEMON_HINT}"
            )
        return proc.stdout.strip()

    def exec(
        self,
        container_id: str,
        command: str,
        *,
        timeout: int,
        user: str | None = None,
        workdir: str | None = None,
    ) -> ExecResult:
        """Run ``command`` through ``sh -c`` inside the container.

        ``workdir`` defaults to the container's working directory (the repo dir).
        """
        args = ["exec"]
        if workdir:
            args += ["-w", workdir]
        if user:
            args += ["--user", user]
        args += [container_id, "sh", "-c", command]
        start = time.monotonic()
        try:
            proc = _docker(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecResult(
                exit_code=-1,
                output=f"(timed out after {timeout}s)",
                duration_seconds=time.monotonic() - start,
                timed_out=True,
            )
        return ExecResult(
            exit_code=proc.returncode,
            output=proc.stdout + proc.stderr,
            duration_seconds=time.monotonic() - start,
        )

    def exec_argv(
        self,
        container_id: str,
        argv: list[str],
        *,
        timeout: int,
        user: str | None = None,
        workdir: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``argv`` directly (no shell) inside the container; binary output.

        For orchestrator commands over arbitrary path lists: argv is handed to the
        docker API verbatim, so there is nothing to quote and no shell to trip on.
        """
        args = ["exec"]
        if workdir:
            args += ["-w", workdir]
        if user:
            args += ["--user", user]
        args += [container_id, *argv]
        try:
            return _docker_bytes(args, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DockerError(
                f"`{' '.join(argv[:3])} ...` in container {container_id[:12]} timed out "
                f"after {timeout}s."
            ) from e

    def exec_argv_batched(
        self,
        container_id: str,
        argv_prefix: list[str],
        paths: list[str],
        *,
        timeout: int,
        user: str | None = None,
        workdir: str | None = None,
    ) -> None:
        """Run ``argv_prefix + <chunk of paths>`` for every chunk; raise on failure."""
        for start in range(0, len(paths), ARGV_BATCH):
            chunk = paths[start : start + ARGV_BATCH]
            proc = self.exec_argv(
                container_id, [*argv_prefix, *chunk], timeout=timeout, user=user, workdir=workdir
            )
            if proc.returncode != 0:
                raise DockerError(
                    f"`{' '.join(argv_prefix)} ...` failed in container {container_id[:12]} "
                    f"(exit {proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
                )

    def archive(
        self, container_id: str, workdir: str, paths: list[str], dest: Path, *, timeout: int = 600
    ) -> None:
        """Copy ``paths`` (relative to ``workdir``) out of the container into ``dest``.

        One ``tar`` stream per batch instead of one ``docker cp`` per file: the
        difference between seconds and many minutes when a solver touches thousands
        of files. Missing paths are an error (callers pass paths they observed).
        """
        dest.mkdir(parents=True, exist_ok=True)
        for start in range(0, len(paths), ARGV_BATCH):
            chunk = [f"./{p}" for p in paths[start : start + ARGV_BATCH]]
            proc = self.exec_argv(
                container_id,
                ["tar", "-cf", "-", *chunk],
                timeout=timeout,
                user="0",
                workdir=workdir,
            )
            if proc.returncode != 0:
                raise DockerError(
                    f"tar in container {container_id[:12]} failed (exit {proc.returncode}): "
                    f"{proc.stderr.decode(errors='replace').strip()}"
                )
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
                tar.extractall(dest, filter="data")

    def cp_in(self, container_id: str, src: Path | str, dest: str) -> None:
        """Copy a host file/dir into the container (arrives root-owned; chown after).

        ``src`` may be a raw string ending in ``/.`` to copy a directory's *contents*
        (pathlib would normalize that suffix away, changing docker cp semantics).
        """
        proc = _docker(["cp", str(src), f"{container_id}:{dest}"], timeout=300)
        if proc.returncode != 0:
            raise DockerError(f"docker cp {src} -> {dest} failed: {proc.stderr.strip()}")

    def cp_out(self, container_id: str, src: str, dest: Path) -> None:
        """Copy a file/dir out of the container (``src`` may end in ``/.`` for contents)."""
        proc = _docker(["cp", f"{container_id}:{src}", str(dest)], timeout=300)
        if proc.returncode != 0:
            raise DockerError(f"docker cp {src} -> {dest} failed: {proc.stderr.strip()}")

    def rm_force(self, container_id: str) -> None:
        _docker(["rm", "-f", container_id], timeout=120)


def _kubectl(
    args: list[str], *, timeout: int | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["kubectl", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise DockerError("kubectl not found on PATH. Install kubectl and retry.") from e


class Kubernetes(Docker):
    """Docker-compatible container runtime backed by short-lived Kubernetes pods.

    Images must already be available from the cluster's registry. The fleet CLI
    handles the local build/tag/push step before dispatching jobs here.
    """

    runtime_name = "kubernetes"

    def __init__(
        self,
        *,
        namespace: str = "default",
        network_policy: str = "task-bundle-deny-egress",
        image_pull_policy: str = "IfNotPresent",
    ) -> None:
        self.namespace = namespace
        self.network_policy = network_policy
        self.image_pull_policy = image_pull_policy

    def ensure_available(self) -> None:
        proc = _kubectl(["cluster-info"], timeout=30)
        if proc.returncode != 0:
            raise DockerError(f"Kubernetes cluster unreachable: {proc.stderr.strip()}")
        policy = _kubectl(
            ["get", "networkpolicy", self.network_policy, "-n", self.namespace], timeout=30
        )
        if policy.returncode != 0:
            raise DockerError(
                f"Kubernetes backend requires NetworkPolicy {self.network_policy!r} in "
                f"namespace {self.namespace!r} to preserve solver network isolation."
            )

    def version(self) -> str:
        proc = _kubectl(["version", "--client", "-o", "json"], timeout=30)
        if proc.returncode != 0:
            return "unknown"
        try:
            data = json.loads(proc.stdout)
            return str(data["clientVersion"]["gitVersion"])
        except (KeyError, json.JSONDecodeError):
            return "unknown"

    def image_exists(self, tag: str) -> bool:
        # Registry reachability is established when Kubernetes starts the pod.
        return True

    def image_id(self, tag: str) -> str | None:
        return None

    def pull(self, image: str, *, timeout: int = 1800) -> None:
        raise DockerError("Kubernetes images must be pushed to a registry before execution.")

    def build(self, context: Path, tag: str, *, network: bool = True, timeout: int = 1800) -> str:
        raise DockerError("Build task images with Docker, then push them for Kubernetes.")

    def run_detached(
        self, image: str, *, network_off: bool = True, workdir: str | None = None
    ) -> str:
        name = f"task-bundle-{secrets.token_hex(6)}"
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": {"app.kubernetes.io/name": "task-bundle", "task-bundle/network": "off"},
            },
            "spec": {
                "restartPolicy": "Never",
                "securityContext": {
                    "runAsUser": 1000,
                    "runAsGroup": 1000,
                    "fsGroup": 1000,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "worker",
                        "image": image,
                        "imagePullPolicy": self.image_pull_policy,
                        "command": ["sleep", "infinity"],
                        "workingDir": workdir or WORKDIR,
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "limits": {"cpu": CPU_LIMIT, "memory": "4Gi"},
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                        },
                    }
                ],
            },
        }
        proc = _kubectl(
            ["apply", "-f", "-"], timeout=60, stdin=json.dumps(manifest, separators=(",", ":"))
        )
        if proc.returncode != 0:
            raise DockerError(f"Could not create Kubernetes pod {name}: {proc.stderr.strip()}")
        ready = _kubectl(
            [
                "wait",
                "--for=condition=Ready",
                f"pod/{name}",
                "-n",
                self.namespace,
                "--timeout=120s",
            ],
            timeout=130,
        )
        if ready.returncode != 0:
            self.rm_force(name)
            raise DockerError(f"Kubernetes pod {name} did not become ready: {ready.stderr.strip()}")
        return name

    def exec(
        self,
        container_id: str,
        command: str,
        *,
        timeout: int,
        user: str | None = None,
        workdir: str | None = None,
    ) -> ExecResult:
        del user  # Kubernetes exec uses the pod's hardened uid (1000) for every command.
        start = time.monotonic()
        try:
            proc = _kubectl(
                [
                    "exec",
                    "-n",
                    self.namespace,
                    container_id,
                    "--",
                    "sh",
                    "-c",
                    f"cd {workdir or WORKDIR} && {command}",
                ],
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(-1, f"(timed out after {timeout}s)", time.monotonic() - start, True)
        return ExecResult(proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start)

    def cp_in(self, container_id: str, src: Path | str, dest: str) -> None:
        proc = _kubectl(["cp", str(src), f"{self.namespace}/{container_id}:{dest}"], timeout=300)
        if proc.returncode != 0:
            raise DockerError(f"kubectl cp {src} -> {dest} failed: {proc.stderr.strip()}")

    def cp_out(self, container_id: str, src: str, dest: Path) -> None:
        proc = _kubectl(["cp", f"{self.namespace}/{container_id}:{src}", str(dest)], timeout=300)
        if proc.returncode != 0:
            raise DockerError(f"kubectl cp {src} -> {dest} failed: {proc.stderr.strip()}")

    def rm_force(self, container_id: str) -> None:
        _kubectl(
            [
                "delete",
                "pod",
                container_id,
                "-n",
                self.namespace,
                "--ignore-not-found=true",
                "--wait=false",
            ],
            timeout=60,
        )
