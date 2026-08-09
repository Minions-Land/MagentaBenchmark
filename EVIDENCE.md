# Evidence status

MagentaBench has not yet produced a real-benchmark result that passes the full
BMP evidence and claim-gate chain. Passing unit or conformance tests proves the
implementation behavior covered by those tests; it is not benchmark-result
evidence and must not be presented as such.

Two real exploratory probes are now retained in the standard
`bmp-integration-probe-v1` format:

- `records/swebench-astropy-6938-probe/probe.json`: a retained candidate patch
  and public input from a manually observed SWE-bench attempt. Codex/model
  identity and the two focused-test outcomes survive only in the historical
  summary prose; the probe schema rejects unbound `details` and `usage`, and
  `bmp-verify-probe` does not replay or substantiate them. The
  execution adapter and model activation were not part of the BMP Pipeline, so
  this is not a claim or independently proven Agent result.
- `records/terminal-bench-regex-probe/probe.json`: one Terminal-Bench 2.1 task
  entered Docker through Harbor 0.20.0. The official verifier could not
  download `uv` and then reported `uvx: command not found`; the probe is
  classified as `verifier_failure`, not an Agent score. The retained excerpt
  and secret-free Harbor projection are adjacent to the record.

Both records pass `bmp-verify-probe`, meaning every declared identity digest is
recomputed from a retained manifest/reference and every retained reference is
rehashed. This proves the bound bytes and failure taxonomy only. It does not
turn historical prose, a projected native result, or an operator observation
into independently replayed execution evidence, and proves neither benchmark
quality nor a leaderboard number.

Other checked-in AOSE reports and historical records are retained negative
examples. They predate current byte, lineage, network-observation, scoring, and
standalone-verification requirements. See `records/RETROACTIVE.md` before citing
any of them; the two integration-probe directories above use the newer probe
format but remain explicitly non-claim evidence.

A future successful result is publishable only when its persisted report can
be reloaded from production JSON and independently verified from its
content-addressed `RecordIndex`, complete lineage, and referenced bytes. For a
claim, every primitive gate must additionally have positive evidence; a score
or a successful process exit is insufficient.

BMP is the Benchmark-side protocol. HCP is Magenta's Harness Component
Protocol. BMP may consume a canonical resolved sidecar emitted through the
Magenta adapter, but no MagentaBench artifact or test is evidence that BMP owns
or reimplements HCP resolution.

Configuration profiles are also BMP artifacts: registry names resolve to
content-addressed TOML objects, envelope/raw/inline/CLI overlays are recorded
as a replayable composition, JSON Schema is checked before execution, and
standalone verification rehashes every source reference. A custom benchmark
adapter must register explicit digest-bound loader, backend, and exact
execution capabilities; configuration freedom does not permit an unregistered
loader or a silent fallback. Project-loaded adapter declarations, entrypoint
bytes, and local import-closure bytes are manifest-bound and rehashed by
standalone verification. Magenta v0.1.23 requested/effective settings and the
final HCP sidecar activation receipt are retained as runtime evidence; this is
not evidence that BMP owns HCP resolution.

Evolution subjects have a neutral `EvolutionRunEvidence` contract plus an
executable BMP-owned `EvolutionRuntime`. The deterministic registered adapter
has run evolver and meta-evolver cases through Pipeline and standalone report
verification. Its runtime receipt binds every search/holdout query, the
selection-before-holdout order, exact budget debits, and recursively reconciled
parent usage. These are provider-free exploratory conformance runs; their local
process network boundary is explicitly unobservable, so they do not establish
production isolation or a claim-ready benchmark result.

The current protocol surface also includes:

- content-addressed JSON custom case ordering, rechecked at compile, load,
  schedule, and standalone-verification time;
- a generic factor contrast (`factor_path`, `control_value`,
  `treatment_value`) alongside the legacy subject-ID contrast; and
- a preregistered statistical plan/receipt with paired units, sample variance,
  confidence interval method, holdout split, and multiple-comparison control.

These are implemented contracts. Real models now require a digest-bound
execution capability, an optional secret-free `ProviderBinding`, and a runtime
`ModelActivationReceipt`; Pipeline preserves a missing native observation as
`unobserved`, while claim and standalone gates require an exact match plus
observable token/cost usage. No real benchmark record has yet produced that
complete positive receipt, so this path still has no positive real claim.

The read-only HCP authority binding is tracked at
`docs/authority/magenta-hcp-authority.json`; it pins Magenta commit
`78e2998f5bb78aa029c5cfe6f9508777f307679d` and is independently verified by
`bmp-verify-authority`. BMP remains the benchmark-side protocol; HCP remains
Magenta's protocol.
