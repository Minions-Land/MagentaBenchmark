# Terminal-Bench verifier runtime probe v2

This is a public, path-safe metadata receipt for the preregistered
`terminal-bench-regex-smoke-900s` exploratory run. It is not the raw record,
an Agent score, or a claim-ready result.

## Result

- The unchanged official verifier completed in 263.295 seconds.
- The no-provider subject did not create `/app/regex.txt`, so the case is
  `verified_fail` with reward `0.0`, not `verifier_error` or `timeout`.
- Input, output, and total tokens are all `0`; cost is `0.0`.
- The schedule is valid and its budget ledger reconciles exactly.
- `bmp-verify-report` passed against the retained original bytes.

## Publication boundary

The original 44-file record remains byte-for-byte unchanged outside Git. It
contains machine-specific absolute references and depends on 1,125
content-addressed source files. Publishing those bytes directly would make the
repository non-portable and expose host layout. `PUBLIC_RECEIPT.json` records
the inventory digest and the important report, index, manifest, verifier, and
schedule digests without copying those paths.

The run is not linked into the generated experiment ledger yet. Issue #77 must
materialize the retained bytes behind explicit relocation maps and repeat
standalone verification before a finished run link is safe to merge.

## Remaining gaps

This run does not satisfy runtime identity or isolation acceptance: image,
executable, runtime-manifest, and container receipt identities are absent, and
the network observation is `unobservable`. Issue #37 remains open. A future
fresh run must close those fields; this result must not be relabeled after the
fact.
