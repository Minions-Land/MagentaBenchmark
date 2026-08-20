# Benchmark Task Matrices

[`reports/benchmark_task_matrices.json`](../reports/benchmark_task_matrices.json)
is a derived, non-claim view beside the generated experiment ledger. It puts
one task or case on each row and one method or configuration in each method
column. It is a scan-friendly table, not a hand-maintained progress board and
not a replacement for the typed ledger records.

The projection is governed by
[`Minions-Land/MagentaBenchmark#135`](https://github.com/Minions-Land/MagentaBenchmark/issues/135).
Only the safe fields named there are retained. Every benchmark entry and every
row remains `claim_eligible=false`.

| Benchmark view | Rows | Method columns | Observation state |
| --- | ---: | ---: | --- |
| CMTBench | 50 | 8 | task-level adopted evaluator verdict |
| BiomniBench DA | 50 | 10 | task-level judge status and rubric score |
| NatureBench | 31 | 1 | declaration-only; no fixed result file |
| BioML-Bench | 8 | 1 | external-unavailable; declaration-only |
| SWE-bench Lite (not Verified) | 1 | 1 | exploratory local probe summary |

## Fixed Provenance

The CMTBench projection is parsed transiently from
`Minions-Land/MinionsOS2-Bench@150fa100ead4ab51acdfc24ed246a8c5b2141466`,
`experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/per_answer_regrade.csv`:
blob `88fc2f305f97ef1fcaab247602eb20c948c17945`, 221376 bytes,
SHA-256 `cde0aa20311f255fcc4892d69ec0b58702d16f8e27473481276c2cdad4cdcbad`.

The BiomniBench DA projection is parsed transiently from
`Minions-Land/AOSEBench@def4dae7520807d254612b3590eb32b9aa977924`:

| Regime | Repository-relative path | Git blob | Bytes | SHA-256 |
| --- | --- | --- | ---: | --- |
| default | `results/default/_summary/llm_judge_summary.tsv` | `0e5d4e04c830b60d1a54b5f4f0171e1c96382c5f` | 35972 | `af5b16706bf758b79c090ecab73df46d3a23cc1defe6917839a5871b7dd54f5d` |
| xhigh | `results/xhigh/_summary/llm_judge_summary.tsv` | `68e7f39dd8692c904a3a8cc775b5bef2f50b0648` | 37959 | `1d587f28659a5fc026f8d0b6c14e6ef361991484d24d22cf59725afeee117373` |

At `Minions-Land/AOSEBench-NatureBench@4b512029f3ad37746502ce377e4fcc2027fd46db`
on `NatureBranch`, the checked tree contains no completed
`opus4.7_medium.csv`. NatureBench rows therefore expose task declarations and
`not-observed` status only; they contain no `g` or `upstream_g` value.

The proposed `MinionsOS_Paper@48173d1` BioML-Bench source is not available as a
fixed GitHub organization snapshot. BioML-Bench rows retain their task IDs but
expose `external-unavailable` status only; no AUROC, baseline, or leaderboard
number is asserted.

The one SWE-bench row is bound to a repository-local probe snapshot and remains
exploratory. Its focused verifier outcome is not a SWE-bench claim or a
statistical comparison.

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

The values `18, 34, 28, 34, 16, 32, 16, 36` are percentages, not numerators
over 50.

## Evidence Boundary

The JSON intentionally excludes answers, prompts, gold/private test data,
model outputs, traces, stdout/stderr, commands, provider logs, credentials,
authenticated URLs, machine-private paths, and raw source bytes. A failed,
unresolved, missing, or unavailable result stays visible and is never converted
into a positive result. Promotion to a reproduced or claim-ready result still
requires materialized bytes, the complete report graph, standalone verification,
and the normal MagentaBench review gates.

Inspect the whole table or one benchmark without a spreadsheet:

```bash
jq '.benchmarks[] | {benchmark_id, row_count, record_origin, observation_status, method_summaries}' \
  reports/benchmark_task_matrices.json

jq '.benchmarks[] | select(.benchmark_id == "cmtbench")' \
  reports/benchmark_task_matrices.json
```
