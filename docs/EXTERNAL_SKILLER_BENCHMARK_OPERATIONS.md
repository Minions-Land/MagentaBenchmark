# External Lessons: Benchmark Infrastructure And Operations

This runbook distills operational techniques for shared, long-running agent
benchmarks. It supplements MagentaBench's authoritative runbook; repository
rules, experiment contracts, and active leases take precedence.

Related material: [team collaboration workflow](EXTERNAL_SKILLER_WORKFLOW.md)
and the [numeric baseline declaration](external-evidence/skiller_tau2_baselines.json).

## Infrastructure As A Project Capability

Do not hide infrastructure in one operator's shell history. Maintain a
capability catalog that names interfaces without exposing secret values:

| Capability | Required contract |
| --- | --- |
| API | endpoint identifier, model IDs, environment variable names, quota, timeout, health check |
| Credentials | injected by environment or secret manager; never copied into docs or logs |
| GPU/model service | service owner, model ID, port/adapter, health check, memory and concurrency limits |
| Dataset | revision, split/case manifest, digest, read/write boundary |
| Runtime | locked dependency identity, entry command, proxy/no-proxy rules |
| Shared resources | job slots, endpoint concurrency, GPU occupancy, maintenance window |
| Monitoring | status artifact, log location, completion counter, alert/escalation owner |

Pre-provisioned API credentials and model services reduce onboarding time and
secret sharing. They do not authorize an operator to restart, replace, or
increase the concurrency of a shared service.

## Resource Limits Have Layers

Record separate limits instead of publishing one ambiguous “concurrency”
number:

| Layer | Example contract | Default reaction at limit |
| --- | --- | --- |
| Work-package slots | maximum simultaneous jobs and per-job concurrency | queue the new job |
| Shared endpoint | total API concurrency, rate bucket, and 429 threshold | lower concurrency, wait, report |
| GPU/service | memory, model replicas, ports, and owner | ask before start/restart/preemption |
| Storage | durable artifact root and free-space floor | stop before large writes |

The task-specific contract overrides a generic example. A method that updates
shared state between cases may require concurrency one even when the endpoint
could serve more requests. Never trade method validity for wall-clock speed.

## Ordered Gates

Run from the cheapest structural checks to the most expensive execution:

1. identity and configuration;
2. schema, static, and interface validation;
3. unit/contract tests;
4. one-case or minimal smoke;
5. parity and mechanism/fingerprint checks;
6. representative qualification;
7. full run;
8. completeness and independent metric review;
9. receipt and handoff.

For MagentaBench, the standard entry checks remain:

```bash
uv run --frozen bmp-agent validate
uv run --frozen bmp-collab validate
uv run --frozen bmp-collab modes
uv run --frozen bmp-lab doctor
git diff --check
```

Do not start a costly run until the issue scope and lease are durable. Use a
fresh record root for every execution and never run concurrent writers against
one root.

## Run Contract

Freeze these fields before observing outcomes:

```text
benchmark/dataset revision and explicit case set
subject, model, provider, harness, and evaluator identity
treatment/control and held-fixed variables
task order, repetitions, seeds, retry and resume policy
per-case and total budgets
primary metric, denominator, uncertainty, and decision rule
artifact root, filenames, hashes, and retention
invalidation, stop, and escalation conditions
```

Do not retry because a score is unattractive. An infrastructure retry is a new
retained attempt unless the frozen protocol explicitly permits a verified
resume. Preserve failures and partial results; do not delete or overwrite a
run to reuse its name.

## Runtime Sentinels

Before launch:

- count existing jobs and inspect shared services;
- verify the output name is unused and the target has capacity;
- ensure proxy rules do not route internal model endpoints incorrectly;
- verify environment variable names are present without printing values;
- confirm the process will write only inside its authorized work package.

After launch:

- record PID/job ID, exact command identity, log and status locators;
- verify progress advances and the expected model/configuration is active;
- inspect a small number of completed cells for parity and required mechanism
  events;
- treat a lost SSH session as unknown state, not as proof of failure;
- query the stable job before any restart.

At completion:

- compare actual simulations with `tasks x repetitions`;
- verify unique task/trial cells, missing rewards, duplicates, and terminal
  states;
- check fingerprints/verifier receipts before calculating the headline rate;
- separate task inference usage from memory-building or maintenance usage;
- retain the raw artifacts and publish only a safe, reviewable receipt.

## Receipt Format

Each delivered benchmark result should contain:

1. one-sentence conclusion and evidence class;
2. protocol conditions and exact denominator;
3. result table with numerator and denominator;
4. completeness, parity, and mechanism checks;
5. artifact identities or content hashes;
6. deviations, retries, failures, and unrun checks;
7. limitations and the next decision.

External numbers that were not replayed through MagentaBench remain
`external-declaration` with `claim_eligible=false`. Retain numeric facts and
conditions only; do not copy another project's private method design,
implementation, prompts, trajectories, or machine paths.

## Practical Failure Rules

- Rate limiting: reduce concurrency or wait; do not create a retry storm.
- Repeated timeout: stop after the contract's retry limit and retain the cell
  as missing/failed rather than looping indefinitely.
- Output path conflict: choose a new identity; never delete an existing run.
- Unknown process ownership: observe only and ask the owner before any signal.
- Configuration drift: stop the arm, retain it as invalid, and use a new run
  identity after correction.
- Secret or private-path exposure: stop publication and rotate/redact at the
  source; do not hide the incident by editing raw evidence.
