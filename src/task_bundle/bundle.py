"""Bundle spec: pydantic models for task.json plus load/scaffold/state helpers.

A bundle is a self-contained directory describing one task (see DESIGN.md §2):

    my-task/
      task.json            machine-readable metadata (validated here)
      description.md       problem statement shown to the solver
      patch.diff           golden patch (optional; used by `task verify-gold`)
      tests/fail2pass/     hidden: must FAIL on baseline, PASS after golden patch
      tests/pass2pass/     hidden: must PASS on baseline and after golden patch
      .task/               tool-managed state (workspace clone, state.json)

Hidden tests may instead be expressed SWE-bench style as a test patch plus
explicit test identifiers (``tests.test_patch`` + ``fail2pass_ids``/``pass2pass_ids``);
exactly one of the two formats must be in use by validate/run time.
"""

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from task_bundle.errors import BundleError

TASK_JSON = "task.json"
DESCRIPTION_MD = "description.md"
GOLD_PATCH = "patch.diff"
FAIL2PASS_DIR = Path("tests/fail2pass")
PASS2PASS_DIR = Path("tests/pass2pass")
STATE_DIR = Path(".task")

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoSpec(_StrictModel):
    """Where the task's repository lives and the exact commit it is pinned to."""

    url: str
    commit: str

    @field_validator("commit")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        if not _FULL_SHA.match(v):
            raise ValueError(
                f"commit must be a full 40-character lowercase SHA (got {v!r}); "
                "short SHAs can drift as history grows"
            )
        return v


class EnvironmentSpec(_StrictModel):
    """How to build the task image: base image, setup commands, env allowlist."""

    base_image: str
    setup_commands: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    network_during_setup: bool = True


class TestsSpec(_StrictModel):
    """How tests are executed and which hidden tests gate the task.

    ``command_template`` is pure data: the engine substitutes ``{test_path}`` with a
    staged test path (native format) or a test identifier (SWE-bench format) and runs
    the result in the container. Pass/fail comes from the exit code, so any language
    or framework works.
    """

    command_template: str
    staging_dir: str = "tests/"
    timeout_seconds: int = Field(default=300, gt=0)
    visible_test_paths: list[str] = Field(default_factory=list)
    # SWE-bench compatibility format (mutually exclusive with the tests/ directories):
    test_patch: str | None = None
    fail2pass_ids: list[str] = Field(default_factory=list)
    pass2pass_ids: list[str] = Field(default_factory=list)

    @field_validator("command_template")
    @classmethod
    def _has_placeholder(cls, v: str) -> str:
        if "{test_path}" not in v:
            raise ValueError("command_template must contain the {test_path} placeholder")
        return v


class SolverSpec(_StrictModel):
    """Solver-workspace shaping beyond the always-stripped hidden tests and .git."""

    workspace_excludes: list[str] = Field(default_factory=list)


class TaskSpec(_StrictModel):
    """Top-level schema for task.json."""

    schema_version: Literal[1] = 1
    id: str | None = None
    repo: RepoSpec
    language: str = "python"
    environment: EnvironmentSpec
    tests: TestsSpec
    solver: SolverSpec = Field(default_factory=SolverSpec)


class HiddenTestFormat(StrEnum):
    """Which of the two hidden-test representations a bundle uses."""

    DIRECTORIES = "directories"
    TEST_PATCH = "test_patch"


class BundleState(BaseModel):
    """Tool-managed lifecycle state persisted in .task/state.json."""

    status: Literal["scaffolded", "initialized", "validated"] = "scaffolded"
    image_tag: str | None = None
    image_digest: str | None = None
    initialized_at: str | None = None
    validated_at: str | None = None


