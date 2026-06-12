# task-bundle

A CLI for packaging SWE-bench-style coding tasks as portable **bundles**, validating
their baseline test contract in containers, and running LLM solvers against them in
isolation — with every command logged to a queryable SQLite database.

> **Status: work in progress.** Built in reviewable milestones; see
> [PROGRESS.md](PROGRESS.md) for what exists today and [DESIGN.md](DESIGN.md) for the
> full architecture. Currently implemented: bundle spec, `task init` (with image
> build), `task validate`, `task run` (stub solver), and SQLite command logging
> with `task logs` / `task runs`.

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

Validate the included example bundle end-to-end (offline; Docker required):

```sh
uv sync
uv run python examples/make_toy_origin.py   # deterministic local git origin
uv run task init examples/toy-calc          # clone + build + smoke-test image
uv run task validate examples/toy-calc      # baseline contract, 3x per suite
uv run task run examples/toy-calc --gold    # stub solver applies golden patch -> RESOLVED
uv run task run examples/toy-calc           # no-op stub -> UNRESOLVED
```

Expected: a table showing the pass2pass suite `passed passed passed` and the
fail2pass suite `failed failed failed`, then "Baseline contract holds".

Or scaffold your own bundle from any repo:

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
- Hidden tests reach containers only via `docker cp` into a *running* container at
  validate/grade time, so they never appear in any image layer.
- An automated leak guard (`workspace.assert_no_hidden_content`, covered by the test
  suite) detects hidden-test content in solver-visible trees by byte comparison.

Tests outside the hidden buckets stay visible to the solver.

## Isolation model

Task containers run with: no network (`--network none`; build-time network is on so
dependencies can install), non-root uid 1000, 4 GB memory / 2 CPU / 512 pid limits,
all capabilities dropped except CHOWN/DAC_OVERRIDE/FOWNER (needed only by root
orchestrator execs that stage tests), and `no-new-privileges`. Each validate attempt
gets a fresh container so runs cannot contaminate each other.

## Command reference

| Command | Status | Purpose |
|---|---|---|
| `task init <bundle> [--repo URL --commit SHA] [--base-image IMG] [--test-command TPL] [--setup CMD]... [--force] [--skip-build] [--rebuild]` | ✅ | Scaffold the bundle, clone the repo at the pinned commit, build + smoke-test the task image (cached by content hash). |
| `task validate <bundle> [--attempts N] [--rebuild]` | ✅ | Baseline contract: pass2pass all pass, fail2pass all fail; each suite runs 3× in fresh containers and flaky tests are flagged. Exit 2 with specific reasons on violation. |
| `task logs [<command-id>]` | ✅ | No argument: list recent commands. With an id: show argv, exit code, per-test results, artifacts, and the command log. |
| `task runs list` / `task runs show <run-id>` | ✅ | Query solver runs (populated by `task run`, milestone 4). |
| `task run <bundle> [--solver stub] [--patch FILE \| --gold] [--rebuild]` | ✅ (stub) | Baseline → solve → grade in separate containers; before/after table, RESOLVED/UNRESOLVED verdict, sorted-key `report.json` + `solver.diff` artifacts, run recorded in DB. `--solver claude` lands in milestone 5. |
| `task import-swebench <instance-id>` | planned (M6) | Convert a SWE-bench Pro instance into a bundle. |
| `task verify-gold` / `task diff` / `task doctor` / `task clean` | planned (M7) | Authoring and ops helpers. |

## Observability

Every CLI invocation is recorded in SQLite (default `~/.task-bundle/task.db`;
override with `--db`, `--artifacts-dir`, or `TASK_BUNDLE_DB`/`TASK_BUNDLE_ARTIFACTS`)
and prints its command id when it finishes — even on failure. Collaborators can then
inspect what happened without re-running anything:

```sh
uv run task logs                  # recent commands: id, name, exit code, bundle
uv run task logs cmd_<id>         # one command: argv, per-test results, artifacts, log
uv run task runs list             # past solver runs and verdicts
```

Tables: `commands` (one row per invocation), `runs` (solver runs + verdict/cost),
`test_results` (per test, per attempt, per phase), `artifacts` (paths to on-disk
build logs, test output, diffs, transcripts under `~/.task-bundle/artifacts/<command-id>/`).

## Example bundle

`examples/toy-calc/` is a complete validatable bundle: a tiny calculator repo with a
deliberately buggy `divide()`, hidden fail2pass tests covering the fix, hidden
pass2pass tests guarding `add`/`subtract`, and a golden `patch.diff`. Its repository
is generated by `examples/make_toy_origin.py` with pinned author/date, so the commit
SHA is identical on every machine and `task.json` can pin it — no network needed.

## Development

```sh
uv run pytest          # test suite (docker-marked tests auto-skip without a daemon)
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy            # strict type-checking
```

Layout: `src/task_bundle/` (`bundle.py` spec/state, `workspace.py` pinned clones,
`cli.py` typer app, `errors.py` actionable error hierarchy); fixture toy repo under
`tests/fixtures/toy_repo/`.
