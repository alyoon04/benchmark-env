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
| `import-swebench` auto-runs `verify-gold` after init (default; `--no-verify` opts out) | The editable-install assumption can't be *fixed* for a non-editable instance, only detected; auto-verify makes a non-gradeable bundle fail loudly at import instead of silently grading every solver UNRESOLVED |
| Repo path for prebuilt images is *discovered* from the base image's `WorkingDir` (not hardcoded `/app`; `/` and empty fall back to `/workspace`); the symlink to the clean tree is applied at build time, not via task.json setup | Removes the brittle `/app` hardcode and works for any prebuilt-image convention; keeping the symlink (vs overlaying our clone onto the image's repo dir) keeps the solver's tree artifact-free, which diff capture requires |
| Artifact preservation (compiled extensions under the repo path) was *not* added | It conflicts with clean diff capture (a solver editing a tree that also holds image build artifacts would diff them as changes); true support needs an apply-diff-in-place grade — documented future work |
| **Solve in place; grade by replaying a changeset** (replaces clone-swap + wipe-then-overlay). The solve container's repo dir is hashed before/after (`find -exec sha256sum`, as root); modified/added/deleted paths, filtered by the repo's `.gitignore` + a tiny bytecode hygiene list, are the changeset; only those files are copied out and replayed into a fresh grade container | Grading no longer discards anything under the repo dir that isn't git-tracked (submodules, `node_modules`, compiled extensions, caches) — the exact failure the multi-instance sweep hit. No git/patch needed in the image; deletions handled; the diff is rendered host-side from the grade container's pristine copies, so it is honest to what was graded |
| Prebuilt images keep their own repo dir (`WORKDIR` discovered, no COPY, no symlink); `.git` (incl. submodule `.git` files) scrubbed at build; tree chowned to the sandbox uid; `workspace_excludes` become `rm -rf` | The image's tree with its installed deps *is* the task; the symlink-to-clean-clone trick was what lost the deps. Full-history `.git` in the image would violate the test-hiding invariant. `IMAGE_LAYOUT_VERSION` in the cache key invalidates old-layout images |
| Leak guard runs over the solve container's manifest (sha256 set intersection), not a host tree | Covers whatever the base image shipped, not just what the engine copied; zero extra I/O since the manifest is needed for the changeset anyway |
| Stub solver and hidden-test staging both go through `materialize_patch` (sparse: only the touched files are copied from the clone) + `push_files` (cp in present paths, `rm` missing, chown) | One file-transport primitive for gold patches, test patches, and changeset replay; no whole-monorepo copies per phase |
| Changeset ignores follow `git check-ignore` on the host clone (tracked files never ignored) plus `__pycache__/`, `*.pyc`, `*.pyo`, `.pytest_cache/` | Same semantics as the previous `git add -A` snapshot without needing a repo in the tree; the model's own `run_command` test runs leave bytecode behind that is not a change |
| Changed files leave the container via one `tar` stream per 500 paths (`Docker.archive`), not one `docker cp` per file | Seconds instead of minutes when a solver touches thousands of files |
| Go imports: test command scoped to the packages the test patch touches, ids anchored per level (`^Top$/^sub$`), `GOCACHE=/tmp/task-bundle-go-build` | `-run` is an unanchored per-level regex (`Test_x/case` also matches `case_extra`); `./...` compiled the whole module per id; the default `$HOME/.cache` had been pre-created root-owned by orchestrator execs |
| ClaudeSolver: default `claude-opus-5`, adaptive thinking + `--effort` (sent only to models that take it), top-level `cache_control` prompt caching, cache reads/writes priced separately (0.1x / 1.25x input) | The one recorded live solve re-sent ~460K input tokens for 2.5K output; caching turns most of that into cache reads. Effort is the primary cost/quality lever on current models |
| ClaudeSolver tools: `search` (grep -rnE) and `edit_file` (exact single match; refuses 0 or >1 matches; `is_error` tool results) added; whole-file `write_file` kept for new files | Exploration by grep and targeted edits are what competitive coding agents do; whole-file rewrites lose unrelated content and burn output tokens |
| API errors after the SDK's retries, refusals, `max_tokens` truncation and a context budget end the solve gracefully (recorded as `stop_reason`); the edits so far are graded | On a fleet, a crashed run is a lost sample; a run that stopped early with a reason is data |
| Structured `trajectory.jsonl` artifact (start/assistant/tool_result/error/end events, exact tool I/O, per-step usage) alongside `transcript.txt`; `stop_reason` + cache stats + solver config in `report.json` | Failure taxonomies, ablations and SFT/RL exports need the exact model-visible I/O, not a summary; the DB schema is unchanged (cost already reflects caching) |
| `--effort` is part of the fleet job identity | Two sweeps at different effort are different experiments; resume must not conflate them |

## Current state

All eight milestones complete. The full surface — `init`, `validate`, `run`
(stub + claude), `verify-gold`, `import-swebench`, `diff`, `logs`, `runs`,
`fleet`, `doctor`, `clean` — is implemented, tested, and documented. Fleet execution
adds bounded concurrency, SQLite resume, multi-sample pass@k, disk-pressure backoff,
and local/Kubernetes runtimes. `DESIGN_NOTES.md` is
the distilled final design deliverable; `.github/workflows/ci.yml` runs ruff +
mypy + the non-docker suite on a 3.11/3.12 matrix.

Post-milestone: grading moved to **solve-in-place + changeset replay** (see the
decision table), closing the scope gap the multi-instance sweep exposed — repos
whose deps live under the repo dir outside git now grade correctly (see
`evaluation/multi-instance/`). Fleet adds resumable concurrent execution on top
of that grading path. Ruff + mypy --strict are clean.

## Possible follow-ups (out of original scope)

- Live ClaudeSolver run on real instances with the rewritten loop (caching, effort,
  edit_file); the previous live solve predates it. No credentials were available in the
  session that made the change, so it is verified with a scripted client + Docker only.
- A second provider backend (OpenAI-compatible endpoint for open-weight models) behind
  the same `Solver` protocol and trajectory format.
- Server-side context editing (`clear_tool_uses`) instead of the hard context budget.
- Non-editable-install `import-swebench` support via an optional per-language reinstall
  step (in-place grading now handles editable installs, submodules, `node_modules` and
  compiled extensions; a non-editable install is still detected loudly by the auto
  verify-gold guard).
- Batched test execution and safe warm-container reuse after apply-diff-in-place grading.
- Digest-pin example base images for fully reproducible builds.
