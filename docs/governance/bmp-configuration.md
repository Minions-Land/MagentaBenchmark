# BMP Configuration And Adapter Contract

This document is binding for configuration and benchmark integration. BMP is
the benchmark-side protocol; HCP remains owned by Magenta and crosses the
boundary only as its opaque resolved sidecar.

## Configuration

Configuration is an identity-bearing tree, not a fixed list of agent fields.
Adapters own the meaning of paths. BMP owns composition order, JSON/TOML-safe
types, secret-like key rejection, source bytes, and the final digest.

```toml
[experiment]
id = "agent-budget-sweep"
benchmark = "fake.exact.v1"
subject = "fake.control"
protocol = "fake.deterministic.v1"

[experiment.configuration]
profiles = ["agent.base", "agent.opus-4-6"]
files = ["configs/local-agent.toml"]
raw_files = ["configs/provider-native.toml"]

[experiment.configuration.values.agent]
max_model_turns = 300

[factors]
"experiment.configuration.values.agent.model" = ["claude-opus-4.6", "gpt-5.4"]
```

Profiles are named references in the content-addressed configuration registry.
They can be updated or deleted without rewriting old TOML objects. Profiles
listed later, external files, and inline values override earlier values by a
deterministic deep merge. An external file uses a `[configuration]` envelope;
its bytes are recorded as an `ArtifactRef`. `raw_files` is a separate,
explicit opt-in for a raw TOML document. A raw document containing a
`[configuration]` table is rejected rather than silently changing mode. The
compiler records the ordered composition recipe (including `extends`, source
mode, ownership, merged JSON Schema, and inline overlays), so standalone
verification can replay the same tree instead of checking bytes only.

```bash
bmp-config put agent.base configs/agent-base.toml
bmp-config list
bmp-config get agent.base
bmp-config delete agent.base

# Experiment-local overlays (never mutate the registry):
bmp-compile experiment.toml --profile agent.base \
  --config configs/local-agent.toml \
  --raw-config configs/provider-native.toml \
  --set agent.model='"gpt-5.4"'
bmp-run experiment.toml --record-root records/run-1 --set agent.reasoning_effort='"high"'
```

The compiler resolves the selected tree into `ResolvedManifestMetadata.configuration`.
Its digest is part of manifest identity, and standalone verification rehashes
all source refs. Editing a registry object after a run therefore invalidates
the report instead of silently changing what the run means.

Configuration is only one factor in a fair comparison. The resolved manifest
also binds the benchmark loader, subject, backend, protocol, case-order policy,
seed, budget, and adapter closure. Case order may be `fixed`,
`seeded_random`, `random`, `custom`, or `explicit`; the observed order and
allocation ledger are recorded. Counterbalancing is applied before execution,
and `allowed_diff` compares semantic resolved projections rather than source
TOML formatting. Thus model, agent, harness, and evolution factors can vary in
one experiment without allowing an unlisted transport, cache, tool, workspace,
or retry change to hide in the comparison.

The optional profile/file `schema` is validated with JSON Schema during
composition (`check_schema` plus value validation). TOML date/time values are
normalized to ISO strings before entering the JSON identity. Secret-like keys
are rejected recursively, while ordinary controls such as `max_tokens` and
`token_budget` remain valid.

## Benchmark adapters

An external benchmark can declare `kind = "custom"` and keep benchmark-native
task/verifier semantics in its adapter-owned `config` tree:

```toml
[benchmark]
id = "terminal-bench.v3"
kind = "custom"
adapter = "terminal-bench"
bmp_version = "0.1"
source = "/opt/benchmarks/terminal-bench"
content_globs = ["tasks/*.yaml", "verifier.py"]
verifier = "terminal_bench.verifier:v3"
scoring_kind = "continuous"
authoritative_reward_metric = "reward"

[benchmark.config]
task_split = "test"
```

The adapter must implement the existing `BenchmarkLoader` contract and expose
a SHA-256 digest. Put a digest-bound `[adapter]` declaration under
`registries/adapters/*.toml`; `Pipeline` verifies and loads it from the project
root. A production custom run must declare all three resolved capabilities:
`benchmark_loader`, the selected `backend_factory`, and the exact execution
compatibility tuple `(benchmark_adapter, backend_adapter, subject_interface)`.
The compiler fails before execution when one is missing. The resolved
declaration, entrypoint bytes, and statically discovered local Python import
closure are included in the manifest; standalone verification rehashes every
byte. Unused plugin entrypoints are not imported. An authorized host may also
explicitly extend `AdapterRegistry` with the same `AdapterCapability`.
Unknown tuples fail closed; BMP never falls back to FakeBackend or a
benchmark's declared metric. The same extension point applies to backend
factories and execution adapters.

