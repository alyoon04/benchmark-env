"""Solver implementations: pluggable strategies for producing a code change."""

from task_bundle.solver.base import SolveContext, Solver, SolveResult
from task_bundle.solver.stub import StubSolver

__all__ = ["SolveContext", "SolveResult", "Solver", "StubSolver"]
