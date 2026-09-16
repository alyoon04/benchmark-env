"""Tests for the pure parts of harness.py: cache keys and Dockerfile generation."""

from pathlib import Path

from task_bundle.bundle import Bundle
from task_bundle.harness import (
    generate_dockerfile,
    image_cache_key,
    image_tag,
    resolve_work_dir,
)

SHA = "a" * 40
SHA_B = "b" * 40


def make_bundle(tmp_path: Path, name: str = "t", **kwargs: object) -> Bundle:
    return Bundle.scaffold(
        tmp_path / name,
        repo_url="https://github.com/org/repo",
        commit=SHA,
        base_image="python:3.11-slim",
        test_command="python -m pytest {test_path} -q",
        **kwargs,  # type: ignore[arg-type]
    )


class TestCacheKey:
    def test_stable_for_identical_specs(self, tmp_path: Path) -> None:
        a = make_bundle(tmp_path, "a")
        b = make_bundle(tmp_path, "b")
        assert image_cache_key(a) == image_cache_key(b)

    def test_changes_with_commit(self, tmp_path: Path) -> None:
        a = make_bundle(tmp_path, "a")
        b = make_bundle(tmp_path, "b")
        b.spec.repo.commit = SHA_B
        assert image_cache_key(a) != image_cache_key(b)

    def test_changes_with_setup_commands(self, tmp_path: Path) -> None:
        a = make_bundle(tmp_path, "a")
        b = make_bundle(tmp_path, "b", setup_commands=["pip install pytest"])
        assert image_cache_key(a) != image_cache_key(b)

    def test_tag_contains_task_id_and_key(self, tmp_path: Path) -> None:
        a = make_bundle(tmp_path, "my-task")
        assert image_tag(a) == f"task-bundle/my-task:{image_cache_key(a)}"


class TestDockerfile:
    def test_minimal(self, tmp_path: Path) -> None:
        df = generate_dockerfile(make_bundle(tmp_path))
        assert df.startswith("FROM python:3.11-slim\n")
        assert "COPY --chown=1000:1000 repo/ /workspace/" in df
        assert "RUN" not in df  # no setup commands -> no RUN layers

    def test_setup_commands_joined_and_chowned(self, tmp_path: Path) -> None:
        b = make_bundle(tmp_path, setup_commands=["apt-get update", "pip install -e ."])
        df = generate_dockerfile(b)
        assert "RUN apt-get update && pip install -e ." in df
        assert "RUN chown -R 1000:1000 /workspace" in df

    def test_env_vars_sorted_and_quoted(self, tmp_path: Path) -> None:
        b = make_bundle(tmp_path)
        b.spec.environment.env = {"B": "two words", "A": "1"}
        df = generate_dockerfile(b)
        a_idx, b_idx = df.index("ENV A="), df.index("ENV B=")
        assert a_idx < b_idx
        assert "ENV B='two words'" in df

    def test_deterministic(self, tmp_path: Path) -> None:
        a = make_bundle(tmp_path, "a")
        b = make_bundle(tmp_path, "b")
        assert generate_dockerfile(a) == generate_dockerfile(b)

    def test_native_layout_copies_clone(self, tmp_path: Path) -> None:
        df = generate_dockerfile(make_bundle(tmp_path), work_dir="/workspace")
        assert "COPY" in df
        assert "WORKDIR /workspace" in df
        assert ".git" not in df  # the clean tree never had one

    def test_prebuilt_layout_keeps_repo_in_place(self, tmp_path: Path) -> None:
        """The image's own repo dir is used as-is: no COPY, no symlink, .git scrubbed."""
        df = generate_dockerfile(make_bundle(tmp_path), work_dir="/app")
        assert "COPY" not in df
        assert "ln -s" not in df
        assert "WORKDIR /app" in df
        assert "find /app -name .git -prune -exec rm -rf {} +" in df
        assert "RUN chown -R 1000:1000 /app" in df

    def test_prebuilt_layout_removes_excludes(self, tmp_path: Path) -> None:
        b = make_bundle(tmp_path)
        b.spec.solver.workspace_excludes = ["tests/hidden", "secret.md"]
        df = generate_dockerfile(b, work_dir="/app")
        assert "rm -rf /app/tests/hidden" in df
        assert "rm -rf /app/secret.md" in df