```toml
[adapter]
id = "terminal-bench"
kind = "adapter"
adapter = "terminal-bench"
bmp_version = "0.1"
adapter_kind = "benchmark_loader"
source = "plugins/terminal-bench"
entrypoint = "loader.py:TerminalBenchLoader"
digest = "<sha256 of loader.py>"
supported_benchmark_kinds = ["custom"]
```

The `digest` remains the entrypoint digest for compatibility. The resolved
artifact additionally binds the helper closure digest and relative paths, so a
helper-only edit changes the manifest identity and invalidates old evidence.

For the Magenta subject adapter, generic configuration paths are projected to
Magenta's public v0.1.23 flags (`transport`, cache retention, prompt-cache
mode, cache telemetry/diagnostics, tool search, provider, and model). Retry and
provider timeout controls are materialized only through the explicit
`MAGENTA_CODING_AGENT_DIR` settings bridge. The adapter persists requested and
runtime-effective projections, their digests, the final runtime-manifest
sequence, and the HCP activation receipt. A requested value without matching
effective evidence is recorded as an activation mismatch, never assumed to
have taken effect.

The adapter may project that record into the neutral
`ConfigurationActivationReceipt`, which binds the resolved configuration
artifact digest, consuming adapter, requested/activated paths, secret-free
projections, and their digests. Claim provenance requires a `matched` receipt
when a configuration artifact is present; exploratory provenance can retain an
unobserved configuration for diagnostics without turning it into a claim.

## Provider/model activation

A provider-backed model is a separate trust boundary from generic adapter
configuration. The execution declaration may pin a secret-free
`ProviderBinding`; its credential reference contains only a name and SHA-256 of
the credential value, never the value itself. The selected execution capability
must also declare how native activation will be observed:

```toml
[execution]
backend = "provider-backed-harness"
model = "gpt-5.6"

[execution.provider_binding]
provider_id = "openai"
base_url = "https://api.openai.com/v1"
wire_api = "responses"
model_id = "gpt-5.6"

[execution.provider_binding.credential_ref]
name = "openai-primary"
value_sha256 = "<sha256>"
secret = true
source_file = "credentials/providers.toml"

# In the selected execution AdapterCapability:
# model_activation_source = "provider_response" | "runtime_manifest" |
#                           "native_result" | "adapter_receipt"
```

After execution, the adapter emits a `ModelActivationReceipt` that records the
requested and activated provider/model, the relocatable provider-binding
digest, its declared observation source, and content-addressed native evidence.
BMP never treats a CLI flag or the requested manifest value as proof of
activation. A missing observation is persisted as `status = "unobserved"` so an
exploratory run can retain the provider call and diagnostics, but isolation and
claim gates fail closed. A claim additionally requires observable total-token
and monetary-cost usage; unknown values remain `null`, never zero.

## Evolution and meta-evolution boundary

Evolution-oriented systems are intentionally modeled as protocol participants,
not as a growing list of named benchmarks. An evolution adapter may expose a
hand-designed procedure, an agent-controlled search, an editable workflow, a
memory design, or a meta-controller through the same neutral lifecycle:

```text
seed -> candidate generation -> feedback/verification -> revision
     -> accept/reject/invalid -> next search state -> termination
```

The adapter is responsible for serializing every candidate and search-state
transition. BMP is responsible for the external evaluation contract, budgets,
ordering, isolation namespaces, and immutable lineage. Rejected and invalid
candidates remain in the record; they are never filtered into a successful
subset. A meta-evolution adapter nests the same lineage for the mechanism that
changes the search procedure, memory, or harness, while keeping the evaluator
and claim gate outside that mechanism.

This boundary can represent AEVO-style interactive steering, ADAS/DGM-style
workflow evolution, and memory/meta-agent evolution without making BMP choose a
mutation operator, prompt optimizer, or internal HCP product. New systems need
an adapter declaration and capability tuple, not a change to BMP core.

This makes the protocol open for new benchmarks while keeping execution,
scoring, lineage, and evidence verification in one shared BMP implementation.
`EvolutionRunEvidence` remains algorithm-neutral. The BMP-owned
`EvolutionRuntime` now supplies the external lifecycle around an adapter and
emits a content-addressed `EvolutionRuntimeReceipt`. The receipt retains every
search or holdout evaluator query, exact budget debits and remaining caps, and
the transition sequence at which selection became immutable. The sealed
holdout authority is injected separately and cannot be invoked until after the
select transition. Meta-evolution additionally debits the recursively verified
parent runtime into the outer budget ledger.

The registered `deterministic_evolution` adapter exercises this full path
through Pipeline without a model provider. It is a conformance runtime, not a
claim that an in-process adapter is adversarially isolated: its exploratory
report deliberately records the local process network boundary as unobservable
and therefore has `isolation_valid=false`. Production adapters must provide
their own process/container isolation and evaluator authorities while reusing
the same receipt and standalone-verification contract.
