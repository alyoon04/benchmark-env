"""Workspace management: pinned-commit clones (and, later, cleaned solver trees).

The baseline workspace lives at ``<bundle>/.task/workspace`` and is the
orchestrator-side source of truth for the repo at the pinned commit. The clone is
shallow (``fetch --depth 1 <sha>``) so the checkout cannot contain future commits —
defense in depth for the test-hiding invariant, and faster besides.
"""

import shutil
import subprocess
from pathlib import Path

from task_bundle.errors import GitError


def _git(args: list[str], cwd: Path, timeout: int = 600) -> str:
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as e:
        raise GitError("git executable not found on PATH. Install git and retry.") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"`{' '.join(cmd)}` timed out after {timeout}s.") from e
    if proc.returncode != 0:
        raise GitError(f"`{' '.join(cmd)}` failed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    return proc.stdout


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
        return _git(["rev-parse", "HEAD"], cwd=repo).strip()
    except GitError:
        return None
