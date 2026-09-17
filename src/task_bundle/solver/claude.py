"""ClaudeSolver: an agentic tool-use loop over the Claude API.

The model gets six tools — list_dir, read_file, search, edit_file, write_file,
run_command — and every one of them executes *inside the hardened task container*
(network off, non-root, resource-limited) that the orchestrator hands over as the
workspace. The model's edits land directly in that container's repo dir; the
orchestrator derives the changeset afterwards, so solver-controlled code never runs
on the host.

Cost and robustness:

- **Prompt caching.** The request is rendered tools -> system -> messages with a
  frozen tool list and system prompt, and top-level ``cache_control`` caches the
  growing conversation prefix, so each turn re-reads the previous turn's prefix at
  cache-read rates instead of paying for it again. Cache reads/writes are tracked
  and priced separately.
- **Adaptive thinking + effort.** On models that support it the request sends
  ``thinking: adaptive`` and ``output_config.effort`` (``--effort``), the primary
  cost/quality lever for agentic work.
- **Graceful failure.** API errors that survive the SDK's retries end the solve
  instead of crashing the run: the edits made so far are still graded. Refusals,
  output truncation, and a context budget are handled the same way.

Budgets: a max iteration count, a wall-clock deadline, a context-token budget, and
per-tool output caps. Every step is recorded in a structured trajectory (JSONL) with
the exact tool inputs and the exact outputs the model saw, alongside the human-
readable transcript.
"""

import os
import posixpath
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol, cast

import anthropic

from task_bundle.bundle import utc_now_iso
from task_bundle.container import SANDBOX_UID, Docker
from task_bundle.errors import SolverError
from task_bundle.solver.base import SolveContext, SolveResult

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS_PER_CALL = 16_000
MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_FILE_READ_CHARS = 50_000
MAX_SEARCH_LINES = 200
RUN_COMMAND_TIMEOUT = 300
API_MAX_RETRIES = 5
DEFAULT_CONTEXT_BUDGET_TOKENS = 400_000

# USD per million tokens (input, output), longest matching prefix wins. Cache writes
# cost 1.25x input and cache reads 0.1x input on every listed model.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.1

# Adaptive thinking + effort: Opus/Sonnet/Fable/Mythos 4.6 and later. Older models
# and Haiku take the legacy budget_tokens form, which we simply don't send.
_ADAPTIVE_MODEL = re.compile(r"^claude-(?:opus|sonnet|fable|mythos)-(\d+)(?:-(\d+))?")

