"""Tests for cleaned-tree construction and the hidden-content leak guard."""

import subprocess
from pathlib import Path

import pytest

from task_bundle.errors import HiddenTestLeak
from task_bundle.workspace import (
    apply_patch,
    assert_no_hidden_content,
    build_clean_tree,
    patch_changed_paths,
    resolve_repo_url,
)

PATCH = """\
diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1 +1 @@
-old = 1
+old = 2
"""


class TestGitInsideForeignRepo:
    """git operations must ignore an unrelated enclosing repo (e.g. a git-managed $HOME).

    Inside a foreign repo, `git apply` silently skips every patch path outside the
    cwd prefix and exits 0 — numstat returns nothing and apply becomes a no-op.
    """

    @pytest.fixture()
    def inside_repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        work = tmp_path / "deep" / "work"
        (work / "pkg").mkdir(parents=True)
        (work / "pkg/mod.py").write_text("old = 1\n")
        (work / "p.diff").write_text(PATCH)
        return work

    def test_patch_changed_paths(self, inside_repo: Path) -> None:
        assert patch_changed_paths(inside_repo / "p.diff") == ["pkg/mod.py"]

    def test_apply_patch(self, inside_repo: Path) -> None:
        apply_patch(inside_repo, inside_repo / "p.diff")
        assert (inside_repo / "pkg/mod.py").read_text() == "old = 2\n"


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (ws / "src").mkdir()
    (ws / "src/app.py").write_text("print('app')\n")
    (ws / "tests").mkdir()
    (ws / "tests/test_visible.py").write_text("def test_v(): pass\n")
    (ws / "tests/test_secret.py").write_text("def test_secret(): assert fixed()\n")
    return ws


class TestBuildCleanTree:
    def test_git_always_stripped(self, tmp_path: Path) -> None:
        dest = tmp_path / "clean"
        build_clean_tree(make_workspace(tmp_path), dest)
        assert not (dest / ".git").exists()
        assert (dest / "src/app.py").is_file()
        assert (dest / "tests/test_visible.py").is_file()

    def test_excludes_never_copied(self, tmp_path: Path) -> None:
        dest = tmp_path / "clean"
        build_clean_tree(make_workspace(tmp_path), dest, excludes=["tests/test_secret.py"])
        assert not (dest / "tests/test_secret.py").exists()
        assert (dest / "tests/test_visible.py").is_file()

    def test_directory_exclude(self, tmp_path: Path) -> None:
        dest = tmp_path / "clean"
        build_clean_tree(make_workspace(tmp_path), dest, excludes=["tests"])
        assert not (dest / "tests").exists()

    def test_rebuild_replaces_dest(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        dest = tmp_path / "clean"
        build_clean_tree(ws, dest)
        (dest / "stale.txt").write_text("stale")
        build_clean_tree(ws, dest)
        assert not (dest / "stale.txt").exists()


class TestLeakGuard:
    def test_clean_tree_passes(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        hidden = tmp_path / "hidden_test.py"
        hidden.write_text("def test_hidden(): assert deep_magic()\n")
        dest = tmp_path / "clean"
        build_clean_tree(ws, dest)
        assert_no_hidden_content(dest, [hidden.read_bytes()])  # must not raise

    def test_identical_content_detected_under_any_name(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        hidden = tmp_path / "hidden_test.py"
        hidden.write_text((ws / "tests/test_secret.py").read_text())
        dest = tmp_path / "clean"
        build_clean_tree(ws, dest)  # test_secret.py copied in (not excluded)
        with pytest.raises(HiddenTestLeak, match="byte-identical"):
            assert_no_hidden_content(dest, [hidden.read_bytes()])

    def test_same_name_different_content_is_fine(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        hidden = tmp_path / "test_visible.py"  # same name as a repo file
        hidden.write_text("def test_v(): assert new_behavior()\n")  # different bytes
        dest = tmp_path / "clean"
        build_clean_tree(ws, dest)
        assert_no_hidden_content(dest, [hidden.read_bytes()])  # must not raise


class TestResolveRepoUrl:
    def test_remote_urls_untouched(self, tmp_path: Path) -> None:
        url = "https://github.com/org/repo"
        assert resolve_repo_url(url, tmp_path) == url
        ssh = "git@github.com:org/repo.git"
        assert resolve_repo_url(ssh, tmp_path) == ssh

    def test_absolute_paths_untouched(self, tmp_path: Path) -> None:
        assert resolve_repo_url("/srv/repo", tmp_path) == "/srv/repo"

    def test_relative_path_resolved_against_bundle(self, tmp_path: Path) -> None:
        bundle = tmp_path / "examples/toy-calc"
        bundle.mkdir(parents=True)
        assert resolve_repo_url("../.origin", bundle) == str(tmp_path / "examples/.origin")
