# task-bundle

A CLI for packaging SWE-bench-style coding tasks as portable **bundles**, validating
their baseline test contract in containers, and running LLM solvers against them in
isolation — with every command logged to a queryable SQLite database.

> **Status: feature-complete**, built in reviewable milestones. See
> [DESIGN_NOTES.md](DESIGN_NOTES.md) for the design rationale, [PROGRESS.md](PROGRESS.md)
> for the decision log, and [DESIGN.md](DESIGN.md) for the working design. The full
> command surface — `init`, `validate`, `run` / `fleet` (stub + claude solvers), `verify-gold`,
> `import-swebench`, `diff`, `logs` / `runs`, `doctor`, and `clean` — is implemented,
> tested, and documented.
>
> **Validated scope:** the full flow (incl. a live Claude solve → RESOLVED) is proven
> end-to-end on real SWE-bench Pro instances — see [`evaluation/`](evaluation/).
> Grading works **in place** on the image's own repo tree, so instances whose
> dependencies live under the repo dir outside git (submodule checkouts, `node_modules`,
> compiled extensions) grade correctly; the cross-language sweep in
> [`evaluation/multi-instance/`](evaluation/multi-instance/) shows the openlibrary
> instance that used to be refused now verifying, and the Go path executing real
> `go test` runs (blocked on this arm64 host only by the amd64 toolchain segfaulting
> under qemu). `verify-gold` still *refuses* (never mis-grades) anything it cannot
> prove solvable.

## Why

Benchmark task authors need to check how LLMs perform on their tasks: package a repo
at a pinned commit, hide the grading tests from the model, let it attempt a fix in a
container, then grade the attempt with SWE-bench semantics (`RESOLVED` ⇔ all
fail2pass tests now pass AND all pass2pass tests still pass). Doing that by hand is
slow and brittle; `task` makes it a few commands.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), git, and Docker (for
building and running task images). Run `task doctor` to check your environment.

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

Optional `patch.diff` at the bundle root is the golden patch (used by
`task verify-gold` and `task run --gold`).

## The test-hiding guarantee

The solver must never see fail2pass/pass2pass tests before grading. This is enforced
structurally, not by convention (DESIGN_NOTES.md §3):

- Hidden tests live only in the bundle on the host; they are staged into a separate
  evaluation container **after** the solver's changes are captured — they never enter
  the solver's container or any image layer (no copy-then-delete).
- Native task images are built by *excluding* hidden paths from the copy, and `.git`
  is scrubbed (also from prebuilt images, whose clones carry full history); the host
  clone itself is shallow (`fetch --depth 1 <sha>`), so hidden tests can't be recovered
  from history.
- Hidden tests reach containers only via `docker cp` into a *running* container at
  validate/grade time, so they never appear in any image layer.
- An automated leak guard hashes every file in the solve container before the solver
  starts and aborts (exit 3) if any file is byte-identical to a hidden test — covered by
  unit tests and an end-to-end test that plants a leak.

Tests outside the hidden buckets stay visible to the solver.

## How a run grades

The solver works **in place** inside a hardened container of the task image. The
orchestrator hashes the repo tree before and after; the difference, filtered through the
repo's own `.gitignore`, is the solver's changeset. A *fresh* container then gets exactly
that changeset replayed (files copied in, deletions applied), the hidden tests staged, and
the suites run. Nothing else in the image is touched, so dependencies that live under the
repo directory but outside git — submodule checkouts, `node_modules`, compiled
extensions — are present at grade time exactly as the image shipped them. No `git` or
`patch` is needed inside the image.

## Solvers

- **stub** — applies a provided `--patch` (or the bundle's golden patch via
  `--gold`), or no-ops. Deterministic; used to prove the harness: gold ⇒ RESOLVED,
  no-op ⇒ UNRESOLVED.
- **claude** — an agentic loop over the Claude API (`--model`, default
  `claude-opus-5` or `$ANTHROPIC_MODEL`; needs `ANTHROPIC_API_KEY` or an `ant auth
  login` profile). The model gets `list_dir` / `read_file` / `search` (grep) /
  `edit_file` (exact-match replace, refuses ambiguous matches) / `write_file` /
  `run_command`, all executed **inside the hardened solve container** (network off,
  non-root) — solver-controlled code never runs on the host, and only the files it
  changed ever leave the container.
  - **Cost:** the request is rendered for prompt caching (frozen tools + system prompt,
    top-level `cache_control`), so each turn re-reads the conversation prefix at cache
    rates; cache reads/writes are priced separately in the run stats. `--effort`
    (`low|medium|high|xhigh|max`, default `high`) sets adaptive-thinking depth — the
    main cost/quality lever for agentic runs.
  - **Budgets:** `--max-iterations` (default 30), 30-minute wall clock, a 400K-token
    context budget, bounded tool output.
  - **Robustness:** API errors that survive the SDK's retries, refusals, and output
    truncation end the solve gracefully and grade whatever was edited; the reason is
    recorded as the run's `stop_reason`.
  - **Trajectories:** every step is written to `trajectory.jsonl` — exact tool inputs,
    the exact outputs the model saw, per-step usage and stop reasons — alongside the
    human-readable `transcript.txt`, so runs can be analyzed or turned into training
    data without re-running anything.

