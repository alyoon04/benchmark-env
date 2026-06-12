"""The Solver protocol: anything that can attempt a task given a workspace.

A solver receives a workspace tree that is guaranteed to contain no hidden tests
(the orchestrator enforces this before the solve phase) and mutates it in place.
The orchestrator snapshots the tree beforehand and captures the diff afterwards,
so solvers never produce patches themselves — they just edit files.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class SolveContext:
    """Everything a solver may see: the cleaned workspace and the problem statement."""

    workspace: Path
    description: str
    test_command_template: str
    timeout_seconds: int


@dataclass
class SolveResult:
    """Solver telemetry; the actual output is the mutated workspace."""

    transcript: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    extra: dict[str, str] = field(default_factory=dict)


class Solver(Protocol):
    """Strategy interface; implementations: StubSolver (M4), ClaudeSolver (M5)."""

    name: str
    model: str | None

    def solve(self, ctx: SolveContext) -> SolveResult: ...
