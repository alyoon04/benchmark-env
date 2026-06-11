# Progress

## Milestone checklist

- [x] 0. Design docs (DESIGN.md, PROGRESS.md) + spec sanity check with reviewer
- [x] 1. Bundle spec + `task init` (scaffold, clone at pinned SHA)
- [ ] 2. Container build + `task validate` (image build + smoke test, hidden-test staging, 3× flake detection) ← **next**
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
| Docker image build moved from M1 (`init`) into M2 | Keeps M1 reviewable; `init` will build+smoke-test the image once container.py exists |
| Clone = `git init` + `fetch --depth 1 <sha>` + detached checkout | Pin to exact SHA; shallow history is defense-in-depth for test hiding |
| CLI `main()` catches TaskError → friendly stderr + exit code; raw tracebacks only for real bugs | UX bar: predictable failures must be actionable, not stack traces |

## Current state

Milestone 1 complete and hand-verified: `task init` scaffolds bundles and clones at
the pinned SHA (tested against GitHub and local file:// origins). 26 tests, ruff +
mypy --strict clean. Awaiting reviewer approval before milestone 2.

Modules so far: `bundle.py` (TaskSpec/Bundle/state), `workspace.py` (pinned shallow
clone), `cli.py` (typer app, `init` only), `errors.py`. Fixture toy repo (buggy
`divide()`) at `tests/fixtures/toy_repo/`.

## Next steps (milestone 2)

1. `container.py`: docker wrapper (build/run/exec/cp with resource limits).
2. Image build + smoke test wired into `task init`; cache key per DESIGN.md §4.
3. Hidden-test staging via `docker cp`; `task validate` with 3× flake detection.
4. Toy example bundle under `examples/` exercising fail2pass/pass2pass on the fixture repo.
