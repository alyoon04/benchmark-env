"""End-to-end docker integration tests for `task init` (build) and `task validate`.

Skipped automatically when no Docker daemon is available. Both bundles point at one
shared origin so the task image is built once and cache-hit afterwards.
"""

from pathlib import Path

import pytest
from conftest import F2P_TEST, P2P_TEST, PYTEST_CMD, SETUP_PIP_PYTEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.bundle import Bundle
from task_bundle.cli import app
from task_bundle.db import Database
from task_bundle.errors import ContractViolation

pytestmark = pytest.mark.docker

runner = CliRunner()


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
        test_command=PYTEST_CMD,
        setup_commands=SETUP_PIP_PYTEST,
    )
    result = runner.invoke(app, ["validate", str(bundle.path)])
    assert result.exit_code != 0
    assert "no hidden tests" in str(result.exception)
