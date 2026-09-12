# Task Bundle CLI — Design

A CLI tool (`task`) that packages SWE-bench-style coding tasks as portable "bundles",
builds containerized environments for them, validates the baseline test contract,
runs LLM solvers against them in isolation, and grades the results — with every
command logged to a queryable SQLite database.

This document is the working design. It is the source of truth for decisions;
`DESIGN_NOTES.md` (final deliverable) will be distilled from it at the end.

---

## 1. Goals and non-goals

**Goals**

1. Correct baseline-vs-solver flow with SWE-bench semantics:
   `RESOLVED` ⇔ all fail2pass tests pass post-solver AND all pass2pass tests still pass.
2. Structural (not conventional) enforcement of the test-hiding invariant — the solver
   can never see fail2pass/pass2pass tests before grading.
3. Language-agnostic engine: test commands are *data* in `task.json`, never code in the
   engine. Nothing assumes pytest or Python-in-the-container.
4. Reproducibility: pinned commits, image caching keyed on content hashes, flaky-test
   detection (3× runs, per SWE-bench Pro §4.3), recorded tool versions, deterministic
   JSON reports.
5. Isolation: solver runs as non-root, network-off by default, resource-limited,
   with no path to the host filesystem.
6. Observability: every CLI invocation logged to SQLite; artifacts (diffs, test output,
   transcripts, build logs) stored on disk and referenced by path.
7. Fleet execution: bounded concurrency, disk-pressure admission control, durable resume,
   multi-sample pass@k, and interchangeable local/Kubernetes runtimes.

**Non-goals (documented as future work in DESIGN_NOTES.md)**

- A hosted scheduler/control plane.
- Automatic cluster provisioning or registry credential management.
- Cross-run warm-container reuse.

---

## 2. Bundle spec

A bundle is a self-contained directory describing one task:

```
my-task/
  task.json            # machine-readable task metadata (pydantic-validated)
  description.md       # problem statement shown to the solver
  patch.diff           # golden patch (optional; used by `task verify-gold`)
  tests/
    fail2pass/         # hidden: must FAIL on baseline, PASS after golden patch
    pass2pass/         # hidden: must PASS on baseline and after golden patch
  .task/               # tool-managed state (gitignored inside the bundle)
    workspace/         # cloned repo at pinned commit (baseline source of truth)
    state.json         # init status, image tag, image digest, timestamps
```

### 2.1 `task.json` schema (pydantic `TaskSpec`)

```jsonc
{
  "schema_version": 1,
  "id": "my-task",                       // slug; defaults to directory name
  "repo": {
    "url": "https://github.com/org/repo",
    "commit": "<full 40-char SHA>"        // full SHA required: short SHAs can drift
  },
  "language": "python",                   // informational only; engine never branches on it
  "environment": {
    "base_image": "python:3.11-slim",     // or any image; digest pin recommended
    "setup_commands": [                   // run at image BUILD time, in order
      "pip install -e .[test]"
    ],
    "env": {"PYTHONDONTWRITEBYTECODE": "1"},  // env var allowlist (nothing else leaks in)
    "network_during_setup": true          // builds may fetch deps; run-time net is OFF
  },
  "tests": {
    "command_template": "python -m pytest {test_path} -x -q",
    //  {test_path} substituted per staged test file/dir. Pure data — works for
    //  `go test {test_path}`, `npx jest {test_path}`, `cargo test ...`, etc.
    "staging_dir": "tests/",              // where hidden tests get copied (relative to repo root)
    "timeout_seconds": 300,               // per test-suite invocation
    "visible_test_paths": []              // repo paths that stay visible to solver (default: everything in repo)
  },
  "solver": {
    "workspace_excludes": []              // extra repo paths to strip from solver workspace (besides hidden tests + .git)
  }
}
```

Notes:

- **Test commands are data.** The engine only does template substitution and exec.
  Per-test pass/fail is parsed from exit codes per invocation (one invocation per
  staged test path), not from framework-specific output. This keeps the engine
  language-agnostic at the cost of one container-exec per test file — fine at task scale.
- **Hidden tests live only in the bundle** (`tests/fail2pass/`, `tests/pass2pass/`),
  never in the solver image. `staging_dir` says where they land *in the eval
  container* at grading time so relative imports work.