SYSTEM_PROMPT = """\
You are an expert software engineer resolving an issue in a repository at {repo_dir} \
inside a sandboxed container. There is no network access. The repository's own \
visible tests are available; the grading tests are hidden from you.

Work method:
1. Locate the relevant code with search, list_dir, and read_file before changing \
anything. Understand the existing behavior and conventions.
2. Make targeted changes with edit_file (exact-match replace). Use write_file only \
for new files or full rewrites. Keep changes minimal and consistent with the codebase.
3. Verify with run_command: run the relevant existing tests, or a quick script that \
exercises the fix. Fix regressions you introduce.
4. When the fix is complete and verified, reply with a short summary and NO tool \
calls — that ends the session. Your edits in {repo_dir} are the deliverable; do not \
output patches or ask questions.

Complete the whole task in this session: you are working autonomously and nobody \
will answer follow-ups."""

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
        "description": "Read a file from the repository (truncated if very large).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": (
            "Search file contents with an extended regular expression (grep -rnE). "
            "Returns matching lines as path:line:text, at most 200 lines."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Extended regex to search for."},
                "path": {
                    "type": "string",
                    "description": "Relative file or directory to search; '.' for the whole repo.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace one exact occurrence of old_str in a file with new_str. old_str must "
            "match exactly once (include enough surrounding lines to make it unique); "
            "the tool refuses if it matches zero or several times."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string", "description": "Exact text to replace."},
                "new_str": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_str", "new_str"],
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


def _safe_path(path: str, repo_dir: str) -> str:
    """Resolve a model-supplied path to a container path inside ``repo_dir``.

    Relative paths are repo-rooted. An absolute path that already lies under the
    repo dir is accepted as-is (models frequently echo the absolute path they saw
    in ``run_command`` output); any other absolute path is treated as repo-rooted.
    Anything that normalizes to outside the repo dir is rejected.
    """
    candidate = posixpath.normpath(path)
    if candidate == repo_dir or candidate.startswith(repo_dir + "/"):
        norm = candidate
    else:
        norm = posixpath.normpath(posixpath.join(repo_dir, path.lstrip("/")))
    if norm != repo_dir and not norm.startswith(repo_dir + "/"):
        raise ValueError(f"path {path!r} escapes the repository root")
    return norm


def supports_adaptive_thinking(model: str) -> bool:
    """Whether ``model`` takes ``thinking: adaptive`` + ``output_config.effort``."""
    m = _ADAPTIVE_MODEL.match(model)
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    return major >= 5 or (major == 4 and minor >= 6)


def _credential_profile_dir() -> Path:
    return Path.home() / ".config" / "anthropic"


def client_kwargs() -> dict[str, Any]:
    """Constructor arguments for the SDK client, from the environment.

    An organization-level API key (one not created inside a workspace) is rejected
    by the API unless every request names a workspace via the
    ``anthropic-workspace-id`` header; ``ANTHROPIC_WORKSPACE_ID`` supplies it.
    Workspace-scoped keys need nothing extra.
    """
    kwargs: dict[str, Any] = {"max_retries": API_MAX_RETRIES}
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if workspace:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    return kwargs


class ClaudeSolver:
    """Agentic solver over the Claude API with hard iteration/time/context budgets."""

    name = "claude"
    model: str | None

    def __init__(
        self,
        model: str | None = None,
        max_iterations: int = 30,
        wall_clock_seconds: int = 1800,
        effort: str | None = None,
        context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
        client: MessagesClient | None = None,
    ) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.max_iterations = max_iterations
        self.wall_clock_seconds = wall_clock_seconds
        self.effort = effort or DEFAULT_EFFORT
        if self.effort not in EFFORT_LEVELS:
            raise SolverError(
                f"Unknown effort level {self.effort!r}; choose one of {', '.join(EFFORT_LEVELS)}."
            )
        self.context_budget_tokens = context_budget_tokens
        self._client = client

    @property
    def model_id(self) -> str:
        return self.model or DEFAULT_MODEL

    def _messages(self) -> MessagesClient:
        if self._client is not None:
            return self._client
        has_credentials = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or _credential_profile_dir().is_dir()
        )
        if not has_credentials:
            raise SolverError(
                "No Anthropic credentials found: set ANTHROPIC_API_KEY (or run `ant auth "
                "login`) to use --solver claude, or use --solver stub for deterministic runs."
            )
        # The SDK retries 429s, 5xx and connection errors with backoff on its own.
        return cast(MessagesClient, anthropic.Anthropic(**client_kwargs()).messages)

    def request_kwargs(self, repo_dir: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """The Messages API call, rendered for prompt-cache stability.

        Order is tools -> system -> messages; the tool list and system prompt never
        change within a solve, and the top-level ``cache_control`` caches the last
        cacheable block so each turn's request reuses the previous turn's prefix.
        """
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": MAX_TOKENS_PER_CALL,
            "system": [{"type": "text", "text": SYSTEM_PROMPT.format(repo_dir=repo_dir)}],
            "tools": TOOLS,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        if supports_adaptive_thinking(self.model_id):
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.effort}
        return kwargs

    def solve(self, ctx: SolveContext) -> SolveResult:
        messages_api = self._messages()
        docker, cid, repo_dir = ctx.docker, ctx.container_id, ctx.repo_dir
        deadline = time.monotonic() + self.wall_clock_seconds
        transcript: list[str] = []
        trajectory: list[dict[str, Any]] = [
            {
                "type": "start",
                "ts": utc_now_iso(),
                "model": self.model_id,
                "effort": self.effort if supports_adaptive_thinking(self.model_id) else None,
                "max_iterations": self.max_iterations,
                "wall_clock_seconds": self.wall_clock_seconds,
                "context_budget_tokens": self.context_budget_tokens,
                "repo_dir": repo_dir,
                "tools": [t["name"] for t in TOOLS],
                "description": ctx.description,
            }
        ]
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"Resolve this issue:\n\n{ctx.description}"}
        ]
        stop = f"iteration budget ({self.max_iterations}) exhausted"
        iterations = 0
        for iteration in range(1, self.max_iterations + 1):
            if time.monotonic() > deadline:
                stop = f"wall-clock budget ({self.wall_clock_seconds}s) exhausted"
                break
            iterations = iteration
            try:
                response = messages_api.create(**self.request_kwargs(repo_dir, messages))
            except anthropic.APIError as e:
                # The SDK already retried what is retryable; keep the edits made so far.
                stop = f"api error after retries: {type(e).__name__}: {e}"
                if usage["output"] == 0:
                    # Nothing was attempted: a billing/outage refusal must not be graded
                    # as a model failure. Fail the run so a fleet resume retries it.
                    raise SolverError(f"API refused before the model produced anything: {e}") from e
                transcript.append(f"[{iteration}] {stop}")
                trajectory.append(
                    {"step": iteration, "type": "error", "ts": utc_now_iso(), "error": stop}
                )
                break
            step_usage = _usage(response)
            for key in usage:
                usage[key] += step_usage[key]
            context_used = (
                step_usage["input"] + step_usage["cache_read"] + step_usage["cache_write"]
            )

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            texts = [
                b.text.strip()
                for b in response.content
                if getattr(b, "type", None) == "text" and b.text.strip()
            ]
            stop_reason = getattr(response, "stop_reason", None)
            for text in texts:
                transcript.append(f"[{iteration}] assistant: {text}")
            trajectory.append(
                {
                    "step": iteration,
                    "type": "assistant",
                    "ts": utc_now_iso(),
                    "text": "\n".join(texts),
                    "tool_calls": [
                        {"id": t.id, "name": t.name, "input": t.input} for t in tool_uses
                    ],
                    "stop_reason": stop_reason,
                    "usage": step_usage,
                    "context_tokens": context_used,
                }
            )
            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                stop = f"model refused (category: {category or 'unspecified'})"
                transcript.append(f"[{iteration}] {stop}")
                break
            if not tool_uses:
                stop = (
                    f"model finished after {iteration} iteration(s)"
                    if stop_reason != "max_tokens"
                    else f"output truncated at max_tokens after {iteration} iteration(s)"
                )
                break

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for tool in tool_uses:
                started = time.monotonic()
                output, is_error = self._execute_tool(docker, cid, repo_dir, tool.name, tool.input)
                transcript.append(
                    f"[{iteration}] {tool.name}({_summarize(tool.input)}) -> "
                    f"{'ERROR ' if is_error else ''}{output[:200]!r}"
                )
                trajectory.append(
                    {
                        "step": iteration,
                        "type": "tool_result",
                        "ts": utc_now_iso(),
                        "tool_use_id": tool.id,
                        "name": tool.name,
                        "output": output,
                        "is_error": is_error,
                        "duration_seconds": round(time.monotonic() - started, 3),
                    }
                )
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": tool.id,
                    "content": output,
                }
                if is_error:
                    result["is_error"] = True
                results.append(result)
            messages.append({"role": "user", "content": results})

            if context_used > self.context_budget_tokens:
                stop = (
                    f"context budget exhausted ({context_used} > "
                    f"{self.context_budget_tokens} tokens) after {iteration} iteration(s)"
                )
                transcript.append(f"[{iteration}] {stop}")
                break

        transcript.append(f"stopped: {stop}")
        cost = _estimate_cost(
            self.model_id,
            usage["input"],
            usage["output"],
            usage["cache_read"],
            usage["cache_write"],
        )
        trajectory.append(
            {
                "type": "end",
                "ts": utc_now_iso(),
                "reason": stop,
                "iterations": iterations,
                "usage": dict(usage),
                "cost_usd": cost,
            }
        )
        return SolveResult(
            transcript="\n".join(transcript),
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_write_tokens=usage["cache_write"],
            cost_usd=cost,
            stop_reason=stop,
            trajectory=trajectory,
            extra={"model": self.model_id, "effort": self.effort, "iterations": str(iterations)},
        )

    # -- tools ---------------------------------------------------------------------

    def _execute_tool(
        self, docker: Docker, cid: str, repo_dir: str, name: str, tool_input: dict[str, Any]
    ) -> tuple[str, bool]:
        """Run one model-requested tool inside the container -> (output, is_error)."""
        try:
            if name == "list_dir":
                path = _safe_path(tool_input["path"], repo_dir)
                result = docker.exec(cid, f"ls -la {shlex.quote(path)}", timeout=30)
                return result.output[:MAX_TOOL_OUTPUT_CHARS], not result.ok
            if name == "read_file":
                path = _safe_path(tool_input["path"], repo_dir)
                result = docker.exec(cid, f"cat {shlex.quote(path)}", timeout=30)
                if not result.ok:
                    return result.output[:MAX_TOOL_OUTPUT_CHARS], True
                text = result.output
                if len(text) > MAX_FILE_READ_CHARS:
                    text = (
                        text[:MAX_FILE_READ_CHARS]
                        + f"\n... [truncated: {len(result.output)} chars total; "
                        "use search to locate the region you need]"
                    )
                return text, False
            if name == "search":
                return self._search(docker, cid, repo_dir, tool_input)
            if name == "edit_file":
                return self._edit_file(
                    docker, cid, repo_dir, tool_input["path"], tool_input["old_str"],
                    tool_input["new_str"],
                )  # fmt: skip
            if name == "write_file":
                return (
                    self._write_file(
                        docker, cid, repo_dir, tool_input["path"], tool_input["content"]
                    ),
                    False,
                )
            if name == "run_command":
                result = docker.exec(cid, str(tool_input["command"]), timeout=RUN_COMMAND_TIMEOUT)
                suffix = " (timed out)" if result.timed_out else f" (exit {result.exit_code})"
                return (result.output[:MAX_TOOL_OUTPUT_CHARS] or "(no output)") + suffix, False
            return f"error: unknown tool {name!r}", True
        except (ValueError, KeyError, TypeError) as e:
            return f"error: {e}", True

    def _search(
        self, docker: Docker, cid: str, repo_dir: str, tool_input: dict[str, Any]
    ) -> tuple[str, bool]:
        pattern = str(tool_input["pattern"])
        path = _safe_path(str(tool_input.get("path") or "."), repo_dir)
        proc = docker.exec_argv(
            cid,
            ["grep", "-rnIE", "--exclude-dir=.git", "-e", pattern, "--", path],
            timeout=60,
            workdir=repo_dir,
        )
        text = proc.stdout.decode(errors="replace")
        if proc.returncode == 1 and not text:
            return "(no matches)", False
        if proc.returncode not in (0, 1):
            return proc.stderr.decode(errors="replace")[:MAX_TOOL_OUTPUT_CHARS] or "error", True
        prefix = repo_dir + "/"
        lines = [line.removeprefix(prefix) for line in text.splitlines()]
        if len(lines) > MAX_SEARCH_LINES:
            lines = [
                *lines[:MAX_SEARCH_LINES],
                f"... [{len(lines) - MAX_SEARCH_LINES} more matching lines; narrow the pattern]",
            ]
        return "\n".join(lines)[:MAX_TOOL_OUTPUT_CHARS], False

    def _edit_file(
        self, docker: Docker, cid: str, repo_dir: str, path: str, old: str, new: str
    ) -> tuple[str, bool]:
        """Exact-match single replacement; refuses ambiguous or missing matches."""
        dest = _safe_path(path, repo_dir)
        if not old:
            return "error: old_str must not be empty", True
        proc = docker.exec_argv(cid, ["cat", "--", dest], timeout=30, user="0")
        if proc.returncode != 0:
            return (
                f"error: cannot read {path}: {proc.stderr.decode(errors='replace').strip()}",
                True,
            )
        content = proc.stdout.decode("utf-8", errors="surrogateescape")
        count = content.count(old)
        if count == 0:
            return (
                f"error: old_str not found in {path}; read the file and copy the text exactly",
                True,
            )
        if count > 1:
            return (
                f"error: old_str matches {count} times in {path}; include more surrounding "
                "context so it is unique"
            ), True
        updated = content.replace(old, new, 1)
        self._put_file(docker, cid, dest, updated.encode("utf-8", errors="surrogateescape"))
        line = content[: content.index(old)].count("\n") + 1
        old_lines, new_lines = old.count("\n") + 1, new.count("\n") + 1
        return f"edited {path} at line {line} ({old_lines} -> {new_lines} lines)", False

    def _write_file(self, docker: Docker, cid: str, repo_dir: str, path: str, content: str) -> str:
        dest = _safe_path(path, repo_dir)
        parent = posixpath.dirname(dest)
        docker.exec(
            cid,
            f"mkdir -p {shlex.quote(parent)} && chown {SANDBOX_UID} {shlex.quote(parent)}",
            timeout=30,
            user="0",
        )
        self._put_file(docker, cid, dest, content.encode("utf-8", errors="surrogateescape"))
        return f"wrote {len(content)} chars to {dest.removeprefix(repo_dir + '/')}"

    @staticmethod
    def _put_file(docker: Docker, cid: str, dest: str, data: bytes) -> None:
        """Write via host temp file + docker cp (avoids any shell-quoting pitfalls)."""
        with tempfile.NamedTemporaryFile("wb", suffix=".tmp", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            docker.cp_in(cid, tmp, dest)
        finally:
            os.unlink(tmp)
        docker.exec(cid, f"chown {SANDBOX_UID} {shlex.quote(dest)}", timeout=30, user="0")


def _usage(response: Any) -> dict[str, int]:
    u = getattr(response, "usage", None)
    return {
        "input": int(getattr(u, "input_tokens", 0) or 0),
        "output": int(getattr(u, "output_tokens", 0) or 0),
        "cache_read": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "cache_write": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    }


def _summarize(tool_input: dict[str, Any]) -> str:
    hidden = {"content", "new_str", "old_str"}
    shown = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in tool_input.items() if k not in hidden)
    elided = ", ".join(f"{k}=..." for k in tool_input if k in hidden)
    return ", ".join(part for part in (shown, elided) if part)


def _estimate_cost(
    model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> float | None:
    for prefix in sorted(_PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            in_rate, out_rate = _PRICES[prefix]
            usd = (
                input_tokens * in_rate
                + output_tokens * out_rate
                + cache_read * in_rate * _CACHE_READ_MULTIPLIER
                + cache_write * in_rate * _CACHE_WRITE_MULTIPLIER
            ) / 1_000_000
            return round(usd, 6)
    return None
