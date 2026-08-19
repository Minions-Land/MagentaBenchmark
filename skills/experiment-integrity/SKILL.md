---
name: experiment-integrity
description: Verify experiment identity, boundaries, smoke behavior, completeness, mechanism evidence, provenance, and claim eligibility before accepting benchmark results. Use when Codex must audit a run, diagnose a mismatch, calculate a defensible metric, or prepare a receipt without overstating external evidence.
---

# Experiment Integrity

Treat a result as a claim only after its evidence chain passes the required
sentinels. Keep progress, validity, completeness, and quality as separate
states.

## Five Sentinels

Run these checks in order:

1. **Identity**: source, branch/commit, dataset/input revision, model, effective
   configuration, seed, and harness match the frozen contract.
2. **Boundary**: writes, secrets, artifact paths, process ownership, and shared
   resources stayed within authorization; unrelated work was not touched.
3. **Smoke**: a representative operation succeeded and emitted expected
   structured fields, tool calls, and terminal state.
4. **Completeness**: expected slots, unique IDs, task-trial coverage, missing
   values, duplicates, errors, and terminal states are accounted for.
5. **Mechanism/evidence**: required fingerprints, parity receipts, event logs,
   hashes, and provenance exist and prove the intended treatment was active.

Fail closed at the first required sentinel. Use `not_run`, `incomplete`,
`invalid_setup`, `infrastructure_failure`, `verifier_failure`, and
`algorithmic_failure` distinctly.

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

- `reproduced`: current harness and evidence passed all required gates;
- `external-declaration`: supplied by another source and not replayed here;
- `incomplete`: work remains or cells are missing;
- `invalid`: protocol/configuration drift or failed required sentinel;
- `infrastructure-failure`: execution blocked by environment or service.

External numeric facts may be retained with source, denominator, conditions,
and limitations, but use `claim_eligible=false` until independently replayed.
Do not copy another project's private paths, prompts, source code, raw traces,
or credentials into a reusable report.

See [references/audit-template.md](references/audit-template.md).
[scripts/verify_grid.py](scripts/verify_grid.py) is an optional reference
adapter for a simple grid. Adapt or replace it when the active contract uses a
different result schema, unique key, state model, or artifact layout; the
contract's evidence semantics, not this script, define acceptance.
