"""Run report assembly and deterministic JSON emission.

Reports are written with sorted keys and stable formatting so two runs of the same
deterministic solver produce byte-identical reports apart from ids and timestamps.
"""

import json
import platform
import subprocess
from pathlib import Path

from task_bundle import __version__
from task_bundle.bundle import Bundle
from task_bundle.container import Docker
from task_bundle.run import RunOutcome
from task_bundle.solver.base import Solver


def tool_versions(docker: Docker) -> dict[str, str]:
    """Versions of everything that affects reproducibility, recorded per run."""
    try:
        git_version = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except FileNotFoundError:
        git_version = "unknown"
    return {
        "container_runtime": docker.runtime_name,
        docker.runtime_name: docker.version(),
        "git": git_version,
        "python": platform.python_version(),
        "task_bundle": __version__,
    }


def build_report(
    run_id: str,
    command_id: str,
    bundle: Bundle,
    solver: Solver,
    outcome: RunOutcome,
    image_tag: str,
    image_digest: str | None,
    versions: dict[str, str],
) -> dict[str, object]:
    """Assemble the structured evaluation artifact for one run."""
    baseline_status = {e.test: e.status for e in outcome.baseline}
    tests = [
        {
            "test": r.test,
            "bucket": r.bucket,
            "baseline_status": baseline_status.get(r.test, "unknown"),
            "post_solver_status": r.status,
        }
        for r in outcome.results
    ]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "command_id": command_id,
        "task_id": bundle.spec.id,
        "repo": {"url": bundle.spec.repo.url, "commit": bundle.spec.repo.commit},
        "solver": solver.name,
        "model": solver.model,
        "started_at": outcome.started_at,
        "finished_at": outcome.finished_at,
        "image": {"tag": image_tag, "digest": image_digest},
        "tool_versions": versions,
        "tests": tests,
        "verdict": outcome.verdict,
        "diff": outcome.diff,
        "stats": {
            "input_tokens": outcome.solve.input_tokens,
            "output_tokens": outcome.solve.output_tokens,
            "cost_usd": outcome.solve.cost_usd,
        },
    }


def write_report(path: Path, report: dict[str, object]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
