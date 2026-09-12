"""Convert public SWE-bench Pro instances (ScaleAI/SWE-bench_Pro) into task bundles.

Instances are fetched from the HuggingFace datasets-server JSON API (no `datasets`
dependency). The resulting bundle uses the SWE-bench test format: hidden tests are
a *test patch* against the baseline plus explicit fail2pass/pass2pass test ids, and
the environment is the instance's prebuilt Docker image (jefzda/sweap-images), so
no dependency installation is needed.
"""

import ast
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from task_bundle.bundle import (
    Bundle,
    EnvironmentSpec,
    RepoSpec,
    SolverSpec,
    TaskSpec,
    TestsSpec,
)
from task_bundle.errors import TaskError

DATASET = "ScaleAI/SWE-bench_Pro"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
FILTER_URL = "https://datasets-server.huggingface.co/filter"
IMAGE_REPO = "jefzda/sweap-images"
PAGE = 100

# Per-language default test commands for import-swebench; {test_path} receives one
# test identifier. The Python and Go defaults are validated end-to-end (see
# evaluation/multi-instance/). The JS/TS ones are best-effort starting points —
# override with --test-command. `verify-gold` runs automatically at import and will
# flag a default that doesn't actually execute the instance's tests, so a wrong guess
# fails loudly rather than silently mis-grading.
_TEST_COMMANDS = {
    "python": "python -m pytest {test_path} -q",
    "js": "npx mocha {test_path}",
    "ts": "npx mocha {test_path}",
    "go": "go test -run {test_path} {packages}",
}

# Containers run as uid 1000 with no passwd entry; tools that write under $HOME
# (e.g. ansible's ~/.ansible/tmp) need a writable one. Go additionally gets its own
# build cache path: the default $HOME/.cache can be pre-created root-owned by
# orchestrator execs, which made `go test` fail with "permission denied".
_LANGUAGE_ENV: dict[str, dict[str, str]] = {
    "go": {"HOME": "/tmp", "GOCACHE": "/tmp/task-bundle-go-build"},
}
_DEFAULT_ENV = {"HOME": "/tmp"}

_HTTP_ATTEMPTS = 3


