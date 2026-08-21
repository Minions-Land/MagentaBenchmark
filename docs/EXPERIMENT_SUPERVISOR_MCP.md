# Experiment Supervisor MCP Boundary

Magenta's current experiment tool is an MCP-facing client of an injected
`ExperimentService`.  In the pinned upstream source
`Minions-Land/Magenta@065d9d0d3231ecd84e62f38511a16577214babfd`, the tool
offers bounded `experiment_submit`, `experiment_status`, and
`experiment_watch` operations (plus cancellation/retry operations).  The
service may use a Unix socket, but the socket is an execution detail and is
never benchmark evidence.

MagentaBench owns only the thin boundary in
`MagentaBench.collab.supervisor_mcp`.  It does not copy Magenta or ATPS code,
discover GPUs, start or stop Supervisor, write the lab ledger, or turn logs
into metrics.  The transport is injected so a fake transport can test the
contract without a service.  The versioned `MagentaExperimentWireClient`
matches the pinned Magenta `ExperimentService` request contract; the older
`SupervisorMcpClient` remains the receipt-only boundary for callers that
already have an exported terminal handoff.

```python
from MagentaBench.collab.supervisor_mcp import (
    ExperimentExecutionRequest,
    MagentaExperimentWireClient,
)

wire = MagentaExperimentWireClient(transport)
ack = wire.submit(
    ExperimentExecutionRequest(
        experiment_id="exp-001",
        command="python train.py",
        cwd="/workspace",
        gpu_count=2,
        timeout_seconds=3600,
    ),
    identity_context=frozen_request,
)
status = wire.status(ack.experiment_id)
events = wire.watch(ack.experiment_id, after_sequence=0)
```

`submit` requires the immutable BMP/Magenta/Supervisor identity context and
returns an opaque acceptance object, never a `SupervisorReceipt`.
The Magenta service contract does not promise a terminal report in the submit
response.  A later, explicitly versioned status projection may provide a
terminal receipt; only then may the existing receipt validator read the
trusted artifact tree.  BMP identity fields are retained in the immutable
experiment bundle/request context and are deliberately not sent as unknown
Supervisor parameters.  This prevents an identity-free dispatch, but the wire
client cannot prove from a digest alone that the exact command, cwd, GPU, and
timeout fields are the projection of `config_sha256`.  The live adapter must
derive those fields from the digest-bound bundle/configuration and retain that
projection as reviewed evidence; until then the execution-to-config adoption
gate remains open.

After a separately reviewed status projection has selected an exported
terminal receipt, call `validate_terminal_receipt(...,
identity_context=frozen_request)` before `bmp-verify-report`.  This helper
checks the receipt against the immutable BMP identity and report tree; it does
not parse arbitrary status objects or write the ledger.

The transport is injected so a fake transport can test the contract without a
service:

```python
from MagentaBench.collab.supervisor_mcp import (
    SupervisorMcpClient,
    serialize_supervisor_receipt,
)

client = SupervisorMcpClient(transport)
receipt = client.submit(frozen_request)
status = client.status(receipt.experiment_id)
events = client.watch(receipt.experiment_id, after_sequence=status.sequence)

# Persist this exact, key-sorted handoff after the artifact tree is exported.
receipt_json = serialize_supervisor_receipt(receipt)
```

## Terminal Receipt Contract

Only an exported terminal receipt, obtained after submit through a separately
versioned status/watch projection, must bind all of the following:

| Group | Required identity |
| --- | --- |
| Supervisor | `experiment_id`, profile SHA-256, deployment SHA-256 |
| Magenta | immutable code commit, interface version |
| BMP | spec, manifest, dataset, evaluator, and config SHA-256 |
| Run | run id, fresh durable `record_root`, terminal state |
| Report | relative report locator, SHA-256, exact byte size |
| Workflow | standalone verification state; `claim_eligible=false` |

