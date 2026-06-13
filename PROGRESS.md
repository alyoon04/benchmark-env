# Progress

## Milestone checklist

- [x] 0. Design docs (DESIGN.md, PROGRESS.md) + spec sanity check with reviewer
- [x] 1. Bundle spec + `task init` (scaffold, clone at pinned SHA)
- [x] 2. Container build + `task validate` (image build + smoke test, hidden-test staging, 3× flake detection)
- [x] 3. SQLite logging + `task logs` / `task runs list|show`
- [x] 4. `task run` with StubSolver (two-phase run, grading, JSON report)
- [x] 5. `task run` with ClaudeSolver (agentic loop, capped)
- [x] 6. End-to-end on real SWE-bench Pro instance (`task import-swebench`)
- [x] 7. Extra commands: `verify-gold`, `diff`, `doctor`, `clean`
- [x] 8. Polish: README, DESIGN_NOTES.md, CI, final checklist

## Decisions made

| Decision | Rationale |
|---|---|
| Hidden tests staged via `docker cp` into running containers only; never in any image | Structural test-hiding: nothing to delete, no layer leakage |
| One task image serves solve/validate/grade phases; built from cleaned tree | Validation exercises the exact grading path |
| `.git` scrubbed from solver workspace; orchestrator diffs host-side | Solver can't recover hidden tests from history; shallow clone as defense-in-depth |
| Test command is a data template (`command_template` + `{test_path}`), one exec per test path, pass/fail from exit code | Language-agnostic engine; no pytest assumptions |
| Bundle supports two test formats: native dirs (f2p/p2p) OR SWE-bench test_patch + test ids | Real SWE-bench Pro instances modify existing test files; both must work |
| SQLite via stdlib + thin repository layer; artifacts on disk, paths in DB | Zero-config, queryable, no blobs |
| ULIDs for ids, sorted-key JSON, UTC ISO timestamps | Deterministic, sortable outputs |
| Build-time network ON, run-time network OFF | Deps need fetching; solver gets no exfil/cheat channel |
| Docker image build moved from M1 (`init`) into M2 | Keeps M1 reviewable; `init` will build+smoke-test the image once container.py exists |
| Clone = `git init` + `fetch --depth 1 <sha>` + detached checkout | Pin to exact SHA; shallow history is defense-in-depth for test hiding |
| CLI `main()` catches TaskError → friendly stderr + exit code; raw tracebacks only for real bugs | UX bar: predictable failures must be actionable, not stack traces |
| docker CLI via subprocess, not the docker SDK | One fewer heavy dep; failures surface the exact command; `harness.py` added to module layout for bundle-aware orchestration above the raw wrapper |
| Containers: `--cap-drop ALL` + re-add only CHOWN/DAC_OVERRIDE/FOWNER | Root-user orchestrator execs need them to stage hidden tests; solver runs as uid 1000 with no-new-privileges so it gains nothing |
| Fresh container per validate attempt | State mutated by one run (caches/temp files) can't fake or mask flakiness in the next |
| Smoke test = container starts + can exec; real test-runner proof is `task validate` | Test command is opaque data; there is no generic "dry-run" flag across frameworks |
| Example bundle repo = deterministic local origin (`examples/make_toy_origin.py`, fixed commit env → same SHA everywhere) + bundle-relative repo URLs | Committed example must be validatable offline on any machine without machine-specific paths |
| Leak guard compares file *contents*, not names | Same-named test file with different content is legitimate (SWE-bench p2p); identical bytes under any name is a leak |
| SWE-bench test-patch format: schema accepts it, but `validate` rejects it until M6 | Can't exercise it honestly without a real instance; clear error instead of untested code |
| Every command wrapped in `record_command` context manager (insert at start, exit code in `finally`) | Crashes still leave a queryable row; ids printed after every command |
| DB/artifacts default to `~/.task-bundle/`, overridable via `--db`/`--artifacts-dir` or `TASK_BUNDLE_DB`/`TASK_BUNDLE_ARTIFACTS` env | Tests isolate via env (autouse fixture); collaborators share one machine-local DB by default |
| Ids = ms-hex timestamp + random suffix (no ulid dep) | Time-sortable, collision-safe, zero deps |
| `artifacts.py` folded into `CommandRecord.save_artifact` + `db.add_artifact` | Too small to justify a module; DESIGN.md layout deviation noted here |
| Per-attempt rows in test_results (not just consolidated) | Flake patterns are visible later via `task logs <id>` |
| Solvers mutate a workspace tree; orchestrator snapshots (host-only .git) and diffs | Solvers never produce patches; uniform diff capture for stub and LLM solvers |
| Grade phase = wipe /workspace + docker-cp the solved tree (no in-container patch tooling) | Language-agnostic (no git/patch needed in image); handles file deletions; `cp src/.` string preserved (pathlib strips `/.` — caused a real bug) |
| Baseline suites re-run once inside `task run` | Honest per-test before/after in one report without trusting stale validate state |
| Completed run exits 0 regardless of verdict | UNRESOLVED is data, not a CLI failure; ERROR verdict + nonzero exit reserved for infra failures |
| run.py is DB-free; CLI persists RunOutcome | Orchestration testable without sqlite; single write path |
| ClaudeSolver tools (list_dir/read_file/write_file/run_command) all execute inside the hardened container; host workspace synced out only after the loop | Solver-controlled code never touches the host; isolation story is uniform |
| write_file via host tempfile + docker cp, not shell heredoc | No quoting pitfalls with arbitrary model content |
| Model paths confined by _safe_path (normpath under /workspace, .. rejected) | Tool layer tidiness on top of container confinement |
| ClaudeSolver budgets: max_iterations (30), wall-clock (1800s), per-call max_tokens, tool-output truncation | Bounded cost/time even on runaway loops |
| API client injected via protocol; scripted fake drives the real-container docker test | Full loop tested deterministically; live API only for hand verification |
| TEST_PATCH staging: patch applied host-side to a clean baseline copy, only the changed files docker-cp'd in; staged refs are the bundle's explicit test ids | Patch tooling never required inside the image; nothing hidden enters a layer |
| TEST_PATCH solver visibility: baseline versions of patch-touched files stay visible; hidden = the patch + the *patched* file versions (leak guard now compares byte blobs, not files) | Real SWE-bench semantics: the repo at the pinned commit is exactly what the solver gets |
| import-swebench fetches rows via the HF datasets-server JSON API (filter endpoint, row-scan fallback incl. on HTTP 500) | No heavyweight `datasets` dep; the filter endpoint 500s intermittently |
| Imported bundles use the prebuilt instance image + `rm -rf /app && ln -s /workspace /app` setup command + `HOME=/tmp` env | Deps in the image are installed against /app (editable); the symlink makes tests import the /workspace tree the solver edits — engine stays generic, fix is bundle data |
| `run_detached` pins `--entrypoint sleep` | sweap images set `ENTRYPOINT ["/bin/bash"]`, which mangled the idle command into `bash sleep infinity` |
| `_git` sets `GIT_CEILING_DIRECTORIES` to cwd's parent on every invocation | A foreign enclosing repo (e.g. a git-managed $HOME) made `git apply` silently skip every patch path and exit 0 — numstat returned nothing, staging no-opped, and the f2p test "passed" on baseline |
| `docker info` timeout treated as "daemon available" (ensure_available + test skip probe) | A large pull in flight can make the daemon slow to answer info while still serving runs |
| `verify-gold` reuses the run pipeline (gold StubSolver) but records NO run row — only command + per-phase test_results (baseline/post_gold) | It's an authoring check (sibling to `validate`), not a solver attempt; keeping it out of `runs list` keeps that table about solver performance |
| `check_gold_contract` is stricter than `run_verdict`: an f2p that was already green is a problem even though the verdict is still RESOLVED | verify-gold proves the *patch* is what solves the task; an already-passing f2p proves nothing |
| `diff`/`doctor`/`clean` are not wrapped in `record_command` (open DB directly or not at all) | They're query/ops commands like `logs`/`runs`; `clean --all` would also delete the artifact dir it just created under record_command |
| `clean` always previews the removal set and prompts (skip with `--yes`); never deletes the SQLite DB | Destructive op — confirmation is the guardrail; history must survive a disk reclaim |
| `clean` tests use a unique bundle id and exercise `--all` only via the abort path | Docker images are machine-global, not test-isolated; a real `--all` deletion would nuke the dev's other images |

## Current state

All eight milestones complete. The full surface — `init`, `validate`, `run`
(stub + claude), `verify-gold`, `import-swebench`, `diff`, `logs`, `runs`,
`doctor`, `clean` — is implemented, tested, and documented. `DESIGN_NOTES.md` is
the distilled final design deliverable; `.github/workflows/ci.yml` runs ruff +
mypy + the non-docker suite on a 3.11/3.12 matrix. User-facing docs and CLI
messages were swept of stale milestone/"planned" references. 124 tests
(12 docker-marked), ruff + mypy --strict clean.

## Possible follow-ups (out of original scope)

- Live ClaudeSolver hand-test against a real SWE-bench Pro instance (only stub
  proven end-to-end on the real instance so far).
- Per-language `import-swebench` install handling (drop the editable-`/app`
  assumption); the guard note lives next to `setup_commands` in `swebench.py`.
- Batched test execution (one exec for many test paths) if throughput matters.
- Digest-pin example base images for fully reproducible builds.