def _http_json(url: str) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(1, _HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < _HTTP_ATTEMPTS:
                time.sleep(2 * attempt)
    raise TaskError(
        f"Could not reach the HuggingFace datasets server ({last}). "
        "import-swebench needs network access."
    ) from last


def fetch_instance(instance_id: str) -> dict[str, Any]:
    """Fetch one dataset row by instance id (filter API, row-scan fallback)."""
    where = urllib.parse.quote(f"\"instance_id\"='{instance_id}'")
    try:
        data = _http_json(
            f"{FILTER_URL}?dataset={urllib.parse.quote(DATASET)}&config=default"
            f"&split=test&where={where}"
        )
        rows = data.get("rows", [])
    except TaskError:
        rows = []  # the filter endpoint 500s intermittently; the row scan still works
    if rows:
        return dict(rows[0]["row"])
    # The filter index is sometimes still building; fall back to scanning pages.
    offset = 0
    while True:
        data = _http_json(
            f"{ROWS_URL}?dataset={urllib.parse.quote(DATASET)}&config=default"
            f"&split=test&offset={offset}&length={PAGE}"
        )
        page = data.get("rows", [])
        if not page:
            break
        for row in page:
            if row["row"]["instance_id"] == instance_id:
                return dict(row["row"])
        offset += PAGE
    raise TaskError(
        f"Instance {instance_id!r} not found in {DATASET}. "
        "Check the id on https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro."
    )


def _listish(value: Any) -> list[str]:
    """Dataset list fields arrive as real lists, JSON strings, or Python-repr strings.

    (The public dataset is inconsistent: e.g. fail_to_pass uses single-quoted
    Python repr while pass_to_pass is JSON.)
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = ast.literal_eval(value)
    return [str(x) for x in value]


def _textish(value: Any) -> str:
    """Some text fields are double-encoded JSON strings ('"..."')."""
    if isinstance(value, str) and value.startswith('"'):
        try:
            return str(json.loads(value))
        except json.JSONDecodeError:
            return value
    return str(value or "")


def go_test_packages(test_patch: str) -> str:
    """Package dirs (``./a/ ./b/``) of the ``_test.go`` files a test patch touches.

    Scoping ``go test`` to those packages instead of ``./...`` avoids compiling the
    whole module per test id; falls back to ``./...`` if the patch names no test file.
    """
    dirs = set()
    for line in test_patch.splitlines():
        if line.startswith("+++ b/") and line.endswith("_test.go"):
            parent = line[len("+++ b/") :].rpartition("/")[0]
            dirs.add(f"./{parent}/" if parent else "./")
    return " ".join(sorted(dirs)) or "./..."


def go_run_pattern(test_id: str) -> str:
    """Anchor a Go test id for ``-run``: ``Top/sub`` -> ``^Top$/^sub$``.

    ``go test -run`` splits its pattern on ``/`` and matches each element as an
    unanchored regex against that subtest level, so a bare ``Test_x/case`` would
    also select ``Test_x/case_extra``. Anchoring each level selects exactly the
    named test (and, for a parent id, all of its subtests).
    """
    return "/".join(f"^{re.escape(part)}$" for part in test_id.split("/"))


def convert_instance(
    row: dict[str, Any], dest: Path, *, test_command: str | None = None, timeout: int = 600
) -> Bundle:
    """Write a task bundle for ``row`` at ``dest`` and return it (not yet initialized)."""
    if (dest / "task.json").exists():
        raise TaskError(f"{dest} already contains a bundle; choose another directory.")
    language = str(row.get("repo_language") or "python")
    command = test_command or _TEST_COMMANDS.get(language)
    if command is None:
        raise TaskError(f"No default test command for language {language!r}; pass --test-command.")
    fail2pass_ids = _listish(row["fail_to_pass"])
    pass2pass_ids = _listish(row["pass_to_pass"])
    if language == "go":
        command = command.replace("{packages}", go_test_packages(str(row["test_patch"])))
        if test_command is None:
            fail2pass_ids = [go_run_pattern(t) for t in fail2pass_ids]
            pass2pass_ids = [go_run_pattern(t) for t in pass2pass_ids]
    spec = TaskSpec(
        id=dest.name,
        repo=RepoSpec(url=f"https://github.com/{row['repo']}", commit=str(row["base_commit"])),
        language=language,
        environment=EnvironmentSpec(
            base_image=f"{IMAGE_REPO}:{row['dockerhub_tag']}",
            # The prebuilt image already has the repo at its WorkingDir with every
            # dependency installed against it (editable installs, submodules,
            # node_modules, module caches). The engine keeps that directory intact
            # and the solver works in it in place, so nothing installed *under* the
            # repo is lost (see harness.generate_dockerfile); no setup commands are
            # needed here.
            # LIMITATION: a *non-editable* install imports from site-packages
            # regardless of the repo tree, so the solver's edits would be invisible
            # and every gold run would grade a false UNRESOLVED. `import-swebench`
            # runs `verify-gold` automatically to catch exactly that before the
            # bundle is trusted.
            setup_commands=[],
            env=_LANGUAGE_ENV.get(language, _DEFAULT_ENV),
        ),
        tests=TestsSpec(
            command_template=command,
            timeout_seconds=timeout,
            test_patch="tests/test_patch.diff",
            fail2pass_ids=fail2pass_ids,
            pass2pass_ids=pass2pass_ids,
        ),
        solver=SolverSpec(),
    )
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "task.json").write_text(json.dumps(spec.model_dump(), indent=2, sort_keys=True) + "\n")
    (dest / "tests/test_patch.diff").write_text(str(row["test_patch"]))
    (dest / "patch.diff").write_text(str(row["patch"]))
    description = f"# {row['instance_id']}\n\n{_textish(row['problem_statement'])}\n"
    if _textish(row.get("requirements")):
        description += f"\n## Requirements\n\n{_textish(row['requirements'])}\n"
    if _textish(row.get("interface")):
        description += f"\n## Interface\n\n{_textish(row['interface'])}\n"
    (dest / "description.md").write_text(description)
    state_dir = dest / ".task"
    state_dir.mkdir(exist_ok=True)
    (state_dir / ".gitignore").write_text("*\n")
    return Bundle(dest, spec)
