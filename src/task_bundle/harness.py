"""Bundle-aware container orchestration: task images, staging, suites, changesets.

Bridges the pure layers (bundle spec, grading, workspace) and the docker primitives:

- ``ensure_image`` builds the task image with a content-addressed tag. Native bundles
  COPY a *cleaned* clone (no .git, no workspace_excludes) to ``/workspace``; prebuilt
  images keep their own repo directory intact — submodules, ``node_modules``, build
  artifacts and all — with only ``.git`` scrubbed.
- Solvers work **in place** in a container of that image. ``manifest`` hashes every
  file in the repo dir; the before/after manifests give the solver's changeset, which
  ``pull_changes`` copies out and ``push_files`` replays into a fresh grade container.
- ``run_baseline_suites`` / ``execute_staged_suite`` stage hidden tests into running
  containers via ``docker cp`` (per DESIGN.md §3 they exist in no image layer) and
  execute each test through the bundle's command template.
"""

import hashlib
import json
import shlex
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from task_bundle.bundle import Bundle, HiddenTestFormat
from task_bundle.container import ARGV_BATCH, SANDBOX_UID, WORKDIR, Docker
from task_bundle.errors import BundleError, DockerError
from task_bundle.grading import Bucket, Status, TestExecution
from task_bundle.workspace import (
    Changeset,
    build_clean_tree,
    materialize_patch,
    parse_manifest,
)

IMAGE_REPO = "task-bundle"
BASELINE_ATTEMPTS = 3
IMAGE_LAYOUT_VERSION = 2  # bump when generate_dockerfile changes shape: invalidates caches
MANIFEST_TIMEOUT = 900


@dataclass(frozen=True)
class TaskImage:
    """A built task image and the in-container path its repository lives at."""

    tag: str
    repo_dir: str


