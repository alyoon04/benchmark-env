"""ClaudeSolver: an agentic tool-use loop over the Claude API.

The model gets four tools — list_dir, read_file, write_file, run_command — and every
one of them executes *inside the hardened task container* (network off, non-root,
resource-limited). The host workspace is only synced back from the container after
the loop finishes, so solver-controlled code never runs on the host.

Budgets: a max iteration count, a wall-clock deadline, and per-tool output caps.
Token usage is accumulated across calls and reported in the run stats.
"""

import os
import posixpath
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol, cast

from task_bundle.container import WORKDIR, Docker
from task_bundle.errors import SolverError
from task_bundle.solver.base import SolveContext, SolveResult

DEFAULT_MODEL = "claude-opus-4-7"
MAX_TOKENS_PER_CALL = 8192
MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_FILE_READ_CHARS = 50_000
RUN_COMMAND_TIMEOUT = 300

# USD per million tokens (input, output); prefix-matched, best effort for cost stats.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}

SYSTEM_PROMPT = """\
You are an expert software engineer fixing an issue in a repository mounted at \
/workspace inside a sandboxed container (no network access).

Work method:
1. Explore the repository (list_dir, read_file) to understand the code.
2. Make the change by writing complete file contents with write_file.
3. Verify with run_command where possible (the repo's visible tests are available; \
the grading tests are hidden from you).
4. When you are confident in your fix, reply with a short summary and NO tool calls \
— that ends the session. Your edits to /workspace are the deliverable; do not output \
patches.

Be surgical: change only what the issue requires."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_dir",
        "description": "List a directory inside the repository (like ls -la).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path, '.' for root."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the repository.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the repository with the given full content.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the repository root (no network; bounded time/output)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


class MessagesClient(Protocol):
    """The slice of the anthropic client we use (substitutable in tests)."""

    def create(self, **kwargs: Any) -> Any: ...


def _safe_path(path: str) -> str:
    """Resolve a model-supplied path to a container path inside /workspace."""
    norm = posixpath.normpath(posixpath.join(WORKDIR, path.lstrip("/")))
    if norm != WORKDIR and not norm.startswith(WORKDIR + "/"):
        raise ValueError(f"path {path!r} escapes the repository root")
    return norm


class ClaudeSolver:
    """Agentic solver over the Claude API with hard iteration/time budgets."""

    name = "claude"
    model: str | None

    def __init__(
        self,
        model: str | None = None,
        max_iterations: int = 30,
        wall_clock_seconds: int = 1800,
        client: MessagesClient | None = None,
    ) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.max_iterations = max_iterations
        self.wall_clock_seconds = wall_clock_seconds
        self._client = client

    def _messages(self) -> MessagesClient:
        if self._client is not None:
            return self._client
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SolverError(
                "ANTHROPIC_API_KEY is not set. Export it (e.g. in ~/.zshrc) to use "
                "--solver claude, or use --solver stub for deterministic runs."
            )
        from anthropic import Anthropic  # imported lazily: only claude runs need it

        return cast(MessagesClient, Anthropic().messages)

    def solve(self, ctx: SolveContext) -> SolveResult:
        if ctx.docker is None or ctx.image_tag is None:
            raise SolverError("ClaudeSolver needs a docker handle and task image tag.")
        messages_api = self._messages()
        docker = ctx.docker
        deadline = time.monotonic() + self.wall_clock_seconds
        transcript: list[str] = []
        input_tokens = output_tokens = 0

        cid = docker.run_detached(ctx.image_tag)
        try:
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": f"Resolve this issue:\n\n{ctx.description}"}
            ]
            stop = "budget exhausted"
            for iteration in range(1, self.max_iterations + 1):
                if time.monotonic() > deadline:
                    stop = f"wall-clock budget ({self.wall_clock_seconds}s) exhausted"
                    break
                response = messages_api.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS_PER_CALL,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )
                input_tokens += response.usage.input_tokens
                output_tokens += response.usage.output_tokens
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        transcript.append(f"[{iteration}] assistant: {block.text.strip()}")
                if not tool_uses:
                    stop = f"model finished after {iteration} iteration(s)"
                    break
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for tool in tool_uses:
                    output = self._execute_tool(docker, cid, tool.name, tool.input)
                    transcript.append(
                        f"[{iteration}] {tool.name}({_summarize(tool.input)}) -> {output[:200]!r}"
                    )
                    results.append(
                        {"type": "tool_result", "tool_use_id": tool.id, "content": output}
                    )
                messages.append({"role": "user", "content": results})
            transcript.append(f"stopped: {stop}")
            self._sync_workspace_out(docker, cid, ctx.workspace)
        finally:
            docker.rm_force(cid)

        return SolveResult(
            transcript="\n".join(transcript),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_estimate_cost(self.model or DEFAULT_MODEL, input_tokens, output_tokens),
        )

    def _execute_tool(self, docker: Docker, cid: str, name: str, tool_input: dict[str, Any]) -> str:
        """Run one model-requested tool inside the container; errors become tool output."""
        try:
            if name == "list_dir":
                result = docker.exec(
                    cid, f"ls -la {shlex.quote(_safe_path(tool_input['path']))}", timeout=30
                )
                return result.output[:MAX_TOOL_OUTPUT_CHARS]
            if name == "read_file":
                result = docker.exec(
                    cid, f"cat {shlex.quote(_safe_path(tool_input['path']))}", timeout=30
                )
                return result.output[:MAX_FILE_READ_CHARS]
            if name == "write_file":
                return self._write_file(docker, cid, tool_input["path"], tool_input["content"])
            if name == "run_command":
                result = docker.exec(cid, str(tool_input["command"]), timeout=RUN_COMMAND_TIMEOUT)
                suffix = " (timed out)" if result.timed_out else f" (exit {result.exit_code})"
                return (result.output[:MAX_TOOL_OUTPUT_CHARS] or "(no output)") + suffix
            return f"error: unknown tool {name!r}"
        except (ValueError, KeyError) as e:
            return f"error: {e}"

    def _write_file(self, docker: Docker, cid: str, path: str, content: str) -> str:
        """Write via host temp file + docker cp (avoids any shell-quoting pitfalls)."""
        dest = _safe_path(path)
        parent = posixpath.dirname(dest)
        docker.exec(cid, f"mkdir -p {shlex.quote(parent)}", timeout=30, user="0")
        with tempfile.NamedTemporaryFile("w", suffix=".tmp", delete=False) as f:
            f.write(content)
            tmp = f.name
        try:
            docker.cp_in(cid, tmp, dest)
        finally:
            os.unlink(tmp)
        docker.exec(cid, f"chown 1000:1000 {shlex.quote(dest)}", timeout=30, user="0")
        return f"wrote {len(content)} chars to {dest.removeprefix(WORKDIR + '/')}"

    @staticmethod
    def _sync_workspace_out(docker: Docker, cid: str, workspace: Path) -> None:
        """Replace the host workspace (except the orchestrator .git) with the container tree."""
        with tempfile.TemporaryDirectory(prefix="task-bundle-out-") as tmp:
            out = Path(tmp) / "tree"
            out.mkdir()
            docker.cp_out(cid, f"{WORKDIR}/.", out)
            for entry in workspace.iterdir():
                if entry.name == ".git":
                    continue
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            for entry in out.iterdir():
                if entry.name == ".git":
                    continue  # paranoid: a container tree should never contain .git
                target = workspace / entry.name
                shutil.move(str(entry), target)


def _summarize(tool_input: dict[str, Any]) -> str:
    return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in tool_input.items() if k != "content") + (
        ", content=..." if "content" in tool_input else ""
    )


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    for prefix, (in_rate, out_rate) in _PRICES.items():
        if model.startswith(prefix):
            return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 6)
    return None
