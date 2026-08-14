# MagentaBench

MagentaBench is a benchmark-side protocol for evaluating an agent without
losing track of what a result actually measures. It can evaluate a coding
agent, research agent, multi-agent system, evolving harness, or another
agent-backed workflow, provided that the benchmark, execution environment, and
evidence contract are made explicit.

It does not replace an agent framework or a benchmark's official verifier.
Instead, it binds them into one replayable experiment:

```text
experiment declaration
  -> resolved manifest
  -> benchmark loader / backend factory / execution adapter
  -> scheduled executions
  -> persisted records and report
  -> standalone verification
```

BMP owns experiment identity, case allocation, execution contracts, metrics,
and report verification. Agent harnesses, provider credentials, and
benchmark-native verifiers remain adapter-owned boundaries.

## Start Here

Choose the path that matches the work you want to do.

| Goal | Start with | What you get |
| --- | --- | --- |
| Verify a checkout without a model or API key | [Five-minute smoke run](#five-minute-smoke-run) | A compiled and independently verified exploratory report |
| Run an existing agent/benchmark pairing | [Existing paths](#existing-paths-and-readiness) | The precise readiness state and prerequisites |
| Evaluate a new agent or harness | [Bring any agent](#bring-any-agent) | The adapter and capability contract |
| Add a benchmark or execution target | [Extend the system](#extend-the-system) | The supported extension boundary |
| Coordinate people and agents | [Collaborate safely](#collaborate-safely) | Leases, checkpoints, and mergeable work units |
| Compare current and historical experiments | [Experiment ledger](docs/EXPERIMENT_LEDGER.md) | Provenance-aware design, run, observation, and asset tables |
| Recover Git, Python, or OCI inputs | [Mirror acceleration](docs/MIRROR_ACCELERATION.md) | Fetch-only Git, locked Python, and digest-bound image acquisition |

## What Makes a Result Useful

An agent score is only interpretable when the things that could have changed
are explicit. MagentaBench therefore binds the benchmark, subject, backend,
protocol, case order, seed, budget, resolved configuration, and adapter code
into the manifest identity. Changing any of those creates a different
experiment rather than silently changing the meaning of a number.

This matters for every agent type. A coding agent can change its tools or
workspace policy; a research agent can change its retrieval or evaluator; a
multi-agent system can change delegation, memory, or coordination; an evolving
system can change its search procedure. Those are legitimate factors to study,
but they must be declared and observed rather than hidden behind a model name
or a successful process exit.

The declared run purpose selects the report type: `purpose = "exploratory"`
produces `observation_report.json`, while `purpose = "claim"` produces
`claim_report.json`. A claim report is not automatically publishable. It must
still have `claim_eligible = true`, satisfy every required gate, and pass
standalone verification. A requested model name, a green process, a schema
declaration, or a fake-fixture score is not evidence of a real-agent result.

## Install

Requirements:

- Python 3.10 or newer for the core and subprocess conformance paths
- Python 3.12 or newer for the Harbor/Terminal-Bench path
- [uv](https://docs.astral.sh/uv/)
- Docker only for container-backed benchmark paths

Use the locked project environment. On the Aliyun host, the checked-in lock
uses the documented mirror; choose an equivalent reproducibility policy before
rewriting `uv.lock` elsewhere.

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ uv sync --frozen --extra test

# Confirm the repository state before modifying or running shared work.
uv run --frozen bmp-agent validate
uv run --frozen bmp-agent modes
uv run --frozen bmp-lab doctor
```

`bmp-agent` is the agent-facing alias for `bmp-collab`. It reports the derived
experiment queue and execution-mode readiness; it does not launch a model.

Check the host's acceleration policy without fetching Git or pulling an image:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
  uv run --frozen python -m MagentaBench.acquisition.cli doctor
```

Mirrors are transport/cache locations only. GitHub `origin` remains the only
push target, and OCI identity remains the canonical repository plus immutable
digest. The fetch-only Git setup, pinned Terminal-Bench OCI specs, cache-only
verification, acquisition receipts, and Apptainer/cloud boundaries are in
[`docs/MIRROR_ACCELERATION.md`](docs/MIRROR_ACCELERATION.md).

The repository-wide experiment table is generated from its authoritative
records, so parallel branches never edit a shared spreadsheet:

```bash
uv run --frozen bmp-collab ledger
uv run --frozen bmp-collab ledger --table metrics
uv run --frozen bmp-collab ledger --format csv --table metrics
uv run --frozen bmp-collab ledger --table observations
uv run --frozen bmp-collab validate-imports
```

The BMP truth tables include every checked-in declaration. Run and metric rows
are added only through lab-linked, standalone-verified evidence. The unified
catalog and observation views may also include strict historical imports, but
they preserve origin, evidence tier, comparability, and `claim_eligible=false`
instead of laundering legacy results into BMP metrics. See
[`docs/EXPERIMENT_LEDGER.md`](docs/EXPERIMENT_LEDGER.md) for the data model and
GitHub workflow, and [`docs/HISTORICAL_IMPORTS.md`](docs/HISTORICAL_IMPORTS.md)
for the public/private import boundary.

## Five-Minute Smoke Run

This path executes a deterministic subprocess fixture. It calls no model,
requires no provider credential, and is useful for validating an installation
or an integration before spending money on a real run.

```bash
# Compile first: this checks that the declared benchmark, subject, backend,
# protocol, and metrics resolve into canonical manifests.
uv run --frozen bmp-compile \
  MagentaBench/conformance/experiments/subprocess-echo-smoke.toml >/dev/null

# A record root must be fresh for every execution. Do not pre-create it and do
# not reuse it for a different invocation.
record_root="../magentabench-records/subprocess-echo-$(date -u +%Y%m%dT%H%M%SZ)"

uv run --frozen bmp-run \
  MagentaBench/conformance/experiments/subprocess-echo-smoke.toml \
  --record-root "$record_root"

# Verify the report from its retained bytes, independently of the run command.
uv run --frozen bmp-verify-report \
  "$record_root/subprocess-echo-smoke/observation_report.json"
```

The final command must report `verified`. Its report is still exploratory: the
fixture proves the protocol path, not an agent's real-world capability.

## Bring Any Agent

MagentaBench is deliberately agent-neutral. Your agent does not have to be
Magenta, Codex, Claude Code, or a particular SDK. It needs a registered,
digest-bound way to run inside a benchmark and to emit the evidence that its
claim scope requires. This is an adapter-development path, not a generic
`--agent-command` flag: the current system intentionally fails closed when an
integration has not been registered.

For a new agent or harness, follow this sequence:

1. Define the question first: which benchmark, task split, official verifier,
   primary metric, budget, and comparison factor are fixed?
2. Add or select a **subject declaration** that pins the agent source,
   adapter name, interface, launch identity, and trace capability. A subject
   declaration identifies the agent; it does not execute it.
3. Select a **backend declaration** and registered `BackendFactory` that own
   the process, container, or remote-sandbox lifecycle and its runtime and
   isolation evidence.
4. Implement and register an `ExecutionAdapter` for the exact
   `(benchmark adapter, backend adapter, subject interface)` tuple. It invokes
   the agent and supplies the reset hook required by the protocol's
   `state_reset` policy. Unknown combinations fail at compile time; BMP never
   falls back to a fake backend.
5. If the benchmark is new, add a digest-bound `BenchmarkLoader` while keeping
   scoring authority in the benchmark's official verifier.
6. Write an experiment TOML that pins the benchmark, dataset, evaluator,
   subject, protocol, metrics, and execution budget.
7. Compile, run one preregistered exploratory case into a fresh record root,
   and verify the persisted report before scaling out.

The binding configuration and adapter contract is in
[`docs/governance/bmp-configuration.md`](docs/governance/bmp-configuration.md).
The existing Magenta/Terminal-Bench path shows all of these pieces together:

| Contract | Example |
| --- | --- |
| Subject declaration | [`registries/subjects/terminal-bench-magenta.toml`](registries/subjects/terminal-bench-magenta.toml) |
| Backend declaration | [`registries/backends/harbor-020-terminal-bench.toml`](registries/backends/harbor-020-terminal-bench.toml) |
| Benchmark loader capability | [`registries/adapters/terminal-bench-loader.toml`](registries/adapters/terminal-bench-loader.toml) |
| Backend factory capability | [`registries/adapters/terminal-bench-backend.toml`](registries/adapters/terminal-bench-backend.toml) |
| Execution capability | [`registries/adapters/terminal-bench-execution.toml`](registries/adapters/terminal-bench-execution.toml) |
| Adapter implementation | [`plugins/terminal_bench/`](plugins/terminal_bench/) |
| Protocol | [`registries/protocols/terminal-bench-probe.toml`](registries/protocols/terminal-bench-probe.toml) |
| Experiment declaration | [`MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml`](MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml) |

### Configuration and Comparisons

Use configuration profiles, files, raw provider-native documents, or explicit
overrides to make agent controls part of experiment identity. The following is
a template: create the profile envelope, local configuration, and experiment
TOML before running it.

```bash
profile_toml=path/to/profile-envelope.toml
local_config=path/to/local-configuration.toml
experiment_toml=path/to/experiment.toml

uv run --frozen bmp-config put agent.base "$profile_toml"
uv run --frozen bmp-config list

uv run --frozen bmp-compile "$experiment_toml" \
  --profile agent.base \
  --config "$local_config" \
  --set agent.max_model_turns=300
```

Profiles are composed deterministically and their source bytes are recorded.
Configuration paths are adapter-owned: `agent.max_model_turns` above is an
illustrative path and has an effect only when the selected adapter declares,
consumes, and reports its activation. Configuration alone does not create a
comparison. Register the factor being varied and declare its control/treatment
contrast and repetitions in the experiment. The verified
[`subprocess-echo-smoke.toml`](MagentaBench/conformance/experiments/subprocess-echo-smoke.toml)
is a complete small example of a one-factor, counterbalanced comparison.
Put provider credentials in the environment or an external secret manager,
never in TOML, Git, the lab ledger, or a record root. A provider-backed claim
also needs an observed `ModelActivationReceipt` and observable usage; a CLI
flag alone does not prove that the requested model actually ran.

### Multi-Agent and Evolving Systems

A multi-agent or evolving system is still a subject plus an execution contract.
Its adapter must retain the orchestration state that explains the observed
result: delegation/coordination behavior, candidate transitions, evaluator
queries, budgets, and terminal outcomes. Rejected, invalid, and failed
candidates remain in the evidence; they are not filtered out after the fact.

The protocol provides a neutral lifecycle for `evolver` and `meta_evolver`
scopes. New systems add an adapter and capability tuple rather than teaching
the BMP core about a particular prompt optimizer, workflow, memory design, or
agent framework. See
[`docs/governance/bmp-configuration.md`](docs/governance/bmp-configuration.md)
for the evidence boundary.

## Extend the System

Use the smallest boundary that matches the change.

| You are adding | Add it here | Do not do this |
| --- | --- | --- |
| A benchmark | A digest-bound `BenchmarkLoader` and benchmark declaration | Reimplement its native verifier in BMP |
| An agent/harness | A subject declaration, execution adapter, and exact compatibility tuple | Hard-code an agent name in the compiler |
| A backend | An isolated `BackendFactory`, adapter declaration, and receipts | Treat a cloud product name as isolation evidence |
| A provider/model | A secret-free provider binding plus observed activation evidence | Store an API key or infer activation from a requested value |
| A setting comparison | A declared factor and resolved configuration | Change tools, retries, or budgets outside the manifest |

Execution modes and their current ceilings are derived from the registry:

```bash
uv run --frozen bmp-agent modes
```

`local-process` and registered Docker paths can be BMP-gated, while
AppContainer, E2B, and other remote sandbox slots remain exploratory until an
adapter closes their runtime, network, export, teardown, and verification
boundaries. Read
[`docs/governance/EXECUTION_MODES.md`](docs/governance/EXECUTION_MODES.md)
before adding a new target.

## Existing Paths and Readiness

The repository includes conformance coverage for fake, deterministic,
subprocess, and repeated-sampling paths. It also includes a
Terminal-Bench/Harbor/Magenta integration, but it is **not currently a
claim-ready real-model result**. The pinned Terminal-Bench Docker images,
provider binding, and observed provider/model activation path must be restored
before a real-agent run can be promoted beyond exploratory status.

Start every new run with the derived state rather than assuming a path is
ready:

```bash
uv run --frozen bmp-agent next
uv run --frozen bmp-agent modes
uv run --frozen bmp-lab status
```

The current inventory and blockers are in
[`docs/EXPERIMENT_MATRIX.md`](docs/EXPERIMENT_MATRIX.md), and the authoritative
evidence status is in [`EVIDENCE.md`](EVIDENCE.md).

## Read the Output Correctly

Each execution writes a fresh record root containing plans, per-run records,
an aggregate, and one of these reports:

```text
<record-root>/
  <experiment-id>/
    plan.json
    events.jsonl
    record_index.json
    manifests/
    <manifest-digest>/
      cases/
    aggregate.json
    observation_report.json  # purpose = "exploratory"
    claim_report.json        # purpose = "claim"; eligibility is inside
```

The ignored `.runs/` directory is scratch space, not durable publication
storage. Preserve partial records before cleanup and retain the whole
experiment directory: `record_index.json`, manifests, case evidence, aggregate,
and report are one verification unit. The only publishable result is a
persisted report whose referenced bytes can be independently reloaded and
verified. For a claim report, standalone verification is necessary but not
sufficient; `claim_eligible` and every claim gate must also be positive.

## Collaborate Safely

The `lab/` ledger coordinates active owners, leases, blockers, checkpoints,
and run links. It is not a second benchmark result store and it does not make
provider calls exactly-once.

```bash
uv run --frozen bmp-lab doctor
uv run --frozen bmp-lab status
uv run --frozen bmp-lab show <issue-id>
uv run --frozen bmp-lab recover <issue-id>
```

For a new experiment, create one mergeable bundle around an existing BMP TOML.
This is a template: the BMP file and a lab issue already bound to that exact
file must exist first.

```bash
experiment_id=my-experiment
bmp_spec=path/to/my-experiment.toml
lab_issue=existing-bound-lab-issue

uv run --frozen bmp-collab scaffold "$experiment_id" \
  --bmp-spec "$bmp_spec" \
  --lab-issue "$lab_issue" \
  --question "What is being tested?" \
  --hypothesis "What should change?" \
  --stop-condition "Stop on an infrastructure or verifier failure."
```

Claim the issue's declared write scope before editing shared files or starting
an expensive run. Publish the lease, checkpoint before interruption, and use a
new record root for a new execution. One bundle directory or one lab issue is
the normal unit of parallel work. The full process is in
[`docs/EXPERIMENT_COLLABORATION.md`](docs/EXPERIMENT_COLLABORATION.md) and
[`docs/GITHUB_DEVELOPMENT.md`](docs/GITHUB_DEVELOPMENT.md). Lease, checkpoint,
and recovery semantics are documented in
[`docs/LAB_OPERATIONS.md`](docs/LAB_OPERATIONS.md).

## Contributor Checks

For a source or documentation change, run the checks appropriate to its scope:

```bash
uv run --frozen --extra test pytest -q
uv run --frozen python -m compileall -q MagentaBench plugins tests
bash scripts/audit_hcp_boundary.sh
uv run --frozen bmp-agent validate
uv run --frozen bmp-lab doctor
git diff --check
```

The full recovery, preflight, artifact, and claim-promotion procedure is in
[`docs/EXPERIMENT_RUNBOOK.md`](docs/EXPERIMENT_RUNBOOK.md). Read the project
rules in [`AGENTS.md`](AGENTS.md) before changing protocol, adapter, registry,
or experiment work.