## Fleet execution

`task fleet` scales the same correctness pipeline across tasks and independent
samples. A bounded worker pool enforces both `--concurrency` and a per-host
`--container-limit`; new work pauses when free space on the artifact volume falls
below `--min-free-disk-gb`. Every task/model/config/sample tuple has a stable job
and run id in SQLite. Repeating a command therefore reuses completed results and
retries only interrupted or errored jobs.

```sh
# Four samples for every bundle, with at most eight live containers.
uv run task fleet bundles/* --solver claude \
  --samples 4 --concurrency 16 --container-limit 8
```

The command prints per-job progress, pass@1 through pass@k, and writes a structured
`fleet_summary.json`. The pass@k values use the standard unbiased estimator across
tasks, not just the raw fraction of successful attempts.

Execution can also be moved to Kubernetes while orchestration, SQLite state, and
artifacts remain local. Build credentials for the target registry must already be
configured in Docker, and the cluster must have a default-deny egress policy for
solver pods:

```sh
kubectl apply -n eval -f examples/kubernetes-deny-egress.yaml
uv run task fleet bundles/* --solver claude --samples 4 \
  --backend kubernetes --registry ghcr.io/acme --kube-namespace eval
```

The Kubernetes backend preserves the same ephemeral-pod, non-root, resource-limit,
hidden-test staging, and network-isolation boundaries as local execution. Images
must include `tar`, which `kubectl cp` requires.

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
| `task runs list` / `task runs show <run-id>` | ✅ | Query solver runs (populated by `task run`). |
| `task run <bundle> [--solver stub\|claude] [--patch FILE \| --gold] [--model M] [--effort E] [--max-iterations N] [--rebuild]` | ✅ | Baseline → solve in place → replay changeset → grade, in separate containers; before/after table, changed/added/deleted counts, RESOLVED/UNRESOLVED verdict, sorted-key `report.json` + `solver.diff`/`transcript.txt`/`trajectory.jsonl` artifacts, run + token/cache/cost stats recorded in DB. `claude` solver needs Anthropic credentials. |
| `task fleet <bundle>... [--samples K] [--concurrency N] [--container-limit N] [--backend local\|kubernetes]` | ✅ | Concurrent, disk-aware, resumable multi-task/multi-sample execution with content-derived run ids, pass@k, and a JSON fleet summary. |
| `task import-swebench <instance-id> [--dest DIR] [--test-command TPL] [--timeout N] [--no-init] [--no-verify]` | ✅ | Convert a public SWE-bench Pro instance (ScaleAI/SWE-bench_Pro on HuggingFace) into a ready-to-validate bundle: prebuilt instance image as base, hidden tests as test patch + explicit f2p/p2p ids, gold patch saved as `patch.diff`. The repo path is discovered from the image (no `/app` hardcode) and used in place, so submodules/`node_modules`/caches under it survive; Go instances get scoped packages, anchored `-run` patterns, and a writable `GOCACHE`. After `--init` it auto-runs `verify-gold` so a non-gradeable instance fails loudly at import. See `evaluation/` for real end-to-end runs. |
| `task verify-gold <bundle> [--rebuild]` | ✅ | Prove solvability: apply `patch.diff` via a deterministic stub solver and confirm every fail2pass test flips to pass and every pass2pass holds. Exit 2 (naming each offending test) if the golden patch doesn't cleanly resolve the task. Records no run — it's an authoring check. |
| `task diff <run-id>` | ✅ | Print the unified diff a run's solver produced (raw, pipeable to `git apply`). |
| `task doctor` | ✅ | Preflight checks — Docker daemon, git, `ANTHROPIC_API_KEY`, free disk. Exit 1 if a required dependency is missing. |
| `task clean <bundle> \| --run <id> \| --all [--yes]` | ✅ | Reclaim disk: remove task-bundle images, workspace clones, and run artifacts. Previews and prompts before deleting (skip with `--yes`); never touches the SQLite history. |

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
`fleets` / `fleet_jobs` (durable scheduler state and retry attempts),
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

Layout: `src/task_bundle/` (`bundle.py` spec/state, `workspace.py` pinned clones +
changeset/diff math, `harness.py` container orchestration, `run.py` the two-phase
pipeline, `cli.py` typer app, `errors.py` actionable error hierarchy); fixture toy repo
under `tests/fixtures/toy_repo/`.
