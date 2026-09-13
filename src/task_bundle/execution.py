"""Persisted execution of one solver run, shared by ``run`` and ``fleet``."""

import json
from dataclasses import dataclass
from pathlib import Path

from task_bundle.bundle import Bundle, utc_now_iso
from task_bundle.container import Docker
from task_bundle.db import Database
from task_bundle.harness import TaskImage
from task_bundle.report import build_report, tool_versions, write_report
from task_bundle.run import RunOutcome, execute_run
from task_bundle.solver.base import Solver


@dataclass(frozen=True)
class RecordedRun:
    """A completed run plus the paths and metadata written around it."""

    run_id: str
    outcome: RunOutcome
    report_path: Path
    image_digest: str | None


def execute_recorded_run(
    *,
    db: Database,
    command_id: str,
    run_id: str,
    artifact_dir: Path,
    docker: Docker,
    bundle: Bundle,
    image: TaskImage,
    solver: Solver,
    restart: bool = False,
) -> RecordedRun:
    """Execute, persist, and report one run.

    Fleet run ids are content-derived, so ``restart`` resets a prior errored row
    before retrying it. Ordinary one-off runs retain the existing insert-only
    behavior.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    versions = tool_versions(docker)
    digest = docker.image_id(image.tag)
    started_at = utc_now_iso()
    insert = db.restart_run if restart else db.insert_run
    insert(
        run_id,
        command_id,
        str(bundle.spec.id),
        solver.name,
        solver.model,
        started_at,
        image.tag,
        digest,
        json.dumps(versions, sort_keys=True),
    )
    try:
        outcome = execute_run(docker, bundle, image, solver)
    except BaseException:
        db.finish_run(run_id, "ERROR", utc_now_iso())
        raise

    db.record_test_results(command_id, "baseline", outcome.baseline, run_id=run_id)
    db.record_test_results(command_id, "post_solver", outcome.post_solver, run_id=run_id)
    db.finish_run(
        run_id,
        outcome.verdict,
        utc_now_iso(),
        outcome.solve.input_tokens,
        outcome.solve.output_tokens,
        outcome.solve.cost_usd,
    )

    artifacts = {
        "solver_diff": ("solver.diff", outcome.diff),
        "solver_transcript": ("transcript.txt", outcome.solve.transcript),
        "test_output": (
            "run_tests.txt",
            "\n".join(
                f"=== {e.test} [{e.bucket}] {phase}: {e.status} ===\n{e.output}"
                for phase, executions in (
                    ("baseline", outcome.baseline),
                    ("post_solver", outcome.post_solver),
                )
                for e in executions
            ),
        ),
    }
    if outcome.solve.trajectory:
        artifacts["solver_trajectory"] = (
            "trajectory.jsonl",
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in outcome.solve.trajectory),
        )
    for type_, (filename, content) in artifacts.items():
        path = artifact_dir / filename
        path.write_text(content)
        db.add_artifact(command_id, type_, str(path), run_id=run_id)

    report = build_report(run_id, command_id, bundle, solver, outcome, image.tag, digest, versions)
    report_path = artifact_dir / "report.json"
    write_report(report_path, report)
    db.add_artifact(command_id, "report", str(report_path), run_id=run_id)
    return RecordedRun(run_id, outcome, report_path, digest)
