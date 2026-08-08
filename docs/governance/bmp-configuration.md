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

[experiment.configuration.values.agent]
max_model_turns = 300

[factors]
"experiment.configuration.values.agent.model" = ["claude-opus-4.6", "gpt-5.4"]
```

Profiles are named references in the content-addressed configuration registry.
They can be updated or deleted without rewriting old TOML objects. Profiles
listed later, external files, and inline values override earlier values by a
deterministic deep merge. An external file uses a `[configuration]` envelope;
its bytes are recorded as an `ArtifactRef`.

```bash
bmp-config put agent.base configs/agent-base.toml
bmp-config list
bmp-config get agent.base
bmp-config delete agent.base
```

The compiler resolves the selected tree into `ResolvedManifestMetadata.configuration`.
Its digest is part of manifest identity, and standalone verification rehashes
all source refs. Editing a registry object after a run therefore invalidates
the report instead of silently changing what the run means.

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
a SHA-256 digest. An authorized host may explicitly extend `AdapterRegistry`
with an `AdapterCapability` whose digest must equal the implementation digest.
The current `Pipeline` API treats registry injection as a test override and
therefore cannot produce verified benchmark evidence; production plugin
loading remains closed until it has a separately authenticated activation
receipt. Unknown tuples fail closed; BMP never falls back to FakeBackend or a
benchmark's declared metric. The same extension point applies to backend
factories and execution adapters.

This makes the protocol open for new benchmarks while keeping execution,
scoring, lineage, and evidence verification in one shared BMP implementation.
Custom benchmarks may compile for `purpose = "exploratory"`; inactive research
claim scopes remain closed until their positive evidence classes are implemented.