class Bundle:
    """A task bundle on disk: validated spec + filesystem layout + state."""

    def __init__(self, path: Path, spec: TaskSpec) -> None:
        self.path = path.resolve()
        self.spec = spec
        if self.spec.id is None:
            self.spec.id = self.path.name

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load and validate an existing bundle, raising BundleError with context."""
        task_json = path / TASK_JSON
        if not task_json.is_file():
            raise BundleError(
                f"No {TASK_JSON} found in {path}. "
                f"Scaffold one with: task init {path} --repo <url> --commit <sha>"
            )
        try:
            raw = json.loads(task_json.read_text())
        except json.JSONDecodeError as e:
            raise BundleError(f"{task_json} is not valid JSON: {e}") from e
        try:
            spec = TaskSpec.model_validate(raw)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            raise BundleError(f"{task_json} failed schema validation: {problems}") from e
        return cls(path, spec)

    @classmethod
    def scaffold(
        cls,
        path: Path,
        repo_url: str,
        commit: str,
        base_image: str,
        test_command: str,
        setup_commands: list[str] | None = None,
    ) -> Self:
        """Create the bundle skeleton on disk and return the loaded Bundle."""
        if (path / TASK_JSON).exists():
            raise BundleError(f"{path / TASK_JSON} already exists; refusing to overwrite.")
        try:
            spec = TaskSpec(
                id=path.name,
                repo=RepoSpec(url=repo_url, commit=commit),
                environment=EnvironmentSpec(
                    base_image=base_image, setup_commands=setup_commands or []
                ),
                tests=TestsSpec(command_template=test_command),
            )
        except ValidationError as e:
            problems = "; ".join(err["msg"] for err in e.errors())
            raise BundleError(f"Invalid bundle options: {problems}") from e
        path.mkdir(parents=True, exist_ok=True)
        (path / FAIL2PASS_DIR).mkdir(parents=True, exist_ok=True)
        (path / PASS2PASS_DIR).mkdir(parents=True, exist_ok=True)
        (path / TASK_JSON).write_text(
            json.dumps(spec.model_dump(), indent=2, sort_keys=True) + "\n"
        )
        desc = path / DESCRIPTION_MD
        if not desc.exists():
            desc.write_text("# Problem statement\n\nDescribe the task for the solver here.\n")
        state_dir = path / STATE_DIR
        state_dir.mkdir(exist_ok=True)
        (state_dir / ".gitignore").write_text("*\n")
        bundle = cls(path, spec)
        bundle.save_state(BundleState())
        return bundle

    # -- filesystem layout -----------------------------------------------------

    @property
    def workspace_dir(self) -> Path:
        """Baseline clone of the repo at the pinned commit (orchestrator-side only)."""
        return self.path / STATE_DIR / "workspace"

    @property
    def description_path(self) -> Path:
        return self.path / DESCRIPTION_MD

    @property
    def gold_patch_path(self) -> Path:
        return self.path / GOLD_PATCH

    @property
    def test_patch_path(self) -> Path:
        """Location of the SWE-bench-style test patch (TEST_PATCH format only)."""
        return self.path / (self.spec.tests.test_patch or "tests/test_patch.diff")

    @property
    def fail2pass_dir(self) -> Path:
        return self.path / FAIL2PASS_DIR

    @property
    def pass2pass_dir(self) -> Path:
        return self.path / PASS2PASS_DIR

    def hidden_test_files(self, bucket: Literal["fail2pass", "pass2pass"]) -> list[Path]:
        """All files in a hidden bucket, sorted for deterministic ordering."""
        root = self.fail2pass_dir if bucket == "fail2pass" else self.pass2pass_dir
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".gitkeep")

    def test_format(self) -> HiddenTestFormat:
        """Determine which hidden-test format this bundle uses; raise if ambiguous."""
        has_dirs = bool(self.hidden_test_files("fail2pass") or self.hidden_test_files("pass2pass"))
        has_patch = self.spec.tests.test_patch is not None
        if has_dirs and has_patch:
            raise BundleError(
                f"Bundle {self.spec.id} defines hidden tests both as tests/ directories and "
                "as tests.test_patch in task.json; use exactly one format."
            )
        if has_patch:
            return HiddenTestFormat.TEST_PATCH
        if has_dirs:
            return HiddenTestFormat.DIRECTORIES
        raise BundleError(
            f"Bundle {self.spec.id} has no hidden tests: add files under tests/fail2pass/ "
            "(and optionally tests/pass2pass/), or set tests.test_patch in task.json."
        )

    # -- state -----------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.path / STATE_DIR / "state.json"

    def load_state(self) -> BundleState:
        if not self._state_path.is_file():
            return BundleState()
        return BundleState.model_validate_json(self._state_path.read_text())

    def save_state(self, state: BundleState) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(state.model_dump_json(indent=2) + "\n")


def utc_now_iso() -> str:
    """UTC timestamp in ISO-8601, second precision — used everywhere for determinism."""
    return datetime.now(UTC).isoformat(timespec="seconds")
