"""Concurrent, resumable orchestration primitives for benchmark fleets."""

import hashlib
import json
import math
import shutil
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class FleetJob:
    """One task/sample pair with content-derived, retry-stable identifiers."""

    id: str
    run_id: str
    config_hash: str
    bundle_path: Path
    task_id: str
    solver: str
    model: str | None
    max_iterations: int
    sample: int
    image_tag: str
    patch: Path | None = None
    gold: bool = False


@dataclass(frozen=True)
class FleetResult:
    job_id: str
    run_id: str
    task_id: str
    sample: int
    verdict: str
    error: str | None = None
    resumed: bool = False


class FleetBackend(Protocol):
    """Where a fleet job runs; scheduling and persistence stay backend-neutral."""

    name: str

    def run(self, job: FleetJob) -> FleetResult: ...


class LocalBackend:
    """Execute jobs in this process; container placement is handled by the runtime."""

    name = "local"

    def __init__(self, execute: Callable[[FleetJob], FleetResult]) -> None:
        self._execute = execute

    def run(self, job: FleetJob) -> FleetResult:
        return self._execute(job)


class DiskGuard:
    """Pause admissions with bounded polling while the artifact volume is low."""

    def __init__(
        self,
        path: Path,
        min_free_gb: float,
        backoff_seconds: float,
        *,
        disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
        wait: Callable[[float], object] | None = None,
        on_pressure: Callable[[float], None] | None = None,
    ) -> None:
        self.path = path
        self.min_free_bytes = int(min_free_gb * 1_000_000_000)
        self.backoff_seconds = backoff_seconds
        self._disk_usage = disk_usage
        self._wait = wait or threading.Event().wait
        self._on_pressure = on_pressure

    def wait_for_capacity(self) -> None:
        if self.min_free_bytes <= 0:
            return
        while True:
            free = self._disk_usage(self.path).free
            if free >= self.min_free_bytes:
                return
            if self._on_pressure is not None:
                self._on_pressure(free / 1_000_000_000)
            self._wait(self.backoff_seconds)


class FleetScheduler:
    """Bounded worker pool with a separate per-host container admission limit."""

    def __init__(
        self,
        backend: FleetBackend,
        *,
        concurrency: int,
        container_limit: int,
        disk_guard: DiskGuard,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if container_limit < 1:
            raise ValueError("container_limit must be at least 1")
        self.backend = backend
        # A run currently owns at most one live container at a time. Keeping this
        # admission bound separate makes the invariant explicit and future-proofs
        # workers that may use more than one.
        self.workers = min(concurrency, container_limit)
        self.disk_guard = disk_guard

    def run(
        self,
        jobs: Iterable[FleetJob],
        *,
        on_result: Callable[[FleetResult, int, int], None] | None = None,
    ) -> list[FleetResult]:
        ordered = list(jobs)
        results: dict[str, FleetResult] = {}

        def admitted(job: FleetJob) -> FleetResult:
            self.disk_guard.wait_for_capacity()
            return self.backend.run(job)

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="fleet") as pool:
            futures = {pool.submit(admitted, job): job for job in ordered}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # one task must not strand the rest of a sweep
                    result = FleetResult(
                        job.id,
                        job.run_id,
                        job.task_id,
                        job.sample,
                        "ERROR",
                        f"{type(exc).__name__}: {exc}",
                    )
                results[job.id] = result
                if on_result is not None:
                    on_result(result, len(results), len(ordered))
        return [results[job.id] for job in ordered]


def stable_job(
    *,
    bundle_path: Path,
    task_id: str,
    repo_commit: str,
    solver: str,
    model: str | None,
    max_iterations: int,
    sample: int,
    image_tag: str,
    patch: Path | None = None,
    gold: bool = False,
) -> FleetJob:
    """Build an idempotent job keyed by instance, model, solver config, and sample."""
    patch_digest = None
    if patch is not None:
        patch_digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    payload = {
        "task_id": task_id,
        "repo_commit": repo_commit,
        "solver": solver,
        "model": model,
        "max_iterations": max_iterations,
        "image_tag": image_tag,
        "patch_sha256": patch_digest,
        "gold": gold,
    }
    config_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    job_digest = hashlib.sha256(f"{config_digest}:{sample}".encode()).hexdigest()
    return FleetJob(
        id=f"job_{job_digest[:24]}",
        run_id=f"run_{job_digest[:24]}",
        config_hash=config_digest,
        bundle_path=bundle_path.resolve(),
        task_id=task_id,
        solver=solver,
        model=model,
        max_iterations=max_iterations,
        sample=sample,
        image_tag=image_tag,
        patch=patch.resolve() if patch is not None else None,
        gold=gold,
    )


def fleet_identity(jobs: Iterable[FleetJob]) -> tuple[str, str]:
    """Return an identity independent of worker tuning and requested sample count."""
    keys = sorted({job.config_hash for job in jobs})
    digest = hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()
    return f"fleet_{digest[:20]}", digest


def pass_at_k(results: Iterable[FleetResult], k: int) -> float | None:
    """Unbiased pass@k estimator averaged across tasks with at least ``k`` samples."""
    if k < 1:
        raise ValueError("k must be at least 1")
    by_task: dict[str, list[FleetResult]] = defaultdict(list)
    for result in results:
        by_task[result.task_id].append(result)
    estimates = []
    for task_results in by_task.values():
        n = len(task_results)
        if n < k:
            continue
        solved = sum(result.verdict == "RESOLVED" for result in task_results)
        estimate = 1.0 if n - solved < k else 1.0 - math.comb(n - solved, k) / math.comb(n, k)
        estimates.append(estimate)
    return sum(estimates) / len(estimates) if estimates else None
