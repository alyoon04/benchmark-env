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

