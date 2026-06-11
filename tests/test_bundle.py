"""Schema validation and bundle layout tests for bundle.py."""

import json
from pathlib import Path

import pytest

from task_bundle.bundle import Bundle, HiddenTestFormat, TaskSpec
from task_bundle.errors import BundleError

SHA = "a" * 40


def valid_spec_dict() -> dict:
    return {
        "schema_version": 1,
        "repo": {"url": "https://github.com/org/repo", "commit": SHA},
        "environment": {"base_image": "python:3.11-slim"},
        "tests": {"command_template": "python -m pytest {test_path} -q"},
    }


class TestTaskSpec:
    def test_valid_spec_parses(self) -> None:
        spec = TaskSpec.model_validate(valid_spec_dict())
        assert spec.repo.commit == SHA
        assert spec.tests.timeout_seconds == 300

    def test_short_sha_rejected(self) -> None:
        d = valid_spec_dict()
        d["repo"]["commit"] = "abc123"
        with pytest.raises(ValueError, match="40-character"):
            TaskSpec.model_validate(d)

    def test_uppercase_sha_rejected(self) -> None:
        d = valid_spec_dict()
        d["repo"]["commit"] = "A" * 40
        with pytest.raises(ValueError, match="40-character"):
            TaskSpec.model_validate(d)

    def test_command_template_requires_placeholder(self) -> None:
        d = valid_spec_dict()
        d["tests"]["command_template"] = "pytest -q"
        with pytest.raises(ValueError, match="test_path"):
            TaskSpec.model_validate(d)

    def test_unknown_keys_rejected(self) -> None:
        d = valid_spec_dict()
        d["surprise"] = True
        with pytest.raises(ValueError):
            TaskSpec.model_validate(d)

    def test_nonpositive_timeout_rejected(self) -> None:
        d = valid_spec_dict()
        d["tests"]["timeout_seconds"] = 0
        with pytest.raises(ValueError):
            TaskSpec.model_validate(d)


class TestBundleLoad:
    def test_missing_task_json_gives_scaffold_hint(self, tmp_path: Path) -> None:
        with pytest.raises(BundleError, match="task init"):
            Bundle.load(tmp_path)

    def test_invalid_json_reported(self, tmp_path: Path) -> None:
        (tmp_path / "task.json").write_text("{not json")
        with pytest.raises(BundleError, match="not valid JSON"):
            Bundle.load(tmp_path)

    def test_schema_errors_name_the_field(self, tmp_path: Path) -> None:
        d = valid_spec_dict()
        d["repo"]["commit"] = "tooshort"
        (tmp_path / "task.json").write_text(json.dumps(d))
        with pytest.raises(BundleError, match=r"repo\.commit"):
            Bundle.load(tmp_path)


class TestScaffold:
    def test_scaffold_creates_layout_and_roundtrips(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "my-task"
        Bundle.scaffold(
            bundle_dir,
            repo_url="https://github.com/org/repo",
            commit=SHA,
            base_image="python:3.11-slim",
            test_command="python -m pytest {test_path} -q",
        )
        assert (bundle_dir / "task.json").is_file()
        assert (bundle_dir / "description.md").is_file()
        assert (bundle_dir / "tests/fail2pass").is_dir()
        assert (bundle_dir / "tests/pass2pass").is_dir()
        assert (bundle_dir / ".task/.gitignore").read_text() == "*\n"
        loaded = Bundle.load(bundle_dir)
        assert loaded.spec.id == "my-task"
        assert loaded.load_state().status == "scaffolded"

    def test_scaffold_refuses_overwrite(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "t"
        for attempt in range(2):
            if attempt == 0:
                Bundle.scaffold(bundle_dir, "u", SHA, "img", "x {test_path}")
            else:
                with pytest.raises(BundleError, match="already exists"):
                    Bundle.scaffold(bundle_dir, "u", SHA, "img", "x {test_path}")

    def test_task_json_is_deterministic(self, tmp_path: Path) -> None:
        Bundle.scaffold(tmp_path / "a", "u", SHA, "img", "x {test_path}")
        Bundle.scaffold(tmp_path / "b", "u", SHA, "img", "x {test_path}")
        a = (tmp_path / "a/task.json").read_text()
        b = (tmp_path / "b/task.json").read_text()
        assert a == b.replace('"b"', '"a"')  # only the id differs
        assert json.loads(a) == json.loads(json.dumps(json.loads(a), sort_keys=True))


class TestHiddenTestFormat:
    def _bundle(self, tmp_path: Path) -> Bundle:
        return Bundle.scaffold(tmp_path / "t", "u", SHA, "img", "x {test_path}")

    def test_directories_format(self, tmp_path: Path) -> None:
        b = self._bundle(tmp_path)
        (b.fail2pass_dir / "test_fix.py").write_text("def test_fix(): pass\n")
        assert b.test_format() is HiddenTestFormat.DIRECTORIES
        assert b.hidden_test_files("fail2pass") == [b.fail2pass_dir / "test_fix.py"]

    def test_patch_format(self, tmp_path: Path) -> None:
        b = self._bundle(tmp_path)
        b.spec.tests.test_patch = "tests/test_patch.diff"
        b.spec.tests.fail2pass_ids = ["tests/test_x.py::test_fix"]
        assert b.test_format() is HiddenTestFormat.TEST_PATCH

    def test_both_formats_rejected(self, tmp_path: Path) -> None:
        b = self._bundle(tmp_path)
        (b.fail2pass_dir / "test_fix.py").write_text("def test_fix(): pass\n")
        b.spec.tests.test_patch = "tests/test_patch.diff"
        with pytest.raises(BundleError, match="exactly one format"):
            b.test_format()

    def test_no_hidden_tests_rejected(self, tmp_path: Path) -> None:
        b = self._bundle(tmp_path)
        with pytest.raises(BundleError, match="no hidden tests"):
            b.test_format()
