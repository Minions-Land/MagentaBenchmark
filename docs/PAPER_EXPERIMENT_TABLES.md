# Paper Experiment Tables

MagentaBench keeps one result truth source: the generated experiment ledger.
The paper table is a deterministic, read-only projection of that ledger for a
paper appendix, a camera-ready table, or a notebook. It is not a second ledger
and it must never be hand-edited.

The task-level identity columns introduced here are
`magentabench-paper-experiment-table-v2`. Version 1 remains reproducible from
the repository commit that emitted it; consumers must not interpret a v2
header as byte-compatible with v1.

## Render

Run from a clean, pinned checkout with the locked environment:

```bash
uv run --frozen python -m MagentaBench.collab.paper_table \
  --project-root . --format markdown > /mnt/aliyunsb/paper-table.md

uv run --frozen python -m MagentaBench.collab.paper_table \
  --project-root . --format csv > /mnt/aliyunsb/paper-table.csv

uv run --frozen python -m MagentaBench.collab.paper_table \
  --project-root . --format json > /mnt/aliyunsb/paper-table.json
```

For a reproducible appendix subset, repeat a selector rather than editing the
output. Selectors are applied after the full ledger has been generated and do
not alter row values or provenance:

```bash
uv run --frozen python -m MagentaBench.collab.paper_table \
  --project-root . --format csv \
  --benchmark-id terminal-bench-2.1 \
  --metric-id reward.authoritative.v1
```

The available selectors are `--benchmark-id`, `--dataset-id`, and
`--metric-id`; each may be supplied more than once. An empty selection is a
valid table with the same fixed header.

The output is disposable. Re-run it after changing a bundle, lab link, report,
or historical import; do not commit an exported table as benchmark evidence.
The command is read-only. A nonzero exit status means the underlying ledger
had a source or verification error, even when a partial view was printed.
For relocated durable artifacts, pass the same normalized path mapping used by
the ledger, for example `--map /old/records=/restored/records`.

## Row Contract

The fixed `PAPER_COLUMNS` header is the public projection contract. Each row is
one metric observation or, when no metric can be produced, one terminal/run
state:

| Group | Columns | Meaning |
| --- | --- | --- |
| Identity | `row_id`, `row_kind`, `record_origin`, `lab_issue`, `experiment_id`, `run_id`, `source_run_id`, `source_run_record_id`, `parent_run_id`, `aggregate_run_id`, `aggregate_run_record_id`, `aggregate_reconciliation_status`, `result_granularity`, `purpose` | Stable row identity, ownership and content-addressed reconciliation linkage, and whether the row is an observation or a result-less run. |
| Benchmark unit | `benchmark_id`, `dataset_id`, `dataset_split`, `case_id`, `question`, `case_or_question`, `unit_id`, `unit_kind`, `attempt_id` | The benchmark, split, case/question, source unit, attempt, and method comparison unit. Missing fields stay `-`. |
| Method and metric | `method_id`, `subject_id`, `model`, `code_commit`, `provider_id`, `harness_id`, `protocol_id`, `metric_id`, `metric_state`, `result_status`, `result_reason`, `value`, `unit`, `direction`, `aggregation` | Method/runtime identity and the reported value. A numeric zero is a real value, never a missing value. |
| Denominator | `denominator`, `denominator_*`, `planned_rollout_count`, `task_count`, `rollouts_per_task` | Planned, observed, zero-filled, excluded, missing, and invalid counts remain explicit. |
| Uncertainty | `uncertainty`, `uncertainty_method`, `uncertainty_confidence_level`, `uncertainty_lower`, `uncertainty_upper` | The reported uncertainty object and its scalar fields. No interval is synthesized. |
| Verification | `evaluator_id`, `evidence_tier`, `source_evidence_class`, `verification_status`, `standalone_verification`, `terminal_state`, `claim_eligible`, `claim_status`, `validity_gates`, `failure_breakdown`, `verified_manifest_refs` | Official evaluator/evidence boundary and claim gate status. Standalone state is retained but not certified by this projection. |
| Conditions | `configuration_*`, `factor_values`, `condition_digest`, `conditions`, `backend_id`, `execution_mode`, `image_digest`, `budget`, `comparability` | Reproducibility and comparability context, encoded as canonical JSON where structured. |
| Provenance | `record_root`, `report_ref`, `manifest_digest`, `metric_digest`, `dataset_*`, `provenance_*`, `limitations`, `source_id`, `record_id`, `logical_key_sha256`, `supersedes` | Content identity and source limitations needed to locate or audit the row. |

`row_kind=observation` rows are copied from the ledger's `observations` table,
including legacy records and metric states such as `invalid` or `missing`.
Historical `unit-result` records retain their source run, unit, attempt,
outcome, and optional aggregate link rather than collapsing into a summary.
For task-level analysis, select `result_granularity=unit`; use linked aggregate
rows only as reconciliation evidence so the same source outcomes are not
counted twice.

For the H20 Issue #159 import, this selector yields exactly 2,360 rows: 1,500
BiomniBench-DA, 800 CMTBench, and 60 SWE-bench Verified. The full legacy
ledger has 2,837 observations because 58 owner metrics are retained as
explicit historical reconciliation rows in addition to the 2,360 unit rows.
NatureBench contributes its existing aggregate/declaration rows and no new
task rows. The five-case SWE-bench denominator remains five even though the
public population is 500. `BiomeBench` and `BioML-Bench` have no authoritative
H20 task source and must not be inferred into this table.
`row_kind=run` rows preserve failed, non-terminal, or report-less attempts that
have no observation. Declaration-only designs are intentionally left in the
ledger's `experiments` and `catalog` tables rather than presented as results.
Missing source metadata is rendered as `unknown` (for `record_origin`) or an
empty cell; it is never guessed from a benchmark name. In particular, an
empty `code_commit`, evaluator, denominator, or record root is an explicit
provenance gap that cannot become a claim through this projection.

Rows are sorted by benchmark, dataset, split, case/question, method, model,
metric, run, row kind, and row id. No event timestamp or “latest” heuristic is
used. Structured cells use compact, sorted-key JSON in both CSV and Markdown;
empty cells render as `-` in Markdown and as an empty CSV field.

## Claim Boundary

The projection never upgrades evidence. `claim_eligible=true` is copied only
when the authoritative ledger row contains the boolean `true`; missing or
non-boolean values become `claim_eligible=false` with
`claim_status=not-derived`. The paper table does not verify reports, approve
claims, or interpret Supervisor/MCP receipts. The only claim path remains:

```text
experiment submit/status/watch
  -> immutable artifact export and BMP report
  -> bmp-verify-report
  -> bmp-lab link-run
  -> bmp-collab ledger
  -> paper_table projection
```

Supervisor logs, service status, and transport receipts are operational
provenance only; they are not metric rows. Changing code, evaluator, dataset,
or configuration creates a new run identity and a new projection row. Old rows
remain bound to their original report, manifest, commit, and record root.

## Review and Reproduction

Paper table changes are code changes, not spreadsheet edits. Reviewers should
check the fixed header, deterministic ordering/encoding, source digest and
verifier status, denominator completeness, and that invalid/missing/zero rows
remain visible. Regenerate the table from the same commit and immutable record
roots before publication; a diff is evidence of changed source facts, not a
manual correction mechanism.
