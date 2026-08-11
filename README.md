# MagentaBench

MagentaBench implements the Benchmark-side Protocol (BMP) for running agents
against external benchmarks with replayable, content-addressed evidence. BMP
owns experiment identity, case allocation, execution contracts, metrics, and
standalone verification. Magenta's Harness Component Protocol (HCP), provider
credentials, and benchmark-native verifiers remain owned by their adapters.

## Current Readiness

The repository currently contains protocol and conformance coverage, including
fake, deterministic, subprocess, and repeated-sampling fixtures. It does not
yet contain a claim-ready real-model benchmark result. The checked-in
Terminal-Bench path has Harbor 0.20.0, a native loader/backend, and a
digest-bound `magenta-cli` execution capability alongside the `harbor-nop`
probe. The pinned Terminal-Bench Docker images, provider binding, and a real
provider/model activation path must be restored before a real-agent run.

Treat every retained probe as exploratory unless its persisted report passes
the current standalone verifier and all claim gates. A process exit code,
schema declaration, requested model name, or fake/conformance score is not
evidence of a real model result.

## Quick Checks

Use the project virtual environment or `uv`; the system Python is not the
project runtime. Resolve the dependency-source policy before changing
`uv.lock`: an Aliyun index is useful on this host, but it must be an explicit
reproducibility decision rather than an incidental `uv run` side effect.

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ uv sync --frozen --extra test
uv run --extra test pytest -q
uv run python -m compileall -q MagentaBench plugins tests
bash scripts/audit_hcp_boundary.sh
git diff --check
```

`uv.lock` records the same Aliyun package URLs for reproducible installs. The
`test` extra includes the pinned Harbor 0.20.0 Python API on Python 3.12+; the
Harbor executable itself remains pinned by the backend registry.

The release/claim preflight also requires generated schemas, the registry lock,
the retained probes, and authority receipt to verify independently. The exact
commands and the recovery procedure are in
[`docs/EXPERIMENT_RUNBOOK.md`](docs/EXPERIMENT_RUNBOOK.md).

## Execution Chain

An experiment flows through:

`Compiler -> Pipeline -> AdapterRegistry -> loader/backend/execution adapter -> Scheduler -> gates/report -> standalone verifier`

Every production custom benchmark must register a digest-bound loader, backend
factory, and exact execution capability. Missing capabilities fail at compile
time; BMP never silently falls back to a fake backend. A real model additionally
needs a secret-free `ProviderBinding`, a matching runtime
`ModelActivationReceipt`, observable usage, and the corresponding evidence
bytes.

## Documentation

- [`docs/EXPERIMENT_RUNBOOK.md`](docs/EXPERIMENT_RUNBOOK.md): recovery,
  preflight, first-run procedure, artifact handling, and release gates.
- [`docs/EXPERIMENT_MATRIX.md`](docs/EXPERIMENT_MATRIX.md): the current
  runnable, exploratory, blocked, and deferred experiment inventory.
- [`docs/EXPERIMENT_PLAN.md`](docs/EXPERIMENT_PLAN.md): the high-level staged
  plan retained from the server-interruption handoff.
- [`.docs/05-current-state.md`](.docs/05-current-state.md): verified protocol
  boundaries and known gaps.
- [`EVIDENCE.md`](EVIDENCE.md): the authoritative evidence status; it currently
  reports no claim-ready real benchmark result.
- [`.docs/03-iron-laws.md`](.docs/03-iron-laws.md): measurement and evidence
  invariants for contributors.

## Artifact Rule

Use a fresh record root for every execution. The ignored `.runs/` directory is
scratch storage, not durable publication storage; preserve it before cleanup
and copy completed artifacts to an immutable location with a record index.
Never put credential values, provider tokens, or unreviewed workspace files in
a record root. Historical files under `records/` are regression inputs and
negative examples; do not repair them in place.

The only publishable result is a persisted report that can be independently
reloaded and verified from its referenced bytes. Until that condition is met,
label output `exploratory` and do not use it as a leaderboard number or model
claim.
