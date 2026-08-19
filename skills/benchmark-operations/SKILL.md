---
name: benchmark-operations
description: Operate long-running benchmarks and evaluations with a frozen protocol, isolated run artifacts, ordered validation gates, shared-resource limits, live monitoring, and evidence-backed receipts. Use when Codex must launch, resume, monitor, audit, or hand off a benchmark or experiment without changing the shared harness or corrupting another run.
---

# Benchmark Operations

Run expensive evaluations as reproducible work packages. Treat the protocol,
resource limits, output identity, and acceptance checks as a contract that is
frozen before interpreting results.

## Start With A Contract

Record these fields before launch:

- benchmark and dataset revision, explicit task/split list, and task order;
- subject, agent model, simulator/evaluator model, harness, provider, and
  held-fixed variables;
- treatment/control arms, repetitions, seeds, retry/resume policy, and primary
  metric with denominator and threshold;
- authorized source/output roots, expected artifacts, retention policy, and
  disk floor;
- per-job and shared endpoint/GPU concurrency, timeout, rate-limit response,
  stop rules, escalation owner, and estimated duration.

For stateful or online methods, also freeze repetition semantics. Prefer an
explicit trial-major sequence (`trial 1: task 1..N`, then trial 2, and so on)
with an immutable starting snapshot for each trial. If state is intentionally
shared across trials, record that decision because the trials are not
independent. A missing task order, state policy, or endpoint quota blocks
`READY`.

Use `<PROJECT_ROOT>`, `<RUN_ROOT>`, and environment variables in reusable
commands. Do not put credentials, private hosts, or raw conversations in the
contract.

## Gate Order

Run the cheapest checks first and stop at the first required failure:

1. identity and effective configuration;
2. schema, static, and interface checks;
3. unit/contract tests;
4. one-case smoke;
5. parity and mechanism checks;
6. representative qualification;
7. full run;
8. completeness and repository-accountable metric review;
9. receipt and handoff.

Do not call an incomplete run a score. Distinguish infrastructure failure,
invalid setup, incomplete execution, verifier failure, and algorithmic failure.
Read [references/gates-and-receipts.md](references/gates-and-receipts.md) for
the gate evidence and receipt fields.

## Shared Resource Discipline

Before launch, inspect existing jobs and the output name. Keep four limits
separate: work-package slots, shared endpoint quota, GPU/service capacity, and
storage. The task contract overrides generic server capacity.

- Queue when the slot limit is reached; never raise concurrency silently.
- Reduce concurrency or wait after repeated rate limits; do not create a retry
  storm.
- An online method that updates memory between tasks may require
  `max-concurrency=1` and task-id ordering even if the endpoint supports more.
- Do not signal a process unless its full command and ownership are confirmed.
- A lost SSH session means unknown state. Query the stable job before restart.

## Run Identity And Artifacts

Create a new run identity for every materially different code, input,
dependency, hardware, threshold, or retry policy. Never overwrite an existing
run or delete a failed run to make the next attempt look continuous.

```text
<RUN_ROOT>/
  logs/
  provenance/
  results/
  STATUS.md
  SHA256SUMS.txt
```

Capture the exact command, effective environment names (not values), source
and dependency revisions, PID/job ID, start/end times, and all retries.

## Monitoring

After launch, check a stable status artifact and log at a fixed interval:

- process/job is alive and belongs to this work package;
- completion count advances and has no duplicate or missing cells;
- the observed model/configuration matches the contract;
- required mechanism events and error rates remain within bounds;
- disk, GPU, endpoint quota, and timeout budgets remain safe.

For stateful runs, monitor the last committed sequence, state version/hash,
checkpoint age, and worker lease/heartbeat. Commit the result and its state
checkpoint under one idempotency key before advancing. On an unknown network
result, inspect the durable commit first; do not blindly replay a side-effecting
task.

Do not modify the job from a status check. Escalate when progress stalls,
configuration drifts, or a hard sentinel fails.

## Completion And Handoff

Before computing a headline metric, verify expected slots, unique task-trial
cells, missing rewards, duplicate IDs, terminal states, and required
fingerprints. Report task-inference cost separately from memory-building or
maintenance cost.

Deliver a short receipt containing the conclusion, protocol and denominator,
numerator/denominator table, integrity/parity/mechanism results, artifact
locators or hashes, deviations/retries, unrun checks, limitations, and the next
decision. Link to raw logs instead of pasting them.

For multi-operator work, use
[project-management](../project-management/SKILL.md) for work packages and
handoffs, and [experiment-integrity](../experiment-integrity/SKILL.md) for
sentinel verification. In MagentaBenchmark, the repository's
[GitHub development workflow](../../docs/GITHUB_DEVELOPMENT.md) and `bmp-lab`
lease/event chain are authoritative; do not create a parallel progress or
approval ledger.
