# Sweep: SWE-bench Pro (openlibrary + ansible) × Claude Opus 5

First fleet-scale result. 37 gradeable bundles (36 `internetarchive/openlibrary`
instances from a 40-instance bulk import, plus `ansible/ansible` combine_vars), two
independent samples each, `claude-opus-5` at effort `high`, 30-iteration cap, four
containers in parallel on an M-series Mac (colima + Rosetta).

```sh
uv run task import-swebench --repo internetarchive/openlibrary --limit 40 --dest bundles
uv run task fleet --from-summary bundles/import_summary.json bundles/ansible-combine-vars \
  --solver claude --samples 2 --concurrency 4 --container-limit 4 --max-cost-usd 80
```

Fleet `fleet_02d8191864a6fb8be27f`; `fleet_summary.json` is the tool's own output.
Wall clock: 70 minutes for 74 attempts.

## Headline

| Metric | Value |
|---|---|
| Attempts the API served | 60 (30 tasks × 2, ansible included) |
| Resolved | 43 |
| **pass@1** | **71.7%** (43/60) |
| **pass@2** | **83.3%** (25 of 30 tasks solved at least once) |
| Solved in both samples | 18 of 30 |
| **pass@1 excluding 3 memorized tasks** (see contamination check) | **70.4%** (38/54) |
| Solver spend | $67.55 total, $0.91 mean / $0.96 median per attempt, $2.29 max |
| Cost per resolved attempt | $1.57 |
| Prompt-cache hit rate | 94.7% (50.8M cache reads vs 3.0K uncached input tokens) |
| Iterations | mean 20.4 (resolved 24.1, unresolved 15.3); 34 attempts hit the 30 cap |

**Why 60 and not 74.** The Anthropic account's credit balance ran out about $67 into
the fleet. 14 attempts (7 tasks, both samples) were refused by the API before the model
produced a single token (`credit balance is too low`), and the harness graded them
UNRESOLVED because no fix was made. Two further attempts on one task were cut off by
the same refusal mid-solve; they are counted (one had already resolved, one had not).
The raw fleet numbers are therefore pass@1 58.1% / pass@2 67.6% over 74; the table
above excludes the 14 zero-progress refusals as non-attempts. They are reset to `error`
in SQLite and will run on the next resume. A refusal with zero model progress now
yields an `ERROR` verdict (retryable) instead of `UNRESOLVED`, so a billing or outage
event cannot masquerade as model failures again.

## Contamination check

These repositories and their fix commits are public and predate the model's training
cutoff, so a high score could be memorization. `scripts/diff_similarity.py` compares
every resolved attempt's diff with the gold patch (`diff_similarity.json` has the
per-attempt numbers):

