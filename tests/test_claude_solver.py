"""ClaudeSolver tests with a scripted fake API client (no real API calls).

The docker-marked test drives the full agentic loop against a real container:
scripted tool calls read calc.py, write the fix, verify with run_command, then
finish — proving tool execution, in-place changeset capture, and budget accounting
without LLM nondeterminism.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import pytest
from conftest import F2P_TEST, P2P_TEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.cli import app
from task_bundle.container import Docker, ExecResult
from task_bundle.db import Database
from task_bundle.errors import SolverError
from task_bundle.solver import SolveContext
from task_bundle.solver.claude import (
    ClaudeSolver,
    _estimate_cost,
    _safe_path,
    supports_adaptive_thinking,
)

runner = CliRunner()

FIXED_CALC = """\"\"\"calc, fixed.\"\"\"


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def divide(a, b):
    return a / b
"""


def _msg(
    *blocks: SimpleNamespace,
    stop_reason: str = "tool_use",
    input_tokens: int = 100,
    cache_read: int = 0,
    cache_write: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=50,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        stop_reason=stop_reason,
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
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, Exception):
            raise response
        return response


class FakeDocker:
    """Enough of Docker for tool execution without a daemon: every command 'succeeds'."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def exec(self, cid: str, command: str, **kwargs: Any) -> ExecResult:
        self.commands.append(command)
        return ExecResult(exit_code=0, output=f"ran: {command}", duration_seconds=0.01)


def make_ctx(docker: Any = None) -> SolveContext:
    return SolveContext(
        docker=cast(Docker, docker or FakeDocker()),
        container_id="cid",
        repo_dir="/workspace",
        baseline_tree=Path("/nonexistent"),
        description="fix divide",
        test_command_template="python -m pytest {test_path} -q",
        timeout_seconds=60,
    )


