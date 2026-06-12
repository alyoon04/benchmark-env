"""End-to-end docker tests for `task run` with the stub solver.

The two deterministic proofs of grading correctness: the gold patch must grade
RESOLVED, a no-op must grade UNRESOLVED (DESIGN.md §10, assignment validation spec).
"""

import json
from pathlib import Path

import pytest
from conftest import F2P_TEST, P2P_TEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.cli import app
from task_bundle.db import Database

pytestmark = pytest.mark.docker

runner = CliRunner()

GOLD_PATCH = Path(__file__).parents[1] / "examples/toy-calc/patch.diff"


def latest_run_and_report(db_path: Path) -> tuple[dict, dict]:
    db = Database(db_path)
    run = db.list_runs()[0]
    report_artifact = next(a for a in db.artifacts_for(run["command_id"]) if a["type"] == "report")
    return dict(run), json.loads(Path(report_artifact["path"]).read_text())


def test_gold_patch_resolves(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    result = runner.invoke(
        app, ["run", str(bundle.path), "--solver", "stub", "--patch", str(GOLD_PATCH)]
    )
    assert result.exit_code == 0, result.output
    assert "RESOLVED" in result.output
    run, report = latest_run_and_report(isolated_db)
    assert run["verdict"] == "RESOLVED"
    assert report["verdict"] == "RESOLVED"
    assert "+    return a / b" in report["diff"]
    statuses = {(t["test"], t["baseline_status"], t["post_solver_status"]) for t in report["tests"]}
    assert ("tests/test_f2p.py", "failed", "passed") in statuses
    assert ("tests/test_p2p.py", "passed", "passed") in statuses
    # deterministic emission: sorted keys
    raw = json.dumps(report, sort_keys=True, indent=2) + "\n"
    artifact = next(
        a for a in Database(isolated_db).artifacts_for(run["command_id"]) if a["type"] == "report"
    )
    assert Path(artifact["path"]).read_text() == raw

    show = runner.invoke(app, ["runs", "show", run["id"]])
    assert show.exit_code == 0, show.output
    assert "RESOLVED" in show.output


def test_noop_solver_unresolved(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    result = runner.invoke(app, ["run", str(bundle.path), "--solver", "stub"])
    assert result.exit_code == 0, result.output  # a completed run exits 0; verdict is data
    assert "UNRESOLVED" in result.output
    run, report = latest_run_and_report(isolated_db)
    assert run["verdict"] == "UNRESOLVED"
    assert report["diff"] == ""
    statuses = {(t["test"], t["post_solver_status"]) for t in report["tests"]}
    assert ("tests/test_f2p.py", "failed") in statuses
    assert ("tests/test_p2p.py", "passed") in statuses


def test_claude_solver_not_yet_available(tmp_path: Path, shared_origin: tuple[str, str]) -> None:
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    result = runner.invoke(app, ["run", str(bundle.path), "--solver", "claude"])
    assert result.exit_code != 0
    assert "milestone 5" in str(result.exception)