- For SWE-bench-style tasks where hidden tests are *modifications* to existing repo
  test files (a test patch), the import path (`task import-swebench`) stores the test
  patch as `tests/test_patch.diff` and the f2p/p2p test *identifiers* in `task.json`
  (`tests.fail2pass_ids` / `tests.pass2pass_ids`); grading applies the patch and runs
  the named tests. The directory layout is the native format; the patch+ids form is
  the SWE-bench compatibility format. `TaskSpec` supports both (exactly one required).

### 2.2 Bundle lifecycle states

`scaffolded` → `initialized` (repo cloned, image built, smoke-tested) → `validated`
(baseline contract confirmed) → runs accumulate. State is recorded in
`.task/state.json` and mirrored in the DB.

---

## 3. Test-hiding invariant (the core correctness property)

**Invariant: no hidden-test content ever enters the solver's workspace or image.**

Enforcement is structural:

1. **Construct-clean, never copy-then-delete.** The solver workspace is built by
   copying the baseline repo from `.task/workspace/` with hidden test paths *excluded
   from the copy*. For SWE-bench-format bundles the *baseline* versions of files the
   test patch touches stay visible (that is what the repo at the pinned commit
   contains — real SWE-bench semantics); what is hidden is the patch itself and the
   *patched* versions, enforced by the content leak guard.
   Nothing to delete ⇒ nothing lingering in image layers.
2. **`.git` is scrubbed** from the solver workspace. The repo's history could contain
   the test files (SWE-bench test patches come from a future commit, but defense in
   depth: the pinned-commit tree itself may contain f2p tests that we strip). The
   solver gets a plain directory tree, not a git repo. The orchestrator keeps its own
   git checkout host-side for diffing.
3. **Hidden tests never mount into the solver container.** They are staged only into a
   *separate, fresh evaluation container* after the solver's diff is captured.
4. **Two-phase run:** solve phase (solver container, no hidden tests, network off) →
   snapshot diff → grade phase (fresh eval container from the baseline image, solver
   diff applied, hidden tests staged, suites executed).
5. **Automated guard test:** `task run` executes a pre-flight assertion that no
   hidden-test path (and no content fingerprint of hidden test files) exists in the
   solver workspace, and the tool's own pytest suite covers this invariant with a
   fixture bundle. Failure aborts the run loudly.

Visible tests (everything not in the hidden buckets) remain in the solver workspace,
per the assignment.

---

## 4. Container lifecycle

Two image roles, one Dockerfile generated by the tool:

- **Task image** (`task-bundle/<task-id>:<cache-key>`): base_image + repo at pinned
  commit + setup_commands executed. Cache key = sha256(repo url, commit, base_image,
  setup_commands, env)[:16] — repeat `init` is a no-op if the image exists.
- **Solver workspace**: at run time, a container from the task image where the repo
  dir has been replaced by the *cleaned* copy (hidden tests excluded, `.git` removed).
  Implemented by mounting/copying the cleaned tree over the repo path in a fresh
  container — the original layers never contained hidden tests either, because the
  image is built from the cleaned baseline too. (Belt and suspenders: the image build
  context *is* the cleaned tree + hidden buckets are never in the build context.)
- **Eval container**: fresh container from the same task image. Apply solver diff
  (`git apply` host-side onto a temp copy, or in-container `patch`), stage hidden
  tests, run suites.

Wait — building the image from the cleaned tree means baseline validation also runs
on the cleaned tree, with hidden tests staged in. That's exactly right: **validate and
grade use the same staging mechanism**, so validation exercises the grading path.

**Decision: the task image never contains hidden tests at all.** Hidden tests are
staged via `docker cp` into started containers only. One image serves solve,
validate, and grade phases.

Container run flags (solve phase):
`--network none --user 1000:1000 --memory 4g --cpus 2 --pids-limit 512 --cap-drop ALL
--cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER
--security-opt no-new-privileges` (the three re-added caps are needed by root-user
*orchestrator* execs that stage hidden tests; the solver runs as uid 1000 and cannot
use them), writable workspace volume only, per-run timeout
enforced by the orchestrator (SIGKILL on expiry). Grade phase: same minus solver,
plus staged tests; network stays off.

