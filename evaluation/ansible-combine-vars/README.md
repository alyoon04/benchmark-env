# Evaluation: real SWE-bench Pro instance (milestone 6)

End-to-end run of the full pipeline against a public SWE-bench Pro instance:

- **Instance**: `instance_ansible__ansible-0ea40e09d1b35bcb69ff4d9cecf3d0defa4b36e8-v30a923fb5c164d6cd18280c02422f75e611e8fb2`
  (ansible/ansible @ `f7234968d241`, 1 fail2pass + 15 pass2pass tests, TEST_PATCH format)
- **Reproduce**:

  ```sh
  task import-swebench instance_ansible__ansible-0ea40e09d1b35bcb69ff4d9cecf3d0defa4b36e8-v30a923fb5c164d6cd18280c02422f75e611e8fb2 --dest <bundle>
  task validate <bundle>                                  # contract holds, 3x consistent
  task run <bundle> --solver stub --patch <bundle>/patch.diff   # RESOLVED
  task run <bundle> --solver stub                               # UNRESOLVED
  task run <bundle> --solver claude                             # live LLM solve
  ```

- `run-gold-stub.report.json` — gold patch via StubSolver → **RESOLVED**
  (fail2pass flipped to pass, all pass2pass held)
- `run-noop-stub.report.json` — no-op StubSolver → **UNRESOLVED**
  (fail2pass still failing, pass2pass unaffected)
- `run-claude.report.json` — **live ClaudeSolver → RESOLVED** (claude-opus-4-7,
  $2.37, 461.7K input / 2.5K output tokens). Claude's fix adds `__or__`/`__ror__`/
  `__ior__` to `VarsWithSources` — a *different* valid implementation than the gold
  patch, so it is a genuine solve, not memorization. Proves the agentic loop
  (in-container tools, host-side diff capture, two-phase grading, cost accounting).
- `run-claude-opus5.report.json` + `.trajectory.jsonl` — the same instance on the
  **rewritten agent loop** (claude-opus-5, effort high): **RESOLVED in 7 iterations
  for $0.18**. Prompt caching served 42.0K of 51.4K input tokens from cache (14
  uncached); the fix was made with one `edit_file` call and verified with the repo's
  visible tests before finishing. Same class of fix as before, plus a changelog
  fragment. The trajectory is the structured per-step record (tool inputs, exact
  outputs the model saw, per-turn usage).

| Agent loop | Model | Iterations | Input tokens (uncached / cached) | Output | Cost |
|---|---|---|---|---|---|
| original (whole-file writes, no caching) | claude-opus-4-7 | — | 461.7K / 0 | 2.5K | $2.37 |
| rewritten (edit tool, search, caching, effort) | claude-opus-5 | 7 | 14 / 42.0K read + 9.5K written | 4.1K | **$0.18** |

Same verdict, ~13× cheaper. One wart visible in the trajectory: the model passed an
absolute `/app/...` path to `edit_file`, which the path guard mis-rooted; it recovered
on the next turn. Fixed in the solver afterwards (absolute paths under the repo dir are
now accepted as-is).

