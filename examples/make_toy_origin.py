"""Create the local git origin for the examples/toy-calc bundle.

The toy-calc bundle needs a clonable repository, but a committed example cannot
point at a machine-specific path or require network. This script builds a git repo
at examples/.origin from the committed fixture tree (tests/fixtures/toy_repo) with
a fully deterministic commit (fixed author/date), so the resulting SHA is identical
on every machine, and syncs that SHA into examples/toy-calc/task.json.

Usage:  uv run python examples/make_toy_origin.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests/fixtures/toy_repo"
ORIGIN = REPO_ROOT / "examples/.origin"
TASK_JSON = REPO_ROOT / "examples/toy-calc/task.json"

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def make_origin() -> str:
    """(Re)build the origin repo and return its deterministic baseline SHA."""
    if ORIGIN.exists():
        shutil.rmtree(ORIGIN)
    shutil.copytree(FIXTURE, ORIGIN)
    git(["init", "--quiet", "--initial-branch=main"], cwd=ORIGIN)
    git(["config", "uploadpack.allowReachableSHA1InWant", "true"], cwd=ORIGIN)
    git(["add", "-A"], cwd=ORIGIN)
    git(["commit", "--quiet", "-m", "baseline: buggy divide"], cwd=ORIGIN)
    return git(["rev-parse", "HEAD"], cwd=ORIGIN)


def sync_task_json(sha: str) -> bool:
    """Pin task.json to the origin SHA; return True if it changed."""
    spec = json.loads(TASK_JSON.read_text())
    if spec["repo"]["commit"] == sha:
        return False
    spec["repo"]["commit"] = sha
    TASK_JSON.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    return True


def main() -> int:
    sha = make_origin()
    print(f"origin ready at {ORIGIN} (baseline {sha})")
    if sync_task_json(sha):
        print(f"updated {TASK_JSON} to the new SHA — commit the change")
    else:
        print("task.json already pinned to this SHA")
    print("\nnext: uv run task init examples/toy-calc && uv run task validate examples/toy-calc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
