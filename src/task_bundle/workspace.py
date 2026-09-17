"""Workspace management: pinned-commit clones, patch materialization, changesets, diffs.

The baseline workspace lives at ``<bundle>/.task/workspace`` and is the
orchestrator-side source of truth for the repo at the pinned commit. The clone is
shallow (``fetch --depth 1 <sha>``) so the checkout cannot contain future commits —
defense in depth for the test-hiding invariant, and faster besides.

Solvers work *in place* in a container of the task image; this module holds the pure
host-side pieces of that protocol: parsing content manifests, computing the changeset
between two manifests (honouring the repo's own ``.gitignore``), rendering a unified
diff from before/after copies of the changed files, and the hidden-content leak guard.
"""

import fnmatch
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from task_bundle.errors import GitError, HiddenTestLeak

_NO_USER_CONFIG = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

# Engine hygiene, applied on top of the repo's own .gitignore when computing a
# changeset: bytecode/caches that any test run leaves behind and that no solver
# means as part of its fix. Deliberately tiny — everything else is the repo's call.
DEFAULT_IGNORES = [
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".hypothesis/",
]


def _git(
    args: list[str],
    cwd: Path,
    timeout: int = 600,
    env: dict[str, str] | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    # Every call site operates on a repo at cwd or on plain files (apply/numstat).
    # Stop upward .git discovery so an unrelated enclosing repo (e.g. a git-managed
    # $HOME) can't hijack the invocation — inside a foreign repo, `git apply`
    # silently skips paths outside the cwd prefix and exits 0. Ceiling entries must
    # be proper ancestors of the search start, hence the parent.
    ceiling = {"GIT_CEILING_DIRECTORIES": str(Path(cwd).resolve().parent)}
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **ceiling, **(env or {})},
        )
    except FileNotFoundError as e:
        raise GitError("git executable not found on PATH. Install git and retry.") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"`{' '.join(cmd)}` timed out after {timeout}s.") from e
    if proc.returncode not in ok_codes:
        raise GitError(f"`{' '.join(cmd)}` failed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    return proc


def _git_out(args: list[str], cwd: Path, timeout: int = 600) -> str:
    return _git(args, cwd, timeout).stdout


def resolve_repo_url(url: str, bundle_path: Path) -> str:
    """Resolve a repo URL, allowing bundle-relative local paths.

    A url like ``../.origin`` (no scheme, not absolute) is resolved against the
    bundle directory so committed example bundles can reference repos created on
    the local machine without machine-specific absolute paths in task.json.
    """
    if "://" in url or url.startswith(("git@", "/")):
        return url
    return str((bundle_path / url).resolve())


def clone_at_commit(url: str, commit: str, dest: Path, force: bool = False) -> None:
    """Materialize ``url`` at exactly ``commit`` into ``dest`` via a shallow fetch.

    Idempotent: if ``dest`` already holds the pinned commit, this is a no-op unless
    ``force`` re-clones from scratch.
    """
    if dest.exists() and any(dest.iterdir()):
        if not force and _head_commit(dest) == commit:
            return
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet"], cwd=dest)
    _git(["remote", "add", "origin", url], cwd=dest)
    try:
        _git(["fetch", "--quiet", "--depth", "1", "origin", commit], cwd=dest)
    except GitError as e:
        raise GitError(
            f"Could not fetch commit {commit} from {url}.\n"
            "Check that the URL is reachable and the SHA exists on the remote "
            "(some servers refuse direct-SHA fetches; full clone fallback is not "
            "implemented to keep checkouts shallow).\n"
            f"Underlying error: {e}"
        ) from e
    _git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=dest)
    actual = _head_commit(dest)
    if actual != commit:
        raise GitError(f"Checkout mismatch: expected {commit}, got {actual}.")


def _head_commit(repo: Path) -> str | None:
    if not (repo / ".git").exists():
        return None
    try:
        return _git_out(["rev-parse", "HEAD"], cwd=repo).strip()
    except GitError:
        return None


def build_clean_tree(workspace: Path, dest: Path, excludes: list[str] | None = None) -> None:
    """Copy the baseline workspace to ``dest`` WITHOUT .git and without ``excludes``.

    This is how native bundles' image build contexts are made: excluded content is
    never copied in the first place (never copy-then-delete), which is the
    structural half of the test-hiding invariant (DESIGN.md §3). ``excludes`` are
    repo-root-relative paths (files or directories).
    """
    if dest.exists():
        shutil.rmtree(dest)
    excluded = {".git", *(excludes or [])}
    excluded_abs = {(workspace / e).resolve() for e in excluded}

    def _ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if (Path(directory) / n).resolve() in excluded_abs}

    shutil.copytree(workspace, dest, ignore=_ignore, symlinks=True)