| Bucket (share of gold patch's added code lines reproduced verbatim) | Attempts | Tasks |
|---|---|---|
| ≥ 80% and overall similarity ≥ 0.8 (near-verbatim) | 4 | 3 |
| 50–80% (partial) | 19 | 12 |
| < 50% (independent) | 20 | 15 |

The near-verbatim cases are memorization, not spec-following: on
`openlibrary-02f647f7d525` the model reproduced the gold patch's docstrings word for
word including `See #9440`, an issue number that appears nowhere in the task
description. The partial bucket is ambiguous by construction: SWE-bench Pro descriptions
carry a *Requirements* and an *Interface* section that name the classes, functions and
fields to add, so any correct fix shares structure with the reference.

Sensitivity of the headline to exclusions:

| Basis | Tasks | Attempts | pass@1 | Solved ≥ once |
|---|---|---|---|---|
| All served | 30 | 60 | 71.7% | 83.3% |
| Excluding the 3 near-verbatim tasks | 27 | 54 | 70.4% | 81.5% |
| Excluding every task with any attempt ≥ 50% reproduced (strict) | 15 | 30 | 56.7% | 66.7% |

The number to quote is the middle row. The strict row is a lower bound that also throws
out fixes that are similar because the spec leaves little room, so the truth is between
them. Either way this pilot is above published SWE-bench Pro figures for frontier
models (roughly 25–45% on the full public set): the 36 openlibrary instances are the
first 40 in dataset order rather than a random sample, single-repo, Python-only, and
filtered by verify-gold, all of which favour the solver. Treat it as a pipeline
validation and cost model, not a leaderboard entry.

## Failure taxonomy (31 unresolved attempts, from the reports)

| Bucket | Attempts | Notes |
|---|---|---|
| No code change made | 15 | 14 API credit refusals (excluded above) + 1 cut off mid-solve |
| fail2pass still failing, pass2pass intact, hit iteration cap | 12 | The dominant *model* failure: ran out of turns while still working |
| fail2pass still failing, pass2pass intact, model stopped early | 2 | Declared done with a wrong or incomplete fix |
| fail2pass failing and pass2pass broken, hit cap | 2 | Regressed existing behaviour |

Two observations that shape the next run:

- **The iteration cap is the binding constraint, not the model.** 34 of 74 attempts hit
  30 iterations; 14 of those were still RESOLVED (edits already in place when the cap
  hit). 12 unresolved attempts were mid-work at the cap. A 50-iteration run is the
  obvious ablation and will cost roughly 1.4× per attempt.
- **Regressions are rare.** Only 2 of 31 failures broke a pass2pass test. The agent's
  habit of running the visible tests before finishing (visible in the trajectories)
  is doing its job.

## Per-task results

| task | f2p/p2p | sample 1 | sample 2 | cost s1 | cost s2 | iters s1/s2 |
|---|---|---|---|---|---|---|
| ansible-combine-vars | 1/15 | RESOLVED | RESOLVED | $0.15 | $0.14 | 8/8 |
| openlibrary-0180e2ca33a1 | 1/77 | *credit refused* | *credit refused* | — | — | — |
| openlibrary-02f647f7d525 | 3/15 | RESOLVED | RESOLVED | $0.32 | $0.40 | 11/12 |
| openlibrary-0c5d154db9a1 | 43/0 | RESOLVED | RESOLVED | $0.80 | $0.76 | 30/30 |
| openlibrary-1d2cbffd8cbd | 2/0 | RESOLVED | UNRESOLVED | $1.18 | $1.11 | 30/30 |
| openlibrary-1e32ae3ec085 | 7/119 | UNRESOLVED | UNRESOLVED | $1.75 | $1.33 | 30/30 |
| openlibrary-2edaf7283cf5 | 47/33 | RESOLVED | UNRESOLVED (credit ran out mid-solve) | $0.14 | $0.03 | 8/5 |
| openlibrary-2f590171b1d9 | 2/4 | *credit refused* | *credit refused* | — | — | — |
| openlibrary-31d6ecf3c04c | 7/14 | RESOLVED | RESOLVED | $0.85 | $0.64 | 20/17 |
| openlibrary-3677dd20bcdd | 3/0 | *credit refused* | *credit refused* | — | — | — |
| openlibrary-409914bf541b | 1/5 | *credit refused* | *credit refused* | — | — | — |
| openlibrary-46bcf7c30178 | 2/1 | RESOLVED | RESOLVED | $0.94 | $0.85 | 29/24 |
| openlibrary-46d7d325e6ed | 2/41 | RESOLVED | RESOLVED | $0.72 | $0.56 | 30/30 |
| openlibrary-4ff15b75531e | 54/33 | UNRESOLVED | UNRESOLVED | $1.89 | $1.95 | 30/30 |
| openlibrary-53d376b14889 | 2/7 | *credit refused* | *credit refused* | — | — | — |
| openlibrary-5c6c22f3d2ed | 15/16 | UNRESOLVED | UNRESOLVED | $1.47 | $1.55 | 30/25 |
| openlibrary-5f7d8d190e2f | 16/9 | RESOLVED | RESOLVED | $0.85 | $1.08 | 22/26 |
| openlibrary-630221ab686c | 1/77 | RESOLVED | UNRESOLVED | $1.83 | $1.09 | 30/30 |
| openlibrary-69cb6f271d8e | 1/37 | *credit refused* | *credit refused* | — | — | — |
| openlibrary-71b18af1fa3b | 1/6 | UNRESOLVED | RESOLVED | $1.71 | $2.10 | 30/30 |
| openlibrary-79dccb33ad74 | 7/0 | RESOLVED | RESOLVED | $1.53 | $1.26 | 30/23 |
| openlibrary-7b1ec94b425e | 1/0 | RESOLVED | UNRESOLVED | $0.41 | $0.33 | 9/11 |
| openlibrary-80f511d3344a | 12/0 | UNRESOLVED | UNRESOLVED | $1.21 | $1.20 | 30/30 |
| openlibrary-8c988af810a4 | 4/4 | RESOLVED | RESOLVED | $1.57 | $1.40 | 30/30 |
| openlibrary-90475fb6c168 | 1/8 | RESOLVED | RESOLVED | $0.37 | $0.22 | 10/6 |
| openlibrary-9a9204b43f9a | 5/10 | UNRESOLVED | RESOLVED | $1.10 | $1.53 | 30/30 |
| openlibrary-9d9f3a199838 | 2/0 | RESOLVED | RESOLVED | $0.90 | $1.04 | 23/30 |
| openlibrary-afb819f8166c | 41/1 | *credit refused* | *credit refused* | — | — | — |
| openlibrary-c9795319b19c | 2/57 | UNRESOLVED | UNRESOLVED | $2.26 | $1.66 | 30/30 |
| openlibrary-d38cb5a4162a | 3/51 | RESOLVED | RESOLVED | $1.26 | $0.96 | 29/30 |
| openlibrary-ddbbdd64ecde | 1/16 | RESOLVED | RESOLVED | $1.26 | $0.79 | 30/27 |
| openlibrary-de903b9535d5 | 1/60 | RESOLVED | RESOLVED | $1.37 | $0.84 | 30/21 |
| openlibrary-e0e34eb48957 | 1/0 | RESOLVED | RESOLVED | $0.87 | $1.11 | 22/26 |
| openlibrary-e9e9d8be33f0 | 6/0 | UNRESOLVED | RESOLVED | $2.29 | $2.12 | 30/30 |
| openlibrary-f1f4efd65942 | 1/1 | RESOLVED | RESOLVED | $0.97 | $1.12 | 30/30 |
| openlibrary-facafbe7339c | 1/2 | RESOLVED | RESOLVED | $1.17 | $1.10 | 29/27 |
| openlibrary-febda3f008cb | 4/5 | RESOLVED | RESOLVED | $2.22 | $1.88 | 30/30 |

## Caveats

- 37 tasks is a pilot: one repository family plus ansible, and the 40 openlibrary
  instances are the first 40 in dataset order, not a random sample. The full Python
  split (266 instances) is the number to publish; this run validates the pipeline and
  gives the cost model for it.
- Per-attempt cost here ($0.91 mean) is about 5× the single ansible run ($0.18):
  openlibrary is a larger codebase and most attempts used 20 to 30 turns.
- Every number traces to `fleet_summary.json`, the per-run `report.json` files, and
  `trajectory.jsonl` per attempt under the artifacts directory of the fleet command.
