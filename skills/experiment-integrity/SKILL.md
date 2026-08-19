---
name: experiment-integrity
description: Verify experiment identity, boundaries, smoke behavior, completeness, mechanism evidence, provenance, and claim eligibility before accepting benchmark results. Use when Codex must audit a run, diagnose a mismatch, calculate a defensible metric, or prepare a receipt without overstating external evidence.
---

# Experiment Integrity

Treat a result as a claim only after its evidence chain passes the required
sentinels. Keep progress, validity, completeness, and quality as separate
states.

## Ordered Sentinels

Run these checks in order:

1. **Boundary/privacy**: writes, secrets, artifact paths, process ownership,
   and shared resources stayed within authorization.
2. **Identity**: source, branch/commit, dataset/input revision, model, effective
   configuration, seed, and harness match the frozen contract.
3. **Resource**: CPU/GPU/API/storage/concurrency and job ownership match the
   frozen allocation.
4. **Schema/interface**: the adapter, command, input, output, and verifier
   contracts validate before execution.
5. **Smoke**: a representative operation succeeded and emitted expected
   structured fields, tool calls, and terminal state.
6. **Completeness**: expected slots, unique IDs, task-trial coverage, missing
   values, duplicates, errors, and terminal states are accounted for.
7. **Mechanism/evidence**: required fingerprints, parity receipts, event logs,
   hashes, and provenance exist and prove the intended treatment was active.
8. **Accountable review/claim**: repository-authorized review confirms the
   evidence class, negative boundary, limitations, and claim eligibility. In
   MagentaBenchmark, only `PoorOtterBob` supplies final approval; other review
   is advisory.

Fail closed at the first required sentinel. Keep operational outcome separate
from evidence class: distinguish `not-run`, `infrastructure-failure`,
`verifier-failure`, and `algorithmic-failure`. A verifier failure cannot become
a reproduced score. An algorithmic failure may be a complete reproduced result
only when execution and verifier evidence are valid.

## Parity And Completeness

Compare effective runtime metadata, not intended command text. For a grid of
tasks and trials, verify:

```text
expected = number_of_tasks * number_of_trials
actual = number_of_unique_task_trial_cells
missing = expected - actual
duplicates = repeated_task_trial_cells
```

Do not compute a rate until `actual == expected`, required rewards are present,
and terminal/error states are classified. A partial count is status only.

For ordered or stateful methods, additionally verify that each next
`state_before_hash` matches the prior committed `state_after_hash` (or the
contract's explicit checkpoint), sequence IDs are monotonic, and retries do
not apply a side effect twice. A complete grid with a broken state chain is
still invalid.

## Provenance And Mechanism

Preserve the exact command, source/dependency identities, input hashes, run ID,
environment names, start/end times, retry history, and raw artifact locators.
For memory or retrieval methods, separately report task-inference tokens and
maintenance/building tokens. Check method-specific events (injection, update,
no-op, fingerprint, or verifier receipt) before interpreting a score.

## Claim Boundaries

Label every result as one of:

- `reproduced`: current harness, evidence, and accountable review passed all
  required gates;
- `external-declaration`: supplied by another source and not replayed here;
- `incomplete`: work remains or cells are missing;
- `invalid`: protocol/configuration drift or failed required sentinel;
- `infrastructure-failure`: execution blocked by environment or service.

External numeric facts may be retained with source, denominator, conditions,
and limitations, but require `claim_eligible=false` until independently
replayed and reviewed.
Do not copy another project's private paths, prompts, source code, raw traces,
or credentials into a reusable report.

See [references/audit-template.md](references/audit-template.md).
[scripts/verify_grid.py](scripts/verify_grid.py) is an optional reference
adapter for a simple grid. It requires a frozen expected-grid JSON file and one
or more explicit required result fields; observed rows never define the
expected identities. Adapt or replace it when the active contract uses a
different result schema, unique key, state model, or artifact layout; the
contract's evidence semantics, not this script, define acceptance.
