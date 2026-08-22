# Historical Benchmark Imports

MagentaBench can catalog prior benchmark designs, evaluated observations, and
recoverable assets without pretending that they were produced by the current
BMP runner. Historical imports are strict, content-addressed inputs to generated
views. The existing `experiments`, `runs`, and `metrics` tables keep their
standalone-verification semantics unchanged.

## Evidence Boundary

The ledger distinguishes record origin and evidence tier:

| Origin or tier | Meaning | May be a BMP claim? |
| --- | --- | --- |
| `bmp` / `bmp-standalone` | A lab-linked report and every indexed byte passed standalone BMP verification. | Only when the report independently sets `claim_eligible=true`. |
| `legacy-import` / `legacy-evaluated` | A pinned historical source contains an evaluator output, but it was not replayed through BMP. | No. |
| `declaration-only` | The source describes an experiment or cohort but contains no evaluated result. | No. |
| `candidate` | A recoverable asset or implementation lead still needs materialization or evaluation. | No. |

Import validation always forces legacy `claim_eligible` to false. A declaration
or candidate cannot contain metrics, and no imported row is copied into the BMP
`metrics` table. The generated `observations` table is the explicit comparison
surface across origins.

## Mergeable Layout

Use one immutable directory per exact source snapshot:

```text
imports/<source-snapshot-id>/
  source.json
  records/
    <record-id>.json
```

The source binds a normalized repository identity, full commit SHA, root tree
OID, visibility, explicit license status, and normalizer ID and digest. A
declared license requires its identifier; `not-detected` and `unknown` remain
explicit blockers rather than being interpreted as permission. A branch is
only a `ref_hint`; it never means "latest". Changing the source commit or
normalizer creates a new snapshot directory.

Records are typed as declarations, evaluated runs, unit results, or assets.
They carry whitelisted benchmark, dataset, method, model, evaluator,
execution, metric, denominator, uncertainty, and provenance fields. Arbitrary
metadata maps are not accepted. Each provenance item binds a
repository-relative path, Git blob OID, content SHA-256, and size. The
canonical payload determines both `record_id` and the record filename.

`kind=unit-result` is the task-level contract. One record binds exactly one
source run, benchmark unit, attempt, and metric outcome. Its natural identity
is `(source, experiment, source_run_id, unit_id, attempt_id, metric_id)` and
its `record_id` binds the complete condition, method/model, optional code
commit, denominator, evaluator, result status/reason, and provenance. The
result status must agree with the metric state: `success` and
`verified_fail` are observed numeric results; `missing` and `no_output` are
missing; timeout, invalid output, agent/harness/verifier/infrastructure error,
and unsupported outcomes are invalid. `source_evidence_class` retains whether
the source row was legacy evaluated evidence, a derived non-claim view, or a
historical official-harness report. All three remain under the conservative
`legacy-evaluated` BMP evidence umbrella and hard-false claim boundary.

Task rows project as `result_granularity=unit` and retain `unit_id`,
`unit_kind`, `attempt_id`, `source_run_id`, and `source_run_record_id`. The
record ID binds the exact owning run in the same source snapshot, experiment,
and condition set. Optional `aggregate_run_record_id` binds a separate,
content-addressed historical run used only for reconciliation; the ledger
derives its display `aggregate_run_id` from that record. The paired
`aggregate_reconciliation_status` says whether this metric matched, was not
compared, or mismatched; a link alone never implies equality. Neither
the import validator nor the paper projection recomputes that source-declared
status. The source-specific normalizer must reconcile the complete unit
population against the aggregate and bind the evidence bytes. Neither
relationship is execution lineage, so task rows do not project either run as
a parent. Use unit rows for a task-level paper denominator and aggregate rows only for
reconciliation; never sum or average both populations together. Validation
never guesses a link from a method name or timestamp.
Every unit metric accounts for exactly one planned unit and cannot be excluded
or zero-filled, and its aggregation is `none`; aggregation policy belongs in
the linked run record. Source and unit metric identities must match on metric
ID, definition digest, unit, and direction. Reconciliation cohorts bind the
benchmark and dataset version/snapshot, complete method/model/provider
identity, harness protocol/configuration, evaluator identity, and all declared
comparability digests. Execution settings may differ for a derived task
projector, so they are not silently treated as cohort identity. Every unit
population that names the same aggregate record and metric must declare one
consistent reconciliation status.

`unit_id`, `unit_kind`, and optional `result_reason` are identifiers or short
reason codes, not a publication channel for prompts or answers. Unit records
do not accept question text. Never copy raw questions, prompts, answers, gold
content, traces, or logs into a historical record.