# -- patches -----------------------------------------------------------------


def apply_patch(tree: Path, patch: Path) -> None:
    """Apply a unified diff to ``tree`` (a plain directory; no repo required)."""
    try:
        _git(["apply", "--whitespace=nowarn", str(patch)], cwd=tree)
    except GitError as e:
        raise GitError(
            f"Patch {patch} does not apply cleanly to the workspace: {e}\n"
            "Was it generated against the pinned commit?"
        ) from e


def _rename_sources(patch: Path) -> list[str]:
    """Sources of ``rename``/``copy`` hunks, which ``--numstat`` reports only by destination."""
    lines = patch.read_text(errors="replace").splitlines()
    return [
        line[len(prefix) :]
        for line in lines
        for prefix in ("rename from ", "copy from ")
        if line.startswith(prefix)
    ]


def patch_changed_paths(patch: Path) -> list[str]:
    """Repo-relative paths a unified diff touches (via ``git apply --numstat``).

    numstat reports a rename/copy by its *destination* only, so the ``rename from`` /
    ``copy from`` sources are parsed out of the patch and appended: ``git apply`` needs
    the source staged before it will run, and the source has to be deleted wherever the
    patch is replayed.
    """
    out = _git_out(["apply", "--numstat", str(patch)], cwd=patch.parent)
    paths = [line.split("\t", 2)[2] for line in out.splitlines() if line.strip()]
    seen = set(paths)
    for source in _rename_sources(patch):
        if source not in seen:
            seen.add(source)
            paths.append(source)
    return paths


@contextmanager
def materialize_patch(baseline: Path, patch: Path) -> Iterator[tuple[Path, list[str]]]:
    """Apply ``patch`` to a *sparse* copy of ``baseline`` holding only the touched files.

    Yields ``(tree, paths)``: the temporary tree with the patched files, and every
    path the patch touches (a path absent from ``tree`` was deleted by the patch).
    Sparse because ``git apply`` only reads the files it patches — copying a whole
    monorepo to change three files is what made large instances impractical.
    """
    paths = patch_changed_paths(patch)
    with tempfile.TemporaryDirectory(prefix="task-bundle-patch-") as tmp:
        tree = Path(tmp) / "tree"
        tree.mkdir()
        for rel in paths:
            src = baseline / rel
            if src.is_file() or src.is_symlink():
                dest = tree / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest, follow_symlinks=False)
        apply_patch(tree, patch)
        yield tree, paths


# -- manifests and changesets ------------------------------------------------


def parse_manifest(text: str) -> dict[str, str]:
    """Parse ``sha256sum`` output (``<hash>  ./path`` per line) into path -> hash.

    Handles coreutils' escaped form (a leading backslash, with ``\\n``/``\\\\`` in the
    name) so a hostile filename cannot corrupt the changeset.
    """
    manifest: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        escaped = line.startswith("\\")
        body = line[1:] if escaped else line
        digest, _, name = body.partition("  ")
        if escaped:
            name = name.replace("\\n", "\n").replace("\\\\", "\\")
        manifest[name.removeprefix("./")] = digest
    return manifest


