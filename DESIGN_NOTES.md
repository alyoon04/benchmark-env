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

Non-goals (future work): parallel/fleet execution, remote backends (k8s/Modal),
multi-attempt orchestration (pass@k).

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

**Invariant: no hidden-test content ever enters the solver's workspace or image.**

Enforcement is *structural* — the design removes the opportunity to leak rather than
relying on a cleanup step:

1. **Construct-clean, never copy-then-delete.** The solver workspace is built by copying
   the baseline clone with hidden paths *excluded from the copy*. There is nothing to
   delete, so nothing can linger in an image layer. For the test-patch format, the
   *baseline* versions of the files the patch touches stay visible — that is exactly the
   repo at the pinned commit, which is what a real solver would see — while the patch
   itself and the *patched* versions are hidden.
2. **`.git` is scrubbed** from the solver workspace. The solver gets a plain directory
   tree, never a repo whose history could contain future commits. The orchestrator keeps
   its own host-side checkout for diffing. (Clones are also shallow — `git init` + `fetch
   --depth 1 <sha>` — as defense in depth against history recovery.)
3. **Hidden tests are never in any image.** They are staged via `docker cp` into a
   *running* evaluation container only, after the solver's diff has been captured.
4. **A content leak guard runs pre-flight.** Before the solver starts, the cleaned tree
   is scanned and aborts if any file is byte-identical to a hidden blob. The comparison
   is on *contents*, not names: a same-named test file with different bytes is legitimate
   (p2p tests are often modified versions of existing files), while identical bytes under
   any name is a leak.

The tool's own test suite covers this invariant with fixture bundles (`test_workspace.py`),
including the subtle case where a same-named file is fine but byte-identical content is not.

---

## 4. Two-phase run and container lifecycle

A `task run` is two structurally separated phases sharing **one task image**:

```
baseline  →  solve  →  (snapshot diff)  →  grade
```

- **Baseline** — a fresh container; hidden tests staged; suites run once to record each
  test's "before" status.
- **Solve** — a cleaned workspace tree (hidden tests excluded, `.git` removed, leak-guard
  verified) is handed to the solver, which mutates it. The orchestrator snapshots the tree
  before and diffs after. Network is **off**.
- **Grade** — a *fresh* evaluation container gets the solver's tree overlaid onto
  `/workspace`, hidden tests staged via `docker cp`, and suites run once for the "after"
  status. The solver never observes this container.

**One image, built from the cleaned tree, serves every phase.** Because the image build
context is the cleaned tree and hidden tests are staged identically at validate and grade
time, **validation exercises the exact grading path** — there is no separate, untested
grading code.

**Grade applies the solver's work by wipe-then-overlay, not by patching.** The eval
container's `/workspace` is emptied and the solved tree copied in. This needs no `git` or
`patch` inside the image (language-agnostic) and correctly handles files the solver
*deleted*. Solvers therefore never need to produce a patch themselves — the orchestrator
derives the diff uniformly for stub and LLM solvers alike.

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

The ClaudeSolver's tools (`list_dir`, `read_file`, `write_file`, `run_command`) all
execute *inside* the hardened container via `docker exec` — solver-controlled code never
touches the host. The workspace is synced out only after the loop, for diffing. Model file
paths are confined by a `normpath`-under-`/workspace` check on top of the container
boundary, and the loop is bounded by max-iterations, a wall-clock timeout, per-call token
caps, and tool-output truncation.

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
  harness.py    # bundle-aware orchestration: images, staging (both formats), suites
  workspace.py  # pinned clones, cleaned-tree construction, diffing, leak guard
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

Two boundaries do the heavy lifting. **`grading.py` is pure** — the verdict is a function
over test records, so the SWE-bench semantics are table-tested exhaustively without any
I/O. **`run.py` is DB-free** — it returns a `RunOutcome` and the CLI persists it, so the
full pipeline is testable without sqlite and there is a single write path.

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

- **SWE-bench import relies on an editable install.** Prebuilt instance images install
  the repo *editable* at their configured `WorkingDir` (a finder maps `import pkg` to an
  absolute path under it). The engine **discovers that path from the image** (never
  hardcodes `/app`, never touches `/`) and symlinks it to the engine's clean
  `/workspace`, so tests import the code the solver edits while the solver still works in
  an artifact-free tree (keeping diff capture clean). The irreducible limit is a
  *non-editable* install: it imports from site-packages regardless of path, so the
  solver's edits would be invisible — unfixable without a network/language-specific
  reinstall. **`import-swebench` runs `verify-gold` automatically** so this fails loudly
  at import (a gold patch that can't flip f2p) instead of silently grading every solver
  `UNRESOLVED`. Per-language reinstall support is future work.
- **Per-test exec granularity.** One container-exec per test path is simple and language-
  agnostic but slower than a single batched invocation. Fine at task scale; a batched mode
  is possible if throughput ever matters.
- **No remote/parallel execution.** Single task, single machine, one attempt — by design;
  fleet and pass@k orchestration are out of scope.
- **`clean --all` deletes machine-global images.** Docker images are not namespaced per
  bundle run, so `--all` removes every `task-bundle/*` image; hence the confirmation
  prompt and the per-bundle / per-run targeting options.

---

## 11. End-to-end evidence

`evaluation/ansible-combine-vars/` holds a full run on a real SWE-bench Pro instance
(ansible/ansible, 1 fail2pass + 15 pass2pass): `validate` holds 3× consistent, the gold
patch grades `RESOLVED`, a no-op grades `UNRESOLVED`, and `verify-gold` confirms
solvability. The committed reports are the deterministic proof that the grading pipeline
is correct end-to-end on data the engine had never seen during development.
