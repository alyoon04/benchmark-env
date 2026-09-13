# Multi-instance evaluation (SWE-bench Pro)

A sweep across repositories and languages from
[ScaleAI/SWE-bench_Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro),
exercising `import-swebench` (with auto `verify-gold`), the in-place grading path, the
stub solver, and (on ansible) the **live Claude solver**.

The sweep was run twice: once with the original *clone-swap* grading, which exposed a
scope gap, and again after grading moved to **solve-in-place + changeset replay**
(DESIGN_NOTES.md §4). The `before-in-place.*` files are the original failure logs.

## Results

| Instance | Lang | Before in-place | After in-place | Evidence |
|---|---|---|---|---|
| `ansible/ansible` (combine_vars) | Python | ✅ solvable | ✅ solvable | [`../ansible-combine-vars/`](../ansible-combine-vars/): stub gold → RESOLVED, no-op → UNRESOLVED, **Claude → RESOLVED** (claude-opus-4-7, $2.37; Claude's fix differs from the gold patch), and again on the rewritten loop: **claude-opus-5, 7 iterations, $0.18** (42K/51K input tokens from cache). |
| `internetarchive/openlibrary` `…798055d1…` (import_standard_ebooks) | Python | ❌ `ModuleNotFoundError: infogami` | ✅ **solvable** — f2p fails on baseline with the real bug (`AttributeError: 'dict' object has no attribute 'id'`), passes after gold | `openlibrary-import-standard-ebooks.verify-gold-PASS.txt`, `before-in-place.openlibrary-verify-gold-FAIL.txt` |
| `internetarchive/openlibrary` `…e8084193…` (marc parse) | Python | — | ✅ **solvable**: 1 f2p + 58 p2p; stub gold → RESOLVED (1 file changed), no-op → UNRESOLVED (0 changes, p2p all still green) | `openlibrary-marc-parse.run-gold-stub.report.json`, `openlibrary-marc-parse.run-noop-stub.report.json` |
| `future-architect/vuls` `…e52fa8d6…` (shouldDownload) | Go | ❌ `package TestParse is not in std` + `GOCACHE … permission denied` | ⚠️ **harness path works; blocked by the host emulator.** All 9 test ids now execute real `go test` runs: 4 p2p subtests pass on baseline *and* after gold, and 2 p2p fail→pass. The remaining failures are the Go toolchain segfaulting under `qemu-x86_64` (`compile`/`asm`/`vet: signal: segmentation fault`), which reproduces in the raw base image with no harness involved. | `vuls-should-download.verify-gold-qemu-segfaults.txt`, `vuls-raw-base-image-qemu-segfaults.txt` (3/3 crashes with no harness), `vuls-should-download.task.json`, `before-in-place.vuls-verify-gold-FAIL.txt` |
| `protonmail/webclients` | JS | — not run | — not run | Predicted to work now (`node_modules` under the repo dir is preserved in place); not yet attempted. The JS default test command is still a best-effort guess — override with `--test-command`. |

## What changed and why it matters

The first sweep's failures had one root cause: grading swapped a **clean git clone** in for
the image's repo directory, which discarded everything under that directory that git does
not track — openlibrary's `vendor/infogami` submodule checkout, `node_modules`, Go/pip
caches. `verify-gold` correctly *refused* those instances (correct-or-refuses), but they
were not gradeable.

Grading now **replays only the solver's changeset** (modified/added/deleted files, from a
before/after content manifest of the solve container) onto a fresh container's *own*
complete tree. Nothing else is touched, so the image's dependencies are present at grade
time exactly as shipped. The image's full-history `.git` (16k commits in openlibrary's)
is scrubbed at build so the test-hiding invariant still holds; the leak guard now hashes
the running container rather than a host copy.

Per-language fixes exposed by the sweep and folded into `import-swebench`:

- **Go**: `-run` patterns are anchored per level (`^Top$/^sub$`; a bare `Test_x/case`
  also matched `case_extra`), `go test` is scoped to the packages the test patch touches
  instead of `./...`, and `GOCACHE` gets its own writable path (the default under
  `$HOME/.cache` had been pre-created root-owned by orchestrator execs).

## The remaining boundary (and why it is not the harness)

`vuls` fails on this host because the prebuilt images are `linux/amd64` and this machine
is arm64 (colima VM, qemu binfmt, no Rosetta). The Go toolchain crashes intermittently
under that emulation; Python survives it. Running the same `go test` directly in the base
image, outside the harness, segfaults the same way. On an amd64 host — or an arm64 Docker
Desktop with Rosetta — the Go path has no known blocker: the harness gets the test ids,
package scoping, cache, and in-place tree right, as the passing subtests show.

The correct-or-refuses property held throughout: the emulator failures surfaced as
`verify-gold` refusing the instance with per-test reasons, never as a silent mis-grade.

## Environment note

Prebuilt images are ~0.8–1.4 GB each. `task clean <bundle> --yes` reclaims the task
image and clone per instance; the `jefzda/sweap-images` base must be removed by hand.