def tree_manifest(tree: Path) -> dict[str, str]:
    """Host-side equivalent of the in-container manifest: relative path -> sha256."""
    manifest = {}
    for path in sorted(tree.rglob("*")):
        if path.is_file() and not path.is_symlink():
            manifest[path.relative_to(tree).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return manifest


@dataclass(frozen=True)
class Changeset:
    """What a solver changed, as sorted repo-relative paths."""

    modified: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def present(self) -> list[str]:
        """Paths that exist after the change (to copy into the grade container)."""
        return sorted((*self.modified, *self.added))

    @property
    def existed(self) -> list[str]:
        """Paths that existed before the change (whose baseline versions the diff needs)."""
        return sorted((*self.modified, *self.deleted))

    @property
    def all_paths(self) -> list[str]:
        return sorted((*self.modified, *self.added, *self.deleted))

    def __bool__(self) -> bool:
        return bool(self.modified or self.added or self.deleted)


def compute_changeset(
    before: Mapping[str, str], after: Mapping[str, str], ignored: Iterable[str] = ()
) -> Changeset:
    """Diff two manifests; ``ignored`` paths (per git rules) are dropped from every bucket."""
    skip = set(ignored)
    modified = [p for p in before if p in after and before[p] != after[p] and p not in skip]
    added = [p for p in after if p not in before and p not in skip]
    deleted = [p for p in before if p not in after and p not in skip]
    return Changeset(tuple(sorted(modified)), tuple(sorted(added)), tuple(sorted(deleted)))


def submodule_dirs(repo: Path) -> set[str]:
    """Repo-relative paths recorded as submodule pointers (gitlinks, mode 160000)."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                **_NO_USER_CONFIG,
                "GIT_CEILING_DIRECTORIES": str(repo.resolve().parent),
            },
        )
    except FileNotFoundError as e:
        raise GitError("git executable not found on PATH. Install git and retry.") from e
    if proc.returncode != 0:
        return set()
    dirs = set()
    for entry in proc.stdout.split("\0"):
        if entry.startswith("160000 "):
            dirs.add(entry.split("\t", 1)[1])
    return dirs


def matches_default_ignores(path: str) -> bool:
    """Apply ``DEFAULT_IGNORES`` to a path without git (directory and glob patterns)."""
    parts = path.split("/")
    for pattern in DEFAULT_IGNORES:
        if pattern.endswith("/"):
            if pattern[:-1] in parts[:-1]:
                return True
        elif fnmatch.fnmatch(parts[-1], pattern):
            return True
    return False


def ignored_paths(repo: Path, paths: Iterable[str]) -> set[str]:
    """Subset of ``paths`` that git would ignore in ``repo`` (its .gitignore rules).

    Tracked files are never reported ignored (plain ``check-ignore`` semantics), so a
    tracked file the solver edited always counts even if a pattern matches it.
    ``DEFAULT_IGNORES`` are layered on via a throwaway excludes file.

    Paths inside a *submodule* cannot be asked of the superproject (``check-ignore``
    exits 128 with "is in submodule"), and the host clone never initializes
    submodules, so those paths get only the ``DEFAULT_IGNORES`` treatment: bytecode
    and caches a test run leaves under a vendored checkout are dropped, real edits
    there still count.
    """
    candidates = [p for p in paths if p]
    if not candidates or not (repo / ".git").exists():
        return set()
    submodules = submodule_dirs(repo)
    inside = [p for p in candidates if any(p == s or p.startswith(s + "/") for s in submodules)]
    outside = [p for p in candidates if p not in set(inside)]
    ignored = {p for p in inside if matches_default_ignores(p)}
    if not outside:
        return ignored
    with tempfile.NamedTemporaryFile("w", suffix=".gitignore", delete=False) as f:
        f.write("\n".join(DEFAULT_IGNORES) + "\n")
        excludes = f.name
    try:
        proc = subprocess.run(
            ["git", "-c", f"core.excludesFile={excludes}", "check-ignore", "--stdin", "-z"],
            cwd=repo,
            input="\0".join(outside) + "\0",
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                **_NO_USER_CONFIG,
                "GIT_CEILING_DIRECTORIES": str(repo.resolve().parent),
            },
        )
    except FileNotFoundError as e:
        raise GitError("git executable not found on PATH. Install git and retry.") from e
    finally:
        os.unlink(excludes)
    if proc.returncode not in (0, 1):  # 1 = nothing ignored
        raise GitError(f"git check-ignore failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return ignored | {p for p in proc.stdout.split("\0") if p}


def render_diff(before: Path, after: Path) -> str:
    """Unified diff between two sparse trees, in ``git apply``-able ``a/``/``b/`` form.

    The trees are staged as ``a`` and ``b`` and diffed with ``--no-prefix``, so the
    directory names *become* the conventional prefixes. Files present on only one
    side come out as creations/deletions; binaries as "Binary files ... differ".
    """
    with tempfile.TemporaryDirectory(prefix="task-bundle-diff-") as tmp:
        root = Path(tmp)
        shutil.copytree(before, root / "a", symlinks=True)
        shutil.copytree(after, root / "b", symlinks=True)
        proc = _git(
            [
                "-c",
                "core.quotepath=off",
                "diff",
                "--no-index",
                "--no-prefix",
                "--no-color",
                "a",
                "b",
            ],
            cwd=root,
            env=_NO_USER_CONFIG,
            ok_codes=(0, 1),  # 1 = differences found
        )
        return proc.stdout


# -- leak guard ---------------------------------------------------------------


def assert_no_hidden_content(manifest: Mapping[str, str], hidden_blobs: list[bytes]) -> None:
    """Abort if any hidden blob's exact content appears in a solver-visible manifest.

    Pre-flight guard run on the solve container before the solver starts. Content
    comparison (sha256 of the blob vs. the manifest's per-file hashes), not name
    comparison: a same-named file with different content is legitimate, while
    identical bytes under any name is a leak.
    """
    hidden = {hashlib.sha256(blob).hexdigest() for blob in hidden_blobs}
    for path, digest in manifest.items():
        if digest in hidden:
            raise HiddenTestLeak(
                f"File {path} in the solver-visible tree is byte-identical to a hidden "
                "test. Refusing to continue; the solver must never see hidden tests."
            )
