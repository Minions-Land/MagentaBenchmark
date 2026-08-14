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

For that final identity check, the ledger recompiles the bundle's SHA-256-pinned
BMP declaration and compares every resolved `(run_id, manifest_digest)` with
the standalone-verified manifests. The digest covers benchmark, dataset,
evaluator, metrics, protocol, backend, subject, model, regime/stage, factors,
configuration, evolver, and budget identity. Record-index order is not used as
identity because parallel runs may finish in another order; missing, extra,
duplicate, or digest-drifted run IDs still fail closed.

After repository validation, the ledger captures one reduced lab state snapshot
and uses that snapshot for every projection row. The linked report is verified
from a private copy of the exact bytes named by its lab SHA-256/size, and each
manifest is hashed and parsed from one read. Replacing a path after verification
therefore produces an error and no metric rows. Duplicate BMP experiment IDs
also make the command nonzero rather than silently selecting one declaration.

## Normalized Tables

The JSON output contains three tables instead of one lossy wide row:

| Table | Row identity | Purpose |
| --- | --- | --- |
| `experiments` | `experiment_id` | Stable design plus current collaboration projection |
| `runs` | `experiment_id`, `lab_run_id` | Operational run state and standalone-verification outcome |
| `metrics` | `experiment_id`, `lab_run_id`, `parent_run_id`, `metric_id` | Comparable method, resolved factors, configuration identity, data, metric result, denominator, and uncertainty |

CSV emits one selected table. JSON emits all tables and is the recommended
input for a dashboard, notebook, GitHub Actions artifact, or database import.
Do not join metric ids into columns in the repository: long-form metric rows
allow new metrics and methods to merge without rewriting every historical row.
Each metric row carries the verified manifest's resolved `factor_values` and
configuration id, digest, and profile ids, so two identically named methods
with different effective settings do not collapse into one comparison cell.
`method_id` names the subject, evolver, or meta-evolver; configuration remains
in its own columns and never replaces method identity. Experiment rows expose
`run_count`, sorted `run_ids`, and all observed `run_states`. They deliberately
do not claim a "latest" run because the reduced lab model sorts stable run IDs,
not event timestamps.
CSV keeps a fixed header even when a table has no rows. Use repeatable
`--map OLD=NEW` arguments after moving a durable record root; the ledger passes
the same relocation mapping through standalone report verification. Mapping
prefixes must be normalized absolute POSIX paths without `.` or `..` segments.
JSON and CSV remain machine-readable on success; any source or verification
error makes the command nonzero and is also reported on stderr.

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

GitHub Actions checks out complete Git history and may publish JSON and
CSV command output as disposable UI artifacts. Repository-only experiment rows
need no external data. A finished run appears only when its content-addressed
report, index, manifests, and referenced evidence are available to the job.
Local or provider-specific exporters may materialize those bytes elsewhere and
pass explicit `--map OLD=NEW` arguments. The checked-in workflow does not yet
guess an artifact store, download remote evidence, or accept unverified bytes;
once a finished external run is linked, unavailable bytes intentionally fail
the job until a separately reviewed materialization step is installed. That
materializer is a follow-up to the source-only GitHub integration and must bind
each downloaded byte to its recorded digest before invoking the ledger.

Generated files are caches only; the sources above remain the recoverable
truth. Paths outside the checkout are represented by content digests (or an
`<external>` marker for a non-artifact root), so two materialization hosts
produce the same tables. Never commit credential values, authenticated
locators, machine-local scratch roots, or an exported table as benchmark
evidence.

## Interpretation

`standalone_verification=verified` means the report and indexed bytes replayed
successfully. It does not make an exploratory observation a claim. A claim row
is publishable only when its report also derives `claim_eligible=true` and all
required evidence gates are positive. Missing, invalid, and zero-filled metric
counts remain separate because collapsing them into a score would change the
denominator.
