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


class TestListInstances:
    """Page scan with filters; the HTTP layer is stubbed with two synthetic pages."""

    @pytest.fixture()
    def pages(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        import task_bundle.swebench as sw

        def row(iid: str, repo: str, lang: str, sha: str) -> dict:
            return {
                "row": {
                    **ROW,
                    "instance_id": iid,
                    "repo": repo,
                    "repo_language": lang,
                    "base_commit": sha,
                }
            }

        page1 = [
            row("i1", "ansible/ansible", "python", "a" * 40),
            row("i2", "future-architect/vuls", "go", "b" * 40),
        ]
        page2 = [row("i3", "internetarchive/openlibrary", "python", "c" * 40)]
        calls: list[str] = []

        def fake_http(url: str) -> dict:
            calls.append(url)
            if "offset=0" in url:
                return {"rows": page1}
            if f"offset={sw.PAGE}" in url:
                return {"rows": page2}
            return {"rows": []}

        monkeypatch.setattr(sw, "_http_json", fake_http)
        return calls

    def test_filters_by_repo_and_language(self, pages: list[str]) -> None:
        from task_bundle.swebench import list_instances

        assert [r["instance_id"] for r in list_instances()] == ["i1", "i2", "i3"]
        assert [r["instance_id"] for r in list_instances(language="python")] == ["i1", "i3"]
        assert [r["instance_id"] for r in list_instances(repo="future-architect/vuls")] == ["i2"]
        assert list_instances(repo="nope/nope") == []

    def test_limit_stops_scanning_early(self, pages: list[str]) -> None:
        from task_bundle.swebench import list_instances

        pages.clear()
        assert [r["instance_id"] for r in list_instances(limit=1)] == ["i1"]
        assert len(pages) == 1  # never fetched the second page


class TestBundleName:
    def test_repo_and_short_sha(self) -> None:
        from task_bundle.swebench import bundle_name, test_counts

        assert bundle_name(ROW) == f"ansible-{ROW['base_commit'][:12]}"
        f2p, p2p = test_counts(ROW)
        assert (f2p, p2p) == (1, 15)


class TestBulkImport:
    """Bulk mode: per-instance outcomes are recorded and a re-run resumes."""

    @pytest.fixture()
    def three_rows(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        import task_bundle.cli as cli

        rows = []
        for i, sha in enumerate(("1" * 40, "2" * 40, "3" * 40), 1):
            rows.append({**ROW, "instance_id": f"inst-{i}", "base_commit": sha})
        monkeypatch.setattr(cli, "list_instances", lambda **kw: rows)
        monkeypatch.setattr(
            cli, "fetch_instance", lambda iid: next(r for r in rows if r["instance_id"] == iid)
        )
        return rows

    def test_outcomes_and_resume(
        self, tmp_path: Path, isolated_db: Path, monkeypatch: pytest.MonkeyPatch, three_rows: list
    ) -> None:
        import task_bundle.cli as cli
        from task_bundle.errors import ContractViolation, DockerError

        inits: list[Path] = []
        monkeypatch.setattr(cli, "init", lambda p: inits.append(p))

        def fake_verify(p: Path) -> None:
            if p.name.endswith("2" * 12):
                raise ContractViolation("golden patch does not resolve task")
            if p.name.endswith("3" * 12):
                raise DockerError("Image build failed")

        monkeypatch.setattr(cli, "verify_gold", fake_verify)
        dest = tmp_path / "bundles"
        result = runner.invoke(
            app, ["import-swebench", "--repo", "ansible/ansible", "--dest", str(dest)]
        )
        assert result.exit_code == 0, result.output
        summary = json.loads((dest / "import_summary.json").read_text())
        assert {k: v["status"] for k, v in summary.items()} == {
            "inst-1": "gradeable",
            "inst-2": "refused",
            "inst-3": "error",
        }
        assert "does not resolve" in summary["inst-2"]["reason"]
        assert "DockerError" in summary["inst-3"]["reason"]
        assert (dest / f"ansible-{'1' * 12}" / "task.json").is_file()
        assert len(inits) == 3
        assert "1 bundle(s) ready" in " ".join(result.output.split())

        # Re-run: the gradeable one is skipped, the other two are retried.
        inits.clear()
        result = runner.invoke(
            app, ["import-swebench", "--repo", "ansible/ansible", "--dest", str(dest)]
        )
        assert result.exit_code == 0, result.output
        summary = json.loads((dest / "import_summary.json").read_text())
        assert summary["inst-1"]["status"] == "skipped"
        assert [p.name[-12:] for p in inits] == ["2" * 12, "3" * 12]

    def test_list_only_prints_table_without_importing(
        self, tmp_path: Path, isolated_db: Path, three_rows: list
    ) -> None:
        result = runner.invoke(
            app,
            ["import-swebench", "--language", "python", "--list", "--dest", str(tmp_path / "b")],
        )
        assert result.exit_code == 0, result.output
        assert "3 matching instance(s)" in result.output
        assert not (tmp_path / "b").exists()

    def test_id_and_bulk_flags_are_exclusive(self, isolated_db: Path) -> None:
        result = runner.invoke(app, ["import-swebench", "some-id", "--repo", "x/y"])
        assert result.exit_code != 0
        assert "drop the id" in str(result.exception)
        result = runner.invoke(app, ["import-swebench"])
        assert result.exit_code != 0
        assert "bulk import" in str(result.exception)
