"""Tests for `task clean`: remove images / workspaces / artifacts, with confirmation."""

from pathlib import Path

import pytest
from conftest import F2P_TEST, P2P_TEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.bundle import Bundle
from task_bundle.cli import app
from task_bundle.container import Docker
from task_bundle.db import Database
from task_bundle.errors import TaskError

runner = CliRunner()


def _seed_run(db_path: Path, artifacts_dir: Path) -> tuple[str, Path]:
    """Insert a command+run row and create its artifacts dir; return (run_id, dir)."""
    db = Database(db_path)
    db.insert_command("cmd_test", "run", "[]", "/b", "2026-01-01T00:00:00+00:00", None)
    db.insert_run("run_test", "cmd_test", "b", "stub", None, "2026-01-01T00:00:00+00:00",
                  "task-bundle/b:k", "sha256:x", "{}")  # fmt: skip
    db.close()
    command_dir = artifacts_dir / "cmd_test"
    command_dir.mkdir(parents=True)
    (command_dir / "report.json").write_text("{}")
    return "run_test", command_dir


def test_requires_exactly_one_target(tmp_path: Path, isolated_db: Path) -> None:
    none = runner.invoke(app, ["clean"])
    assert isinstance(none.exception, TaskError)
    assert "exactly one" in str(none.exception)

    both = runner.invoke(app, ["clean", str(tmp_path / "b"), "--all"])
    assert isinstance(both.exception, TaskError)


def test_clean_run_removes_artifacts_but_keeps_history(tmp_path: Path, isolated_db: Path) -> None:
    run_id, command_dir = _seed_run(isolated_db, tmp_path / "artifacts")
    result = runner.invoke(app, ["clean", "--run", run_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert not command_dir.exists()
    assert Database(isolated_db).get_run(run_id) is not None  # log/run history survives


def test_clean_unknown_run_errors(isolated_db: Path) -> None:
    result = runner.invoke(app, ["clean", "--run", "run_nope", "--yes"])
    assert isinstance(result.exception, TaskError)
    assert "No run" in str(result.exception)


def test_confirmation_abort_keeps_files(tmp_path: Path, isolated_db: Path) -> None:
    run_id, command_dir = _seed_run(isolated_db, tmp_path / "artifacts")
    result = runner.invoke(app, ["clean", "--run", run_id], input="n\n")
    assert result.exit_code != 0  # aborted
    assert command_dir.exists()


@pytest.mark.docker
def test_clean_bundle_removes_image_and_workspace(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    # unique id so removing this image cannot disturb other tests' cached images
    bundle = make_initialized_bundle(
        tmp_path / "clean-me", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST
    )
    docker = Docker()
    assert docker.list_images("task-bundle/clean-me:")
    assert bundle.workspace_dir.exists()

    result = runner.invoke(app, ["clean", str(bundle.path), "--yes"])
    assert result.exit_code == 0, result.output
    assert docker.list_images("task-bundle/clean-me:") == []
    assert not bundle.workspace_dir.exists()
    assert Bundle.load(bundle.path).load_state().status == "scaffolded"
