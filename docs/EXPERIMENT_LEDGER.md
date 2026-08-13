# Experiment Ledger

MagentaBench exposes one generated, read-only view over the repository's
experiment designs, collaboration state, runs, and metric results:

```bash
uv run --frozen bmp-collab ledger
uv run --frozen bmp-collab ledger --table runs
uv run --frozen bmp-collab ledger --table metrics
uv run --frozen bmp-collab ledger --format json
uv run --frozen bmp-collab ledger --format csv --table metrics
uv run --frozen bmp-collab ledger --map /old/artifacts=/restored/artifacts
```

This is the large table for comparing experiments. It is intentionally a
query, not a checked-in spreadsheet or a second progress board.

## Source Boundaries

| Ledger content | Authoritative source | Merge ownership |
| --- | --- | --- |
| Benchmark, dataset, evaluator, subject, protocol, factors, metrics, model, and budget | `MagentaBench/conformance/experiments/*.toml` | BMP protocol owners |
| Question, hypothesis, planned cases, repetitions, target mode, and evidence policy | `experiments/<id>/bundle.json` | One experiment branch or PR |
| Status, owner, lease, blockers, checkpoints, and linked runs | Immutable `lab/issues/<id>/` event chain | Current lease holder |
| Actual method, dataset digest/split, backend, model, metric value, denominator, and uncertainty | Standalone-verified report, record index, manifest, and indexed evidence | Evidence producer and reviewer |

The join is by stable identifiers: bundle id to BMP experiment id, bundle lab
issue to reduced lab state, lab run `report_ref` to a persisted report, and each
metric `parent_run_id` to its verified manifest. An experiment definition
without a collaboration bundle appears as `unmanaged`. A planned or failed run
can appear in the run table, but it produces no metric row. A finished run
produces metric rows only after the linked bytes pass `bmp-verify-report` and
the report, bundle, lab run, and manifest identities agree.

## Normalized Tables

The JSON output contains three tables instead of one lossy wide row:

| Table | Row identity | Purpose |
| --- | --- | --- |
| `experiments` | `experiment_id` | Stable design plus current collaboration projection |
| `runs` | `experiment_id`, `lab_run_id` | Operational run state and standalone-verification outcome |
| `metrics` | `experiment_id`, `lab_run_id`, `parent_run_id`, `metric_id` | Comparable method/data/metric result with denominator and uncertainty |

CSV emits one selected table. JSON emits all tables and is the recommended
input for a dashboard, notebook, GitHub Actions artifact, or database import.
Do not join metric ids into columns in the repository: long-form metric rows
allow new metrics and methods to merge without rewriting every historical row.
CSV keeps a fixed header even when a table has no rows. Use repeatable
`--map OLD=NEW` arguments after moving a durable record root; the ledger passes
the same relocation mapping through standalone report verification.

## GitHub Workflow

Use `main` for the common BMP runtime, schemas, adapters, registries, and this
query implementation. Use a branch such as `experiment/<experiment-id>` for
one experiment bundle and its lab records. Experiment-only work should not
change BMP semantics.

For each new experiment:

1. Open a GitHub Issue that links one `bmp-lab` issue and states the frozen
   treatment, control, held-fixed variables, primary metric, uncertainty,
   decision rule, budget, invalidation conditions, artifact destination, and
   evaluator.
2. Create or select the BMP TOML, then scaffold `experiments/<id>/`. Keep that
   directory as the independently mergeable design unit.
3. Claim and publish the lab lease before shared writes or expensive work.
4. Execute into a fresh durable record root and link every run through
   `bmp-lab link-run`; checkpoint and hand off through the existing shift
   protocol.
5. Standalone-verify the final report, review it, then merge the experiment PR.
   The ledger will expose it without editing a central table.

GitHub Actions may publish the JSON and CSV command output as disposable UI
artifacts. Those generated files are caches only; the sources above remain the
recoverable truth. Never commit credential values, authenticated locators,
machine-local scratch roots, or an exported table as benchmark evidence.

## Interpretation

`standalone_verification=verified` means the report and indexed bytes replayed
successfully. It does not make an exploratory observation a claim. A claim row
is publishable only when its report also derives `claim_eligible=true` and all
required evidence gates are positive. Missing, invalid, and zero-filled metric
counts remain separate because collapsing them into a score would change the
denominator.
