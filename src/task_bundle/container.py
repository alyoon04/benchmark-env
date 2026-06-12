"""Thin Docker wrapper: image build/inspect, hardened detached containers, exec, cp.

Uses the docker CLI via subprocess rather than the docker SDK: one fewer heavy
dependency, identical capability for our needs, and failures surface the exact
command for debugging. All predictable failures raise DockerError with an
actionable message.

Container hardening defaults (DESIGN.md §4): no network, non-root uid, memory/CPU/pid
limits, all capabilities dropped, no-new-privileges. Hidden tests are staged with
``cp_in`` into *running* containers only — they never enter an image layer.
"""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from task_bundle.errors import DockerError

WORKDIR = "/workspace"
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


class Docker:
    """Stateless wrapper over the docker CLI."""

    def ensure_available(self) -> None:
        """Raise DockerError unless the daemon is reachable."""
        proc = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=30)
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

    # -- containers ----------------------------------------------------------

    def run_detached(self, image: str, *, network_off: bool = True) -> str:
        """Start a hardened, idle container and return its id.

        The container runs ``sleep infinity`` so we can ``exec`` repeatedly; callers
        must ``rm_force`` it when done. Assumes ``sleep`` exists in the image (true
        for any practical dev base image).
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
            "-w", WORKDIR,
        ]  # fmt: skip
        if network_off:
            args += ["--network", "none"]
        args += [image, "sleep", "infinity"]
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
        workdir: str = WORKDIR,
    ) -> ExecResult:
        """Run ``command`` through ``sh -c`` inside the container."""
        args = ["exec", "-w", workdir]
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

    def cp_in(self, container_id: str, src: Path, dest: str) -> None:
        """Copy a host file/dir into the container (arrives root-owned; chown after)."""
        proc = _docker(["cp", str(src), f"{container_id}:{dest}"], timeout=300)
        if proc.returncode != 0:
            raise DockerError(f"docker cp {src} -> {dest} failed: {proc.stderr.strip()}")

    def rm_force(self, container_id: str) -> None:
        _docker(["rm", "-f", container_id], timeout=120)