The adapter rejects missing or malformed digests, absolute or traversal paths,
`.runs` scratch roots, nonterminal states, secret-bearing response keys,
non-UTF-8 JSON values, out-of-safe-range wire integers, oversized
responses/reports, report path/size/digest drift, and any
`claim_eligible=true` field.  A relative root and `record_root_fresh=true` are
submit-time assertions; freshness cannot be proven by a later validator, so
the caller must run `check_run_root --require-new` before submission.
Byte-level report checks run when the caller supplies the resolved record root;
the receipt-only `submit` call does not read or mutate artifact bytes.
The wire submit response is only checked for a bounded object and, when it
includes `experiment_id`, an exact match.  A terminal receipt is checked
against every immutable identity in the local request context; a response for
another experiment or source snapshot fails with `receipt-identity-mismatch`.

The receipt-only Python client is intentionally an identity-aware sidecar
boundary, not a drop-in implementation of Magenta's TypeScript
`ExperimentService`.  The pinned Magenta service's wire request requires
`experiment_id`, `command`, `cwd`, and `gpu_count` (with optional `name` and
`timeout_seconds`), and translates an explicitly supplied watch timeout to
Supervisor's `timeout_seconds`; an omitted timeout remains omitted. A future
runtime adapter must therefore:

1. submit those execution fields through Magenta's service;
2. retain the BMP/source identities in the same immutable request context;
3. derive command/cwd/GPU/timeout from the digest-bound configuration and
   preserve the reviewed projection evidence;
4. bind the returned experiment identity to this receipt boundary; and
5. export the report into the declared record root before calling
   `validate_supervisor_receipt(..., artifact_base=<trusted-parent>)` (or pass
   the already-resolved record root as `artifact_root`).

This repository does not implement that live sidecar, does not infer command
or GPU allocation for the receipt-only client, and does not claim that a
Supervisor socket is available.  The wire client validates command, cwd, GPU,
and timeout bounds before sending the exact Magenta fields, but it never
allocates a GPU or starts a service.  `service_status`, `experiment_submit`,
`experiment_status`, `experiment_watch`, `experiment_cancel`, and
`experiment_retry` are all transport calls; the bridge does not assume
method-specific response fields beyond bounded JSON and an optional matching
`experiment_id`.

For submit/cancel/retry, a transport failure after dispatch is
`outcome_unknown` and non-retryable until a fresh `status` call establishes
the authoritative state.  It is never silently retried.  A pre-dispatch
unavailable error is retryable.  Read-only transport failures remain
retryable when their dispatch boundary is known.
`artifact_base` resolves the receipt's relative `record_root` and prevents an
unrelated tree from being silently substituted; `artifact_root` is the
already-resolved equivalent.  Callers must establish this mapping at the
trusted artifact boundary before validation.

`standalone_verification` is retained as a workflow state only.  It is not an
authentication of the report and does not derive claim eligibility.  The only
claim path is:

```text
Supervisor MCP submit/status/watch
  -> immutable receipt and exported artifact tree
  -> bmp-verify-report
  -> bmp-lab link-run
  -> bmp-collab ledger
```

Old runs remain bound to their original code, interface, evaluator, dataset,
configuration, profile, and deployment digests.  Any implementation change
creates a new run identity.  Logs and Supervisor status events are operational
evidence, not metric rows.

## Adoption Gates

This boundary is deliberately unbound until all of these are independently
closed:

1. An exact upstream Magenta commit and interface contract are reviewed.
2. The candidate ATPS Supervisor PR #22 (`c333686...`) has exact-head review
   and green Supervisor tests; it is currently open with failed CI and is not
   imported here.
3. A fresh H20 profile/deployment/socket identity is observed in an explicitly
   leased, no-provider smoke.  No shared service is started by this repository.
4. The exported report graph passes standalone verification and is linked to a
   fresh lab run before ledger projection.

Until those gates close, a receipt is a non-claim handoff and cannot upgrade
any historical or exploratory result.
