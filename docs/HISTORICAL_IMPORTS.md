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
OID, visibility, and normalizer ID and digest. A branch is only a `ref_hint`;
it never means "latest". Changing the source commit or normalizer creates a new
snapshot directory.

Records are typed as declarations, evaluated runs, or assets. They carry
whitelisted benchmark, dataset, method, model, evaluator, execution, metric,
denominator, uncertainty, and provenance fields. Arbitrary metadata maps are
not accepted. Each provenance item binds a repository-relative path, Git blob
OID, content SHA-256, and size. The canonical payload determines both
`record_id` and the record filename.

Two records with the same logical identity but different contents fail closed
inside one snapshot. Across snapshots, retain both immutable versions and use
an explicit `supersedes` edge. Cycles, missing targets, silent replacement, and
implicit timestamp or branch ordering are invalid.

## Public Repository Rule

This repository is public. Public CI validates only checked-in canonical bytes
and never receives a token for private source repositories. Before publishing
any projection from a private source, obtain an explicit visibility decision
and review a whitelisted extraction. A private companion catalog can use the
same format and be supplied to the ledger in an authorized environment.

Regardless of source visibility, never import credentials, `.mcp` material,
raw answers or gold data, traces, provider logs, commands, private host paths,
authenticated or provider URLs, or unrestricted metadata dictionaries. Run the
import validator and secret/path scan before review.

## Workflow

1. Select an exact clean source commit and root tree. Treat source code and
   suggested commands as untrusted input; do not import or execute it.
2. Extract only typed facts from structured JSON, CSV, or TOML. Pin every source
   byte by path, Git blob OID, SHA-256, and size.
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
