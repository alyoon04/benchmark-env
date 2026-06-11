"""Shared fixtures: a local git "origin" built from the committed toy repo.

Network-free: the origin lives in tmp_path and is fetched over the file:// transport,
which exercises the same shallow direct-SHA fetch path used against real remotes.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def toy_origin(tmp_path: Path) -> tuple[str, str, str]:
    """Local git origin from the toy repo.

    Returns (file:// url, baseline_sha, followup_sha) — followup adds a file that
    must be ABSENT when pinned to baseline.
    """
    origin = tmp_path / "toy-origin"
    shutil.copytree(FIXTURES / "toy_repo", origin)
    git(["init", "--quiet", "--initial-branch=main"], cwd=origin)
    # Allow direct-SHA shallow fetches over the file:// transport (GitHub permits
    # reachable-SHA fetches by default; bare local repos do not).
    git(["config", "uploadpack.allowReachableSHA1InWant", "true"], cwd=origin)
    git(["add", "-A"], cwd=origin)
    git(["commit", "--quiet", "-m", "baseline: buggy divide"], cwd=origin)
    baseline_sha = git(["rev-parse", "HEAD"], cwd=origin)
    (origin / "FUTURE.md").write_text("added after the pinned commit\n")
    git(["add", "FUTURE.md"], cwd=origin)
    git(["commit", "--quiet", "-m", "follow-up commit"], cwd=origin)
    followup_sha = git(["rev-parse", "HEAD"], cwd=origin)
    return origin.as_uri(), baseline_sha, followup_sha
