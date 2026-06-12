"""Two-phase solver run: solve (no hidden tests) -> snapshot diff -> grade.

The phases are structurally separated (DESIGN.md §3):

1. **Baseline phase** — fresh container, hidden tests staged, suites run once to
   record per-test "before" statuses.
2. **Solve phase** — a cleaned workspace tree (hidden tests excluded by
   construction, .git scrubbed, leak-guard verified) is handed to the solver,
   which mutates it. The orchestrator snapshots before and diffs after.
3. **Grade phase** — a *fresh* evaluation container gets the solver's tree
   overlaid onto /workspace, hidden tests are staged via docker cp, and suites
   run once for "after" statuses. The solver never observes this container.

No DB access here: the CLI layer persists the returned outcome.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from task_bundle.bundle import Bundle, utc_now_iso
from task_bundle.container import Docker
from task_bundle.grading import (
    ConsolidatedResult,
    TestExecution,
    consolidate,
    run_verdict,
)
from task_bundle.harness import execute_staged_suite, overlay_tree_into_container
from task_bundle.solver.base import SolveContext, Solver, SolveResult
from task_bundle.workspace import (
    assert_no_hidden_content,
    build_clean_tree,
    capture_diff,
    snapshot_tree,
)


@dataclass
class RunOutcome:
    """Everything a completed run produced, ready for persistence and reporting."""

    verdict: str
    diff: str
    baseline: list[TestExecution]
    post_solver: list[TestExecution]
    results: list[ConsolidatedResult]  # consolidated post-solver results
    solve: SolveResult
    started_at: str
    finished_at: str


def execute_run(
    docker: Docker, bundle: Bundle, tag: str, solver: Solver, work_dir: Path
) -> RunOutcome:
    """Run the full baseline -> solve -> grade pipeline for one solver attempt."""
    started_at = utc_now_iso()
    hidden = bundle.hidden_test_files("fail2pass") + bundle.hidden_test_files("pass2pass")

    # Phase 1: baseline statuses (fresh container, hidden tests staged at the end).
    cid = docker.run_detached(tag)
    try:
        baseline = execute_staged_suite(docker, bundle, cid)
    finally:
        docker.rm_force(cid)

    # Phase 2: solve on a cleaned tree the solver may freely mutate.
    solver_ws = work_dir / "solver_workspace"
    build_clean_tree(bundle.workspace_dir, solver_ws, bundle.spec.solver.workspace_excludes)
    assert_no_hidden_content(solver_ws, hidden)
    snapshot_tree(solver_ws)
    solve_result = solver.solve(
        SolveContext(
            workspace=solver_ws,
            description=(
                bundle.description_path.read_text() if bundle.description_path.is_file() else ""
            ),
            test_command_template=bundle.spec.tests.command_template,
            timeout_seconds=bundle.spec.tests.timeout_seconds,
            docker=docker,
            image_tag=tag,
        )
    )
    diff = capture_diff(solver_ws)

    # Phase 3: grade in a fresh evaluation container.
    cid = docker.run_detached(tag)
    try:
        with tempfile.TemporaryDirectory(prefix="task-bundle-solved-") as tmp:
            solved_tree = Path(tmp) / "tree"
            build_clean_tree(solver_ws, solved_tree)  # strips the orchestrator .git
            overlay_tree_into_container(docker, solved_tree, cid)
        post_solver = execute_staged_suite(docker, bundle, cid)
    finally:
        docker.rm_force(cid)

    results = consolidate(post_solver)
    return RunOutcome(
        verdict=run_verdict(results),
        diff=diff,
        baseline=baseline,
        post_solver=post_solver,
        results=results,
        solve=solve_result,
        started_at=started_at,
        finished_at=utc_now_iso(),
    )
