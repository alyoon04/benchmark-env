# Design Notes

`task-bundle` is a CLI (`task`) that packages SWE-bench-style coding tasks as portable
**bundles**, builds containerized environments for them, validates the baseline test
contract, runs LLM solvers against them in isolation, and grades the results with
SWE-bench semantics — logging every command to a queryable SQLite database.

This document explains *what* was built and *why*. It is the distilled, standalone
record of the design; `DESIGN.md` is the working scratchpad it was drawn from, and
`PROGRESS.md` carries the full chronological decision log.

---

## 1. Goals

1. **Correct SWE-bench grading.** A solver attempt is `RESOLVED` iff every fail2pass
   (f2p) test passes after the attempt **and** every pass2pass (p2p) test still passes.
2. **Structural test-hiding.** The solver can never see the grading tests — enforced by
   construction, not by convention.
3. **A language-agnostic engine.** Test commands are *data* in `task.json`; nothing in
   the engine assumes pytest, Python, or any particular framework.
4. **Isolation.** Solver code runs non-root, network-off, resource-limited, with no path
   to the host filesystem.
5. **Reproducibility.** Pinned commits, content-addressed images, flaky-test detection,
   recorded tool versions, deterministic JSON reports.
6. **Observability.** Every invocation is recorded; artifacts (diffs, test output,
   transcripts, build logs, reports) live on disk and are referenced by path.
7. **Scalable execution.** Fleets run task/sample pairs concurrently with bounded
   container admission, disk backoff, durable resume, pass@k, and a Kubernetes runtime.

Non-goals (future work): hosted control-plane operation, automatic cluster provisioning,
and warm-container reuse across independent solver attempts.

---

## 2. Bundle anatomy

A bundle is a self-contained directory describing one task: `task.json` (pydantic-
validated `TaskSpec`), `description.md` (the problem statement shown to the solver),
`patch.diff` (the golden patch), the hidden tests, and a gitignored `.task/` holding
the pinned-commit clone and tool state. The full schema is in the README; two design
points matter here.

**Test commands are data.** `tests.command_template` (e.g. `python -m pytest {test_path}
-q`) is substituted once per staged test path and run; pass/fail comes from the process
exit code, not from parsing framework output. This is what keeps the engine language-
agnostic — `go test {test_path}`, `npx jest {test_path}`, and `cargo test` all work
unchanged — at the cost of one container-exec per test path, which is fine at task scale.

**Two hidden-test formats, exactly one per bundle.**

- **Directories** (`tests/fail2pass/`, `tests/pass2pass/`): hidden test files authored
  alongside the bundle. The native format.
- **Test patch** (`tests/test_patch.diff` + explicit `fail2pass_ids` / `pass2pass_ids`):
  the SWE-bench compatibility format, where the hidden tests are *modifications to
  existing repo test files* plus the node ids to run. This is what real SWE-bench Pro
  instances use, and what `task import-swebench` produces.

`TaskSpec` accepts either; `Bundle.test_format()` resolves which (and refuses a bundle
that defines both, or neither).

---

## 3. The test-hiding invariant (core correctness property)

**Invariant: no hidden-test content ever enters the solver's container or any image
layer.**

Enforcement is *structural* — the design removes the opportunity to leak rather than
relying on a cleanup step:

1. **Construct-clean, never copy-then-delete.** A native bundle's image is built from a
   copy of the baseline clone with hidden paths *excluded from the copy*. There is nothing
   to delete, so nothing can linger in an image layer. For the test-patch format, the
   *baseline* versions of the files the patch touches stay visible — that is exactly the
   repo at the pinned commit, which is what a real solver would see — while the patch
   itself and the *patched* versions are hidden.
