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
from task_bundle.workspace import apply_patch, build_clean_tree, patch_changed_paths

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


def resolve_work_dir(docker: Docker, bundle: Bundle) -> str:
    """The in-container path the repo must live at, discovered from the base image.

    Prebuilt images (e.g. SWE-bench Pro) install the repo *editable* at their
    configured WorkingDir (commonly ``/app``): a finder maps ``import pkg`` to an
    absolute path under it. We must make that path resolve to our cleaned clone so
    tests import the code the solver edits. Native bundles (a plain base with no
    meaningful WorkingDir) use ``/workspace``; ``/`` is treated as "none" so we never
    symlink over the container root.
    """
    base = bundle.spec.environment.base_image
    if not docker.image_exists(base):
        docker.pull(base)
    work_dir = docker.image_workdir(base)
    return work_dir if work_dir and work_dir not in ("/", WORKDIR) else WORKDIR


def generate_dockerfile(bundle: Bundle, work_dir: str = WORKDIR) -> str:
    """Render the task Dockerfile (pure data -> text; committed to artifacts later).

    ``work_dir`` is the base image's repo path (see ``resolve_work_dir``). When it
    differs from ``/workspace`` the base ships an install bound to it, so we point
    that path at our clone with a symlink — discovered, never hardcoded, and never
    ``/``. The solver still works in ``/workspace`` (a clean, artifact-free tree, so
    diff capture stays clean); the symlink only redirects the editable import path.
    """
    env = bundle.spec.environment
    lines = [f"FROM {env.base_image}"]
    lines += [f"ENV {key}={shlex.quote(value)}" for key, value in sorted(env.env.items())]
    lines += [
        f"COPY --chown=1000:1000 repo/ {WORKDIR}/",
        f"WORKDIR {WORKDIR}",
    ]
    if work_dir != WORKDIR:
        lines.append(
            f"RUN rm -rf {shlex.quote(work_dir)} && ln -s {WORKDIR} {shlex.quote(work_dir)}"
        )
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
    work_dir = resolve_work_dir(docker, bundle)
    with tempfile.TemporaryDirectory(prefix="task-bundle-ctx-") as ctx_str:
        ctx = Path(ctx_str)
        build_clean_tree(
            bundle.workspace_dir, ctx / "repo", excludes=bundle.spec.solver.workspace_excludes
        )
        (ctx / "Dockerfile").write_text(generate_dockerfile(bundle, work_dir))
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
    """Materialize hidden tests inside a running container; return test ref -> bucket.

    DIRECTORIES format: hidden test files are copied into the staging dir and the
    returned refs are their workdir-relative paths. TEST_PATCH format: the test
    patch is applied to a host-side copy of the baseline and only the changed files
    are copied in; the returned refs are the bundle's explicit test ids. Either way
    nothing hidden ever enters an image layer.
    """
    if bundle.test_format() is HiddenTestFormat.TEST_PATCH:
        return _stage_test_patch(docker, bundle, container_id)
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


def execute_staged_suite(
    docker: Docker, bundle: Bundle, container_id: str, *, attempt: int = 1
) -> list[TestExecution]:
    """Stage hidden tests into ``container_id`` and execute each once."""
    spec = bundle.spec.tests
    executions = []
    for test_path, bucket in stage_hidden_tests(docker, bundle, container_id).items():
        command = spec.command_template.format(test_path=shlex.quote(test_path))
        result = docker.exec(container_id, command, timeout=spec.timeout_seconds)
        status: Status = "timeout" if result.timed_out else "passed" if result.ok else "failed"
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
    return executions


def _stage_test_patch(docker: Docker, bundle: Bundle, container_id: str) -> dict[str, Bucket]:
    """Apply the test patch host-side and copy the changed files into the container."""
    changed = patch_changed_paths(bundle.test_patch_path)
    with tempfile.TemporaryDirectory(prefix="task-bundle-testpatch-") as tmp:
        tree = Path(tmp) / "tree"
        build_clean_tree(bundle.workspace_dir, tree)
        apply_patch(tree, bundle.test_patch_path)
        for rel in changed:
            src = tree / rel
            dest = f"{WORKDIR}/{rel}"
            if not src.exists():  # the test patch deleted this file
                docker.exec(container_id, f"rm -f {shlex.quote(dest)}", timeout=30, user="0")
                continue
            parent = f"{WORKDIR}/{Path(rel).parent}".rstrip("/.")
            docker.exec(container_id, f"mkdir -p {shlex.quote(parent)}", timeout=30, user="0")
            docker.cp_in(container_id, src, dest)
            docker.exec(container_id, f"chown 1000:1000 {shlex.quote(dest)}", timeout=30, user="0")
    spec = bundle.spec.tests
    staged: dict[str, Bucket] = dict.fromkeys(spec.fail2pass_ids, "fail2pass")
    staged.update(dict.fromkeys(spec.pass2pass_ids, "pass2pass"))
    return staged


def hidden_blobs(bundle: Bundle) -> list[bytes]:
    """Byte contents that must never appear in a solver-visible tree.

    DIRECTORIES: each hidden test file. TEST_PATCH: the patch itself plus the
    *patched* versions of every file it touches (the baseline versions remain
    visible by design — the repo at the pinned commit is what the solver gets).
    """
    if bundle.test_format() is HiddenTestFormat.DIRECTORIES:
        return [
            f.read_bytes()
            for bucket in ("fail2pass", "pass2pass")
            for f in bundle.hidden_test_files(bucket)  # type: ignore[arg-type]
        ]
    blobs = [bundle.test_patch_path.read_bytes()]
    with tempfile.TemporaryDirectory(prefix="task-bundle-hidden-") as tmp:
        tree = Path(tmp) / "tree"
        build_clean_tree(bundle.workspace_dir, tree)
        apply_patch(tree, bundle.test_patch_path)
        for rel in patch_changed_paths(bundle.test_patch_path):
            patched = tree / rel
            if patched.exists():
                blobs.append(patched.read_bytes())
    return blobs


def run_baseline_suites(
    docker: Docker, bundle: Bundle, tag: str, *, attempts: int = BASELINE_ATTEMPTS
) -> list[TestExecution]:
    """Execute all hidden tests ``attempts`` times, each attempt in a fresh container.

    Fresh containers per attempt so state mutated by one run (caches, temp files)
    cannot mask or cause flakiness in the next.
    """
    executions = []
    for attempt in range(1, attempts + 1):
        cid = docker.run_detached(tag)
        try:
            executions.extend(execute_staged_suite(docker, bundle, cid, attempt=attempt))
        finally:
            docker.rm_force(cid)
    return executions


def overlay_tree_into_container(docker: Docker, tree: Path, container_id: str) -> None:
    """Replace the container's /workspace contents with ``tree`` (handles deletions).

    Wipe-then-copy rather than patch-apply: no patch tooling is required inside the
    image (language-agnostic), and files the solver deleted actually disappear.
    """
    docker.exec(container_id, f"find {WORKDIR} -mindepth 1 -delete", timeout=120, user="0")
    docker.cp_in(container_id, f"{tree}/.", f"{WORKDIR}/")
    docker.exec(container_id, f"chown -R 1000:1000 {WORKDIR}", timeout=120, user="0")
