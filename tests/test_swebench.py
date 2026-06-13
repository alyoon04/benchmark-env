"""Converter tests (fixture row, no network) and a TEST_PATCH-format docker test."""

import json
from pathlib import Path

import pytest
from conftest import FIXTURES, P2P_TEST, make_initialized_bundle
from typer.testing import CliRunner

from task_bundle.bundle import Bundle, HiddenTestFormat
from task_bundle.cli import app
from task_bundle.errors import TaskError
from task_bundle.swebench import convert_instance
from task_bundle.workspace import patch_changed_paths

runner = CliRunner()

ROW = json.loads((FIXTURES / "swebench_row.json").read_text())

# Adds a hidden test for the toy repo's buggy divide() as a *new file via patch*.
TOY_TEST_PATCH = """\
diff --git a/tests/test_division.py b/tests/test_division.py
new file mode 100644
--- /dev/null
+++ b/tests/test_division.py
@@ -0,0 +1,10 @@
+import sys
+from pathlib import Path
+
+sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
+
+from calc import divide
+
+
+def test_divide_fixed():
+    assert divide(6, 3) == 2
"""


class TestConvertInstance:
    def test_bundle_layout_and_spec(self, tmp_path: Path) -> None:
        dest = tmp_path / "ansible-task"
        bundle = convert_instance(ROW, dest)
        assert bundle.test_format() is HiddenTestFormat.TEST_PATCH
        assert bundle.spec.repo.url == "https://github.com/ansible/ansible"
        assert bundle.spec.repo.commit == ROW["base_commit"]
        assert bundle.spec.environment.base_image.startswith("jefzda/sweap-images:")
        assert bundle.spec.environment.setup_commands == []
        assert bundle.spec.environment.env == {"HOME": "/tmp"}
        assert bundle.spec.tests.fail2pass_ids == [
            "test/units/utils/test_vars.py::TestVariableUtils::test_combine_vars_replace"
        ]
        assert len(bundle.spec.tests.pass2pass_ids) > 0
        assert (dest / "tests/test_patch.diff").read_text() == ROW["test_patch"]
        assert (dest / "patch.diff").read_text() == ROW["patch"]
        desc = (dest / "description.md").read_text()
        assert ROW["instance_id"] in desc
        assert "## Requirements" in desc
        # round-trips through the normal loader
        loaded = Bundle.load(dest)
        assert loaded.spec.tests.test_patch == "tests/test_patch.diff"

    def test_refuses_existing_bundle(self, tmp_path: Path) -> None:
        convert_instance(ROW, tmp_path / "b")
        with pytest.raises(TaskError, match="already contains"):
            convert_instance(ROW, tmp_path / "b")

    def test_unknown_language_needs_explicit_command(self, tmp_path: Path) -> None:
        row = dict(ROW, repo_language="cobol")
        with pytest.raises(TaskError, match="--test-command"):
            convert_instance(row, tmp_path / "b")
        bundle = convert_instance(row, tmp_path / "b2", test_command="run-cobol-test {test_path}")
        assert bundle.spec.tests.command_template == "run-cobol-test {test_path}"


class TestImportGuard:
    def test_no_init_points_to_verify_gold(
        self, tmp_path: Path, isolated_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-init prints guidance that includes the verify-gold solvability check."""
        import task_bundle.cli as cli

        monkeypatch.setattr(cli, "fetch_instance", lambda _id: ROW)
        dest = tmp_path / "ansible-task"
        result = runner.invoke(
            app, ["import-swebench", "ignored-id", "--dest", str(dest), "--no-init"]
        )
        assert result.exit_code == 0, result.output
        assert "verify-gold" in result.output
        assert (dest / "task.json").exists()


class TestPatchChangedPaths:
    def test_modified_and_new_files_listed(self, tmp_path: Path) -> None:
        patch = tmp_path / "tp.diff"
        patch.write_text(TOY_TEST_PATCH)
        assert patch_changed_paths(patch) == ["tests/test_division.py"]

    def test_real_instance_patch(self, tmp_path: Path) -> None:
        patch = tmp_path / "tp.diff"
        patch.write_text(ROW["test_patch"])
        assert patch_changed_paths(patch) == ["test/units/utils/test_vars.py"]


@pytest.mark.docker
def test_test_patch_format_end_to_end(
    tmp_path: Path, shared_origin: tuple[str, str], isolated_db: Path
) -> None:
    """TEST_PATCH bundles validate and grade correctly (toy repo, no network)."""
    bundle = make_initialized_bundle(
        tmp_path / "tp-bundle", shared_origin, f2p=P2P_TEST, p2p=P2P_TEST
    )
    # convert it to the TEST_PATCH format: drop the directory tests, add patch + ids
    (bundle.fail2pass_dir / "test_f2p.py").unlink()
    (bundle.pass2pass_dir / "test_p2p.py").unlink()
    bundle.test_patch_path.write_text(TOY_TEST_PATCH)
    bundle.spec.tests.test_patch = "tests/test_patch.diff"
    bundle.spec.tests.fail2pass_ids = ["tests/test_division.py::test_divide_fixed"]
    bundle.spec.tests.pass2pass_ids = ["tests/test_visible.py::test_add_visible"]
    (bundle.path / "task.json").write_text(
        json.dumps(bundle.spec.model_dump(), indent=2, sort_keys=True) + "\n"
    )
    gold = Path(__file__).parents[1] / "examples/toy-calc/patch.diff"

    result = runner.invoke(app, ["validate", str(bundle.path), "--attempts", "1"])
    assert result.exit_code == 0, result.output
    assert "contract holds" in " ".join(result.output.split())

    result = runner.invoke(app, ["run", str(bundle.path), "--patch", str(gold)])
    assert result.exit_code == 0, result.output
    assert "RESOLVED" in result.output
    out = " ".join(result.output.split())
    assert "test_divide_fixed" in out
