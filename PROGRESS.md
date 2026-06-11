# Progress

## Milestone checklist

- [ ] 0. Design docs (DESIGN.md, PROGRESS.md) + spec sanity check with reviewer ← **current**
- [ ] 1. Bundle spec + `task init` (scaffold, clone at pinned SHA, image build, smoke test)
- [ ] 2. Container build + `task validate` (hidden-test staging, 3× flake detection)
- [ ] 3. SQLite logging + `task logs` / `task runs list|show`
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

## Current state

Design docs written. Awaiting reviewer sanity check on bundle spec (DESIGN.md §2)
and DB schema (DESIGN.md §6) before implementing milestone 1.

## Next steps

1. Get approval on spec + schema.
2. Milestone 1: `uv init`, pyproject with typer/rich/pydantic/pytest/ruff, `bundle.py`
   TaskSpec models, `task init` (scaffold + clone + image build + smoke test), fixture
   toy repo under `tests/fixtures/`, tests for schema validation.
