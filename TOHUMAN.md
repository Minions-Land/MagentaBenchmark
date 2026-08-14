# MagentaBench For Humans

This is the short human entrypoint for `Minions-Land/MagentaBenchmark`.
MagentaBench is the benchmark-side protocol and evidence ledger around an
agent evaluation. It does not replace an agent, a provider, a benchmark's
official verifier, or a container runtime.

## Start From GitHub

GitHub `main` is the shared source of truth for repository code and tracked
collaboration records. A local checkout or worktree is an execution surface,
not a second canonical repository.

```bash
git clone https://github.com/Minions-Land/MagentaBenchmark.git
cd MagentaBenchmark
git remote -v
git status --short --branch

UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
  uv sync --frozen --extra test

uv run --frozen bmp-agent validate
uv run --frozen bmp-agent modes
uv run --frozen bmp-lab doctor
```

The configured Git mirror is a fetch accelerator only. `origin` points to
GitHub and is the only push target. Never push to a mirror remote, and never
put a token, authenticated URL, or provider secret in a commit, issue, PR,
manifest, log, or artifact.

## Choose A Path

| Goal | Read next | First durable object |
| --- | --- | --- |
| Understand the protocol | [README](README.md) and [BMP boundary law](docs/governance/bmp-boundary-law.md) | None |
| Run an existing pairing | [Experiment matrix](docs/EXPERIMENT_MATRIX.md) and [runbook](docs/EXPERIMENT_RUNBOOK.md) | An experiment bundle and lab issue |
| Compare models, tools, or budgets | [Configuration](docs/governance/bmp-configuration.md) and [experiment ledger](docs/EXPERIMENT_LEDGER.md) | A preregistered experiment |
| Add an agent, benchmark, or backend | [README extension guide](README.md#extend-the-system) | A scoped Issue and adapter change |
| Hand work to another operator | [GitHub development playbook](docs/GITHUB_DEVELOPMENT.md) | A checkpoint, commit, and stable run link |
| Inspect prior benchmark results | [Historical imports](docs/HISTORICAL_IMPORTS.md) | Source provenance and evidence tier |

## The Normal Contribution Loop

1. Define one question, held-fixed variables, acceptance criteria, budget,
   artifact destination, and invalidation conditions in a GitHub Issue and the
   corresponding immutable `bmp-lab` issue.
2. Work on an isolated branch or worktree. Keep experiment-only changes under
   one `experiments/<id>/` bundle; do not change BMP schemas, runner semantics,
   or registries merely to record a result.
3. Claim the declared write scope through `bmp-lab` before editing shared
   files or starting an expensive run. One active writer owns one scope.
4. Compile the exact experiment, use a fresh durable record root, and retain
   manifests, case evidence, aggregate, report, and verifier inputs together.
5. Open a focused pull request. Describe the design, included and excluded
   scope, verification commands, artifacts and digests, risks, and checks not
   run. An Agent review is not human approval.
6. After merge, verify the new `main` commit, publish the checkpoint/release
   records, and close the lab issue only when the durable chain is complete.

## Evidence Is Not The Same As A Score

Every run declares a purpose. `exploratory` output is diagnostic evidence;
`claim` output still needs `claim_eligible = true`, every required gate, and
standalone verification from persisted bytes. A green process, requested model
name, task completion message, or zero exit code does not prove a model ran or
that a benchmark result is comparable.

Keep the full identity tuple fixed and visible: benchmark and dataset revision,
subject and adapter, backend and image digest, provider/model activation,
protocol, case set and order, seed, retry policy, budget, metrics, and record
root. If one of those changes, make a new experiment identity rather than
overwriting a result.

## Execution Targets

Docker and local-process paths are usable only when their registered
capabilities and readiness gates pass. AppContainer, Apptainer, E2B, and other
remote targets remain exploratory until runtime identity, isolation, network,
artifact export, teardown, recovery, and standalone verification are recorded.
The target name alone is not evidence of isolation.

## Shift Handoffs

Before stopping, record one concrete next action in a committed checkpoint:

```text
Issue: <GitHub and lab issue ids>
Checkpoint: <event id and commit>
Run/job: <stable id and observed state>
Record root: <durable locator>
Dirty paths: <none or exact paths>
Next action: <one action>
```

Mention the next operator only after the checkpoint and commit are published.
The mention, assignment, or PR comment is a notification; ownership transfers
only when the incoming operator verifies the checkpoint and acquires a new
lease. The incoming operator must query a live job before restarting it.

## Reading Order

- [README.md](README.md): complete protocol and executable smoke path.
- [TOAGENT.md](TOAGENT.md): compact machine/Agent startup contract.
- [AGENTS.md](AGENTS.md): repository rules and authority boundaries.
- [docs/GITHUB_DEVELOPMENT.md](docs/GITHUB_DEVELOPMENT.md): Issues, PRs, and
  handoff protocol.
- [docs/EXPERIMENT_RUNBOOK.md](docs/EXPERIMENT_RUNBOOK.md): interruption,
  preflight, execution, and claim gates.
- [docs/MIRROR_ACCELERATION.md](docs/MIRROR_ACCELERATION.md): fetch and image
  acceleration without weakening provenance.