Two records with the same logical identity or the same source-scoped natural
identity (experiment, run, unit attempt/metric, or asset ID) but different
contents fail closed.
Changing caller-selected `logical_key` cannot hide a duplicate run. Parent-run
references resolve inside the same experiment, while asset experiment/run
references must be unambiguous inside the same source snapshot. A
`legacy-evaluated` run must bind result or metric provenance. Across snapshots,
retain both immutable versions and use an explicit
`supersedes` edge. Cycles, missing targets, silent replacement, and implicit
timestamp or branch ordering are invalid. Set-like fields are sorted before
record hashing, so input order and omitted model defaults cannot create a second
record identity.

Supersession validation is iterative and admits at most 10,000 records and
100,000 edges per import root. Exceeding either reviewed bound fails closed;
the limits are repository policy rather than runtime tuning knobs.

## Public Repository Rule

This repository is public. The validator therefore accepts a source in the
checked-in `imports/` directory when either:

- `visibility=public`, `license_status=declared`, and a license identifier is
  present; or
- `visibility=private` and `publication_approval` binds an explicit
  `typed-results-only` decision to `Minions-Land/MagentaBenchmark`, including
  the approver, date, durable decision reference, and decision SHA-256.

The private-source exception exposes only the typed declaration and run fields
defined by this schema. Checked-in asset records remain forbidden, so an
approval cannot publish private files indirectly through metadata-only asset
rows. The original source stays marked private and its absent license stays
explicit; the approval is not rewritten as a source license. Unknown sources,
wrong-destination approvals, and unapproved private or license-undetected
sources fail with `publication-approval`, including when `imports/` is reached
through an intermediate symlink alias.

Public CI validates only checked-in canonical records and never receives a
token for private source repositories. A private companion catalog can use the
same format and be supplied explicitly to the ledger in an authorized
environment. Supplying `--imports-dir` asserts that the companion must exist: a
missing path is an error, and relative and absolute spellings normalize to the
same host-independent `<external-imports>/...` locators.

Regardless of source visibility or approval, never import credentials, `.mcp` material,
raw answers or gold data, traces, provider logs, commands, private host paths,
authenticated or provider URLs, or unrestricted metadata dictionaries. Run the
import validator and secret/path scan before review.

## Workflow

1. Select an exact clean source commit and root tree. Treat source code and
   suggested commands as untrusted input; do not import or execute it.
2. Extract only typed facts from structured JSON, CSV, or TOML. Pin every source
   byte by path, Git blob OID, SHA-256, and size.
   For a private checked-in projection, record the explicit publication
   decision in `source.json` and do not create asset records.
3. Write one `source.json` and independently reviewable records. Recompute
   aggregate metrics from per-task records when those records exist; retain a
   source summary only as a consistency input.
4. Run the offline validator and ledger exports. Candidate or missing assets
   remain explicit and cannot emit observations.
5. Use one source directory per PR. A later source snapshot adds records and
   explicit supersession edges instead of rewriting earlier imports.

The generated JSON includes `sources`, `catalog`, `observations`, and `assets`.
CSV exports one selected table. These are disposable views; the canonical
import records and, where permitted, their pinned source bytes remain the
recoverable inputs.

The ledger remains `magentabench-experiment-ledger-v2`. Existing observation
sets without `unit-result` records retain the v2 CSV header byte-for-byte.
When unit records are present, the observations CSV includes the reviewed unit,
source-run, aggregate-link, outcome, and evidence columns; consumers that need
a fixed publication schema should use the explicitly versioned paper table v2.

```bash
uv run --frozen bmp-collab validate-imports
uv run --frozen bmp-collab ledger --table sources
uv run --frozen bmp-collab ledger --table catalog
uv run --frozen bmp-collab ledger --table observations
uv run --frozen bmp-collab ledger --table assets

# An authorized private companion remains outside the public checkout.
uv run --frozen bmp-collab validate-imports --imports-dir /authorized/imports
uv run --frozen bmp-collab ledger --imports-dir /authorized/imports
```

The catalog stores a deterministic
`magentabench-catalog-condition-set-v1` wrapper and its digest. Every variant in
that wrapper contains a complete typed `conditions` object and its own digest;
this preserves multi-factor BMP experiments without selecting an arbitrary
variant. Budgets, image SHA-256, hardware, network policy, repetitions, seeds,
factors, and configuration identity remain queryable. Observation rows use the
same condition, comparability, budget, metric-unit, direction, aggregation, and
provenance shapes for BMP and historical origins. CSV encodes structured cells
as deterministic JSON rather than flattening away conditions that affect
comparability.
