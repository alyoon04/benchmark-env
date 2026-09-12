"""The Solver protocol: anything that can attempt a task inside its container.

A solver receives a running, hardened container of the task image whose repo dir
is guaranteed to contain no hidden tests (the orchestrator verifies this against a
content manifest before the solve phase) and mutates that tree in place. The
orchestrator hashes the tree before and after, so solvers never produce patches
themselves — they just edit files.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from task_bundle.container import Docker


@dataclass
class SolveContext:
    """Everything a solver may use.

    ``docker``/``container_id``/``repo_dir`` are the solver's workspace: a running
    container (network off, non-root, resource-limited) with the repository at
    ``repo_dir``. Solver-controlled operations must run inside it, never on the
    host. ``baseline_tree`` is the orchestrator's host-side clone at the pinned
    commit, for solvers that materialize patches host-side (StubSolver); it is
    never shown to a model.
    """

    docker: Docker
    container_id: str
    repo_dir: str
    baseline_tree: Path
    description: str
    test_command_template: str
    timeout_seconds: int


@dataclass
class SolveResult:
    """Solver telemetry; the actual output is the mutated repo dir in the container."""

    transcript: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    extra: dict[str, str] = field(default_factory=dict)


class Solver(Protocol):
    """Strategy interface; implementations: StubSolver, ClaudeSolver."""

    name: str
    model: str | None

    def solve(self, ctx: SolveContext) -> SolveResult: ...
