"""Unit tests for the in-place changeset protocol's pure pieces (no docker needed).

Manifest parsing, changeset computation with .gitignore filtering, sparse patch
materialization, and unified-diff rendering are all host-side functions over plain
directories, so the whole solve -> changeset -> diff chain is table-testable here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import FIXTURES

from task_bundle.errors import GitError
from task_bundle.workspace import (
    Changeset,
    apply_patch,
    compute_changeset,
    ignored_paths,
    materialize_patch,
    parse_manifest,
    render_diff,
    tree_manifest,
)

GOLD_PATCH = Path(__file__).parents[1] / "examples/toy-calc/patch.diff"


class TestParseManifest:
    def test_plain_lines(self) -> None:
        text = "aa  ./calc.py\nbb  ./tests/test_x.py\n\n"
        assert parse_manifest(text) == {"calc.py": "aa", "tests/test_x.py": "bb"}

    def test_coreutils_escaped_names(self) -> None:
        # coreutils prefixes the line with a backslash and escapes \n and \ in names
        text = "\\cc  ./odd\\nname.txt\n\\dd  ./back\\\\slash\n"
        assert parse_manifest(text) == {"odd\nname.txt": "cc", "back\\slash": "dd"}

    def test_matches_host_manifest(self, tmp_path: Path) -> None:
        tree = tmp_path / "t"
        shutil.copytree(FIXTURES / "toy_repo", tree)
        proc = subprocess.run(
            ["sh", "-c", "find . -type f -exec shasum -a 256 {} +"],
            cwd=tree,
            capture_output=True,
            text=True,
            check=True,
        )
        assert parse_manifest(proc.stdout) == tree_manifest(tree)


class TestComputeChangeset:
    def test_buckets(self) -> None:
        before = {"a": "1", "b": "2", "c": "3"}
        after = {"a": "1", "b": "X", "d": "4"}
        cs = compute_changeset(before, after)
        assert cs == Changeset(modified=("b",), added=("d",), deleted=("c",))
        assert cs.present == ["b", "d"]
        assert cs.existed == ["b", "c"]
        assert cs.all_paths == ["b", "c", "d"]
        assert cs

    def test_identical_manifests_are_empty(self) -> None:
        cs = compute_changeset({"a": "1"}, {"a": "1"})
        assert not cs
        assert cs.all_paths == []

    def test_ignored_paths_dropped_from_every_bucket(self) -> None:
        before = {"keep": "1", "__pycache__/x.pyc": "1", "gone.pyc": "1"}
        after = {"keep": "2", "__pycache__/x.pyc": "9", "new.pyc": "1"}
        cs = compute_changeset(before, after, ignored={"__pycache__/x.pyc", "gone.pyc", "new.pyc"})
        assert cs == Changeset(modified=("keep",))


class TestIgnoredPaths:
    @pytest.fixture()
    def repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text("node_modules/\n*.log\n")
        (repo / "tracked.log").write_text("tracked despite the pattern\n")
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@x", "add", "-f", "tracked.log"],
            cwd=repo,
            check=True,
        )
        return repo

    def test_repo_rules_and_defaults(self, repo: Path) -> None:
        paths = [
            "src/app.py",
            "node_modules/left-pad/index.js",
            "debug.log",
            "tracked.log",
            "pkg/__pycache__/mod.cpython-311.pyc",
            "notes.pyc",
        ]
        ignored = ignored_paths(repo, paths)
        assert ignored == {
            "node_modules/left-pad/index.js",
            "debug.log",
            "pkg/__pycache__/mod.cpython-311.pyc",
            "notes.pyc",
        }
        assert "tracked.log" not in ignored  # tracked files always count

    def test_no_repo_means_nothing_ignored(self, tmp_path: Path) -> None:
        assert ignored_paths(tmp_path, ["a.pyc"]) == set()

    def test_empty_input(self, repo: Path) -> None:
        assert ignored_paths(repo, []) == set()


class TestMaterializePatch:
    def test_sparse_tree_holds_only_touched_files(self, tmp_path: Path) -> None:
        baseline = tmp_path / "base"
        shutil.copytree(FIXTURES / "toy_repo", baseline)
        with materialize_patch(baseline, GOLD_PATCH) as (tree, paths):
            assert paths == ["calc.py"]
            assert sorted(p.name for p in tree.rglob("*") if p.is_file()) == ["calc.py"]
            assert "return a / b" in (tree / "calc.py").read_text()
        assert not tree.exists()  # temporary
        assert "return a * b" in (baseline / "calc.py").read_text()  # baseline untouched

    def test_deleted_file_is_absent_from_tree(self, tmp_path: Path) -> None:
        baseline = tmp_path / "base"
        shutil.copytree(FIXTURES / "toy_repo", baseline)
        (baseline / "OBSOLETE.txt").write_text("remove me\n")
        patch = tmp_path / "rm.diff"
        patch.write_text(
            "diff --git a/OBSOLETE.txt b/OBSOLETE.txt\ndeleted file mode 100644\n"
            "--- a/OBSOLETE.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-remove me\n"
        )
        with materialize_patch(baseline, patch) as (tree, paths):
            assert paths == ["OBSOLETE.txt"]
            assert not (tree / "OBSOLETE.txt").exists()

    def test_rename_stages_its_source(self, tmp_path: Path) -> None:
        """`git apply --numstat` names only a rename's destination; the source still has
        to be copied in for the apply to work, and reported so replays delete it."""
        baseline = tmp_path / "base"
        shutil.copytree(FIXTURES / "toy_repo", baseline)
        patch = tmp_path / "rename.diff"
        patch.write_text(
            "diff --git a/calc.py b/calculator.py\nsimilarity index 100%\n"
            "rename from calc.py\nrename to calculator.py\n"
        )
        with materialize_patch(baseline, patch) as (tree, paths):
            assert sorted(paths) == ["calc.py", "calculator.py"]
            assert not (tree / "calc.py").exists()  # replayed as a deletion
            assert "return a * b" in (tree / "calculator.py").read_text()

    def test_nonapplying_patch_is_actionable(self, tmp_path: Path) -> None:
        baseline = tmp_path / "base"
        shutil.copytree(FIXTURES / "toy_repo", baseline)
        bad = tmp_path / "bad.diff"
        bad.write_text("--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-x\n+y\n")
        with (
            pytest.raises(GitError, match="does not apply cleanly"),
            materialize_patch(baseline, bad),
        ):
            pass


class TestRenderDiff:
    def test_modified_added_deleted_in_git_form(self, tmp_path: Path) -> None:
        before, after = tmp_path / "before", tmp_path / "after"
        (before / "pkg").mkdir(parents=True)
        (after / "pkg").mkdir(parents=True)
        (before / "pkg/mod.py").write_text("x = 1\n")
        (after / "pkg/mod.py").write_text("x = 2\n")
        (before / "old.txt").write_text("gone\n")
        (after / "new.txt").write_text("fresh\n")
        diff = render_diff(before, after)
        assert "diff --git a/pkg/mod.py b/pkg/mod.py" in diff
        assert "-x = 1\n+x = 2" in diff
        assert "--- a/old.txt\n+++ /dev/null" in diff
        assert "--- /dev/null\n+++ b/new.txt" in diff

    def test_output_applies_with_git_apply(self, tmp_path: Path) -> None:
        """The rendered diff is a real patch: applying it to `before` yields `after`."""
        before, after = tmp_path / "before", tmp_path / "after"
        shutil.copytree(FIXTURES / "toy_repo", before)
        shutil.copytree(FIXTURES / "toy_repo", after)
        apply_patch(after, GOLD_PATCH)
        (after / "NEW.md").write_text("added by solver\n")
        (after / "README.md").unlink()
        diff = render_diff(before, after)
        patch = tmp_path / "solver.diff"
        patch.write_text(diff)
        target = tmp_path / "target"
        shutil.copytree(FIXTURES / "toy_repo", target)
        apply_patch(target, patch)
        assert tree_manifest(target) == tree_manifest(after)

    def test_empty_when_identical(self, tmp_path: Path) -> None:
        before, after = tmp_path / "before", tmp_path / "after"
        before.mkdir()
        after.mkdir()
        assert render_diff(before, after) == ""
