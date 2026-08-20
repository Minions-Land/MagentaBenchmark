# MagentaBench Operating Guide

This is the single operational guide for Minions-Land/MagentaBenchmark. It
describes how a person or an agent changes the repository, records an
experiment, and hands work to the next operator. It is deliberately
self-contained: this page is the workflow authority, while protocol and data
documents define contracts and facts.

AGENTS.md remains the repository authority boundary. It can impose stricter
limits than this guide, but no other guide may weaken it. TOHUMAN.md,
TOAGENT.md, and the older workflow paths are compatibility entry points and
must not introduce a second procedure.

## 1. Start

Work from the repository root and record the output before writing:

~~~bash
git status --short --branch
git remote -v
git rev-parse --show-toplevel
git rev-parse HEAD
uv run --frozen bmp-agent validate
uv run --frozen bmp-collab validate
uv run --frozen bmp-collab modes
uv run --frozen bmp-lab doctor
uv run --frozen bmp-lab status --format json
~~~

origin is the canonical GitHub remote and origin/main is the shared base.
A configured Git mirror is fetch acceleration only. Never push to a mirror.
Use the locked environment and the documented Aliyun package mirror; never
put credentials, authenticated URLs, or secret values in commands, Git, lab
events, manifests, logs, or evidence.

For a fresh checkout, install exactly the lockfile before running a command:

~~~bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
  uv sync --frozen --extra test
~~~

Mirrors accelerate transport only. The resolved Git commit, package lock,
container digest, and source tree remain the identities recorded in evidence.

Before any write, declare role, issue, base commit, branch/worktree, exact
write scope, held-fixed behavior, acceptance checks, and recovery location.
Read the relevant issue with bmp-lab show or bmp-lab recover. If the tree,
lease, live job, or lab chain is uncertain, stop and reconcile it first.

## 2. Authority And Roles

Git tracks reviewed code and durable collaboration events. The BMP protocol,
official benchmark verifier, persisted report, and ledger are the sources of
benchmark truth. A chat message, issue label, process log, zero exit code, or
receipt never creates a result claim.

PoorOtterBob is the sole accountable repository reviewer. Other contributors
may implement, coordinate, or provide advisory findings. A reviewer does not
edit another owner's branch. A new bmp-lab approved review is also authored
by PoorOtterBob; do not represent an advisory review as approval.

One active writer owns each path/resource scope. Use a stable event id for one
operation, retry uncertain delivery with the same id, and never reuse it for a
different intent. Create or mutate lab issues and events only through bmp-lab;
their JSON is immutable after publication.

## 3. Branches And Worktrees

Every branch has one purpose and one owner. Branch names are descriptive and
must include the change class:

| Prefix | Use | Allowed content |
| --- | --- | --- |
| docs/ | operating or contract documentation | docs, entrypoints, templates |
| feat/ | reviewed product or protocol implementation | implementation and focused tests |
| fix/ | defect correction | smallest reproducer, fix, regression test |
| experiment/ | one preregistered experiment | one experiments/<id>/ bundle and its evidence links |
| results/ | typed result inventory or import | source snapshots, rows, authority notes |
| ops/ | execution profile or infrastructure | registered profile and readiness checks |
| chore/ | repository maintenance | mechanical, scoped maintenance only |
| archive/ | historical material | explicitly non-current documents |

Use an isolated worktree for every active writer:

~~~bash
git fetch origin main
git worktree add /mnt/aliyunsb/<repo>-<purpose> -b <prefix>/<purpose> origin/main
cd /mnt/aliyunsb/<repo>-<purpose>
git status --short --branch
~~~

The worktree path is an execution surface, not another source of truth.
Never reset, clean, force-push, or overwrite a dirty or live worktree. Do not
reuse an old branch for a new question. If a branch is behind origin/main,
update it with a reviewed merge or rebase decision recorded in the Issue; do
not silently rewrite published history.