2. **`.git` is scrubbed** from the solver's tree. The solver gets a plain directory tree,
   never a repo whose history could contain future commits. Native images are built from
   a `.git`-free copy; prebuilt images ship a full-history clone (openlibrary's carries
   16k commits) and its `.git` — and every submodule's — is removed at build time. The
   orchestrator's own clone stays on the host. (It is also shallow — `git init` + `fetch
   --depth 1 <sha>` — as defense in depth against history recovery.)
3. **Hidden tests are never in any image.** They are staged via `docker cp` into a
   *running* evaluation container only, after the solver's changeset has been captured.
4. **A content leak guard runs pre-flight.** Before the solver starts, the solve
   container's repo dir is hashed (the same manifest that later yields the changeset) and
   the run aborts if any file's sha256 equals a hidden blob's. The comparison is on
   *contents*, not names: a same-named test file with different bytes is legitimate (p2p
   tests are often modified versions of existing files), while identical bytes under any
   name is a leak. Because the guard reads the *running container*, it covers whatever a
   prebuilt base image happened to ship, not just what the engine copied in.

The tool's own test suite covers this invariant at both levels: the pure guard
(`test_workspace.py`, including the same-name-different-bytes case) and an end-to-end
run that plants a byte-identical hidden test under an innocent name and must abort with
exit 3 before any solver runs (`test_run_docker.py`).

---

## 4. Two-phase run and container lifecycle

A `task run` is two structurally separated phases sharing **one task image**:

```
baseline  →  solve in place  →  (manifest diff = changeset)  →  grade
```

- **Baseline** — a fresh container; hidden tests staged; suites run once to record each
  test's "before" status.
- **Solve** — a fresh container *is* the solver's workspace: its repo dir is the image's
  own tree (`.git`-free, leak-guard verified), and the solver mutates it in place.
  Network is **off**. The orchestrator hashes every file before and after (`find` +
  `sha256sum`, run as root so nothing can hide); the difference — modified, added,
  deleted — filtered through the repo's own `.gitignore` (plus a tiny bytecode/cache
  hygiene list) is the **changeset**. Only the changed files are copied out.
- **Grade** — a *fresh* evaluation container gets the changeset replayed into its repo
  dir (changed and added files copied in, deleted files removed), hidden tests staged via
  `docker cp`, and suites run once for the "after" status. The solver never observes
  this container. The unified diff in the report is rendered host-side from the grade
  container's pristine versions of the changed paths versus the solver's.

**One image serves every phase.** Because hidden tests are staged identically at validate
and grade time, **validation exercises the exact grading path** — there is no separate,
untested grading code.

**Grade applies the changeset in place, never a clean clone.** This is the property that
makes prebuilt images gradeable. Dependencies routinely live *under* the repo directory
but outside git — submodule checkouts (openlibrary's `vendor/infogami`), `node_modules`,
compiled extensions, module caches — and an earlier design that swapped a clean clone in
for grading discarded all of them (the auto-`verify-gold` guard caught this and refused
those instances). Replaying only what the solver changed onto the image's complete tree
keeps everything else exactly as the image shipped it. It also needs no `git` or `patch`
inside the image (plain file transport is language-agnostic) and handles deletions.
Solvers never produce a patch themselves — the orchestrator derives the changeset
uniformly for stub and LLM solvers alike (the stub applies its patch host-side to a
sparse copy of the touched files and pushes them in through the same transport).

**Baseline suites are re-run inside `task run`** rather than trusting a prior `validate`,
so every report carries an honest before/after for that exact run.

---

## 5. Isolation and security model

Solve-phase containers run with:

```
--network none --user 1000:1000 --memory 4g --cpus 2 --pids-limit 512
--cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER
--security-opt no-new-privileges
```

The three re-added capabilities are needed only by **root-user orchestrator execs** that
stage hidden tests into directories owned by the sandbox uid; the solver itself runs as
uid 1000 and `no-new-privileges` blocks it from gaining anything. Each attempt gets a
**fresh container**, so state mutated by one run (caches, temp files) cannot mask or cause
flakiness in the next.

**Network is on at build time, off at run time.** Dependency installs need the network;
the solver must get no exfiltration or cheat channel. This is the one deliberate
tradeoff, documented here rather than hidden.

The ClaudeSolver's tools (`list_dir`, `read_file`, `search`, `edit_file`, `write_file`,
`run_command`) all execute *inside* the hardened solve container via `docker exec` —
solver-controlled code never touches the host, and only the changed files ever leave the
container (as data, for grading and the diff). Model file paths are confined by a
`normpath`-under-the-repo-dir check on top of the container boundary, and the loop is
bounded by max-iterations, a wall-clock timeout, a context-token budget, per-call token
caps, and tool-output truncation.

**The agent loop is built to be measured.** Requests are rendered for prompt-cache
stability (tools → frozen system prompt → messages, one top-level `cache_control`
breakpoint) and cache reads/writes are priced separately, so cost per solve is honest
rather than list-price. Adaptive thinking with a per-run `--effort` is the cost/quality
lever. Failures that survive the SDK's retries (network, rate limits, 5xx), refusals, and
truncation end the solve gracefully — the edits made so far are still graded and the
reason lands in the report's `stop_reason` — because on a fleet of hundreds of runs a
crashed run is a lost data point. Every step is recorded as a structured trajectory
(`trajectory.jsonl`: exact tool inputs, exact outputs the model saw, per-step usage) next
to the human-readable transcript; that is the unit of analysis for failure taxonomies,
ablations, and training data. `edit_file` is an exact-match single replacement that
refuses zero or multiple matches, the tool shape that keeps models from rewriting whole
files.

---

## 6. Determinism and reproducibility

- **Pinned commits.** Full 40-char SHA required; short SHAs can drift.
- **Content-addressed images.** The tag's cache key is `sha256(repo, commit, base_image,
  setup_commands, env, excludes)`, so a repeat `init` is a no-op and the image digest is
  recorded per run.
- **Flake detection.** `validate` runs each suite 3× in fresh containers; any test with an
  inconsistent status is flagged `FLAKY` and fails the contract with a specific message
  (per SWE-bench Pro §4.3).
- **Deterministic outputs.** Reports are `json.dumps(..., sort_keys=True, indent=2)`; ids
  are time-ordered (ms-hex timestamp + random suffix, sortable and collision-safe, no
  external ulid dep); all timestamps are UTC ISO-8601.
- **Recorded provenance.** Each run stores `tool_versions` (docker, git, python, CLI).

Residual nondeterminism — LLM sampling, and dependency resolution at build time unless
base images / lockfiles are pinned — is inherent; digest-pinning the base image is the
recommended mitigation.

---

## 7. Observability

Every CLI invocation opens with a `commands` row (id, name, argv, bundle, start time) and
closes with its exit code and duration via a context manager, so even a crash leaves a
queryable record. Solver runs add a `runs` row (verdict, model, tokens, cost, image
digest, tool versions); per-test outcomes land in `test_results` (one row per test, per
attempt, per phase — so flake patterns stay visible); and artifact *paths* (never blobs)
go in `artifacts`. Files live under `artifacts/<command-id>/`.

`task logs`, `task runs list|show`, and `task diff` read this back without re-running
anything. `task verify-gold` records test results on its command but **no run row** — it
is an authoring check (a sibling of `validate`), so it stays out of the solver-performance
table.

---

## 8. Architecture

```
src/task_bundle/
  cli.py        # typer app: parse, delegate, render — thin
  bundle.py     # TaskSpec/BundleState pydantic models; load/scaffold/state
  container.py  # docker CLI wrapper: build, run, exec, cp, image list, limits
  harness.py    # bundle-aware orchestration: images, manifests/changesets, staging, suites
  workspace.py  # pinned clones, sparse patch materialization, changeset math, diff, leak guard
  grading.py    # pure verdict logic (consolidate, run_verdict, contract checks)
  solver/
    base.py     # Solver protocol, SolveContext / SolveResult
    stub.py     # StubSolver (applies a patch, or no-op)
    claude.py   # ClaudeSolver (capped agentic loop, tools exec in-container)
  db.py         # sqlite3 repository layer
  report.py     # run-report model + stable-ordered JSON emission
  swebench.py   # import-swebench converter (HuggingFace -> bundle)
  errors.py     # TaskError hierarchy with actionable messages + exit codes
