"""Tests for `task verify-gold`: proves a bundle's golden patch resolves the task."""

from pathlib import Path

import pytest
from conftest import F2P_TEST, P2P_TEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.bundle import Bundle
from task_bundle.cli import app
from task_bundle.db import Database
from task_bundle.errors import ContractViolation

runner = CliRunner()

GOLD_PATCH = Path(__file__).parents[1] / "examples/toy-calc/patch.diff"
PYTEST_CMD = "python -m pytest {test_path} -x -q"


def test_missing_patch_errors_before_docker(tmp_path: Path, shared_origin: tuple[str, str]) -> None:
    """No docker needed: the missing-patch check fires before the daemon is touched."""
    url, sha = shared_origin
    bundle = Bundle.scaffold(
        tmp_path / "b",
        repo_url=url,
        commit=sha,
        base_image="python:3.11-slim",
        test_command=PYTEST_CMD,
        setup_commands=[],
    )
    (bundle.fail2pass_dir / "test_f2p.py").write_text(F2P_TEST)
    (bundle.pass2pass_dir / "test_p2p.py").write_text(P2P_TEST)
    result = runner.invoke(app, ["verify-gold", str(bundle.path)])
    assert result.exit_code == 1
    assert "No golden patch" in str(result.exception)


@pytest.mark.docker
def test_gold_patch_verified(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    bundle.gold_patch_path.write_text(GOLD_PATCH.read_text())

    result = runner.invoke(app, ["verify-gold", str(bundle.path)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "Golden patch verified" in out
    assert "solvable" in out

    # records per-phase test results on the command, but no solver run
    db = Database(isolated_db)
    command_id = db.recent_commands(1)[0]["id"]
    phases = {t["phase"] for t in db.test_results_for(command_id=command_id)}
    assert phases == {"baseline", "post_gold"}
    assert db.list_runs() == []


# Applies cleanly (creates an unrelated file) but leaves divide() buggy.
INEFFECTIVE_PATCH = """\
diff --git a/NOTES.txt b/NOTES.txt
new file mode 100644
--- /dev/null
+++ b/NOTES.txt
@@ -0,0 +1 @@
+does not touch calc.py
"""


@pytest.mark.docker
def test_ineffective_patch_fails_with_exit_2(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    """A patch that applies but fixes nothing -> fail2pass stays failed -> exit 2."""
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    bundle.gold_patch_path.write_text(INEFFECTIVE_PATCH)

    result = runner.invoke(app, ["verify-gold", str(bundle.path)])
    assert result.exit_code != 0
    assert isinstance(result.exception, ContractViolation)
    assert result.exception.exit_code == 2
    out = " ".join(result.output.split())
    assert "does not fix" in out
    assert "test_f2p.py" in out