Branch ownership is represented by the lab lease, not by a branch name alone.
Claim the declared path scope before editing:

~~~bash
uv run bmp-lab claim <issue-id> --lease-id <lease-id> \
  --event-id <stable-event-id> --branch <branch> \
  --base-commit <base-sha>
git push origin HEAD:<branch>
uv run bmp-lab show <issue-id>
~~~

Renew a long task before the lease expires and release it after handoff or
merge. A mention is notification, not ownership; the incoming operator must
recover the checkpoint and acquire a new lease.

### Branch integration

Keep commits small and independently reviewable. A PR must state its base,
scope, included and excluded paths, held-fixed protocol behavior, tests,
artifacts/digests, risks, and unrun checks. Merge only the exact reviewed head.
The normal order is:

~~~text
issue -> lease -> isolated branch -> commits -> checks -> PR -> PoorOtterBob
review -> exact-head merge -> verify main -> checkpoint -> release lease
~~~

Use a no-fast-forward merge when integrating a meaningful branch so the
purpose and review boundary remain visible:

~~~bash
git fetch origin main <branch>
git diff --check
git log --oneline origin/main..<branch>
git diff --stat origin/main...<branch>
git checkout main
git pull --ff-only origin main
git merge --no-ff <branch>
git push origin main
git show --stat --oneline HEAD
~~~

Do not merge from an unreviewed local commit, and do not close the Issue until
the exact merge commit is verified on origin/main. If two branches touch the
same scope, stop and choose an integration order; resolve conflicts in a new
coordinator worktree with both owners' evidence, never by deleting one side.
Protocol/schema/runner/registry changes are separate PRs from experiment-only
changes. Results imports are separate from code changes.

### Branch relationship matrix

Use this matrix before creating a PR. The target is the branch on which the
change will be reviewed; a branch may depend on another branch only through an
explicit PR or a recorded integration decision.

