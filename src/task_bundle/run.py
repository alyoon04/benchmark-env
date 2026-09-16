"""Two-phase solver run: solve in place (no hidden tests) -> changeset -> grade.

The phases are structurally separated (DESIGN.md §3):

1. **Baseline phase** — fresh container, hidden tests staged, suites run once to
   record per-test "before" statuses.
2. **Solve phase** — a fresh container whose repo dir is the image's own tree
   (leak-guard verified against a content manifest) is handed to the solver, which
   mutates it in place. The orchestrator hashes the tree before and after; the
   difference, filtered by the repo's .gitignore, is the solver's changeset.
3. **Grade phase** — a *fresh* evaluation container gets the changeset replayed
   into its repo dir (files copied in, deletions applied), hidden tests are staged
   via docker cp, and suites run once for "after" statuses. The solver never
   observes this container.

Applying the changeset in place — rather than swapping in a clean clone — is what
keeps everything that lives under the repo dir but outside git (submodules,
``node_modules``, compiled extensions) intact for grading.

No DB access here: the CLI layer persists the returned outcome.
"""

import hashlib
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from task_bundle.bundle import Bundle, utc_now_iso
from task_bundle.container import Docker
from task_bundle.grading import (
    ConsolidatedResult,
    TestExecution,
    consolidate,
    run_verdict,
)
from task_bundle.harness import (
    SnapshotCache,
    TaskImage,
    apply_changeset,
    execute_staged_suite,
    hidden_blobs,
    pull_changes,
    snapshot,
    snapshot_after,
)
from task_bundle.solver.base import SolveContext, Solver, SolveResult
from task_bundle.workspace import (
    Changeset,
    assert_no_hidden_content,
    compute_changeset,
    ignored_paths,
    render_diff,
)


@dataclass
class RunCaches:
    """Work that is a property of the image, shared across attempts in one process.

    - ``snapshots``: the pre-solve tree snapshot per image (hash + stat of every file).
    - ``baselines``: the baseline test phase per (image, hidden-test content). The
      baseline is what the image's tests do before any solver touches it, so every
      sample of the same task in a fleet shares it; each run's report still carries
      the full per-test before/after.
    """

    snapshots: SnapshotCache = field(default_factory=SnapshotCache)
    baselines: dict[str, list[TestExecution]] = field(default_factory=dict)
    baseline_hits: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def baseline_for(
        self, key: str, compute: "Callable[[], list[TestExecution]]"
    ) -> list[TestExecution]:
        with self._lock:
            cached = self.baselines.get(key)
            if cached is not None:
                self.baseline_hits += 1
                return cached
        executions = compute()
        with self._lock:
            self.baselines.setdefault(key, executions)
        return executions


@dataclass
class RunOutcome:
    """Everything a completed run produced, ready for persistence and reporting."""

    verdict: str
    diff: str
    changes: Changeset
    baseline: list[TestExecution]
    post_solver: list[TestExecution]
    results: list[ConsolidatedResult]  # consolidated post-solver results
    solve: SolveResult
    started_at: str
    finished_at: str


def execute_run(
    docker: Docker,
    bundle: Bundle,
    image: TaskImage,
    solver: Solver,
    *,
    caches: RunCaches | None = None,
) -> RunOutcome:
    """Run the full baseline -> solve -> grade pipeline for one solver attempt.

    ``caches`` (used by ``task fleet``) shares the image's pre-solve snapshot and
    baseline test phase across attempts; a plain ``task run`` recomputes both.
    """
    started_at = utc_now_iso()
    hidden = hidden_blobs(bundle)

    # Phase 1: baseline statuses (fresh container, hidden tests staged at the end).
    def run_baseline() -> list[TestExecution]:
        cid = docker.run_detached(image.tag)
        try:
            return execute_staged_suite(docker, bundle, image, cid)
        finally:
            docker.rm_force(cid)

    if caches is None:
        baseline = run_baseline()
    else:
        digest = hashlib.sha256(b"\0".join(hidden)).hexdigest()[:16]
        baseline = caches.baseline_for(f"{image.tag}|{digest}", run_baseline)

    # Phase 2: solve in place. The container is the solver's workspace.
    with tempfile.TemporaryDirectory(prefix="task-bundle-run-") as tmp:
        work = Path(tmp)
        cid = docker.run_detached(image.tag)
        try:
            before = (
                caches.snapshots.get(docker, image, cid)
                if caches is not None
                else snapshot(docker, image, cid)
            )
            assert_no_hidden_content(before.hashes, hidden)
            solve_result = solver.solve(
                SolveContext(
                    docker=docker,
                    container_id=cid,
                    repo_dir=image.repo_dir,
                    baseline_tree=bundle.workspace_dir,
                    description=(
                        bundle.description_path.read_text()
                        if bundle.description_path.is_file()
                        else ""
                    ),
                    test_command_template=bundle.spec.tests.command_template,
                    timeout_seconds=bundle.spec.tests.timeout_seconds,
                )
            )
            after = snapshot_after(docker, image, cid, before)
            candidates = compute_changeset(before.hashes, after)
            changes = compute_changeset(
                before.hashes, after, ignored_paths(bundle.workspace_dir, candidates.all_paths)
            )
            pull_changes(docker, image, cid, changes.present, work / "after")
        finally:
            docker.rm_force(cid)

        # Phase 3: grade in a fresh evaluation container.
        cid = docker.run_detached(image.tag)
        try:
            pull_changes(docker, image, cid, changes.existed, work / "before")
            diff = render_diff(work / "before", work / "after")
            apply_changeset(docker, image, cid, changes, work / "after")
            post_solver = execute_staged_suite(docker, bundle, image, cid)
        finally:
            docker.rm_force(cid)

    results = consolidate(post_solver)
    return RunOutcome(
        verdict=run_verdict(results),
        diff=diff,
        changes=changes,
        baseline=baseline,
        post_solver=post_solver,
        results=results,
        solve=solve_result,
        started_at=started_at,
        finished_at=utc_now_iso(),
    )
