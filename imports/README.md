# Historical Imports

This directory contains content-addressed projections of benchmark work that
was produced outside the current BMP execution path. It is not a second
progress board and imported values are never BMP claims.

Each independently mergeable source snapshot uses this layout:

```text
imports/<source-snapshot-id>/
  source.json
  records/
    <record-id>.json
```

`source.json` pins one repository commit and root tree plus visibility,
license status, and the exact normalizer identity. Each record binds its source
paths and bytes, typed experimental conditions, evidence tier, comparability
limits, and explicit supersession. The record ID is the SHA-256 of its
canonical payload and must match its filename. No global index is checked in;
`bmp-collab ledger` derives the combined views.

Because this is a public checkout, checked-in sources normally must declare
`visibility=public` and a license identifier. A private source may appear only
as an explicitly approved `typed-results-only` projection bound to this
repository and a content-addressed decision. That exception permits typed
declarations and evaluated run facts, never asset records or copied private
bytes. Unapproved private and unknown sources belong in an explicitly selected
external companion directory. Natural experiment/run/asset identities and
references are validated independently of the caller-selected logical key.

Do not copy private repository data into this public repository without an
explicit publication decision. Raw answers, gold data, traces, provider logs,
commands, authenticated URLs, credentials, and machine-specific paths are
forbidden even when publication is approved. See
[`docs/HISTORICAL_IMPORTS.md`](../docs/HISTORICAL_IMPORTS.md) for the complete
boundary and import workflow.

Validate this directory offline with `uv run --frozen bmp-collab
validate-imports`. The root `README.md` and `.gitkeep` are the only non-source
entries accepted by the loader.
