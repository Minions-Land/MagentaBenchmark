# Execution Mode Governance

MagentaBench supports multiple placements through registered adapters, not by
putting provider names into BMP. The authoritative runtime selection remains
`[execution].backend` in the BMP TOML. The collaboration bundle records the
human-visible mode and recovery policy; the backend capability binds executable
code and evidence semantics by digest.

## Current Matrix

Run `uv run bmp-collab modes` for the derived registry view. The checked-in
target requirements live in `execution-profiles/<mode>/profile.json`.

| Mode | Registry state | Isolation | Current ceiling | Work item |
| --- | --- | --- | --- | --- |
| `local-process` | fake/subprocess configured; Harbor shim registered-only | process | BMP-gated for configured backends | none |
| `docker` | Harbor configured; AOSE Docker registered-only until its factory capability is present | task container | BMP-gated for configured backends | benchmark-specific blockers remain in `lab/` |
| `apptainer` | exploratory backend factory registered; host identity remains unbound | task container | exploratory | `apptainer-verifier-boundary` |
| `appcontainer` | no concrete runtime or adapter | task container | exploratory | `appcontainer-backend-adapter` |
| `e2b` | no adapter | microVM | exploratory | `e2b-backend-adapter` |
| `remote-sandbox` | extension slot only | microVM | exploratory | `remote-sandbox-backend-adapter` |

`BMP-gated` does not mean claim-ready. It means the mode has a closed mapping
in the current standalone verifier and may proceed through all normal gates.
The experiment purpose, provider/model activation, case denominator, report,
and referenced evidence still decide what can be claimed.

## Adapter Contract

A new mode implementation must use the existing capability chain:

1. Add an isolated plugin package with a `BackendFactory` and runtime object.
2. Register a backend declaration and a `backend_factory` capability whose
   digest and source closure match the implementation.
3. Register the exact benchmark/backend/subject execution capability. There is
   no name-based fallback.
4. Keep credential values outside declarations and records. Manifests retain
   variable names and credential digests only.
5. Emit runtime identity, effective network, usage, artifact export, terminal
   state, cancellation, and teardown evidence.
6. Add standalone verification for any boundary that is not already closed.

Unknown adapters currently fail the generic network-boundary gate. That is an
intentional fail-closed condition, not a reason to add the adapter name to a
hard-coded map casually. If a provider-neutral activation receipt is needed,
make that a separate BMP protocol-change issue and PR with compatibility and
replay tests.

## Mode Requirements

### Docker

Use immutable image identity, an observed launcher digest/version, read-only
task mounts where applicable, a fresh workspace, and a retained container
receipt. An image tag alone is insufficient. Export all indexed evidence before
container removal and preserve the workspace on incomplete execution.

### Apptainer

Apptainer is the rootless HPC/container placement and is distinct from the
provider-neutral `AppContainer` slot. The checked-in profile and read-only host
probe can observe an absolute launcher path, launcher digest/version/build
configuration, non-root user namespaces, subordinate IDs, FUSE, cgroup v2,
persistent storage, optional GPU visibility, and whether a configured image
path exists. Fakeroot, cgroup v2, and GPU visibility are explicit per-Benchmark
requirements rather than universal Apptainer requirements. The probe never
pulls, builds, inspects, or executes an image.

This mode has a registered exploratory backend factory and runtime receipt
implementation, but its checked-in launcher, build configuration, and image
pins are deliberately non-runnable placeholders. It cannot be selected by a
benchmark execution tuple. The linked `apptainer-verifier-boundary` work item
must independently verify launcher and image identity, effective policy,
artifact export, cancellation, and teardown before the profile can report a
closed boundary. Host readiness and factory registration are not runtime
activation receipts and do not upgrade evidence.

NatureBench-specific translation belongs in the NatureBench repository on its
dedicated `NatureBranch`; it must not import NatureBench task, scoring, hidden
data, or runner source into MagentaBench. Until the generic runtime and verifier
contracts are reviewed, that branch is planning-only: hold the NatureBench
protocol and existing source fixed, and limit work to interface mapping and a
future thin integration package owned by the NatureBench repository.

### AppContainer

`AppContainer` is intentionally provider-neutral until the team identifies the
concrete runtime authority. The adapter must prove what isolation the runtime
actually provides rather than inferring it from the product name. Pin the
launcher/runtime, application image or bundle, filesystem boundary, network
boundary, and lifecycle API. Keep all output exploratory until those facts and
standalone verification are present.

### E2B

Pin the E2B SDK/API version and the sandbox template build identity. Record the
sandbox and request identifiers without embedding an API key. The adapter must
export a content-addressed artifact manifest to durable storage before killing
the sandbox, then retain a destroy receipt. Create, execute, retry, resume, and
destroy are different side effects; document their idempotency keys and never
claim that the lab ledger makes them exactly-once.

### Other Remote Sandboxes

Follow the E2B requirements and add provider-specific identity only inside the
adapter. A provider switch creates a new backend registration and evidence
identity. It must not silently reuse a previous backend id or result lineage.

## Agent Workflow

```bash
uv run bmp-agent
uv run bmp-collab modes
uv run bmp-lab show <execution-work-item>
uv run bmp-lab recover <execution-work-item>
```

Claim only the adapter issue's declared paths. Adapter implementation,
protocol-contract changes, and an experiment run should normally be three
separate PRs. This keeps registry-lock contention bounded and lets unrelated
experiments merge while a cloud adapter is still under review.