class TestUnitBehavior:
    def test_missing_credentials_is_actionable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setattr(
            "task_bundle.solver.claude._credential_profile_dir", lambda: tmp_path / "none"
        )
        with pytest.raises(SolverError, match="ANTHROPIC_API_KEY"):
            ClaudeSolver()._messages()

    def test_bad_effort_is_rejected(self) -> None:
        with pytest.raises(SolverError, match="effort"):
            ClaudeSolver(effort="ultra")

    def test_adaptive_thinking_support_by_model(self) -> None:
        assert supports_adaptive_thinking("claude-opus-5")
        assert supports_adaptive_thinking("claude-sonnet-5")
        assert supports_adaptive_thinking("claude-opus-4-7")
        assert supports_adaptive_thinking("claude-sonnet-4-6")
        assert supports_adaptive_thinking("claude-fable-5-1")
        assert not supports_adaptive_thinking("claude-haiku-4-5")
        assert not supports_adaptive_thinking("claude-sonnet-4-5")
        assert not supports_adaptive_thinking("claude-3-5-sonnet-latest")

    def test_cost_prices_cache_separately(self) -> None:
        # opus-5: $5/M in, $25/M out; cache read 0.1x, cache write 1.25x input
        assert _estimate_cost("claude-opus-5", 1_000_000, 0) == 5.0
        assert _estimate_cost("claude-opus-5", 0, 1_000_000) == 25.0
        assert _estimate_cost("claude-opus-5", 0, 0, cache_read=1_000_000) == 0.5
        assert _estimate_cost("claude-opus-5", 0, 0, cache_write=1_000_000) == 6.25
        assert _estimate_cost("claude-sonnet-4-6", 1_000_000, 0) == 3.0  # longest prefix wins
        assert _estimate_cost("claude-sonnet-5", 1_000_000, 0) == 2.0
        assert _estimate_cost("some-other-model", 100, 100) is None

    def test_request_is_cache_stable_with_thinking_and_effort(self) -> None:
        client = ScriptedClient([_msg(_text("Nothing to do."), stop_reason="end_turn")])
        result = ClaudeSolver(model="claude-opus-5", effort="xhigh", client=client).solve(
            make_ctx()
        )
        assert "finished after 1 iteration" in result.stop_reason
        req = client.requests[0]
        assert req["model"] == "claude-opus-5"
        assert req["cache_control"] == {"type": "ephemeral"}
        assert req["thinking"] == {"type": "adaptive"}
        assert req["output_config"] == {"effort": "xhigh"}
        assert isinstance(req["system"], list) and "/workspace" in req["system"][0]["text"]
        assert [t["name"] for t in req["tools"]] == [
            "list_dir", "read_file", "search", "edit_file", "write_file", "run_command"
        ]  # fmt: skip
        assert "max_tokens" in req and "budget_tokens" not in json.dumps(req)

    def test_legacy_model_gets_no_thinking_params(self) -> None:
        client = ScriptedClient([_msg(_text("done"), stop_reason="end_turn")])
        ClaudeSolver(model="claude-haiku-4-5", client=client).solve(make_ctx())
        req = client.requests[0]
        assert "thinking" not in req and "output_config" not in req

    def test_api_error_after_retries_keeps_edits_and_ends_gracefully(self) -> None:
        docker = FakeDocker()
        client = ScriptedClient(
            [
                _msg(_tool("run_command", "t1", command="echo hi")),
                anthropic.APIConnectionError(request=cast(Any, None)),
            ]
        )
        result = ClaudeSolver(model="claude-opus-5", client=client).solve(make_ctx(docker))
        assert docker.commands == ["echo hi"]  # the tool call before the failure ran
        assert result.stop_reason is not None and "api error" in result.stop_reason
        assert result.input_tokens == 100  # only the successful call is counted
        assert result.trajectory[-1]["type"] == "end"
        assert any(e["type"] == "error" for e in result.trajectory)

    def test_refusal_ends_the_solve(self) -> None:
        client = ScriptedClient([_msg(stop_reason="refusal")])
        result = ClaudeSolver(model="claude-opus-5", client=client).solve(make_ctx())
        assert result.stop_reason is not None and "refused" in result.stop_reason

    def test_context_budget_stops_after_applying_the_turn(self) -> None:
        docker = FakeDocker()
        client = ScriptedClient(
            [
                _msg(_tool("run_command", "t1", command="true"), input_tokens=1000, cache_read=500),
                _msg(_text("would continue"), stop_reason="end_turn"),
            ]
        )
        solver = ClaudeSolver(model="claude-opus-5", client=client, context_budget_tokens=1200)
        result = solver.solve(make_ctx(docker))
        assert docker.commands == ["true"]
        assert len(client.requests) == 1  # no second request once the budget is exceeded
        assert result.stop_reason is not None and "context budget" in result.stop_reason
        assert result.cache_read_tokens == 500

    def test_trajectory_records_exact_tool_io_and_errors(self) -> None:
        docker = FakeDocker()
        client = ScriptedClient(
            [
                _msg(
                    _text("Let me check."),
                    _tool("run_command", "t1", command="ls"),
                    _tool("frobnicate", "t2", x=1),
                    _tool("list_dir", "t3", path="../../etc"),
                ),
                _msg(_text("Done."), stop_reason="end_turn", cache_read=90, cache_write=10),
            ]
        )
        result = ClaudeSolver(model="claude-opus-5", client=client).solve(make_ctx(docker))
        # tool results flow back in one user message with is_error where appropriate
        results = client.requests[1]["messages"][-1]["content"]
        assert [r["tool_use_id"] for r in results] == ["t1", "t2", "t3"]
        assert "is_error" not in results[0]
        assert results[1]["is_error"] is True and "unknown tool" in results[1]["content"]
        assert results[2]["is_error"] is True and "escapes" in results[2]["content"]
        # structured trajectory: start, assistant, 3 tool results, assistant, end
        kinds = [e["type"] for e in result.trajectory]
        assert kinds == ["start", "assistant", "tool_result", "tool_result", "tool_result",
                         "assistant", "end"]  # fmt: skip
        assert result.trajectory[1]["tool_calls"][0] == {
            "id": "t1", "name": "run_command", "input": {"command": "ls"}
        }  # fmt: skip
        assert result.trajectory[2]["output"] == results[0]["content"]  # exactly what the model saw
        assert result.trajectory[-1]["usage"] == {
            "input": 200, "output": 100, "cache_read": 90, "cache_write": 10
        }  # fmt: skip
        assert result.cost_usd == _estimate_cost("claude-opus-5", 200, 100, 90, 10)
        assert all(json.dumps(e) for e in result.trajectory)  # serializable as JSONL

    def test_safe_path_confines_to_repo_dir(self) -> None:
        assert _safe_path("calc.py", "/workspace") == "/workspace/calc.py"
        assert _safe_path("./tests/x.py", "/workspace") == "/workspace/tests/x.py"
        # absolute paths are treated as repo-rooted
        assert _safe_path("/calc.py", "/workspace") == "/workspace/calc.py"
        assert _safe_path("/app/calc.py", "/app") == "/app/app/calc.py"
        assert _safe_path("calc.py", "/app") == "/app/calc.py"
        for escape in ("../../etc/passwd", "a/../../etc", ".."):
            with pytest.raises(ValueError, match="escapes"):
                _safe_path(escape, "/workspace")

    def test_model_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        assert ClaudeSolver().model == "claude-sonnet-4-6"
        assert ClaudeSolver(model="claude-haiku-4-5").model == "claude-haiku-4-5"
        monkeypatch.delenv("ANTHROPIC_MODEL")
        assert ClaudeSolver().model == "claude-opus-5"


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
    report = json.loads(Path(report_artifact["path"]).read_text())
    assert "+    return a / b" in report["diff"]
    assert report["stats"]["stop_reason"] == "model finished after 3 iteration(s)"
    assert report["stats"]["solver_config"]["effort"] == "high"
    trajectory_artifact = next(
        a for a in db.artifacts_for(run_row["command_id"]) if a["type"] == "solver_trajectory"
    )
    events = [
        json.loads(line) for line in Path(trajectory_artifact["path"]).read_text().splitlines()
    ]
    assert events[0]["type"] == "start" and events[-1]["type"] == "end"
    assert any(e["type"] == "tool_result" and e["name"] == "write_file" for e in events)


