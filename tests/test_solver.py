"""Unit tests for StubSolver and the snapshot/diff machinery (no docker needed)."""

import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES

from task_bundle.errors import GitError
from task_bundle.solver import SolveContext, StubSolver
from task_bundle.workspace import capture_diff, snapshot_tree

GOLD_PATCH = Path(__file__).parents[1] / "examples/toy-calc/patch.diff"


def make_ctx(tmp_path: Path) -> SolveContext:
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURES / "toy_repo", ws)
    snapshot_tree(ws)
    return SolveContext(
        workspace=ws,
        description="fix divide",
        test_command_template="python -m pytest {test_path} -q",
        timeout_seconds=60,
    )


class TestStubSolver:
    def test_noop_produces_empty_diff(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        result = StubSolver(None).solve(ctx)
        assert "no patch" in result.transcript
        assert capture_diff(ctx.workspace) == ""

    def test_patch_applied_and_diffed(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        StubSolver(GOLD_PATCH).solve(ctx)
        assert "return a / b" in (ctx.workspace / "calc.py").read_text()
        diff = capture_diff(ctx.workspace)
        assert "-    return a * b" in diff
        assert "+    return a / b" in diff
        assert diff.startswith("diff --git a/calc.py b/calc.py")

    def test_nonapplying_patch_is_actionable(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        bad = tmp_path / "bad.diff"
        bad.write_text("--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-x\n+y\n")
        with pytest.raises(GitError, match="does not apply cleanly"):
            StubSolver(bad).solve(ctx)

    def test_new_files_appear_in_diff(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        (ctx.workspace / "newmod.py").write_text("VALUE = 1\n")
        diff = capture_diff(ctx.workspace)
        assert "newmod.py" in diff
        assert "+VALUE = 1" in diff
