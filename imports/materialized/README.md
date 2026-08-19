# External Evidence Materialization Fixtures

This directory is intentionally outside the historical-import source layout.
It contains small, public, non-claim fixtures and safe manifest examples for
the external-evidence materializer. It is not scanned into the experiment
ledger and must never contain a copied private report, answer, gold file,
trace, provider log, credential, authenticated locator, or host path.

`public-pilot/manifest.json` declares a locally testable `fixture://` object.
Its payload is project-authored and licensed CC0-1.0. It exercises digest,
size, destination, receipt, and non-claim behavior only; it is not a
NatureBench, Terminal-Bench, BiomniBench, or CMTBench result.

`naturebench-4b51202/BLOCKER.md` is the explicit intake boundary for the
current NatureBench candidate. It links the existing typed projection to the
missing immutable-byte license/provenance decision; it contains no copied
NatureBench source or result bytes.

Materialization outputs belong in a fresh directory outside this checkout.
They are reverified before a standalone verifier can consume them, and their
receipts redact runtime relocation prefixes.
