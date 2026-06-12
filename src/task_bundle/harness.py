"""Bundle-aware container orchestration: task images, staging, and suite execution.

Bridges the pure layers (bundle spec, grading) and the docker primitives:

- ``ensure_image`` builds the task image from a *cleaned* tree (no .git, no
  workspace_excludes) with a content-addressed tag, so repeat builds are no-ops.
- ``run_baseline_suites`` stages hidden tests into fresh containers via ``docker cp``
  (per DESIGN.md §3 they exist in no image layer) and executes each test through the
  bundle's command template, 3 attempts by default for flake detection.
"""

import hashlib
import json
import shlex
import tempfile
from pathlib import Path

from task_bundle.bundle import Bundle, HiddenTestFormat
from task_bundle.container import WORKDIR, Docker
from task_bundle.errors import BundleError, DockerError
from task_bundle.grading import Bucket, Status, TestExecution
from task_bundle.workspace import build_clean_tree

IMAGE_REPO = "task-bundle"
BASELINE_ATTEMPTS = 3


def image_cache_key(bundle: Bundle) -> str:
    """Content hash over everything that affects the built image."""
    env = bundle.spec.environment
    material = json.dumps(
        {
            "repo": bundle.spec.repo.url,
            "commit": bundle.spec.repo.commit,
            "base_image": env.base_image,
            "setup_commands": env.setup_commands,
            "env": env.env,
            "workspace_excludes": bundle.spec.solver.workspace_excludes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def image_tag(bundle: Bundle) -> str:
    return f"{IMAGE_REPO}/{bundle.spec.id}:{image_cache_key(bundle)}"


def generate_dockerfile(bundle: Bundle) -> str:
    """Render the task Dockerfile (pure data -> text; committed to artifacts later)."""
    env = bundle.spec.environment
    lines = [f"FROM {env.base_image}"]
    lines += [f"ENV {key}={shlex.quote(value)}" for key, value in sorted(env.env.items())]
    lines += [
        f"COPY --chown=1000:1000 repo/ {WORKDIR}/",
        f"WORKDIR {WORKDIR}",
    ]
    if env.setup_commands:
        # Setup runs as root (system package installs etc.), then the tree is handed
        # back to the sandbox uid the containers run as.
        lines.append("RUN " + " && ".join(env.setup_commands))
        lines.append(f"RUN chown -R 1000:1000 {WORKDIR}")
    return "\n".join(lines) + "\n"


def ensure_image(docker: Docker, bundle: Bundle, *, rebuild: bool = False) -> tuple[str, str]:
    """Build the task image if absent; return (tag, build_log). Cached by content key.

    The build context contains only the cleaned tree: hidden tests live in the
    bundle, outside the workspace, and so can never appear in a layer.
    """
    if not bundle.workspace_dir.is_dir():
        raise BundleError(
            f"Bundle {bundle.spec.id} has no workspace clone. Run `task init {bundle.path}` first."
        )
    tag = image_tag(bundle)
    if not rebuild and docker.image_exists(tag):
        return tag, ""
    with tempfile.TemporaryDirectory(prefix="task-bundle-ctx-") as ctx_str:
        ctx = Path(ctx_str)
        build_clean_tree(
            bundle.workspace_dir, ctx / "repo", excludes=bundle.spec.solver.workspace_excludes
        )
        (ctx / "Dockerfile").write_text(generate_dockerfile(bundle))
        log = docker.build(ctx, tag, network=bundle.spec.environment.network_during_setup)
    return tag, log


def smoke_test(docker: Docker, tag: str) -> None:
    """Verify a hardened container from the image starts and can execute commands."""
    cid = docker.run_detached(tag)
    try:
        result = docker.exec(cid, "exit 0", timeout=30)
        if not result.ok:
            raise DockerError(
                f"Smoke test failed: container from {tag} cannot execute commands "
                f"(exit {result.exit_code}). Output:\n{result.output}"
            )
    finally:
        docker.rm_force(cid)


def stage_hidden_tests(docker: Docker, bundle: Bundle, container_id: str) -> dict[str, Bucket]:
    """Copy hidden tests into a running container; return staged path -> bucket.

    Only the directories format is executable today; the SWE-bench test-patch format
    is wired in with `task import-swebench` (milestone 6).
    """
    if bundle.test_format() is HiddenTestFormat.TEST_PATCH:
        raise BundleError(
            "This bundle uses the SWE-bench test-patch format, which `task validate` "
            "does not execute yet (lands with `task import-swebench`)."
        )
    staging_rel = bundle.spec.tests.staging_dir.strip("/")
    staging_root = f"{WORKDIR}/{staging_rel}"
    docker.exec(container_id, f"mkdir -p {shlex.quote(staging_root)}", timeout=30, user="0")
    staged: dict[str, Bucket] = {}
    for bucket in ("fail2pass", "pass2pass"):
        bucket_dir = bundle.fail2pass_dir if bucket == "fail2pass" else bundle.pass2pass_dir
        for test_file in bundle.hidden_test_files(bucket):
            rel = test_file.relative_to(bucket_dir)
            if str(rel.parent) != ".":
                docker.exec(
                    container_id,
                    f"mkdir -p {shlex.quote(f'{staging_root}/{rel.parent}')}",
                    timeout=30,
                    user="0",
                )
            docker.cp_in(container_id, test_file, f"{staging_root}/{rel}")
            staged[f"{staging_rel}/{rel}"] = bucket
    docker.exec(
        container_id, f"chown -R 1000:1000 {shlex.quote(staging_root)}", timeout=60, user="0"
    )
    return staged


def run_baseline_suites(
    docker: Docker, bundle: Bundle, tag: str, *, attempts: int = BASELINE_ATTEMPTS
) -> list[TestExecution]:
    """Execute all hidden tests ``attempts`` times, each attempt in a fresh container.

    Fresh containers per attempt so state mutated by one run (caches, temp files)
    cannot mask or cause flakiness in the next.
    """
    spec = bundle.spec.tests
    executions = []
    for attempt in range(1, attempts + 1):
        cid = docker.run_detached(tag)
        try:
            staged = stage_hidden_tests(docker, bundle, cid)
            for test_path, bucket in staged.items():
                command = spec.command_template.format(test_path=shlex.quote(test_path))
                result = docker.exec(cid, command, timeout=spec.timeout_seconds)
                status: Status = (
                    "timeout" if result.timed_out else "passed" if result.ok else "failed"
                )
                executions.append(
                    TestExecution(
                        test=test_path,
                        bucket=bucket,
                        attempt=attempt,
                        status=status,
                        duration_seconds=round(result.duration_seconds, 3),
                        output=result.output,
                    )
                )
        finally:
            docker.rm_force(cid)
    return executions
