# Experiment Collaboration Contract

This document is the operating contract for people and agents who design,
execute, review, and recover MagentaBench experiments. It deliberately keeps
experiment intent and execution placement outside the BMP protocol core.

## Three Facts, Three Sources

| Question | Source | Rule |
| --- | --- | --- |
| What is being proposed? | `experiments/<id>/bundle.json` and `PLAN.md` | One isolated bundle per experiment. The bundle pins the BMP TOML digest, protocol id, design, commands, target mode, and evidence requirements. |
| Who may work on it now? | `lab/`, reduced by `bmp-lab` | Issue status, dependencies, blockers, lease, checkpoint, and run links are derived from immutable events. Do not maintain a second board. |
| What actually happened? | Durable record root, report, indexed bytes, standalone verifier | A green process, a `done` lab issue, or a requested model name is not a benchmark claim. |

The bundle is a preregistration overlay, not a replacement for a BMP
declaration. `bmp_spec_sha256` makes an unreviewed declaration edit visible;
the validator fails until the bundle and its review move together. A bundle
does not contain live status or an owner field because those values belong to
the lab ledger and GitHub ownership rules.

## Repository Shape

```text
experiments/
  <experiment-id>/
    bundle.json       # strict collaboration contract
    PLAN.md           # short human/agent handoff and stop conditions
MagentaBench/conformance/experiments/
  <experiment-id>.toml # BMP declaration; unchanged by bundle scaffolding
lab/issues/
  <issue-id>/         # immutable issue and event chain
```

Never add a global hand-edited experiment index. Discover the queue from the
filesystem and lab ledger:

```bash
uv run bmp-collab validate
uv run bmp-collab list
uv run bmp-collab next
uv run bmp-collab modes
uv run bmp-lab doctor
uv run bmp-lab status
```

`bmp-collab scaffold` creates a new bundle around an existing BMP TOML. It
never edits the TOML, a schema, a runner, or a registry:

```bash
uv run bmp-collab scaffold my-experiment \
  --bmp-spec MagentaBench/conformance/experiments/my-experiment.toml \
  --lab-issue my-lab-item \
  --question "What is the preregistered question?" \
  --hypothesis "What should happen under the frozen tuple?" \
  --stop-condition "Stop on an infrastructure or verifier failure." \
  --required-env PROVIDER_API_KEY
```

The primary lab issue must already exist and its `experiment` field must point
to the BMP TOML. Related issues must equal that issue's declared dependencies.
This makes a missing prerequisite a visible validation error rather than a
comment hidden in a PR.

## Execution Targets

An experiment chooses an execution *mode* in its bundle while the BMP TOML
continues to select a digest-bound backend id. The current inventory is
reported by `bmp-collab modes`:

| Mode | Current meaning | Evidence label before a dedicated adapter closes its boundary |
| --- | --- | --- |
| `local-process` | deterministic subprocess/fake fixtures | `claim-candidate` only when the normal BMP gates pass |
| `docker` | Harbor or the pinned AOSE Docker backend; image and launcher identity are retained | `claim-candidate` only after container, network, and report verification |
| `appcontainer` | provider-neutral slot for an application-container runtime | `exploratory` until a concrete runtime and adapter are registered |
| `e2b` | cloud sandbox/microVM slot | `exploratory` until sandbox identity, network observation, export, and teardown are independently verified |
| `remote-sandbox` | future remote execution provider | `exploratory` |

Every target must declare, and its adapter must observe:

- a content-addressed runtime identity (image, template, executable, or
  launcher digest);
- the isolation boundary and workspace lifecycle;
- the network policy and an observation of the effective boundary;
- credential names only, never credential values;
- artifact export before sandbox/container destruction;
- timeout, retry, cancellation, and recovery receipts; and
- a durable record root that is not scratch-only `.runs/`.

Docker is not a synonym for evidence: an image tag without an observed digest
is not pinned. Likewise, an E2B template name or an AppContainer label is not a
runtime activation receipt. The existing standalone verifier has closed
network-boundary mappings for the built-in adapters. An unknown cloud adapter
must first add a digest-bound capability and the corresponding generic
boundary/activation evidence contract; until then it cannot produce a claim.

The intended extension path is:

```text
execution bundle -> registered backend id
                 -> backend_factory capability (digest + source closure)
                 -> execution capability (subject/interface + reset policy)
                 -> observed runtime/network/export receipts
                 -> existing BMP gates and standalone verifier
```

Do not add `if mode == "e2b"` branches to the BMP compiler or verifier. Add a
separate adapter package, registry declarations, focused tests, and (where the
evidence contract truly needs it) a separately reviewed protocol-change PR.
The E2B and AppContainer adapter work items are intentionally separate from
experiment bundles so several agents can work without merge contention.

## Merge Rules

The default unit of parallel work is one bundle directory or one lab issue
directory. Avoid shared generated files and broad matrix edits. A PR should:

1. identify one immutable lab issue and its declared write scope;
2. change one experiment bundle (or one adapter package) where possible;
3. leave BMP schemas, runner semantics, and protocol registries untouched for
   experiment-only work;
4. run `uv run bmp-collab validate`, focused tests, `bmp-lab doctor`, and
   `git diff --check`; and
5. include the exact command, evidence classification, recovery plan, and
   mirror/index policy in the PR template.

The local change classifier makes this boundary executable:

```bash
uv run bmp-collab changes --base-ref origin/main
```

Protocol/shared registry paths require an explicit protocol-change review and
registry declarations require a refreshed `registries/registry.lock.toml`.
Mixing a protocol edit with a bundle is reported as a split-PR warning. GitHub
branch protection should require the aggregate checks described in
`.github/OWNERS.md`; `PoorOtterBob` is the sole CODEOWNER and final reviewer.

## Recovery and Exactly-Once Limits

Use the lab event id as the idempotency key for a coordination mutation. A
retry with the same event id and intent is safe; a model/API invocation is not
made exactly-once by the ledger. Before an interruption:

```bash
uv run bmp-lab checkpoint <issue-id> \
  --event-id checkpoint-<actor>-<n> \
  --actor <actor> \
  --resume-arg uv --resume-arg run --resume-arg bmp-run \
  --resume-arg <experiment.toml> --resume-arg=--record-root \
  --resume-arg <fresh-durable-root> \
  --require-env PROVIDER_API_KEY \
  --next-action "Inspect the checkpoint and reacquire the lease"
uv run bmp-lab recover <issue-id>
```

Checkpoints contain environment variable names only. A recovery command is a
reviewable plan, not an automatic resume, and a new execution must use a fresh
record root. Preserve failed attempts and classify provider, infrastructure,
verifier, and agent outcomes separately.
