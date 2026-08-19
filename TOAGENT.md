# MagentaBench For Agents

This is the compact machine-facing entrypoint. Read [`AGENTS.md`](AGENTS.md)
before acting; this file is an index and an invariant checklist, not a
permission grant. Repository rules, the active lab issue, branch protection,
and the user's requested scope determine what you may do.

## Non-Negotiable Invariants

- Verify the effective repository, worktree, branch, remotes, identity, and
  current commit before any write or run.
- Treat GitHub `origin/main` as canonical shared state. The configured mirror
  is fetch-only acceleration; never push to it.
- Keep BMP protocol identity, execution contracts, and evidence boundaries
  separate from agent/provider implementation and benchmark-native scoring.
- Use a fresh durable record root for every execution. Preserve all attempts,
  failures, invalid results, verifier outcomes, and usage evidence.
- Never store credential values, authenticated URLs, private paths, or copied
  `.tmp` state in tracked files or durable evidence.
- Do not infer human approval, model activation, isolation, benchmark success,
  or claim eligibility from a label, request, log line, or exit code.
- Do not hand-edit, delete, rename, or resequence immutable `lab/` issue/event
  JSON. Use `bmp-lab` for all ledger mutations.

## Deterministic Entry Check

Run from the repository root and retain the commit and return codes:

```bash
git status --short --branch
git remote -v
git rev-parse --show-toplevel
git rev-parse HEAD

uv run --frozen bmp-agent validate
uv run --frozen bmp-collab modes
uv run --frozen bmp-lab doctor
uv run --frozen bmp-lab status --format json
```

If the tree is dirty, an active writer or run exists, or `bmp-lab doctor`
reports a broken chain, stop and reconcile it before writing or starting an
expensive execution. A lost terminal connection is not evidence that a job
failed; query its stable job/run id first.

## Declare Role And Scope

Before changing files, state in the Issue/PR or lab note:

```text
role: implementer | reviewer | coordinator | operator
issue: <GitHub issue and immutable lab issue>
base: <canonical main SHA>
branch/worktree: <exact path and branch>
write scope: <exact paths/resources>
held fixed: <benchmark, dataset, subject, backend, protocol, budget>
acceptance: <observable checks>
recovery: <checkpoint and artifact destination>
```

Claim the declared scope with a stable event and lease id before editing shared
files or launching a costly run. Use an isolated branch/worktree and one active
writer per scope. Contributors may report findings, but `PoorOtterBob` is the
sole accountable repository reviewer. A reviewer does not edit another owner's
branch or manufacture an approval.

## Experiment Contract

An experiment is a mergeable bundle, not a line in a hand-written global
spreadsheet. Preregister the benchmark/dataset revision, explicit case set,
subject and adapter, backend and immutable image/executable identity, provider
and observed activation, protocol, factors, repetitions, metrics, budget,
artifact destination, and invalidation rule.

Compile before running. Execute into a new record root. Verify the persisted
report independently. `purpose = "exploratory"` is diagnostic; a claim needs
positive claim gates and `claim_eligible = true`. Keep every planned slot in
the denominator, including timeout, missing, invalid, and verifier-failure
states.

Use the registered execution capability for the target. Docker or
local-process readiness does not authorize AppContainer, Apptainer, E2B, or a
different cloud boundary. An unknown target is exploratory until runtime
identity, network/isolation, export, teardown, recovery, and verification are
observed.

## GitHub Synchronization

Use GitHub for durable shared code and review:

```bash
git fetch origin main
git diff --check
git push --set-upstream origin <branch>
```

Open a focused PR against `main` with the issue, design, changed paths,
verification results, artifacts/digests, risks, and omissions. Do not force
push. Do not merge, close, release, approve, or alter policy unless the active
request and repository rules authorize that action. Passing checks is not a
review. `PoorOtterBob` provides the sole final review; on his own PR he uses
the exact-head self-review attestation. New `bmp-lab` `approved` reviews must
also be authored by `PoorOtterBob`; other actors can record advisory or
`changes_requested` findings. The repository author may merge only when branch
policy and the requested authority permit it.

## Handoff Receipt

Before interruption or ownership transfer, stop launching new work and publish
one durable checkpoint containing:

```text
issue: <immutable lab issue>
checkpoint: <event id / revision>
commit: <SHA>
branch: <name>
dirty paths: <none or exact list>
run/job: <stable id, queried UTC time, state>
record root: <safe durable locator>
artifacts: <SHA-256 references>
next action: <exactly one action>
risks/blockers: <explicit or none>
released scope: <exact paths/resources>
```

Commit and publish the checkpoint, release the lease when appropriate, and
then notify the next operator. The incoming operator must run `bmp-lab
recover`, verify the live job, and acquire a new lease before writing or
starting a new record root.

## Stop Conditions

Stop and report a blocker when any of these is true:

- the effective repository, branch, base SHA, or write scope is uncertain;
- a required lease, dependency, manifest, image digest, or provider activation
  is missing or cannot be verified;
- a command failed without a retained, content-addressed output;
- a verifier, runtime, network, or artifact boundary is ambiguous;
- a run may still be alive after a connection loss;
- a requested claim would rely on historical or external evidence whose
  provenance/comparability is not explicit.

Use the relevant durable lab event and GitHub Issue/PR to record the blocker,
recovery condition, and one next action. Never silently bypass a gate by
editing the ledger, deleting a failed attempt, or switching to a floating
dependency.

## Canonical References

- [`README.md`](README.md): human-readable protocol and smoke run.
- [`TOHUMAN.md`](TOHUMAN.md): human contributor and handoff guide.
- [`AGENTS.md`](AGENTS.md): complete repository authority and safety rules.
- [`docs/GITHUB_DEVELOPMENT.md`](docs/GITHUB_DEVELOPMENT.md): GitHub workflow.
- [`docs/EXPERIMENT_RUNBOOK.md`](docs/EXPERIMENT_RUNBOOK.md): run gates.
- [`docs/EXPERIMENT_LEDGER.md`](docs/EXPERIMENT_LEDGER.md): generated views.
- [`docs/governance/EXECUTION_MODES.md`](docs/governance/EXECUTION_MODES.md):
  backend and remote-target boundary.
