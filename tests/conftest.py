"""Shared fixtures: a local git "origin" built from the committed toy repo.

Network-free: the origin lives in tmp_path and is fetched over the file:// transport,
which exercises the same shallow direct-SHA fetch path used against real remotes.
"""

import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
from typer.testing import CliRunner

from task_bundle.bundle import Bundle
from task_bundle.cli import app
from task_bundle.db import Database

_runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"


@cache
def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:  # daemon up but busy (e.g. a large pull in flight)
        return True
    return proc.returncode == 0


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every CLI invocation at a per-test DB/artifacts dir (never ~/.task-bundle)."""
    db = tmp_path / "task.db"
    monkeypatch.setenv("TASK_BUNDLE_DB", str(db))
    monkeypatch.setenv("TASK_BUNDLE_ARTIFACTS", str(tmp_path / "artifacts"))
    return db


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if docker_available():
        return
    skip = pytest.mark.skip(reason="Docker daemon not available")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


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


SETUP_PIP_PYTEST = ["pip install --no-cache-dir pytest"]
PYTEST_CMD = "python -m pytest {test_path} -x -q"

F2P_TEST = """
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calc import divide


def test_divide() -> None:
    assert divide(6, 3) == 2
"""

P2P_TEST = """
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""


@pytest.fixture(scope="session")
def shared_origin(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    origin = tmp_path_factory.mktemp("origin") / "toy"
    shutil.copytree(FIXTURES / "toy_repo", origin)
    git(["init", "--quiet", "--initial-branch=main"], cwd=origin)
    git(["config", "uploadpack.allowReachableSHA1InWant", "true"], cwd=origin)
    git(["add", "-A"], cwd=origin)
    git(["commit", "--quiet", "-m", "baseline"], cwd=origin)
    return origin.as_uri(), git(["rev-parse", "HEAD"], cwd=origin)


def make_initialized_bundle(path: Path, origin: tuple[str, str], *, f2p: str, p2p: str) -> Bundle:
    url, sha = origin
    bundle = Bundle.scaffold(
        path,
        repo_url=url,
        commit=sha,
        base_image="python:3.11-slim",
        test_command=PYTEST_CMD,
        setup_commands=SETUP_PIP_PYTEST,
    )
    (bundle.fail2pass_dir / "test_f2p.py").write_text(f2p)
    (bundle.pass2pass_dir / "test_p2p.py").write_text(p2p)
    result = _runner.invoke(app, ["init", str(path)])
    assert result.exit_code == 0, result.output
    return bundle


def seed_fleet_command(db_path: Path, artifacts_dir: Path) -> Path:
    """One ``fleet`` command with two nested runs; only ``run_ok`` recorded artifacts.

    Mirrors what `task fleet` writes when one job errors before the solve phase:
    a runs row with no artifacts, beside a sibling run that produced a diff.
    """
    command_dir = artifacts_dir / "cmd_fleet"
    db = Database(db_path)
    db.insert_command("cmd_fleet", "fleet", "[]", None, "2026-01-01T00:00:00+00:00", None)
    for run_id in ("run_ok", "run_err"):
        db.insert_run(run_id, "cmd_fleet", "b", "stub", None, "2026-01-01T00:00:00+00:00",
                      "task-bundle/b:k", "sha256:x", "{}")  # fmt: skip
        (command_dir / run_id).mkdir(parents=True)
    ok_diff = command_dir / "run_ok" / "solver.diff"
    ok_diff.write_text("+    return a / b\n")
    db.add_artifact("cmd_fleet", "solver_diff", str(ok_diff), run_id="run_ok")
    db.close()
    return command_dir
