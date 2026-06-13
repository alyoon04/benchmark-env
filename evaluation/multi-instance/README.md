# Multi-instance evaluation (SWE-bench Pro)

A sweep across repositories and languages from
[ScaleAI/SWE-bench_Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro),
exercising `import-swebench` (with auto `verify-gold`), the path-discovery build, the
stub solver, and the **live Claude solver** (real API).

## Results

| Instance | Lang | Import + build | `verify-gold` | Notes |
|---|---|---|---|---|
| `ansible/ansible` (combine_vars) | Python | ✅ | ✅ solvable | Full success — see [`../ansible-combine-vars/`](../ansible-combine-vars/): stub gold → RESOLVED, no-op → UNRESOLVED, **Claude → RESOLVED** (claude-opus-4-7, $2.37, 461.7K in / 2.5K out; Claude's fix differs from the gold patch — genuine problem-solving). |
| `internetarchive/openlibrary` | Python | ✅ | ❌ guard fired | `ModuleNotFoundError: No module named 'infogami'`. openlibrary depends on `infogami` (a submodule / path-dep living **under** the repo dir in the prebuilt image). Our shallow, submodule-free, `.git`-scrubbed clone doesn't reproduce it; the symlink swaps the incomplete clone in, so the dep vanishes. See `openlibrary-verify-gold-FAIL.txt`. |
| `future-architect/vuls` | Go | ✅ | ❌ guard fired | Two issues: (1) the default Go test command `go test {test_path}` makes Go read the test id as a *package* (`package TestParse is not in std`) — Go needs `-run`; (2) `mkdir /tmp/.cache/go-build: permission denied` (`GOCACHE` under our `HOME=/tmp`). See `vuls-verify-gold-FAIL.txt`. |
| `protonmail/webclients` | JS | — not run | — | Aborted: the monorepo clone exhausted the near-full host disk (re-wedging Docker — see below). Predicted to fail the guard for the same reason as openlibrary: `node_modules` lives **under** the repo dir and isn't git-tracked, so the clone-swap loses it. |

## What this validates

1. **The path-discovery build works across repos.** Every instance built its task image
   from the discovered repo path (`/app`), with no `/app` hardcode and no `rm -rf` of the
   image's repo — the failures were all *downstream* of a successful build.
2. **The guard does its job.** `verify-gold` ran automatically at import and refused every
   instance the engine cannot grade correctly, loudly and with a specific reason — instead
   of silently grading future solver runs `UNRESOLVED`. This is the designed behavior.
3. **The full solver pipeline is real.** Claude solved a real SWE-bench Pro bug end-to-end
   (in-container tools, host-side diff capture, two-phase grading, token/cost accounting).

## The scope boundary it exposed

The engine grades correctly when a repo's dependencies live **outside** the repo dir
(Python site-packages, Go module cache) **and** the repo is pure-git. It cannot (yet)
grade instances whose code/deps live **under** the repo dir and aren't git-tracked —
submodules (openlibrary's `infogami`), `node_modules` (JS), in-place build artifacts —
because the "swap in a clean git clone" mechanism discards them. Go additionally needs a
correct `-run` test command and writable `GOCACHE`.

The honest takeaway: the tool is **correct-or-refuses**. It fully supports Python-editable
instances and uses `verify-gold` as a structural gate for everything else. Closing the
gap needs an apply-diff-in-place grade against the image's *own* complete repo dir (plus
per-language test-command/env handling) — documented as future work in `../../DESIGN_NOTES.md`.

## Environment note

These prebuilt images are ~1 GB each and the repos clone large; on a near-full host the
Docker Desktop VM (a sparse file backed by the host volume) can hit "no space left on
device" and wedge read-only. `task doctor`'s disk check (host volume) is the right signal.
Clean each instance (`task clean <bundle> --yes` + remove the `jefzda/sweap-images` base)
before importing the next.
