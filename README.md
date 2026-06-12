# task-bundle

A CLI for packaging SWE-bench-style coding tasks as portable **bundles**, validating
their baseline test contract in containers, and running LLM solvers against them in
isolation — with every command logged to a queryable SQLite database.

> **Status: work in progress.** Built in reviewable milestones; see
> [PROGRESS.md](PROGRESS.md) for what exists today and [DESIGN.md](DESIGN.md) for the
> full architecture. Currently implemented: bundle spec + `task init`.

## Why

Benchmark task authors need to check how LLMs perform on their tasks: package a repo
at a pinned commit, hide the grading tests from the model, let it attempt a fix in a
container, then grade the attempt with SWE-bench semantics (`RESOLVED` ⇔ all
fail2pass tests now pass AND all pass2pass tests still pass). Doing that by hand is
slow and brittle; `task` makes it a few commands.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), git, and Docker (for
milestone 2+).

```sh
uv sync
uv run task --help
```

## Quickstart (what works today)

Scaffold a bundle and materialize the repo at a pinned commit:

```sh
uv run task init my-task \
  --repo https://github.com/octocat/Hello-World \
  --commit 7fd1a60b01f91b314f59955a4e4d4e80d8edf11d
```

This creates:

```
my-task/
  task.json            # validated metadata (see "Bundle spec" below)
  description.md       # problem statement shown to the solver
  tests/
    fail2pass/         # hidden: must FAIL on baseline, PASS after the golden patch
    pass2pass/         # hidden: must PASS on baseline and after the golden patch
  .task/               # tool-managed (gitignored): workspace clone, state.json
    workspace/         # the repo at exactly the pinned commit (shallow clone)
```

Re-running `task init my-task` is idempotent; `--force` re-clones from scratch.

## Bundle spec

`task.json` is validated by a pydantic schema (`schema_version: 1`):

| Field | Meaning |
|---|---|
| `repo.url`, `repo.commit` | Repository and **full 40-char SHA** pin (short SHAs rejected) |
| `environment.base_image` | Docker base image for the task |
| `environment.setup_commands` | Commands run at image **build** time (network on) |
| `environment.env` | Explicit env-var allowlist passed into containers |
| `tests.command_template` | Test command with a `{test_path}` placeholder — pure data, so any language/framework works (`go test {test_path}`, `npx jest {test_path}`, ...) |
| `tests.staging_dir` | Where hidden tests are staged in the eval container |
| `tests.timeout_seconds` | Per-test-invocation timeout |
| `tests.test_patch` + `tests.fail2pass_ids`/`pass2pass_ids` | SWE-bench-style alternative to the `tests/` directories: a test patch plus explicit test identifiers. A bundle uses exactly one of the two formats. |

Optional `patch.diff` at the bundle root is the golden patch (used by the planned
`task verify-gold`).

## The test-hiding guarantee

The solver must never see fail2pass/pass2pass tests before grading. This is enforced
structurally, not by convention (DESIGN.md §3):

- Hidden tests live only in the bundle on the host; they are staged into a separate
  evaluation container **after** the solver's diff is captured — they never enter the
  solver's workspace or any image layer (no copy-then-delete).
- The solver workspace is built by *excluding* hidden paths from the copy, and `.git`
  is scrubbed; the workspace clone itself is shallow (`fetch --depth 1 <sha>`), so
  hidden tests can't be recovered from history.
- An automated check asserts no hidden-test path or content appears in the
  solver-visible workspace (lands with `task run`).

Tests outside the hidden buckets stay visible to the solver.

## Command reference

| Command | Status | Purpose |
|---|---|---|
| `task init <bundle> [--repo URL --commit SHA] [--base-image IMG] [--test-command TPL] [--setup CMD]... [--force]` | ✅ | Scaffold the bundle and clone the repo at the pinned commit. Image build + smoke test land in milestone 2. |
| `task validate <bundle>` | planned (M2) | Baseline contract: pass2pass all pass, fail2pass all fail; 3× runs with flaky-test detection. |
| `task logs` / `task runs` | planned (M3) | Query command logs and runs from SQLite. |
| `task run <bundle> --solver {stub,claude}` | planned (M4/M5) | Solve in isolation → grade hidden tests → JSON report. |
| `task import-swebench <instance-id>` | planned (M6) | Convert a SWE-bench Pro instance into a bundle. |
| `task verify-gold` / `task diff` / `task doctor` / `task clean` | planned (M7) | Authoring and ops helpers. |

## Development

```sh
uv run pytest          # test suite (network-free; uses a local file:// git origin)
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy            # strict type-checking
```

Layout: `src/task_bundle/` (`bundle.py` spec/state, `workspace.py` pinned clones,
`cli.py` typer app, `errors.py` actionable error hierarchy); fixture toy repo under
`tests/fixtures/toy_repo/`.