def image_cache_key(bundle: Bundle) -> str:
    """Content hash over everything that affects the built image."""
    env = bundle.spec.environment
    material = json.dumps(
        {
            "layout": IMAGE_LAYOUT_VERSION,
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
    """The in-container path the repo lives at, discovered from the base image.

    Prebuilt images (e.g. SWE-bench Pro) ship the repo — with every dependency
    installed against it — at their configured WorkingDir (commonly ``/app``). That
    directory is the task's repository and is used as-is. Native bundles (a plain
    base with no meaningful WorkingDir) get the clone at ``/workspace``; ``/`` is
    treated as "none" so the container root is never mistaken for a repo.
    """
    base = bundle.spec.environment.base_image
    if not docker.image_exists(base):
        docker.pull(base)
    work_dir = docker.image_workdir(base)
    return work_dir if work_dir and work_dir not in ("/", WORKDIR) else WORKDIR


def generate_dockerfile(bundle: Bundle, work_dir: str = WORKDIR) -> str:
    """Render the task Dockerfile (pure data -> text; committed to artifacts later).

    ``work_dir`` is the repo path (see ``resolve_work_dir``). Two layouts:

    - **native** (``/workspace``): COPY the cleaned clone in. Hidden tests are never
      in the build context, so they cannot be in any layer.
    - **prebuilt** (anything else): the base image's own repo directory is kept
      intact so nothing that lives *under* it but outside git (submodules,
      ``node_modules``, compiled extensions) is lost. Only ``.git`` (including
      submodule ``.git`` files) is removed — the image's clone carries full history,
      which could include the fix and its tests — and the tree is handed to the
      sandbox uid. ``workspace_excludes`` are removed from the tree; content already
      shipped in a base image cannot be structurally excluded, only deleted, and the
      leak guard still checks the running container.
    """
    env = bundle.spec.environment
    lines = [f"FROM {env.base_image}"]
    lines += [f"ENV {key}={shlex.quote(value)}" for key, value in sorted(env.env.items())]
    excludes = bundle.spec.solver.workspace_excludes
    if work_dir == WORKDIR:
        lines += [f"COPY --chown={SANDBOX_UID} repo/ {WORKDIR}/", f"WORKDIR {WORKDIR}"]
    else:
        q = shlex.quote(work_dir)
        scrub = [f"find {q} -name .git -prune -exec rm -rf {{}} +"]
        scrub += [f"rm -rf {shlex.quote(f'{work_dir}/{e}')}" for e in excludes]
        lines += [f"WORKDIR {work_dir}", "RUN " + " && ".join(scrub)]
    if env.setup_commands:
        # Setup runs as root (system package installs etc.).
        lines.append("RUN " + " && ".join(env.setup_commands))
    if work_dir != WORKDIR or env.setup_commands:
        # Hand the tree to the sandbox uid the containers run as.
        lines.append(f"RUN chown -R {SANDBOX_UID} {shlex.quote(work_dir)}")
    return "\n".join(lines) + "\n"


def ensure_image(docker: Docker, bundle: Bundle, *, rebuild: bool = False) -> tuple[TaskImage, str]:
    """Build the task image if absent; return (image, build_log). Cached by content key.

    For native bundles the build context contains only the cleaned tree: hidden
    tests live in the bundle, outside the workspace, and so can never appear in a
    layer. The repo dir of a cached image is read back from its WorkingDir, so an
    image is self-describing.
    """
    if not bundle.workspace_dir.is_dir():
        raise BundleError(
            f"Bundle {bundle.spec.id} has no workspace clone. Run `task init {bundle.path}` first."
        )
    tag = image_tag(bundle)
    if not rebuild and docker.image_exists(tag):
        return TaskImage(tag, docker.image_workdir(tag) or WORKDIR), ""
    work_dir = resolve_work_dir(docker, bundle)
    with tempfile.TemporaryDirectory(prefix="task-bundle-ctx-") as ctx_str:
        ctx = Path(ctx_str)
        if work_dir == WORKDIR:
            build_clean_tree(
                bundle.workspace_dir, ctx / "repo", excludes=bundle.spec.solver.workspace_excludes
            )
        (ctx / "Dockerfile").write_text(generate_dockerfile(bundle, work_dir))
        log = docker.build(ctx, tag, network=bundle.spec.environment.network_during_setup)
    return TaskImage(tag, work_dir), log


def smoke_test(docker: Docker, image: TaskImage) -> None:
    """Verify a hardened container from the image starts and can execute commands."""
    cid = docker.run_detached(image.tag)
    try:
        result = docker.exec(cid, "exit 0", timeout=30)
        if not result.ok:
            raise DockerError(
                f"Smoke test failed: container from {image.tag} cannot execute commands "
                f"(exit {result.exit_code}). Output:\n{result.output}"
            )
    finally:
        docker.rm_force(cid)


# -- in-place changeset protocol -------------------------------------------------


@dataclass(frozen=True)
class TreeSnapshot:
    """Content hashes plus (size, mtime) of every regular file under the repo dir."""

    hashes: dict[str, str]
    stats: dict[str, str]


class SnapshotCache:
    """Per-image cache of the pre-solve snapshot (thread-safe).

    A fresh container from a given image always has the same tree, so hashing it
    once per image — not once per attempt — is exact, and it is the single most
    expensive step of a run on large repositories.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, TreeSnapshot] = {}
        self._lock = threading.Lock()
        self.hits = 0

    def get(self, docker: Docker, image: TaskImage, container_id: str) -> TreeSnapshot:
        with self._lock:
            cached = self._snapshots.get(image.tag)
            if cached is not None:
                self.hits += 1
                return cached
        snap = snapshot(docker, image, container_id)
        with self._lock:
            self._snapshots.setdefault(image.tag, snap)
        return snap


def stat_listing(docker: Docker, image: TaskImage, container_id: str) -> dict[str, str]:
    """``path -> "<size> <mtime>"`` for every regular file under the repo dir (cheap)."""
    proc = docker.exec_argv(
        container_id,
        ["sh", "-c", "find . -type f -exec stat -c '%s %Y %n' {} +"],
        timeout=MANIFEST_TIMEOUT,
        user="0",
        workdir=image.repo_dir,
    )
    if proc.returncode != 0:
        raise DockerError(
            f"Could not list the repository tree in {image.tag} (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}\n"
            "The image needs `find` and `stat` (coreutils or busybox)."
        )
    return parse_stat_listing(proc.stdout.decode(errors="surrogateescape"))


def parse_stat_listing(text: str) -> dict[str, str]:
    listing: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            listing[parts[2].removeprefix("./")] = f"{parts[0]} {parts[1]}"
    return listing


def hash_paths(
    docker: Docker, image: TaskImage, container_id: str, paths: list[str]
) -> dict[str, str]:
    """sha256 of just ``paths`` (relative to the repo dir), batched to stay under ARG_MAX."""
    hashes: dict[str, str] = {}
    for start in range(0, len(paths), ARGV_BATCH):
        chunk = [f"./{p}" for p in paths[start : start + ARGV_BATCH]]
        proc = docker.exec_argv(
            container_id,
            ["sha256sum", *chunk],
            timeout=MANIFEST_TIMEOUT,
            user="0",
            workdir=image.repo_dir,
        )
        if proc.returncode != 0:
            raise DockerError(
                f"Could not hash changed files in {image.tag} (exit {proc.returncode}): "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )
        hashes.update(parse_manifest(proc.stdout.decode(errors="surrogateescape")))
    return hashes


def snapshot(docker: Docker, image: TaskImage, container_id: str) -> TreeSnapshot:
    """Full pre-solve snapshot: every file hashed (leak guard) and stat'ed (change detection)."""
    return TreeSnapshot(
        hashes=manifest(docker, image, container_id),
        stats=stat_listing(docker, image, container_id),
    )


def snapshot_after(
    docker: Docker, image: TaskImage, container_id: str, before: TreeSnapshot
) -> dict[str, str]:
    """Post-solve manifest, hashing only files whose size or mtime changed.

    Deletions come from the name listing; unchanged (size, mtime) pairs keep their
    pre-solve hash. Every write a solver can make through the container updates
    mtime, so this is exact for real edits while costing a stat pass instead of a
    full re-hash of the tree.
    """
    stats = stat_listing(docker, image, container_id)
    changed = sorted(p for p, s in stats.items() if before.stats.get(p) != s)
    return merge_snapshot(before, stats, hash_paths(docker, image, container_id, changed))


def merge_snapshot(
    before: TreeSnapshot, after_stats: dict[str, str], changed_hashes: dict[str, str]
) -> dict[str, str]:
    """Pure: pre-solve hashes, minus files now absent, overlaid with re-hashed changes."""
    hashes = {p: h for p, h in before.hashes.items() if p in after_stats}
    hashes.update(changed_hashes)
    return hashes


def manifest(docker: Docker, image: TaskImage, container_id: str) -> dict[str, str]:
    """sha256 of every regular file under the repo dir: relative path -> digest.

    Runs as root so unreadable files cannot hide from the leak guard or the
    changeset. Symlinks are not followed (and not tracked) — a solver's edits are
    edits to regular files.
    """
    proc = docker.exec_argv(
        container_id,
        ["sh", "-c", "find . -type f -exec sha256sum {} +"],
        timeout=MANIFEST_TIMEOUT,
        user="0",
        workdir=image.repo_dir,
    )
    if proc.returncode != 0:
        raise DockerError(
            f"Could not hash the repository tree in {image.tag} (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}\n"
            "The image needs `find` and `sha256sum` (coreutils or busybox)."
        )
    return parse_manifest(proc.stdout.decode(errors="surrogateescape"))


def pull_changes(
    docker: Docker, image: TaskImage, container_id: str, paths: list[str], dest: Path
) -> None:
    """Copy ``paths`` (relative to the repo dir) out of the container into ``dest``."""
    if paths:
        docker.archive(container_id, image.repo_dir, paths, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)


def push_files(
    docker: Docker, image: TaskImage, container_id: str, tree: Path, paths: list[str]
) -> None:
    """Replay a sparse tree into the container's repo dir.

    Every path present in ``tree`` is copied in; every path absent from it is
    deleted (that is how deletions travel). No patch tooling is needed inside the
    image — this is plain file transport, so it is language-agnostic. Copied files
    and any directories created for them are handed to the sandbox uid.
    """
    present = [p for p in paths if (tree / p).is_file() or (tree / p).is_symlink()]
    kept = set(present)
    missing = [p for p in paths if p not in kept]
    repo = image.repo_dir
    if present:
        with tempfile.TemporaryDirectory(prefix="task-bundle-push-") as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()
            for rel in present:
                dest = stage / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tree / rel, dest, follow_symlinks=False)
            docker.cp_in(container_id, f"{stage}/.", f"{repo}/")
        ancestors = {str(parent) for rel in present for parent in Path(rel).parents}
        ancestors.discard(".")
        docker.exec_argv_batched(
            container_id,
            ["chown", "-h", SANDBOX_UID],
            [f"{repo}/{p}" for p in sorted((*present, *ancestors))],
            timeout=300,
            user="0",
        )
    if missing:
        docker.exec_argv_batched(
            container_id, ["rm", "-f"], [f"{repo}/{p}" for p in missing], timeout=300, user="0"
        )


def apply_changeset(
    docker: Docker, image: TaskImage, container_id: str, changes: Changeset, after: Path
) -> None:
    """Apply a solver's changeset (files in ``after``, deletions implied) to a container."""
    push_files(docker, image, container_id, after, changes.all_paths)


# -- hidden tests ---------------------------------------------------------------


def stage_hidden_tests(
    docker: Docker, bundle: Bundle, image: TaskImage, container_id: str
) -> dict[str, Bucket]:
    """Materialize hidden tests inside a running container; return test ref -> bucket.

    DIRECTORIES format: hidden test files are copied into the staging dir and the
    returned refs are their repo-relative paths. TEST_PATCH format: the test patch is
    applied to a sparse host-side copy of the baseline and only the changed files are
    pushed in; the returned refs are the bundle's explicit test ids. Either way
    nothing hidden ever enters an image layer.
    """
    if bundle.test_format() is HiddenTestFormat.TEST_PATCH:
        with materialize_patch(bundle.workspace_dir, bundle.test_patch_path) as (tree, paths):
            push_files(docker, image, container_id, tree, paths)
        spec = bundle.spec.tests
        staged: dict[str, Bucket] = dict.fromkeys(spec.fail2pass_ids, "fail2pass")
        staged.update(dict.fromkeys(spec.pass2pass_ids, "pass2pass"))
        return staged
    staging_rel = bundle.spec.tests.staging_dir.strip("/")
    staged = {}
    with tempfile.TemporaryDirectory(prefix="task-bundle-stage-") as tmp:
        tree = Path(tmp) / "tree"
        paths = []
        for bucket in ("fail2pass", "pass2pass"):
            bucket_dir = bundle.fail2pass_dir if bucket == "fail2pass" else bundle.pass2pass_dir
            for test_file in bundle.hidden_test_files(bucket):
                rel = f"{staging_rel}/{test_file.relative_to(bucket_dir).as_posix()}"
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(test_file, tree / rel)
                paths.append(rel)
                staged[rel] = bucket
        push_files(docker, image, container_id, tree, paths)
    return staged


def execute_staged_suite(
    docker: Docker, bundle: Bundle, image: TaskImage, container_id: str, *, attempt: int = 1
) -> list[TestExecution]:
    """Stage hidden tests into ``container_id`` and execute each once."""
    spec = bundle.spec.tests
    executions = []
    for test_path, bucket in stage_hidden_tests(docker, bundle, image, container_id).items():
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
    with materialize_patch(bundle.workspace_dir, bundle.test_patch_path) as (tree, paths):
        blobs += [(tree / rel).read_bytes() for rel in paths if (tree / rel).is_file()]
    return blobs


def run_baseline_suites(
    docker: Docker, bundle: Bundle, image: TaskImage, *, attempts: int = BASELINE_ATTEMPTS
) -> list[TestExecution]:
    """Execute all hidden tests ``attempts`` times, each attempt in a fresh container.

    Fresh containers per attempt so state mutated by one run (caches, temp files)
    cannot mask or cause flakiness in the next.
    """
    executions = []
    for attempt in range(1, attempts + 1):
        cid = docker.run_detached(image.tag)
        try:
            executions.extend(execute_staged_suite(docker, bundle, image, cid, attempt=attempt))
        finally:
            docker.rm_force(cid)
    return executions
