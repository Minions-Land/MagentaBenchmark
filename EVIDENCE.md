# Evidence status

MagentaBench has not yet produced a real-benchmark result that passes the full
BMP evidence and claim-gate chain. Passing unit or conformance tests proves the
implementation behavior covered by those tests; it is not benchmark-result
evidence and must not be presented as such.

The checked-in files under `records/` are retained negative examples. Their
historical reports predate current byte, lineage, network-observation, scoring,
and standalone-verification requirements. See `records/RETROACTIVE.md` before
citing any of them.

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
content-addressed TOML objects, external/inline overlays are included in the
resolved manifest identity, and standalone verification rehashes every source
reference. A custom benchmark adapter must register an explicit digest-bound
`AdapterCapability`; configuration freedom does not permit an unregistered
loader or a silent fallback.