Setup commands run at build time *with* network (dependency installs), which is the
standard tradeoff: run-time network off, build-time network on, documented in
DESIGN_NOTES.md.

---

## 5. Command surface

| Command | Purpose |
|---|---|
| `task init <bundle> [--repo URL --commit SHA --base-image IMG]` | Scaffold bundle (if flags given), clone repo at pinned commit, build task image, smoke-test (setup ok + test runner executes). |
| `task validate <bundle>` | Run pass2pass (must all pass) and fail2pass (must all fail) on baseline, 3× each, flag flaky tests; nonzero exit + specific message on contract violation. |
| `task run <bundle> --solver {stub,claude} [--patch FILE] [--model NAME] [--max-iterations N]` | Solve phase → diff snapshot → grade phase → JSON report + verdict. |
| `task verify-gold <bundle>` | Apply `patch.diff`, confirm f2p flips to pass and p2p holds. Proves solvability. |
| `task runs list` / `task runs show <run-id>` | Query runs from DB (rich tables). |
| `task logs <command-id>` | Show the log for any prior CLI invocation. |
| `task diff <run-id>` | Print the solver's patch for a run. |
| `task doctor` | Preflight: docker daemon, API key, disk space, git version. |
| `task clean <bundle|--run RUN_ID|--all>` | Remove containers/images/workspaces. |
| `task import-swebench <instance-id> [--split ...]` | Convert a ScaleAI/SWE-bench_Pro HF instance into a bundle. |

Global: `--db PATH` (default `~/.task-bundle/task.db`), `--artifacts-dir`, `-v/--verbose`.

---

## 6. Database schema (SQLite, stdlib `sqlite3`, thin repository layer in `db.py`)

```sql
CREATE TABLE commands (
  id          TEXT PRIMARY KEY,      -- cmd_<ulid>
  name        TEXT NOT NULL,         -- "init" | "validate" | "run" | ...
  argv        TEXT NOT NULL,         -- JSON array
  bundle_path TEXT,
  started_at  TEXT NOT NULL,         -- ISO-8601 UTC
  finished_at TEXT,
  exit_code   INTEGER,
  log_path    TEXT                   -- artifacts/<id>/command.log
);

CREATE TABLE runs (
  id            TEXT PRIMARY KEY,    -- run_<ulid>
  command_id    TEXT NOT NULL REFERENCES commands(id),
  task_id       TEXT NOT NULL,
  solver        TEXT NOT NULL,       -- "stub" | "claude"
  model         TEXT,                -- null for stub
  verdict       TEXT,                -- RESOLVED | UNRESOLVED | ERROR
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  input_tokens  INTEGER, output_tokens INTEGER, cost_usd REAL,
  image_tag     TEXT, image_digest TEXT,
  tool_versions TEXT                 -- JSON: docker/git/python/cli versions
);

CREATE TABLE test_results (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    TEXT REFERENCES runs(id),       -- null for validate-only results
  command_id TEXT NOT NULL REFERENCES commands(id),
  test_name TEXT NOT NULL,
  bucket    TEXT NOT NULL CHECK (bucket IN ('fail2pass','pass2pass')),
  phase     TEXT NOT NULL CHECK (phase IN ('baseline','post_solver','post_gold')),
  attempt   INTEGER NOT NULL DEFAULT 1,     -- 1..3 for flake detection
  status    TEXT NOT NULL CHECK (status IN ('passed','failed','error','timeout')),
  duration_seconds REAL
);

CREATE TABLE artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id TEXT NOT NULL REFERENCES commands(id),
  run_id     TEXT REFERENCES runs(id),
  type       TEXT NOT NULL,   -- solver_diff | test_output | build_log | solver_transcript | report
  path       TEXT NOT NULL
);

CREATE TABLE fleets (
  id TEXT PRIMARY KEY,                -- fleet_<config hash>
  command_id TEXT NOT NULL REFERENCES commands(id),
  config_hash TEXT NOT NULL,
  backend TEXT NOT NULL,              -- local | kubernetes
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL                -- running | completed | partial
);

CREATE TABLE fleet_jobs (
  id TEXT PRIMARY KEY,                -- stable task/config/sample key
  fleet_id TEXT NOT NULL REFERENCES fleets(id),
  run_id TEXT NOT NULL,
  bundle_path TEXT NOT NULL, task_id TEXT NOT NULL,
  solver TEXT NOT NULL, model TEXT, config_hash TEXT NOT NULL,
  sample INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, verdict TEXT, error TEXT,
  started_at TEXT, finished_at TEXT
);
```

