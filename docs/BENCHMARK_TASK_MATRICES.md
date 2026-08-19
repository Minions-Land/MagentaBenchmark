# Benchmark Task Matrices

[`reports/benchmark_task_matrices.json`](../reports/benchmark_task_matrices.json)
is the task-level companion to the generated experiment ledger. It puts one
task or case on each row and one method or configuration in each method column.
The current projection contains 140 distinct rows across five benchmark views:

| Benchmark view | Rows | Method columns | Native result fields |
| --- | ---: | ---: | --- |
| CMTBench | 50 | 8 | adopted evaluator verdict |
| BiomniBench DA | 50 | 10 | execution verdict and rubric score |
| NatureBench | 31 | 1 | validity verdict, `g`, and upstream `g` when available |
| BioML-Bench | 8 | 1 | baseline comparison and native metric summary |
| SWE-bench Lite (not Verified) | 1 | 1 | focused exploratory probe summary |

The JSON also provides a `method_summaries` projection for scanning. It counts
every source verdict, retains unresolved outcomes, and summarizes numeric
fields with observed count, mean, minimum, and maximum. These summaries never
replace the task rows or benchmark-native semantics. In particular,
BiomniBench's source `success` verdict describes execution/evaluation status;
the rubric `score` remains the result metric.

## CMTBench Check

The fixed 50-task denominator counts unresolved parser/evaluator outcomes as
incorrect, matching the adopted regrade policy at
`MinionsOS2-Bench@150fa10`. The eight columns recompute as follows:

| Method | Correct | Unresolved | Adopted accuracy |
| --- | ---: | ---: | ---: |
| purellm_gpt54 / gpt-5.4 | 9/50 | 0 | 18% |
| purellm / gpt-5.5 | 17/50 | 0 | 34% |
| codex_gpt54 / gpt-5.4 | 14/50 | 1 | 28% |
| codex / gpt-5.5 | 17/50 | 2 | 34% |
| purellm_sonnet46_1m / claude-sonnet-4-6 | 8/50 | 0 | 16% |
| claudecode / claude-sonnet-4-6[1m] | 16/50 | 1 | 32% |
| autoscientist / claude-sonnet-4-6[1m] | 8/50 | 1 | 16% |
| minionsos2 / claude-sonnet-4-6[1m] | 18/50 | 1 | 36% |

This corrects a common notation error: `18, 34, 28, 34, 16, 32, 16, 36`
are percentages, not numerators over 50.

## Evidence Boundary

This matrix is a derived, non-claim view. It omits raw answers, gold payloads,
traces, logs, private host paths, and credentials. Every benchmark entry names
its fixed source snapshot and route, and every entry has
`claim_eligible=false`. BiomniBench DA, CMTBench, NatureBench, and BioML-Bench
remain legacy imports; the SWE-bench item remains an exploratory probe.

The provenance-bearing typed imports and persisted reports remain the source
of truth. A failed, unresolved, missing, or not-beating-baseline result stays
visible and is never converted into a positive result. Promotion to a
reproduced or claim-ready result still requires materialized bytes, the full
report graph, standalone verification, and the normal MagentaBench review
gates.

Inspect the whole table or one benchmark without a spreadsheet:

```bash
jq '.benchmarks[] | {benchmark_id, row_count, method_summaries}' \
  reports/benchmark_task_matrices.json

jq '.benchmarks[] | select(.benchmark_id == "cmtbench")' \
  reports/benchmark_task_matrices.json
```