class _FakeDocker:
    """Stand-in for Docker that reports a fixed WorkingDir without a real daemon."""

    def __init__(self, work_dir: str, *, present: bool = True) -> None:
        self._work_dir = work_dir
        self._present = present
        self.pulled = False

    def image_exists(self, image: str) -> bool:
        return self._present

    def pull(self, image: str) -> None:
        self.pulled = True

    def image_workdir(self, image: str) -> str:
        return self._work_dir


class TestResolveWorkDir:
    def test_prebuilt_repo_path_used(self, tmp_path: Path) -> None:
        docker = _FakeDocker("/app")
        assert resolve_work_dir(docker, make_bundle(tmp_path)) == "/app"  # type: ignore[arg-type]

    def test_empty_workdir_falls_back_to_workspace(self, tmp_path: Path) -> None:
        docker = _FakeDocker("")
        assert resolve_work_dir(docker, make_bundle(tmp_path)) == "/workspace"  # type: ignore[arg-type]

    def test_root_workdir_is_ignored(self, tmp_path: Path) -> None:
        docker = _FakeDocker("/")
        assert resolve_work_dir(docker, make_bundle(tmp_path)) == "/workspace"  # type: ignore[arg-type]

    def test_pulls_base_when_absent(self, tmp_path: Path) -> None:
        docker = _FakeDocker("/app", present=False)
        resolve_work_dir(docker, make_bundle(tmp_path))  # type: ignore[arg-type]
        assert docker.pulled


class TestSnapshots:
    def test_parse_stat_listing_handles_spaces_in_names(self) -> None:
        from task_bundle.harness import parse_stat_listing

        text = "12 1700000000 ./a.py\n7 1700000001 ./dir/with space.txt\nbad line\n"
        assert parse_stat_listing(text) == {
            "a.py": "12 1700000000",
            "dir/with space.txt": "7 1700000001",
        }

    def test_merge_snapshot_drops_deleted_and_overlays_changed(self) -> None:
        from task_bundle.harness import TreeSnapshot, merge_snapshot

        before = TreeSnapshot(
            hashes={"keep": "h1", "edit": "h2", "gone": "h3"},
            stats={"keep": "1 1", "edit": "2 1", "gone": "3 1"},
        )
        after_stats = {"keep": "1 1", "edit": "2 9", "new": "4 9"}
        merged = merge_snapshot(before, after_stats, {"edit": "h2x", "new": "h4"})
        assert merged == {"keep": "h1", "edit": "h2x", "new": "h4"}

    def test_snapshot_cache_computes_once_per_image(self) -> None:
        import task_bundle.harness as h
        from task_bundle.harness import SnapshotCache, TaskImage, TreeSnapshot

        calls: list[str] = []

        def fake_snapshot(docker: object, image: TaskImage, cid: str) -> TreeSnapshot:
            calls.append(image.tag)
            return TreeSnapshot(hashes={"a": "1"}, stats={"a": "1 1"})

        original = h.snapshot
        h.snapshot = fake_snapshot  # type: ignore[assignment]
        try:
            cache = SnapshotCache()
            img = TaskImage("t:1", "/workspace")
            assert cache.get(None, img, "c1").hashes == {"a": "1"}  # type: ignore[arg-type]
            assert cache.get(None, img, "c2").hashes == {"a": "1"}  # type: ignore[arg-type]
            cache.get(None, TaskImage("t:2", "/workspace"), "c3")  # type: ignore[arg-type]
        finally:
            h.snapshot = original
        assert calls == ["t:1", "t:2"]
        assert cache.hits == 1
