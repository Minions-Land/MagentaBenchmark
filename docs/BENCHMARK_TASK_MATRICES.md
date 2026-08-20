# Benchmark Task Matrices

`reports/benchmark_task_matrices.json` is a deterministic, derived view for
comparing historical benchmark task/case rows. It is not a progress board, a
spreadsheet of claims, or a replacement for typed import records and verified
BMP reports. The publication boundary is Issue #135; the implementation is
tracked by PR #136.

## Current View

| Benchmark | Rows | Methods | State |
| --- | ---: | ---: | --- |
| CMTBench | 50 | 8 | adopted evaluator verdicts, including unresolved outcomes |
| BiomniBench DA | 50 | 10 | judge status and score, including no-output rows |
| NatureBench | 31 | 1 | declaration-only; no result file at pinned source |
| BioML-Bench | 8 | 1 | external-unavailable declaration-only |
| SWE-bench Lite (not Verified) | 1 | 1 | exploratory probe-only row |

There are 140 task rows. Every benchmark, row, and method cell explicitly
contains `claim_eligible: false`. A missing or true value is invalid; there is
no parent-field inheritance rule.

## Projection Contract

The checked-in report is produced by
`scripts/historical_imports/benchmark_task_matrices_v1.py` and checked by
`scripts/historical_imports/validate_benchmark_task_matrices.py`. The report's
`projector` object binds the exact implementation path and SHA-256. The
projector is distinct from the historical import normalizers: those normalizers
remain bound to their typed aggregate records and are never presented as task
matrix generators.

`--require-sources` adds a canonical equality gate after the source-byte checks:
the projector rebuilds the view and compares canonical JSON bytes with the
report. Task IDs, cells, aggregates, and safe-field drift therefore fail
closed. The validator also rejects unknown fields, numeric values in
declaration-only rows, authenticated locators, malformed structures, and true
claim eligibility.

To regenerate from authorized fixed source roots (kept outside Git), run:

```bash
python scripts/historical_imports/benchmark_task_matrices_v1.py \
  --report reports/benchmark_task_matrices.json \
  --source-root cmtbench=/authorized/MinionsOS2-Bench \
  --source-root biomnibench-da-default=/authorized/AOSEBench \
  --source-root biomnibench-da-xhigh=/authorized/AOSEBench \
  --source-root naturebench=/authorized/AOSEBench-NatureBench

python scripts/historical_imports/validate_benchmark_task_matrices.py \
  --report reports/benchmark_task_matrices.json --require-sources \
  --source-root cmtbench=/authorized/MinionsOS2-Bench \
  --source-root biomnibench-da-default=/authorized/AOSEBench \
  --source-root biomnibench-da-xhigh=/authorized/AOSEBench \
  --source-root naturebench=/authorized/AOSEBench-NatureBench
```

The source roots must resolve the exact pinned bytes recorded in the report;
the tools verify each expected source mapping, byte size, SHA-256, and Git blob
SHA-1. When a root is a Git checkout, the expected repository-relative path,
`HEAD`, and root tree must also match the recorded commit/tree. A portable
byte-only evidence export may use the approved one-file mapping because it has
no Git tree; it proves the bytes/blob identity, not a live checkout. Mutable
workspaces and scratch copies with any drift fail closed. The projector writes
the report through a fsync-and-replace temporary sibling. Raw source bytes,
answers, prompts, gold data, traces, logs, credentials, and machine-private
paths are never emitted into the report.

## Fixed Sources

CMTBench uses
`Minions-Land/MinionsOS2-Bench@150fa100ead4ab51acdfc24ed246a8c5b2141466` and
the pinned `per_answer_regrade.csv` blob
`88fc2f305f97ef1fcaab247602eb20c948c17945` (221376 bytes,
SHA-256 `cde0aa20311f255fcc4892d69ec0b58702d16f8e27473481276c2cdad4cdcbad`).
The adopted policy maps parser failures to `未解析`, incorrect evaluator
verdicts to `错误`, and correct verdicts to `正确`; all 50 tasks remain in
the denominator. The recomputed correct counts are
`9/50, 17/50, 14/50, 17/50, 8/50, 16/50, 8/50, 18/50`.

BiomniBench DA uses
`Minions-Land/AOSEBench@def4dae7520807d254612b3590eb32b9aa977924` with the
fixed default and xhigh summary blobs. `judge_status=evaluated` is `成功`;
other terminal statuses are `失败`; the numeric score remains separate from
the verdict and no-output rows retain score zero.

NatureBench uses the pinned `NatureBranch` task manifest and cellomics task
set at `4b512029f3ad37746502ce377e4fcc2027fd46db`. It has no completed
`opus4.7_medium.csv`, so every row is `not-observed` with a null verdict and
no numeric value. BioML-Bench has no immutable source and remains declaration
only. BiomeBench has no resolved source and is intentionally absent.

The SWE-bench row is the retained Astropy Lite probe only. Its `probe.json`
identity and outcome are preserved as exploratory evidence; narrative timing,
token, and evaluator prose from `summary.md` are not matrix metrics. It must
not be read as SWE-bench Verified.

## Row Fields

For every future result row, retain benchmark/dataset/split, case or question
ID, run ID, method/model and code commit, denominator, outcome (success,
failure, timeout, invalid, or missing), BMP/manifest/config/dataset/evaluator
digests, fresh durable `record_root`, official verifier and report digest/size,
source/table locator, and evidence tier. Aggregates are derived from those
rows. Historical rows remain non-claim until a new standalone-verified BMP
report is linked through `bmp-lab` and the ledger.

## Inspection

```bash
jq '.benchmarks[] | {benchmark_id, row_count, method_columns, method_summaries}' \
  reports/benchmark_task_matrices.json
python scripts/historical_imports/validate_benchmark_task_matrices.py \
  --report reports/benchmark_task_matrices.json
```
