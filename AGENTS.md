# MagentaBench Contributor Protocol

These rules apply to every human or agent working in this repository.

1. Enter through `uv run bmp-agent`, `uv run bmp-collab validate`, and
   `uv run bmp-collab modes`; then run `uv run bmp-lab doctor` and read the
   relevant issue with `bmp-lab show` or `bmp-lab recover`.
2. Create and mutate `lab/issues/**` only through `bmp-lab`. Never hand-edit,
   delete, rename, or resequence immutable issue and event JSON.
3. Claim the issue's declared write scope, commit and non-force-push the lease
   event to the canonical ref, and confirm it won before editing shared scope or
   launching an expensive experiment. Renew or release it explicitly.
4. Use a stable `--event-id` for one intended operation. Retry uncertain
   delivery with the identical ID and request; never reuse an ID for new intent.
5. Checkpoint before interruption. Record structured resume argv, environment
   variable names only, a concrete next action, and content-addressed recovery
   artifacts. A dirty tree requires a reviewed patch reference.
6. Use a fresh record root for every new execution. Do not run concurrent
   Pipeline writers against one record root, and do not treat `.runs/` as the
   only durable copy.
7. Lab state coordinates work only. Benchmark truth comes from persisted
   evidence and standalone verification; `done`, `finished`, or a zero exit
   code never creates a result claim.
8. Never store credential values or authenticated URLs in code, Git, lab
   events, commands, patches, logs, manifests, or evidence.
9. Use the locked environment. On this host, use the documented Aliyun Python
   mirror for bootstrap and the configured Git mirror for accelerated fetches;
   preserve the canonical GitHub `origin` for authoritative pushes.
10. Before handoff, run the checks in `docs/EXPERIMENT_RUNBOOK.md`, append any
    blocker/checkpoint/run linkage, commit reviewed files, and push them.
11. Put experiment intent in one `experiments/<id>/` bundle. Do not update a
    global progress board and do not edit BMP schemas, runner semantics, or
    protocol registries for an experiment-only change.
12. Select Docker, AppContainer, E2B, or another target through a registered,
    digest-bound backend adapter. Unknown cloud boundaries remain exploratory
    until runtime identity, network, artifact export, teardown, recovery, and
    standalone verification are closed.

The complete operating model and current runtime limitations are documented in
`docs/LAB_OPERATIONS.md`. Experiment collaboration and target-specific rules
are in `docs/EXPERIMENT_COLLABORATION.md` and
`docs/governance/EXECUTION_MODES.md`.
