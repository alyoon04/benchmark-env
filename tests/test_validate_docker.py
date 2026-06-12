"""End-to-end docker integration tests for `task init` (build) and `task validate`.

Skipped automatically when no Docker daemon is available. Both bundles point at one
shared origin so the task image is built once and cache-hit afterwards.
"""

import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES, git
from typer.testing import CliRunner

from task_bundle.bundle import Bundle
from task_bundle.cli import app
from task_bundle.db import Database
from task_bundle.errors import ContractViolation

pytestmark = pytest.mark.docker

runner = CliRunner()

SETUP = ["pip install --no-cache-dir pytest"]
TEST_CMD = "python -m pytest {test_path} -x -q"

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


@pytest.fixture(scope="module")
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
        test_command=TEST_CMD,
        setup_commands=SETUP,
    )
    (bundle.fail2pass_dir / "test_f2p.py").write_text(f2p)
    (bundle.pass2pass_dir / "test_p2p.py").write_text(p2p)
    result = runner.invoke(app, ["init", str(path)])
    assert result.exit_code == 0, result.output
    return bundle


def test_validate_passes_on_correct_bundle(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    bundle = make_initialized_bundle(tmp_path / "good", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    result = runner.invoke(app, ["validate", str(bundle.path)])
    assert result.exit_code == 0, result.output
    assert "contract holds" in " ".join(result.output.split())
    state = bundle.load_state()
    assert state.status == "validated"
    assert state.image_tag and state.image_digest
    # per-attempt test results and the output artifact are queryable afterwards
    db = Database(isolated_db)
    cmd = next(r for r in db.recent_commands() if r["name"] == "validate")
    rows = db.test_results_for(command_id=cmd["id"])
    assert len(rows) == 6  # 2 tests x 3 attempts
    assert any(a["type"] == "test_output" for a in db.artifacts_for(cmd["id"]))


def test_validate_rejects_f2p_that_passes_on_baseline(
    tmp_path: Path, shared_origin: tuple[str, str]
) -> None:
    # The "fail2pass" test exercises add(), which already works -> contract broken.
    bundle = make_initialized_bundle(tmp_path / "broken", shared_origin, f2p=P2P_TEST, p2p=P2P_TEST)
    result = runner.invoke(app, ["validate", str(bundle.path)])
    assert result.exit_code != 0
    assert isinstance(result.exception, ContractViolation)
    assert "cannot distinguish" in " ".join(result.output.split())


def test_validate_without_hidden_tests_fails_fast(
    tmp_path: Path, shared_origin: tuple[str, str]
) -> None:
    url, sha = shared_origin
    bundle = Bundle.scaffold(
        tmp_path / "empty",
        repo_url=url,
        commit=sha,
        base_image="python:3.11-slim",
        test_command=TEST_CMD,
        setup_commands=SETUP,
    )
    result = runner.invoke(app, ["validate", str(bundle.path)])
    assert result.exit_code != 0
    assert "no hidden tests" in str(result.exception)
