"""ClaudeSolver tests with a scripted fake API client (no real API calls).

The docker-marked test drives the full agentic loop against a real container:
scripted tool calls read calc.py, write the fix, verify with run_command, then
finish — proving tool execution, workspace sync-out, and budget accounting without
LLM nondeterminism.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import F2P_TEST, P2P_TEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.cli import app
from task_bundle.db import Database
from task_bundle.errors import SolverError
from task_bundle.solver.claude import ClaudeSolver, _safe_path

runner = CliRunner()

FIXED_CALC = """\"\"\"calc, fixed.\"\"\"


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def divide(a, b):
    return a / b
"""


def _msg(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        stop_reason="tool_use",
    )


def _tool(name: str, tool_id: str, **tool_input: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


class ScriptedClient:
    """Returns a fixed sequence of responses, recording every request."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        kwargs["messages"] = list(kwargs["messages"])  # snapshot: the solver mutates in place
        self.requests.append(kwargs)
        return self.responses[len(self.requests) - 1]


class TestUnitBehavior:
    def test_missing_api_key_is_actionable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(SolverError, match="ANTHROPIC_API_KEY"):
            ClaudeSolver()._messages()

    def test_safe_path_confines_to_workspace(self) -> None:
        assert _safe_path("calc.py") == "/workspace/calc.py"
        assert _safe_path("./tests/x.py") == "/workspace/tests/x.py"
        assert _safe_path("/calc.py") == "/workspace/calc.py"  # absolute treated as repo-rooted
        for escape in ("../../etc/passwd", "a/../../etc", ".."):
            with pytest.raises(ValueError, match="escapes"):
                _safe_path(escape)

    def test_model_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        assert ClaudeSolver().model == "claude-sonnet-4-6"
        assert ClaudeSolver(model="claude-haiku-4-5").model == "claude-haiku-4-5"
        monkeypatch.delenv("ANTHROPIC_MODEL")
        assert ClaudeSolver().model == "claude-opus-4-7"


@pytest.mark.docker
def test_scripted_agentic_run_resolves(
    tmp_path: Path,
    shared_origin: tuple[str, str],
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    client = ScriptedClient(
        [
            _msg(_text("Let me look at the code."), _tool("read_file", "t1", path="calc.py")),
            _msg(
                _tool("write_file", "t2", path="calc.py", content=FIXED_CALC),
                _tool(
                    "run_command",
                    "t3",
                    command="python -c 'from calc import divide; print(divide(6, 3))'",
                ),
            ),
            _msg(_text("Fixed divide() to use true division.")),
        ]
    )
    solver = ClaudeSolver(client=client)
    monkeypatch.setattr("task_bundle.cli._make_solver", lambda *args, **kwargs: solver)
    result = runner.invoke(app, ["run", str(bundle.path), "--solver", "claude"])
    assert result.exit_code == 0, result.output
    assert "RESOLVED" in result.output

    assert len(client.requests) == 3
    # tool results flowed back: 2nd request carries the read_file output
    tool_results = client.requests[1]["messages"][-1]["content"]
    assert "return a * b" in tool_results[0]["content"]
    # run_command executed in-container and returned exit status
    run_output = client.requests[2]["messages"][-1]["content"][1]["content"]
    assert "2.0" in run_output and "exit 0" in run_output

    db = Database(isolated_db)
    run_row = db.list_runs()[0]
    assert run_row["verdict"] == "RESOLVED"
    assert run_row["solver"] == "claude"
    assert run_row["input_tokens"] == 300  # 3 calls x 100
    assert run_row["output_tokens"] == 150
    assert run_row["cost_usd"] is not None and run_row["cost_usd"] > 0
    report_artifact = next(
        a for a in db.artifacts_for(run_row["command_id"]) if a["type"] == "report"
    )
    transcript_artifact = next(
        a for a in db.artifacts_for(run_row["command_id"]) if a["type"] == "solver_transcript"
    )
    assert "model finished after 3 iteration(s)" in Path(transcript_artifact["path"]).read_text()
    assert "+    return a / b" in Path(report_artifact["path"]).read_text()