Artifacts live under `artifacts/<command-id>/` (fleet runs use one nested directory per
run); DB stores paths, never blobs. Every CLI invocation opens with a `commands` insert
and closes with exit status + duration (via a context manager so even crashes record).

---

## 7. Module layout

```
src/task_bundle/
  cli.py          # typer app; thin — parses, delegates, renders rich output
  bundle.py       # TaskSpec pydantic models, bundle load/scaffold/state
  container.py    # Docker + Kubernetes runtimes: run, exec, cp, isolation/limits
  execution.py    # execute + persist/report one solver run
  fleet.py        # worker pool, disk guard, stable ids, pass@k
  harness.py      # bundle-aware orchestration: task images, staging, suite runs
  workspace.py    # pinned clones, cleaned-tree construction, hiding-invariant guard
  grading.py      # verdict logic (pure functions; table-tested)
  solver/
    base.py       # Solver protocol
    stub.py       # StubSolver (applies given patch or no-op)
    claude.py     # ClaudeSolver (agentic loop, capped)
  db.py           # repository layer over sqlite3
  artifacts.py    # artifact dir management
  report.py       # run report model + stable-ordered JSON emission
  errors.py       # TaskError hierarchy with actionable messages
  swebench.py     # import-swebench converter
```

Grading verdict is a pure function over `{test: (bucket, baseline_status,
post_status)}` — table-driven tests enumerate the combinations.

---

## 8. Solver design

```python
class Solver(Protocol):
    name: str
    def solve(self, ctx: SolveContext) -> SolveResult: ...
    # SolveContext: container handle, repo path, problem statement, limits
    # SolveResult: diff (unified), transcript path, token/cost stats
```

- **StubSolver**: applies a caller-provided patch file inside the workspace (or no-op).
  Deterministic harness testing; explicitly allowed by the assignment. The gold-patch
  stub run is the deterministic proof of the RESOLVED path.
- **ClaudeSolver**: agentic loop via `anthropic` SDK. Tools: `list_dir`, `read_file`,
  `write_file`, `run_command` — all executed *inside the container* via docker exec
  (never on the host). Caps: max iterations (default 30), max tokens, wall-clock
  timeout. Model from `--model` / `ANTHROPIC_MODEL`, default `claude-opus-4-7`.
  Diff captured by comparing workspace tree against baseline (host-side git, not
  in-container git — there is no `.git` in the workspace).

---

## 9. Determinism & reproducibility

- Full commit SHA required; clone is `git init + fetch <sha> --depth 1 + checkout`
  (shallow — also limits git-history recovery surface).
- Image cache key from content hash; image digest recorded in runs.
- 3× test executions in validate; tests with inconsistent status flagged FLAKY and the
  contract check fails with a specific message (per SWE-bench Pro §4.3 / Tests, §3.2).
- Run reports: `json.dumps(..., sort_keys=True, indent=2)`; ULIDs for ids (sortable,
  collision-free); all timestamps UTC ISO-8601.
- `tool_versions` recorded per run (docker, git, python, package version).
- Residual nondeterminism: LLM sampling, dependency resolution at build time unless
  base images/lockfiles are pinned — documented, with digest-pinning recommended.

---

## 10. Milestones

1. Bundle spec + `task init` (scaffold, clone, no docker yet → docker build lands here
   too if tractable; smoke test).
2. Container build + `task validate` (staging mechanism, 3× flake detection).
3. SQLite logging + `task logs` / `task runs`.
4. `task run` with StubSolver (two-phase run, grading, JSON report).
5. `task run` with ClaudeSolver.
6. End-to-end on a real SWE-bench Pro instance (`task import-swebench`).
7. Extra commands (`verify-gold`, `diff`, `doctor`, `clean`).
8. Polish: README, DESIGN_NOTES.md, CI, final checklist.

Each milestone: verified by hand, one conventional commit, summary + test commands,
then stop for review.