```

Three boundaries do the heavy lifting. **`grading.py` is pure** — the verdict is a function
over test records, so the SWE-bench semantics are table-tested exhaustively without any
I/O. **`workspace.py`'s changeset pieces are pure too** — manifest parsing, changeset
computation, `.gitignore` filtering, and diff rendering are functions over plain
directories and dicts, so the solve → changeset → diff chain is unit-tested without a
container (including a round-trip proof that the rendered diff `git apply`s back to the
solver's tree). **`run.py` is DB-free** — it returns a `RunOutcome` and the CLI persists
it, so the full pipeline is testable without sqlite and there is a single write path.

The docker engine is driven through the **CLI via subprocess**, not the docker SDK: one
fewer heavy dependency, and failures surface the exact command. Predictable failures raise
a `TaskError` subclass that the CLI renders as a friendly message with an actionable
exit code (`2` = contract/solvability violation, `3` = hidden-test leak); raw tracebacks
are reserved for genuine bugs.

---

## 9. Command surface

| Command | Purpose |
|---|---|
| `init` | Scaffold, clone at the pinned commit, build + smoke-test the task image. |
| `validate` | Confirm the baseline contract (p2p pass, f2p fail), 3× for flake detection. |
| `run` | Baseline → solve → grade; emit verdict, before/after table, JSON report. |
| `fleet` | Concurrent, resumable task/sample runs with disk gating and pass@k. |
| `verify-gold` | Prove solvability: apply `patch.diff`, confirm f2p flips and p2p holds. |
| `import-swebench` | Convert a public SWE-bench Pro instance into a ready-to-validate bundle. |
| `diff` | Print a run's stored solver patch. |
| `logs` / `runs list\|show` | Query recorded commands and runs. |
| `doctor` | Preflight: Docker daemon, git, API key, disk. |
| `clean` | Reclaim disk (images / workspaces / artifacts) with a confirmation prompt. |

`verify-gold` is intentionally stricter than the run verdict: a fail2pass test that was
*already* passing on baseline is flagged, because the golden patch then proves nothing —
the whole point is to show the patch is what makes the task solvable.

---

## 10. Known limitations and future work

- **SWE-bench import relies on the image's install seeing the repo tree.** Prebuilt
  instance images keep the repo at their configured `WorkingDir`; the engine **discovers
  that path from the image** (never hardcodes `/app`, never touches `/`) and the solver
  edits that tree in place, so editable installs, path dependencies, submodules and
  `node_modules` all resolve exactly as they did when the image was built. The
  irreducible limit is a *non-editable* install: it imports from site-packages regardless
  of the tree, so the solver's edits would be invisible — unfixable without a
  network/language-specific reinstall. **`import-swebench` runs `verify-gold`
  automatically** so this fails loudly at import (a gold patch that can't flip f2p)
  instead of silently grading every solver `UNRESOLVED`.
- **Base-image content cannot be structurally excluded, only removed.** For prebuilt
  images, `workspace_excludes` become `rm -rf` steps in the task image (the bytes remain
  in the base layer, though not in the running container). The leak guard still checks
  the running container, so an exclude that failed to remove hidden content aborts.
- **Changesets track regular files.** Symlink creation/retargeting and mode-only changes
  (e.g. `chmod +x`) by a solver are not captured; grading tests rarely depend on either.
  A `.gitignore`d path the solver edits is dropped from the changeset by design (the
  same semantics as `git add -A`).
- **Per-test exec granularity.** One container-exec per test path is simple and language-
  agnostic but slower than a single batched invocation. Fine at task scale; a batched mode
  is possible if throughput ever matters.
- **No cross-run warm-container pool.** `task fleet` provides bounded local/Kubernetes
  concurrency, crash resume, and pass@k, but each grading phase still starts from a fresh
  task image. That preserves isolation while the apply-diff-in-place grading redesign is
  underway; safe warm reuse belongs after that boundary is stable.
- **`clean --all` deletes machine-global images.** Docker images are not namespaced per
  bundle run, so `--all` removes every `task-bundle/*` image; hence the confirmation
  prompt and the per-bundle / per-run targeting options.

---

## 11. End-to-end evidence

`evaluation/ansible-combine-vars/` holds a full run on a real SWE-bench Pro instance
(ansible/ansible, 1 fail2pass + 15 pass2pass): `validate` holds 3× consistent, the gold
patch grades `RESOLVED`, a no-op grades `UNRESOLVED`, `verify-gold` confirms solvability,
and a live Claude solve grades `RESOLVED` with a fix that differs from the gold patch.

`evaluation/multi-instance/` is the cross-repo sweep, run before and after grading moved
to solve-in-place. The instance that previously failed with `ModuleNotFoundError:
infogami` (a submodule under the repo dir that clone-swap discarded) now verifies: its
fail2pass test fails on baseline with the *actual* bug and passes after the gold patch. A
second openlibrary instance (59 tests) grades gold → `RESOLVED` and no-op → `UNRESOLVED`.
The Go instance gets past both of its original failure modes and executes real `go test`
runs; what still blocks it on this arm64 host is the amd64 Go toolchain segfaulting under
qemu, reproduced outside the harness. The committed reports and logs are the deterministic
proof that the grading pipeline is correct end-to-end on data the engine had never seen
during development.
