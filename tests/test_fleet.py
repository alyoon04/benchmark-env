"""Fleet scheduling, idempotency, disk admission, and pass@k."""

import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

from task_bundle.fleet import (
    DiskGuard,
    FleetResult,
    FleetScheduler,
    LocalBackend,
    fleet_identity,
    pass_at_k,
    stable_job,
)


def _job(tmp_path: Path, *, task: str = "task", sample: int = 1):
    return stable_job(
        bundle_path=tmp_path,
        task_id=task,
        repo_commit="a" * 40,
        solver="claude",
        model="model-v1",
        max_iterations=10,
        sample=sample,
        image_tag=f"task-bundle/{task}:abc",
    )


def test_job_and_fleet_ids_are_stable_but_samples_are_distinct(tmp_path: Path) -> None:
    first = _job(tmp_path, sample=1)
    same = _job(tmp_path / "moved", sample=1)
    second = _job(tmp_path, sample=2)

    assert first.id == same.id  # paths do not affect benchmark identity
    assert first.run_id == same.run_id
    assert first.config_hash == second.config_hash
    assert first.id != second.id
    assert fleet_identity([first]) == fleet_identity([first, second])


def test_scheduler_respects_container_limit(tmp_path: Path) -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def execute(job):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return FleetResult(job.id, job.run_id, job.task_id, job.sample, "UNRESOLVED")

    guard = DiskGuard(tmp_path, 0, 0.1)
    scheduler = FleetScheduler(
        LocalBackend(execute), concurrency=8, container_limit=2, disk_guard=guard
    )
    results = scheduler.run(_job(tmp_path, sample=i) for i in range(1, 7))

    assert peak == 2
    assert len(results) == 6


def test_disk_guard_backs_off_until_capacity(tmp_path: Path) -> None:
    Usage = namedtuple("Usage", "total used free")
    readings = iter([Usage(20, 19, 1), Usage(20, 18, 2), Usage(20, 5, 15)])
    waits: list[float] = []
    notices: list[float] = []
    guard = DiskGuard(
        tmp_path,
        min_free_gb=10 / 1_000_000_000,
        backoff_seconds=3,
        disk_usage=lambda _: next(readings),
        wait=waits.append,
        on_pressure=notices.append,
    )

    guard.wait_for_capacity()

    assert waits == [3, 3]
    assert len(notices) == 2


def test_pass_at_k_uses_unbiased_estimator() -> None:
    results = [
        FleetResult(f"j{i}", f"r{i}", "a", i, verdict)
        for i, verdict in enumerate(["RESOLVED", "UNRESOLVED", "UNRESOLVED"], 1)
    ] + [FleetResult(f"k{i}", f"s{i}", "b", i, "UNRESOLVED") for i in range(1, 4)]

    assert pass_at_k(results, 1) == pytest.approx(1 / 6)
    assert pass_at_k(results, 2) == pytest.approx(1 / 3)
    assert pass_at_k(results, 3) == pytest.approx(1 / 2)
    assert pass_at_k(results, 4) is None


def test_spend_cap_stops_admitting_and_leaves_rest_unrun(tmp_path: Path) -> None:
    """Completed cost gates admission; unrun jobs come back SKIPPED (not ERROR)."""
    from task_bundle.fleet import SKIPPED, SpendCap

    ran: list[str] = []

    def execute(job):  # type: ignore[no-untyped-def]
        ran.append(job.task_id)
        return FleetResult(job.id, job.run_id, job.task_id, job.sample, "RESOLVED", cost_usd=0.6)

    jobs = [_job(tmp_path, task=f"t{i}") for i in range(4)]
    cap = SpendCap(1.0)
    results = FleetScheduler(
        LocalBackend(execute),
        concurrency=1,
        container_limit=1,
        disk_guard=DiskGuard(tmp_path, 0, 0),
        spend_cap=cap,
    ).run(jobs)
    # 0.6 < 1.0 admits the second job (total 1.2); the third and fourth are gated.
    assert ran == ["t0", "t1"]
    assert [r.verdict for r in results] == ["RESOLVED", "RESOLVED", SKIPPED, SKIPPED]
    assert results[2].error is not None and "spend cap reached" in results[2].error
    assert cap.spent_usd == pytest.approx(1.2)
    # skipped jobs are not samples
    assert pass_at_k(results, 1) == 1.0


def test_no_cap_admits_everything(tmp_path: Path) -> None:
    from task_bundle.fleet import SpendCap

    cap = SpendCap(None)
    assert cap.admit()
    cap.record(99.0)
    assert cap.admit()
    cap.record(None)
    assert cap.spent_usd == 99.0


def test_bundles_from_summary_filters_and_resolves(tmp_path: Path) -> None:
    import json

    from task_bundle.fleet import bundles_from_summary

    parent = tmp_path / "bundles"
    for name in ("repo-aaaa", "repo-bbbb", "repo-cccc"):
        (parent / name).mkdir(parents=True)
        (parent / name / "task.json").write_text("{}")
    summary = {
        "i1": {"bundle": "elsewhere/repo-aaaa", "status": "gradeable", "reason": ""},
        "i2": {"bundle": str(parent / "repo-bbbb"), "status": "refused", "reason": "x"},
        "i3": {"bundle": "elsewhere/repo-cccc", "status": "skipped", "reason": "already"},
        "i4": {"bundle": "elsewhere/repo-dddd", "status": "error", "reason": "boom"},
    }
    (parent / "import_summary.json").write_text(json.dumps(summary))

    # relative paths that don't resolve from cwd are found next to the summary file
    got = bundles_from_summary(parent / "import_summary.json")
    assert [p.name for p in got] == ["repo-aaaa", "repo-cccc"]
    assert all(p.is_absolute() for p in got)
    assert [p.name for p in bundles_from_summary(parent / "import_summary.json", ["refused"])] == [
        "repo-bbbb"
    ]
    with pytest.raises(FileNotFoundError, match="repo-dddd"):
        bundles_from_summary(parent / "import_summary.json", ["error"])


def test_fleet_cli_requires_paths_or_summary(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from task_bundle.cli import app

    result = CliRunner().invoke(app, ["fleet"])
    assert result.exit_code != 0
    assert "--from-summary" in str(result.exception)
    result = CliRunner().invoke(app, ["fleet", "--from-summary", str(tmp_path / "nope.json")])
    assert result.exit_code != 0
    assert "No import summary" in str(result.exception)
