"""DB repository round-trips and the command-recording CLI flow."""

from pathlib import Path

from typer.testing import CliRunner

from task_bundle.cli import app
from task_bundle.db import Database, new_id
from task_bundle.grading import TestExecution

runner = CliRunner()


class TestDatabase:
    def test_command_roundtrip(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "t.db")
        cid = new_id("cmd")
        db.insert_command(cid, "init", '["init"]', "/b", "2026-01-01T00:00:00+00:00", "/log")
        db.finish_command(cid, 0, "2026-01-01T00:00:05+00:00")
        row = db.get_command(cid)
        assert row is not None
        assert (row["name"], row["exit_code"]) == ("init", 0)
        assert db.recent_commands()[0]["id"] == cid

    def test_run_roundtrip(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "t.db")
        cid, rid = new_id("cmd"), new_id("run")
        db.insert_command(cid, "run", "[]", None, "t0", None)
        db.insert_run(rid, cid, "toy", "stub", None, "t0", "tag", "sha256:x", "{}")
        db.finish_run(rid, "RESOLVED", "t1", 10, 20, 0.05)
        row = db.get_run(rid)
        assert row is not None
        assert (row["verdict"], row["cost_usd"]) == ("RESOLVED", 0.05)
        assert [r["id"] for r in db.list_runs()] == [rid]

    def test_test_results_and_artifacts(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "t.db")
        cid = new_id("cmd")
        db.insert_command(cid, "validate", "[]", None, "t0", None)
        execs = [
            TestExecution("tests/a.py", "fail2pass", 1, "failed", 1.5),
            TestExecution("tests/a.py", "fail2pass", 2, "failed", 1.4),
        ]
        db.record_test_results(cid, "baseline", execs)
        rows = db.test_results_for(command_id=cid)
        assert [r["attempt"] for r in rows] == [1, 2]
        assert rows[0]["phase"] == "baseline"
        db.add_artifact(cid, "test_output", "/tmp/x.txt")
        assert db.artifacts_for(cid)[0]["type"] == "test_output"

    def test_ids_time_ordered(self) -> None:
        a, b = new_id("cmd"), new_id("cmd")
        assert a < b or a.split("-")[0] <= b.split("-")[0]


class TestCommandRecording:
    def test_successful_init_recorded(
        self, tmp_path: Path, toy_origin: tuple[str, str, str], isolated_db: Path
    ) -> None:
        url, sha, _ = toy_origin
        result = runner.invoke(
            app, ["init", str(tmp_path / "b"), "--repo", url, "--commit", sha, "--skip-build"]
        )
        assert result.exit_code == 0, result.output
        assert "command id: cmd_" in " ".join(result.output.split())
        row = Database(isolated_db).recent_commands()[0]
        assert (row["name"], row["exit_code"]) == ("init", 0)
        assert row["finished_at"] is not None

    def test_failed_command_records_exit_code(self, tmp_path: Path, isolated_db: Path) -> None:
        result = runner.invoke(
            app, ["init", str(tmp_path / "b"), "--repo", "https://x", "--commit", "a" * 40]
        )
        assert result.exit_code != 0  # unreachable repo -> GitError
        row = Database(isolated_db).recent_commands()[0]
        assert row["name"] == "init"
        assert row["exit_code"] == 1

    def test_logs_lists_recent_commands(
        self, tmp_path: Path, toy_origin: tuple[str, str, str]
    ) -> None:
        url, sha, _ = toy_origin
        runner.invoke(
            app, ["init", str(tmp_path / "b"), "--repo", url, "--commit", sha, "--skip-build"]
        )
        result = runner.invoke(app, ["logs"])
        assert result.exit_code == 0, result.output
        assert "init" in result.output

    def test_logs_shows_command_detail(
        self, tmp_path: Path, toy_origin: tuple[str, str, str], isolated_db: Path
    ) -> None:
        url, sha, _ = toy_origin
        runner.invoke(
            app, ["init", str(tmp_path / "b"), "--repo", url, "--commit", sha, "--skip-build"]
        )
        cid = Database(isolated_db).recent_commands()[0]["id"]
        result = runner.invoke(app, ["logs", cid])
        assert result.exit_code == 0, result.output
        out = " ".join(result.output.split())
        assert "workspace pinned to" in out  # command.log contents rendered
        assert "exit 0" in out

    def test_logs_unknown_id_is_actionable(self) -> None:
        result = runner.invoke(app, ["logs", "cmd_nope"])
        assert result.exit_code != 0
        assert "task logs" in str(result.exception)

    def test_runs_list_empty_hint(self) -> None:
        result = runner.invoke(app, ["runs", "list"])
        assert result.exit_code == 0
        assert "No runs" in result.output