| Source branch | Normal target | May contain | Must not contain |
| --- | --- | --- | --- |
| docs/* | main | guides, templates, navigation | runtime code or result rows |
| feat/* or fix/* | main | implementation plus focused tests | unrelated experiment outputs |
| experiment/* | main | one bundle and immutable evidence links | BMP schema/runner edits or global tables |
| results/* | main | typed imports, authority notes, source digests | unverified claims or executable changes |
| ops/* | main | registered profiles and readiness checks | live service state or credentials |
| chore/* | main | mechanical maintenance | mixed feature or experiment scope |
| any scoped branch | another scoped branch | only when the dependency is named in both PRs | silent cherry-picks or hidden merge bases |

`main` is the integration branch, not a workspace for experiments. Do not
develop directly on it. A release/tag is cut only from a verified main commit
and never becomes a place to edit results. A branch that changes a frozen
experiment creates a new experiment/version and run identity; it does not
rewrite the old branch or its records.

Inspect branch relationships without mutating them:

~~~bash
git branch --all --verbose --no-abbrev
git log --graph --decorate --oneline --all --max-count=80
git merge-base --is-ancestor <source> <target>
git diff --name-status <target>...<source>
git worktree list --porcelain
~~~

Before retiring a branch, verify that its exact head is merged on the intended
target, its PR and lab issue are closed or explicitly superseded, no live run
or lease names it, and its checkpoint is durable. Delete only the remote/local
branch that was explicitly approved; never use broad cleanup commands and
never remove a worktree containing dirty or unverified artifacts.

### What to keep, merge, or retire

- Keep main small: stable protocol, registered adapters, validators, and
  navigational documentation.
- Keep each experiment self-contained under experiments/<id>/; merge its
  declaration and evidence links, not a mutable global progress table.
- Keep historical results under imports/<source-snapshot-id>/ with source
  commit/tree, normalizer, license/publication decision, and evidence tier.
- Retire a branch after its exact head is merged and its lease is released.
  Do not delete a branch that still owns a live run or an unresolved handoff.

## 4. Issues, Pull Requests, And Handoffs

Open one GitHub Issue for one durable problem or experiment decision. Include
objective, owner, dependencies, acceptance criteria, held-fixed variables,
budget, artifact destination, invalidation rule, and next action. Create the
matching immutable bmp-lab issue before shared writes.

Open a focused PR from the leased branch. The PR description must contain:

~~~text
Issue and lab issue:
Role and owner:
Base and exact head:
Changed paths:
Included / excluded scope:
Protocol and held-fixed behavior:
Verification commands and results:
Artifact locators and SHA-256:
Risks, blockers, and checks not run:
Recovery and next action:
~~~

Read reviews as claims to verify. Only PoorOtterBob can provide final
approval. A requested change requires a new commit and a fresh exact-head
review; do not edit the reviewer's branch. Before handoff, publish a
checkpoint with commit, branch, dirty paths, run/job state, record root,
artifacts, one next action, risks, and released scope, then mention the next
operator with the Issue and checkpoint ids.

## 5. Data And Result Records

Data is identified by benchmark, dataset revision, split, case/question set,
and content digest. A source snapshot records repository/remote, commit,
tree, files, license/visibility, and normalizer. Never copy private raw data,
answers, traces, credentials, or machine-specific paths into this repository.

Each result row represents one case or question within one run. It binds:

~~~text
benchmark, dataset and split, case/question id, run id and purpose
model/provider identity and code commit
denominator and outcome: success, failure, timeout, invalid, or missing
BMP spec, manifest, config, dataset, and evaluator digests
fresh durable record_root and immutable artifact locators
official verifier and persisted report digest/size
publication/source table reference and evidence tier
~~~

Aggregates are derived views. Keep every planned slot in the denominator and
retain negative, invalid, timeout, and verifier-failure outcomes. An imported
or historical summary is never a BMP claim until it has a new verified report,
fresh record root, and the normal bmp-verify-report -> bmp-lab link-run ->
ledger path. claim_eligible comes only from the BMP verifier and ledger gates,
never from a bridge receipt or narrative summary.

## 6. Experiment Bundle

An experiment is a mergeable declaration, not a mutable global spreadsheet.
Create one directory:

~~~text
experiments/<id>/
  bundle.json       # frozen BMP declaration and identities
  PLAN.md           # question, hypothesis, factors, budget, stop rule
  README.md         # scope and operator entry
~~~

Preregister treatment, control, held-fixed variables, benchmark/dataset
revision, explicit case set, subject and adapter, execution backend and
immutable image/executable identity, protocol version, repetitions, primary
metric, uncertainty, budget, artifact destination, and invalidation rule.
Validate the bundle before execution:

~~~bash
uv run --frozen bmp-collab scaffold <id> --bmp-spec <path> \
  --lab-issue <issue-id> --question "..." --hypothesis "..." \
  --stop-condition "..."
uv run --frozen bmp-collab validate
uv run --frozen bmp-collab ledger --format json
~~~

An experiment PR normally touches only its bundle, its immutable lab events,
and focused tests. Do not change BMP schemas, runner semantics, or registries
to record a single result.

## 7. Run, Verify, And Publish

Compile the exact declaration, verify all identities, and use a new durable
record root for every execution. .runs/ is scratch and is never the only
copy. Use check_run_root --require-new before submission. Do not run
concurrent writers against one root.

Execution targets are selected through a registered, digest-bound adapter.
Docker, AppContainer/Apptainer, E2B, and remote/cloud targets each require
runtime identity, network/isolation evidence, artifact export, teardown,
recovery, and standalone verification. An unknown target remains exploratory.
Never start or stop a shared Supervisor or worker without an explicit
operator lease and readiness evidence.

The evidence path is:

~~~text
compile -> preflight -> submit/run -> fresh record_root
       -> persisted case evidence and report
       -> independent official verifier
       -> bmp-verify-report
       -> bmp-lab link-run
       -> derived ledger/result view
~~~

For a declared experiment, the minimum operator sequence is:

~~~bash
uv run --frozen bmp-collab preflight <experiment-id> \
  --actor <lease-holder> --dry-run
uv run --frozen bmp-compile path/to/experiment.toml \
  --manifest-out <record-root>/manifest.json
uv run --frozen bmp-run path/to/experiment.toml \
  --record-root <fresh-record-root>
uv run --frozen bmp-verify-report \
  <fresh-record-root>/<experiment-id>/observation_report.json
uv run --frozen bmp-lab link-run <issue-id> \
  --run-id <run-id> --record-root <fresh-record-root>
uv run --frozen bmp-collab ledger --format json
~~~

Replace placeholders only after the bundle, lease, manifest, and fresh-root
checks pass. Save command output and return codes in the checkpoint; do not
paste secrets. A claim report follows the same path but requires every BMP
claim gate and standalone verification to pass.

The verifier must check report path, digest, size, schema, denominator,
terminal states, evaluator identity, and all claim gates. Logs are diagnostics,
not metrics. A nonzero process is not automatically invalid and a zero exit is
not success. If a run stops unexpectedly, preserve its artifacts and record
the terminal state; never rerun into the same root or overwrite the report.

## 8. Checkpoint, Recovery, And Shift Handoff

Checkpoint before interruption, network loss, or ownership transfer:

~~~text
issue: <GitHub and bmp-lab id>
checkpoint: <event id and revision>
commit / branch / worktree:
dirty paths:
run/job and queried UTC time:
record_root:
artifacts and SHA-256:
next action: exactly one
risks/blockers:
released scope:
~~~

Record environment variable names, not values. A dirty tree needs a reviewed
patch reference. The incoming operator runs bmp-lab recover, confirms the
live job by stable id before restarting anything, verifies the exact commit
and root, then claims a new lease. Old runs remain bound to their original
code, interface, evaluator, config, and dataset snapshots forever.

## 9. Failure And Stop Rules

Stop and report a blocker when repository, base, scope, lease, dependency,
manifest, image digest, provider activation, verifier, runtime, network, or
artifact identity cannot be proven. Also stop when a disconnected terminal may
still have a live job, when a root is not fresh/durable, or when a historical
summary lacks case-level provenance.

Classify outcomes as Supported, Refuted, Inconclusive, or Invalid. Do not
repair a failed run by editing evidence or ledger rows. Make a new versioned
experiment and run identity for a changed implementation.

## 10. Quick Templates

### Branch declaration

~~~text
role: implementer | reviewer | coordinator | operator
issue: <GitHub issue / bmp-lab issue>
base: <origin/main SHA>
branch/worktree: <name / absolute H20 path>
write scope: <paths/resources>
held fixed: <protocol, benchmark, dataset, backend, budget>
acceptance: <observable checks>
recovery: <checkpoint and artifact destination>
~~~

### Result row

~~~text
benchmark / dataset / split / case:
run_id / purpose / terminal state:
model and code commit:
denominator:
BMP / manifest / config / dataset / evaluator digests:
record_root:
official verifier and report digest/size:
outcome and evidence tier:
source/table locator:
~~~

### Definition of done

The exact reviewed head is on the intended base; required checks pass; no
unrun gate is hidden; the immutable lab chain, checkpoint, artifacts, report,
verifier result, and ledger link are present; the lease is released; and the
next operator can recover without private context.

## Command Index

~~~bash
uv run --frozen bmp-agent validate
uv run --frozen bmp-collab validate
uv run --frozen bmp-collab validate-imports
uv run --frozen bmp-collab modes
uv run --frozen bmp-lab doctor
uv run --frozen bmp-lab show <issue-id>
uv run --frozen bmp-lab recover <issue-id>
uv run --frozen bmp-lab status --format json
uv run --frozen bmp-lab checkpoint <issue-id> ...
uv run --frozen bmp-lab link-run <issue-id> ...
git diff --check
~~~
