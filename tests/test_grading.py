"""Table-driven tests for the pure grading logic."""

import pytest

from task_bundle.grading import (
    FLAKY,
    Bucket,
    Status,
    TestExecution,
    check_baseline_contract,
    consolidate,
)


def execs(test: str, bucket: Bucket, statuses: list[Status]) -> list[TestExecution]:
    return [
        TestExecution(test=test, bucket=bucket, attempt=i + 1, status=s, duration_seconds=0.1)
        for i, s in enumerate(statuses)
    ]


class TestConsolidate:
    def test_consistent_status_kept(self) -> None:
        [r] = consolidate(execs("t", "fail2pass", ["failed", "failed", "failed"]))
        assert r.status == "failed"
        assert r.attempt_statuses == ("failed", "failed", "failed")

    def test_inconsistent_status_is_flaky(self) -> None:
        [r] = consolidate(execs("t", "pass2pass", ["passed", "failed", "passed"]))
        assert r.status == FLAKY

    def test_results_sorted_by_test_name(self) -> None:
        executions = execs("b", "pass2pass", ["passed"]) + execs("a", "pass2pass", ["passed"])
        assert [r.test for r in consolidate(executions)] == ["a", "b"]

    def test_attempts_ordered_even_if_recorded_out_of_order(self) -> None:
        executions = execs("t", "pass2pass", ["passed", "failed"])
        [r] = consolidate(list(reversed(executions)))
        assert r.attempt_statuses == ("passed", "failed")


# (bucket, consistent status) -> contract ok?
CONTRACT_TABLE: list[tuple[Bucket, Status, bool]] = [
    ("fail2pass", "failed", True),
    ("fail2pass", "passed", False),
    ("fail2pass", "timeout", False),
    ("fail2pass", "error", True),  # errored test counts as failing on baseline
    ("pass2pass", "passed", True),
    ("pass2pass", "failed", False),
    ("pass2pass", "timeout", False),
    ("pass2pass", "error", False),
]


class TestBaselineContract:
    @pytest.mark.parametrize(("bucket", "status", "ok"), CONTRACT_TABLE)
    def test_single_test_contract(self, bucket: Bucket, status: Status, ok: bool) -> None:
        results = consolidate(execs("t", bucket, [status, status, status]))
        problems = check_baseline_contract(results)
        assert (problems == []) is ok, problems

    def test_flaky_test_always_violates(self) -> None:
        results = consolidate(execs("t", "fail2pass", ["failed", "passed", "failed"]))
        [problem] = check_baseline_contract(results)
        assert "FLAKY" in problem

    def test_f2p_pass_message_is_specific(self) -> None:
        results = consolidate(execs("test_foo", "fail2pass", ["passed"] * 3))
        [problem] = check_baseline_contract(results)
        assert "test_foo" in problem
        assert "cannot distinguish" in problem

    def test_p2p_fail_message_is_specific(self) -> None:
        results = consolidate(execs("test_bar", "pass2pass", ["failed"] * 3))
        [problem] = check_baseline_contract(results)
        assert "test_bar" in problem
        assert "baseline" in problem

    def test_all_good_multi_test(self) -> None:
        executions = (
            execs("f1", "fail2pass", ["failed"] * 3)
            + execs("f2", "fail2pass", ["failed"] * 3)
            + execs("p1", "pass2pass", ["passed"] * 3)
        )
        assert check_baseline_contract(consolidate(executions)) == []

    def test_multiple_problems_all_reported(self) -> None:
        executions = execs("f1", "fail2pass", ["passed"] * 3) + execs(
            "p1", "pass2pass", ["failed"] * 3
        )
        assert len(check_baseline_contract(consolidate(executions))) == 2
