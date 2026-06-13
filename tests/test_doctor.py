"""Tests for `task doctor`: preflight environment checks."""

import pytest
from typer.testing import CliRunner

from task_bundle.cli import _check_api_key, _check_disk, app

runner = CliRunner()


def test_api_key_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _check_api_key()[1] == "ok"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert _check_api_key()[1] == "warn"


def test_disk_check_reports_free_space() -> None:
    name, status, detail = _check_disk()
    assert name == "Disk space"
    assert status in {"ok", "warn"}
    assert "GB free" in detail


def test_doctor_lists_all_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs regardless of Docker; just asserts every check row is rendered."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["doctor"])
    out = " ".join(result.output.split())
    for label in ("Docker daemon", "git", "ANTHROPIC_API_KEY", "Disk space"):
        assert label in out
    assert "WARN" in out  # the unset API key


@pytest.mark.docker
def test_doctor_passes_when_docker_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "All required checks passed" in result.output
