"""Tests for `task diff <run-id>`: prints the stored solver patch for a run."""

from pathlib import Path

import pytest
from conftest import F2P_TEST, P2P_TEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.cli import app
from task_bundle.db import Database
from task_bundle.errors import TaskError

runner = CliRunner()

GOLD_PATCH = Path(__file__).parents[1] / "examples/toy-calc/patch.diff"


def test_unknown_run_errors(isolated_db: Path) -> None:
    result = runner.invoke(app, ["diff", "run_does_not_exist"])
    assert result.exit_code != 0
    assert isinstance(result.exception, TaskError)
    assert "No run" in str(result.exception)


@pytest.mark.docker
def test_diff_prints_solver_patch(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    run = runner.invoke(
        app, ["run", str(bundle.path), "--solver", "stub", "--patch", str(GOLD_PATCH)]
    )
    assert run.exit_code == 0, run.output
    run_id = Database(isolated_db).list_runs()[0]["id"]

    result = runner.invoke(app, ["diff", run_id])
    assert result.exit_code == 0, result.output
    assert "+    return a / b" in result.output


@pytest.mark.docker
def test_diff_empty_for_noop_run(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    assert runner.invoke(app, ["run", str(bundle.path), "--solver", "stub"]).exit_code == 0
    run_id = Database(isolated_db).list_runs()[0]["id"]

    result = runner.invoke(app, ["diff", run_id])
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output
