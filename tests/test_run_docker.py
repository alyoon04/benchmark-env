"""End-to-end docker tests for `task run` with the stub solver.

The two deterministic proofs of grading correctness: the gold patch must grade
RESOLVED, a no-op must grade UNRESOLVED (DESIGN.md §10, assignment validation spec).
"""

import json
from pathlib import Path

import pytest
from conftest import F2P_TEST, P2P_TEST, PYTEST_CMD, SETUP_PIP_PYTEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.bundle import Bundle
from task_bundle.cli import app
from task_bundle.container import Docker
from task_bundle.db import Database
from task_bundle.errors import HiddenTestLeak
from task_bundle.harness import image_tag

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


def test_claude_without_api_key_errors_and_marks_run(
    tmp_path: Path,
    shared_origin: tuple[str, str],
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    result = runner.invoke(app, ["run", str(bundle.path), "--solver", "claude"])
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in str(result.exception)
    assert Database(isolated_db).list_runs()[0]["verdict"] == "ERROR"


# A dependency that lives UNDER the repo dir but outside git — the shape of a
# submodule checkout, node_modules, or a compiled extension in a prebuilt image.
# The hidden tests import it; if grading swapped in a clean clone it would vanish.
F2P_NEEDS_VENDORED_DEP = """
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vendor.helper import TWO
from calc import divide


def test_divide_with_vendored_dep() -> None:
    assert divide(6, 3) == TWO
"""

P2P_NEEDS_VENDORED_DEP = """
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vendor.helper import TWO
from calc import add


def test_add_with_vendored_dep() -> None:
    assert add(1, 1) == TWO
"""

VENDOR_SETUP = [
    "pip install --no-cache-dir pytest",
    "mkdir -p /workspace/vendor && echo 'TWO = 2' > /workspace/vendor/helper.py",
]


def test_untracked_content_under_repo_dir_survives_grading(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    """Regression for the openlibrary/vuls/webclients class of failure: material
    installed under the repo dir by the image build (not in git) must still be
    there when the solver's changes are graded. Gold -> RESOLVED, no-op -> UNRESOLVED
    (and UNRESOLVED because divide() is wrong, not because the dep went missing)."""
    url, sha = shared_origin
    bundle = Bundle.scaffold(
        tmp_path / "vendored",
        repo_url=url,
        commit=sha,
        base_image="python:3.11-slim",
        test_command=PYTEST_CMD,
        setup_commands=VENDOR_SETUP,
    )
    (bundle.fail2pass_dir / "test_f2p.py").write_text(F2P_NEEDS_VENDORED_DEP)
    (bundle.pass2pass_dir / "test_p2p.py").write_text(P2P_NEEDS_VENDORED_DEP)
    assert runner.invoke(app, ["init", str(bundle.path)]).exit_code == 0

    gold = runner.invoke(app, ["run", str(bundle.path), "--patch", str(GOLD_PATCH)])
    assert gold.exit_code == 0, gold.output
    assert "RESOLVED" in gold.output
    _, report = latest_run_and_report(isolated_db)
    assert report["verdict"] == "RESOLVED"
    assert "vendor" not in report["diff"]  # the vendored dep is environment, not a change

    noop = runner.invoke(app, ["run", str(bundle.path)])
    assert noop.exit_code == 0, noop.output
    _, report = latest_run_and_report(isolated_db)
    assert report["verdict"] == "UNRESOLVED"
    statuses = {(t["test"], t["post_solver_status"]) for t in report["tests"]}
    assert ("tests/test_p2p.py", "passed") in statuses  # dep present: p2p still green


def test_hidden_test_in_solver_tree_is_refused(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    """Leak guard on the solve container: a byte-identical copy of a hidden test in
    the repo aborts the run with exit 3 before any solver touches it."""
    url, sha = shared_origin
    # Its own image: the planted file must never poison the cache other tests share.
    bundle = Bundle.scaffold(
        tmp_path / "leaky",
        repo_url=url,
        commit=sha,
        base_image="python:3.11-slim",
        test_command=PYTEST_CMD,
        setup_commands=[*SETUP_PIP_PYTEST, "true  # leak-guard test image"],
    )
    (bundle.fail2pass_dir / "test_f2p.py").write_text(F2P_TEST)
    (bundle.pass2pass_dir / "test_p2p.py").write_text(P2P_TEST)
    assert runner.invoke(app, ["init", str(bundle.path), "--skip-build"]).exit_code == 0
    (bundle.workspace_dir / "notes.py").write_text(F2P_TEST)  # planted under an innocent name
    assert runner.invoke(app, ["init", str(bundle.path)]).exit_code == 0
    try:
        result = runner.invoke(app, ["run", str(bundle.path), "--patch", str(GOLD_PATCH)])
        assert result.exit_code != 0
        assert isinstance(result.exception, HiddenTestLeak)
        assert result.exception.exit_code == 3
        assert "notes.py" in str(result.exception)
        assert Database(isolated_db).list_runs()[0]["verdict"] == "ERROR"
    finally:
        Docker().rmi(image_tag(bundle))


def test_fleet_runs_concurrently_then_resumes_without_duplicate_runs(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    bundle = make_initialized_bundle(tmp_path / "fleet", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    args = [
        "fleet",
        str(bundle.path),
        "--patch",
        str(GOLD_PATCH),
        "--samples",
        "2",
        "--concurrency",
        "2",
        "--container-limit",
        "2",
        "--min-free-disk-gb",
        "0",
    ]

    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "2 jobs (0 resumed, 2 to run)" in " ".join(first.output.split())
    runs = Database(isolated_db).list_runs()
    assert len(runs) == 2
    assert {row["verdict"] for row in runs} == {"RESOLVED"}

    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    assert "2 jobs (2 resumed, 0 to run)" in " ".join(second.output.split())
    assert len(Database(isolated_db).list_runs()) == 2
