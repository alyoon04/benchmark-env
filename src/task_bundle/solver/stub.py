"""StubSolver: applies a caller-provided patch (or nothing) — the deterministic
harness probe. Running it with the golden patch must yield RESOLVED; running it
with no patch must yield UNRESOLVED. Together those two runs prove the grading
pipeline end-to-end without any LLM nondeterminism.

The patch is applied host-side to a sparse copy of the baseline clone and the
touched files are pushed into the solve container — the same transport the
orchestrator uses for hidden tests, so no patch tooling is needed in the image.
"""

from pathlib import Path

from task_bundle.harness import TaskImage, push_files
from task_bundle.solver.base import SolveContext, SolveResult
from task_bundle.workspace import materialize_patch


class StubSolver:
    """Apply ``patch`` inside the solve container, or no-op when ``patch`` is None."""

    name = "stub"
    model: str | None = None

    def __init__(self, patch: Path | None = None) -> None:
        self.patch = patch

    def solve(self, ctx: SolveContext) -> SolveResult:
        if self.patch is None:
            return SolveResult(transcript="stub solver: no patch provided; workspace untouched")
        image = TaskImage(tag="", repo_dir=ctx.repo_dir)
        with materialize_patch(ctx.baseline_tree, self.patch.resolve()) as (tree, paths):
            push_files(ctx.docker, image, ctx.container_id, tree, paths)
        return SolveResult(transcript=f"stub solver: applied {self.patch} ({len(paths)} file(s))")
