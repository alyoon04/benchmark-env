# Progress

## Milestone checklist

- [x] 0. Design docs (DESIGN.md, PROGRESS.md) + spec sanity check with reviewer
- [x] 1. Bundle spec + `task init` (scaffold, clone at pinned SHA)
- [x] 2. Container build + `task validate` (image build + smoke test, hidden-test staging, 3× flake detection)
- [x] 3. SQLite logging + `task logs` / `task runs list|show`
- [x] 4. `task run` with StubSolver (two-phase run, grading, JSON report)
- [ ] 5. `task run` with ClaudeSolver (agentic loop, capped) ← **next**
- [ ] 6. End-to-end on real SWE-bench Pro instance (`task import-swebench`)
- [ ] 7. Extra commands: `verify-gold`, `diff`, `doctor`, `clean`
- [ ] 8. Polish: README, DESIGN_NOTES.md, CI, final checklist

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

## Current state

Milestone 4 complete and hand-verified: `task run --solver stub [--patch F|--gold]`
runs baseline -> solve -> grade in separate containers, enforces the leak guard on
the solver workspace, captures the diff via host-side git snapshot, grades with
SWE-bench semantics (run_verdict table-tested), writes a sorted-key report.json +
solver.diff/transcript artifacts, and records runs/test_results rows. Gold patch ->
RESOLVED, no-op -> UNRESOLVED proven in docker tests and by hand on examples/toy-calc.
91 tests, ruff + mypy --strict clean. Awaiting approval before milestone 5.

Modules added: `solver/` (base protocol, stub), `run.py` (two-phase orchestration,
DB-free), `report.py` (tool versions + deterministic JSON). Shared docker test
fixtures moved to conftest (session-scoped origin).

## Next steps (milestone 5)

1. `solver/claude.py`: agentic loop via anthropic SDK — tools list_dir/read_file/
   write_file/run_command executed against the solver workspace + a solver container
   (network off); caps on iterations/tokens/wall-clock.
2. `--solver claude --model ...` wiring (env ANTHROPIC_MODEL, default claude-opus-4-7).
3. Token/cost accounting into runs row + report stats.
4. Requires ANTHROPIC_API_KEY from the user at runtime.
