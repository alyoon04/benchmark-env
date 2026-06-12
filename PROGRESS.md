# Progress

## Milestone checklist

- [x] 0. Design docs (DESIGN.md, PROGRESS.md) + spec sanity check with reviewer
- [x] 1. Bundle spec + `task init` (scaffold, clone at pinned SHA)
- [x] 2. Container build + `task validate` (image build + smoke test, hidden-test staging, 3× flake detection)
- [x] 3. SQLite logging + `task logs` / `task runs list|show`
- [ ] 4. `task run` with StubSolver (two-phase run, grading, JSON report) ← **next**
- [ ] 5. `task run` with ClaudeSolver (agentic loop, capped)
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

## Current state

Milestone 3 complete and hand-verified: every CLI invocation writes a `commands`
row (argv, timestamps, exit code — recorded even on crash via context-manager
`finally`) and prints its command id. `task validate` persists per-attempt
test_results rows and a full test-output artifact; `task init` saves the image
build log. `task logs` lists recent commands or shows one command's metadata,
test-results table, artifacts, and on-disk log. `task runs list|show` are wired
(runs rows arrive with `task run` in M4). DB schema includes the full `runs`
table + repository methods ready for M4. 74 tests, ruff + mypy --strict clean.
Awaiting reviewer approval before milestone 4.

Modules: `bundle.py`, `workspace.py`, `container.py`, `harness.py`, `grading.py`,
`db.py` (sqlite repository), `cli.py` (`init`, `validate`, `logs`, `runs`), `errors.py`.

## Next steps (milestone 4)

1. `solver/base.py` Solver protocol + `solver/stub.py` (applies provided patch or no-op).
2. Two-phase `task run`: solver workspace (hidden tests absent, leak guard pre-flight)
   → diff snapshot → fresh eval container → staged hidden tests → grade.
3. `grading.py`: run verdict (RESOLVED ⇔ all f2p pass AND all p2p pass post-solver).
4. `report.py`: stable-ordered JSON report; runs/test_results/artifacts rows per DESIGN.md.
