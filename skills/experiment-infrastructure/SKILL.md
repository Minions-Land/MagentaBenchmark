---
name: experiment-infrastructure
description: Safely operate shared experiment infrastructure including environment-injected APIs, pre-provisioned models, GPUs, storage, proxies, dependency locks, job slots, and monitoring. Use when Codex must preflight, launch, or troubleshoot a benchmark while protecting credentials, unrelated processes, shared code, and storage capacity.
---

# Experiment Infrastructure

Treat infrastructure as a capability with an owner, contract, health check,
capacity limit, and change boundary. Read values from the environment without
printing them.

## Build A Capability Catalog

For each capability record only identifiers and rules:

| Capability | Record |
| --- | --- |
| API | endpoint identifier, model IDs, env-var names, quota, timeout, health check |
| Credentials | injection mechanism and owner; never the secret value |
| GPU/model service | model ID, adapter/port, owner, memory and concurrency limit |
| Dataset | revision, split, digest, read/write boundary |
| Runtime | locked dependencies, entry command, proxy/no-proxy rules |
| Shared resources | job slots, API rate bucket, GPU occupancy, maintenance window |
| Monitoring | status artifact, log locator, completion counter, escalation owner |

Pre-provisioned credentials or models reduce setup work; they do not authorize
restarting a service, replacing a model, or raising its concurrency.

## Preflight In Order

1. Confirm authorized project and run roots, ownership, and free storage.
2. Inspect existing jobs and service health without sending signals.
3. Verify source/dependency identity and output-name uniqueness.
4. Check environment variable presence without printing values.
5. Verify internal endpoints bypass proxies and external fetches use the
   approved proxy only when needed.
6. Run the smallest health check and smoke.
7. Record effective command, job ID, limits, and monitoring locations.

Use `<PROJECT_ROOT>` and `<RUN_ROOT>` in reusable instructions. Never create
artifacts under `/root`, a shared framework tree, or another worker's root
unless the contract explicitly authorizes it.

## Resource Layers

Keep these limits distinct: per-package slots, endpoint rate/concurrency,
GPU/service capacity, and storage floor. At a limit, queue or lower the
work-package concurrency according to the contract. Repeated 429s or timeouts
require waiting, reduction, or escalation, not unlimited retries.

Only stop a process that this work package started, after matching its full
command and parent/child ownership. Unknown processes are observed and
escalated, never killed by name.

## Secret And Tree Hygiene

- Use environment injection or a secret manager; do not copy `.env` contents.
- Keep API responses, raw prompts, private paths, and credentials out of logs
  and receipts.
- Put adapters in an isolated work-package directory; do not patch a shared
  harness for convenience.
- Record a pre/post source-tree fingerprint when other jobs share the tree.
- Do not install packages into a shared runtime during a live campaign; use a
  locked environment or isolated virtual environment.

See [references/preflight-checklist.md](references/preflight-checklist.md) for
the reusable checklist. [scripts/check_run_root.py](scripts/check_run_root.py)
is an optional conservative reference adapter: inspect and adapt it to the
project's operating system, scheduler, path policy, and ownership model before
use, or replace it with an equivalent native check. Its filename and presence
are not project requirements.
