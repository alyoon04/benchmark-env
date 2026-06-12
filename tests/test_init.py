"""Integration tests for pinned-commit cloning and the `task init` command."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from task_bundle.bundle import Bundle
from task_bundle.cli import app
from task_bundle.errors import GitError
from task_bundle.workspace import clone_at_commit

runner = CliRunner()


class TestCloneAtCommit:
    def test_clones_exactly_the_pinned_commit(
        self, tmp_path: Path, toy_origin: tuple[str, str, str]
    ) -> None:
        url, baseline, _followup = toy_origin
        dest = tmp_path / "ws"
        clone_at_commit(url, baseline, dest)
        assert (dest / "calc.py").is_file()
        assert not (dest / "FUTURE.md").exists(), "file from a later commit leaked in"

    def test_clone_is_shallow(self, tmp_path: Path, toy_origin: tuple[str, str, str]) -> None:
        url, baseline, _ = toy_origin
        dest = tmp_path / "ws"
        clone_at_commit(url, baseline, dest)
        assert (dest / ".git/shallow").is_file(), "history must not be recoverable"

    def test_idempotent_when_already_pinned(
        self, tmp_path: Path, toy_origin: tuple[str, str, str]
    ) -> None:
        url, baseline, _ = toy_origin
        dest = tmp_path / "ws"
        clone_at_commit(url, baseline, dest)
        marker = dest / "scratch.txt"
        marker.write_text("solver scribbles")
        clone_at_commit(url, baseline, dest)  # no-op: marker survives
        assert marker.exists()
        clone_at_commit(url, baseline, dest, force=True)  # force: fresh tree
        assert not marker.exists()

    def test_unknown_sha_is_actionable(
        self, tmp_path: Path, toy_origin: tuple[str, str, str]
    ) -> None:
        url, _, _ = toy_origin
        with pytest.raises(GitError, match="Could not fetch commit"):
            clone_at_commit(url, "f" * 40, tmp_path / "ws")


class TestInitCommand:
    def test_scaffold_and_init_end_to_end(
        self, tmp_path: Path, toy_origin: tuple[str, str, str]
    ) -> None:
        url, baseline, _ = toy_origin
        bundle_dir = tmp_path / "toy-task"
        result = runner.invoke(
            app, ["init", str(bundle_dir), "--repo", url, "--commit", baseline, "--skip-build"]
        )
        assert result.exit_code == 0, result.output
        bundle = Bundle.load(bundle_dir)
        assert bundle.load_state().status == "initialized"
        assert (bundle.workspace_dir / "calc.py").is_file()

    def test_reinit_existing_bundle_without_flags(
        self, tmp_path: Path, toy_origin: tuple[str, str, str]
    ) -> None:
        url, baseline, _ = toy_origin
        bundle_dir = tmp_path / "toy-task"
        runner.invoke(
            app, ["init", str(bundle_dir), "--repo", url, "--commit", baseline, "--skip-build"]
        )
        result = runner.invoke(app, ["init", str(bundle_dir), "--skip-build"])
        assert result.exit_code == 0, result.output

    def test_scaffold_without_repo_flag_fails_with_usage_hint(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init", str(tmp_path / "x")])
        assert result.exit_code != 0
        assert "--repo" in str(result.exception or result.output)

    def test_short_sha_becomes_friendly_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["init", str(tmp_path / "x"), "--repo", "https://x", "--commit", "badsha"]
        )
        assert result.exit_code != 0
        assert "40-character" in str(result.exception)

    def test_flags_on_existing_bundle_rejected(
        self, tmp_path: Path, toy_origin: tuple[str, str, str]
    ) -> None:
        url, baseline, _ = toy_origin
        bundle_dir = tmp_path / "toy-task"
        runner.invoke(
            app, ["init", str(bundle_dir), "--repo", url, "--commit", baseline, "--skip-build"]
        )
        result = runner.invoke(
            app, ["init", str(bundle_dir), "--repo", url, "--commit", baseline, "--skip-build"]
        )
        assert result.exit_code != 0
