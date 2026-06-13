"""Pure grading logic: consolidating repeated test runs and checking contracts.

No I/O here — everything is a function over test execution records, so the verdict
rules are table-testable. Flake handling follows SWE-bench Pro §4.3: each suite runs
3 times and any test without a consistent status is flagged flaky.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

Bucket = Literal["fail2pass", "pass2pass"]
Status = Literal["passed", "failed", "error", "timeout"]

FLAKY: Literal["flaky"] = "flaky"


@dataclass(frozen=True)
class TestExecution:
    """One execution of one test (a single attempt)."""

    __test__ = False  # tell pytest this is not a test class despite the name

    test: str
    bucket: Bucket
    attempt: int
    status: Status
    duration_seconds: float
    output: str = ""


@dataclass(frozen=True)
class ConsolidatedResult:
    """A test's overall status across all attempts."""

    test: str
    bucket: Bucket
    status: Status | Literal["flaky"]
    attempt_statuses: tuple[Status, ...]


def consolidate(executions: list[TestExecution]) -> list[ConsolidatedResult]:
    """Collapse per-attempt executions into one result per test, flagging flakes."""
    by_test: dict[tuple[str, Bucket], list[TestExecution]] = defaultdict(list)
    for ex in executions:
        by_test[(ex.test, ex.bucket)].append(ex)
    results = []
    for (test, bucket), runs in sorted(by_test.items()):
        statuses = tuple(r.status for r in sorted(runs, key=lambda r: r.attempt))
        status: Status | Literal["flaky"] = statuses[0] if len(set(statuses)) == 1 else FLAKY
        results.append(
            ConsolidatedResult(test=test, bucket=bucket, status=status, attempt_statuses=statuses)
        )
    return results


def run_verdict(post_solver: list[ConsolidatedResult]) -> Literal["RESOLVED", "UNRESOLVED"]:
    """SWE-bench semantics: RESOLVED ⇔ every fail2pass test now passes AND every
    pass2pass test still passes. Anything else — including an empty fail2pass set,
    a flaky result, a timeout, or an error — is UNRESOLVED.
    """
    f2p = [r for r in post_solver if r.bucket == "fail2pass"]
    p2p = [r for r in post_solver if r.bucket == "pass2pass"]
    if not f2p:
        return "UNRESOLVED"
    if all(r.status == "passed" for r in f2p) and all(r.status == "passed" for r in p2p):
        return "RESOLVED"
    return "UNRESOLVED"


def check_baseline_contract(results: list[ConsolidatedResult]) -> list[str]:
    """Return specific problem descriptions; empty list means the contract holds.

    Contract: every pass2pass test passes consistently, every fail2pass test fails
    consistently. Each violation explains why the task is broken and what to do.
    """
    problems = []
    for r in results:
        if r.status == FLAKY:
            problems.append(
                f"{r.bucket} test `{r.test}` is FLAKY on baseline "
                f"(statuses across attempts: {', '.join(r.attempt_statuses)}). "
                "Deflake or drop it; flaky tests make verdicts meaningless "
                "(SWE-bench Pro filters these out)."
            )
        elif r.bucket == "fail2pass" and r.status == "passed":
            problems.append(
                f"fail2pass test `{r.test}` PASSED on baseline; this task cannot "
                "distinguish a correct solution from a no-op. Either the issue is "
                "already fixed at the pinned commit or the test belongs in pass2pass."
            )
        elif r.bucket == "pass2pass" and r.status != "passed":
            problems.append(
                f"pass2pass test `{r.test}` {r.status.upper()} on baseline; the baseline "
                "environment is broken or this test does not belong in pass2pass. "
                "Check setup_commands and the pinned commit."
            )
        elif r.bucket == "fail2pass" and r.status == "timeout":
            problems.append(
                f"fail2pass test `{r.test}` TIMED OUT on baseline; a timeout cannot be "
                "distinguished from a genuine failure. Raise tests.timeout_seconds or "
                "fix the test."
            )
    return problems


def check_gold_contract(
    baseline: list[ConsolidatedResult], post_gold: list[ConsolidatedResult]
) -> list[str]:
    """Return specific problems with a golden-patch run; empty ⇒ the task is solvable.

    Solvable ⇔ every fail2pass test FAILS on baseline and PASSES after the gold patch
    (a genuine flip), and every pass2pass test PASSES both before and after. Stricter
    than ``run_verdict`` on purpose: a fail2pass test that was already green proves
    nothing, so it is a problem here even though the verdict would still be RESOLVED.
    """
    base_status = {(r.test, r.bucket): r.status for r in baseline}
    problems = []
    if not any(r.bucket == "fail2pass" for r in post_gold):
        problems.append("no fail2pass tests defined; there is nothing for the golden patch to fix.")
    for r in post_gold:
        before = base_status.get((r.test, r.bucket))
        if r.bucket == "fail2pass":
            if r.status != "passed":
                problems.append(
                    f"fail2pass test `{r.test}` is {r.status.upper()} after the golden patch; "
                    "patch.diff does not fix it (or does not apply to the tested code)."
                )
            elif before == "passed":
                problems.append(
                    f"fail2pass test `{r.test}` already PASSED on baseline, so the golden patch "
                    "is not what makes it pass — this proves nothing. Move it to pass2pass."
                )
        elif r.bucket == "pass2pass" and r.status != "passed":
            problems.append(
                f"pass2pass test `{r.test}` regressed to {r.status.upper()} after the golden "
                "patch; the patch breaks existing behavior it must preserve."
            )
    return problems