@pytest.mark.docker
def test_scripted_run_captures_deletions_and_ignores_bytecode(
    tmp_path: Path,
    shared_origin: tuple[str, str],
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-place changeset: a deleted file is really gone at grade time and appears in
    the diff as a deletion; a new file appears as a creation; the __pycache__ that
    the model's run_command leaves behind is not part of the changeset."""
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    client = ScriptedClient(
        [
            _msg(
                _tool("write_file", "t1", path="calc.py", content=FIXED_CALC),
                _tool("write_file", "t2", path="docs/NOTES.md", content="fixed divide\n"),
                _tool("run_command", "t3", command="rm README.md && python -c 'import calc'"),
                _tool("run_command", "t4", command="ls __pycache__"),
            ),
            _msg(_text("Done.")),
        ]
    )
    solver = ClaudeSolver(client=client)
    monkeypatch.setattr("task_bundle.cli._make_solver", lambda *args, **kwargs: solver)
    result = runner.invoke(app, ["run", str(bundle.path), "--solver", "claude"])
    assert result.exit_code == 0, result.output
    assert "RESOLVED" in result.output
    assert "changed 1 file(s), added 1, deleted 1" in " ".join(result.output.split())
    # the model saw bytecode in its container (so hygiene filtering is what hid it)
    assert "calc.cpython" in client.requests[1]["messages"][-1]["content"][3]["content"]

    db = Database(isolated_db)
    run_row = db.list_runs()[0]
    diff = Path(
        next(a for a in db.artifacts_for(run_row["command_id"]) if a["type"] == "solver_diff")[
            "path"
        ]
    ).read_text()
    assert "--- a/README.md\n+++ /dev/null" in diff
    assert "--- /dev/null\n+++ b/docs/NOTES.md" in diff
    assert "+    return a / b" in diff
    assert "__pycache__" not in diff


@pytest.mark.docker
def test_scripted_edit_and_search_tools(
    tmp_path: Path,
    shared_origin: tuple[str, str],
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """edit_file does an exact single replacement in-container and refuses ambiguous
    matches; search returns path:line:text hits. The edit alone resolves the task."""
    bundle = make_initialized_bundle(tmp_path / "b", shared_origin, f2p=F2P_TEST, p2p=P2P_TEST)
    client = ScriptedClient(
        [
            _msg(_tool("search", "t1", pattern="def (add|divide)", path=".")),
            _msg(
                _tool("edit_file", "t2", path="calc.py", old_str="return a", new_str="return b"),
                _tool("edit_file", "t3", path="calc.py", old_str="nowhere", new_str="x"),
                _tool(
                    "edit_file",
                    "t4",
                    path="calc.py",
                    old_str="return a * b",
                    new_str="return a / b",
                ),
            ),
            _msg(_text("Fixed divide() with a targeted edit."), stop_reason="end_turn"),
        ]
    )
    solver = ClaudeSolver(model="claude-opus-5", client=client)
    monkeypatch.setattr("task_bundle.cli._make_solver", lambda *args, **kwargs: solver)
    result = runner.invoke(app, ["run", str(bundle.path), "--solver", "claude"])
    assert result.exit_code == 0, result.output
    assert "RESOLVED" in result.output

    search_out = client.requests[1]["messages"][-1]["content"][0]["content"]
    assert "calc.py:" in search_out and "def divide" in search_out and "def add" in search_out
    edits = client.requests[2]["messages"][-1]["content"]
    assert edits[0]["is_error"] is True and "matches" in edits[0]["content"]  # ambiguous
    assert edits[1]["is_error"] is True and "not found" in edits[1]["content"]
    assert "is_error" not in edits[2] and edits[2]["content"].startswith("edited calc.py")

    db = Database(isolated_db)
    run_row = db.list_runs()[0]
    diff = Path(
        next(a for a in db.artifacts_for(run_row["command_id"]) if a["type"] == "solver_diff")[
            "path"
        ]
    ).read_text()
    assert "-    return a * b\n+    return a / b" in diff
    assert diff.count("diff --git") == 1  # exactly one file touched, nothing else
