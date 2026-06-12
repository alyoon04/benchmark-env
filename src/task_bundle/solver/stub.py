"""StubSolver: applies a caller-provided patch (or nothing) — the deterministic
harness probe. Running it with the golden patch must yield RESOLVED; running it
with no patch must yield UNRESOLVED. Together those two runs prove the grading
pipeline end-to-end without any LLM nondeterminism.
"""

from pathlib import Path

from task_bundle.solver.base import SolveContext, SolveResult
from task_bundle.workspace import apply_patch


class StubSolver:
    """Apply ``patch`` to the workspace, or no-op when ``patch`` is None."""

    name = "stub"
    model: str | None = None

    def __init__(self, patch: Path | None = None) -> None:
        self.patch = patch

    def solve(self, ctx: SolveContext) -> SolveResult:
        if self.patch is None:
            return SolveResult(transcript="stub solver: no patch provided; workspace untouched")
        apply_patch(ctx.workspace, self.patch.resolve())
        return SolveResult(transcript=f"stub solver: applied {self.patch}")
