# Progress

## Milestone checklist

- [x] 0. Design docs (DESIGN.md, PROGRESS.md) + spec sanity check with reviewer
- [x] 1. Bundle spec + `task init` (scaffold, clone at pinned SHA)
- [x] 2. Container build + `task validate` (image build + smoke test, hidden-test staging, 3× flake detection)
- [ ] 3. SQLite logging + `task logs` / `task runs list|show` ← **next**
- [ ] 4. `task run` with StubSolver (two-phase run, grading, JSON report)
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

## Current state

Milestone 2 complete and hand-verified: `task init` now builds + smoke-tests the
task image (content-hash cached); `task validate` stages hidden tests via docker cp
into fresh hardened containers, runs each suite 3×, flags flakes, and reports
contract violations with specific messages (exit 2). Example bundle
`examples/toy-calc` validates end-to-end offline. 64 tests (3 docker-marked,
auto-skip without a daemon), ruff + mypy --strict clean. Awaiting reviewer approval
before milestone 3.

Modules: `bundle.py`, `workspace.py` (+clean-tree builder, content leak guard),
`container.py` (docker primitives), `harness.py` (image/staging/suite orchestration),
`grading.py` (pure consolidate + contract check), `cli.py` (`init`, `validate`),
`errors.py`.

## Next steps (milestone 3)

1. `db.py`: sqlite3 repository layer (commands/runs/test_results/artifacts per DESIGN.md §6).
2. Command-logging context manager wired into every CLI command (records even on crash).
3. `artifacts.py`: artifact dirs under `artifacts/<command-id>/` (build log, test output).
4. `task logs <command-id>`, `task runs list`, `task runs show <run-id>` with rich tables.
