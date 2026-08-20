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

Publish ownership before creating the substantive worktree. The canonical
coordination ref is `origin/main`; a feature branch alone is not visible
cross-host coordination. Use a short coordination branch and PR for the issue
and lease event:

~~~bash
git fetch origin main
git worktree add <durable-worktree-parent>/<repo>-lease \
  -b coordination/<issue-id>-lease-<lease-id> origin/main
cd <durable-worktree-parent>/<repo>-lease
uv run --frozen bmp-lab doctor
uv run --frozen bmp-lab show <issue-id>
uv run --frozen bmp-lab claim <issue-id> \
  --event-id <stable-event-id> --actor <actor> \
  --holder <holder> --lease-id <lease-id>
git add lab/issues/<issue-id>/events/<stable-event-id>.json
git commit -m "lab: claim <issue-id> scope"
git push --set-upstream origin coordination/<issue-id>-lease-<lease-id>
gh pr create --base main \
  --head coordination/<issue-id>-lease-<lease-id> \
  --title "lab: claim <issue-id> scope" \
  --body-file <reviewed-body-file>
~~~

After required checks, PoorOtterBob exact-head review, and an authorized merge,
fetch and prove that the claim commit is an ancestor of `origin/main`. Then
create the substantive branch from that canonical state and read the reduced
lease back:

~~~bash
git fetch origin main
git merge-base --is-ancestor <claim-commit> origin/main
git worktree add <durable-worktree-parent>/<repo>-<purpose> \
  -b <prefix>/<purpose> origin/main
cd <durable-worktree-parent>/<repo>-<purpose>
uv run --frozen bmp-lab doctor
uv run --frozen bmp-lab show <issue-id>
~~~

If the coordination PR conflicts, the ref moved, the claim is not on
`origin/main`, or `doctor` reports an overlap or fork, stop. Never force-push or
edit event JSON; fetch the canonical state and replay the still-valid intent
through `bmp-lab` with a new event ID. The worktree path is an execution
surface, not another source of truth. Never reset, clean, force-push, or
overwrite a dirty or live worktree.

`bmp-lab claim` captures the coordination branch and HEAD that created the
event. Ownership is the canonical event's lease ID and exact scope, not the
later feature-branch name. Renew a long task before expiry and publish the
renewal through the same coordination path. Release it after handoff or merge.
A mention is notification, not ownership; the incoming operator must recover
the checkpoint and acquire a new canonical lease.

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
gh pr merge <pr-number> --merge
git fetch origin main
git show --stat --oneline origin/main
~~~

Use the GitHub PR merge path so branch protection and the exact-head review are
enforced. A local no-ff merge is allowed only when repository policy explicitly
authorizes it and the resulting merge is then published and verified on
`origin/main`. Do not merge from an unreviewed local commit, and do not
close the Issue until the exact merge commit is verified on `origin/main`.
If two branches touch the same scope, stop and choose an integration order;
resolve conflicts in a new coordinator worktree with both owners' evidence,
never by deleting one side.
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

When PoorOtterBob authored the PR, GitHub cannot provide an independent
approval. After reviewing the final diff and checks, use the repository's
attributable exact-head attestation in the PR body:

~~~text
- [x] I completed PoorOtterBob final review/self-review
Final review HEAD: `<full-head-sha>`
~~~

Any later commit invalidates that attestation. Protocol-impacting paths also
require the separate exact-head protocol self-review fields from the PR
template. Never check either box before the corresponding review is complete.

## 5. Lab Issue Lifecycle

The GitHub Issue explains the durable problem or experiment decision. The
matching `bmp-lab` issue is the immutable, hash-chained ownership and recovery
record. Create it before claiming a shared scope; every mutation below creates
one new event with a stable event ID.

Create the GitHub Issue first, then create the lab issue through the CLI:

~~~bash
gh issue create --repo Minions-Land/MagentaBenchmark \
  --title "<durable problem or experiment decision>" \
  --body-file <issue-body-file>
uv run --frozen bmp-lab open <issue-id> \
  --title "<title>" --objective "<objective>" \
  --actor <actor> --owner <owner> --priority p1 \
  --benchmark <benchmark> --label <label> \
  --write-path <path> --resource <resource> \
  --acceptance criterion-id="<observable acceptance condition>"
git add lab/issues/<issue-id>/issue.json
git commit -m "lab: open <issue-id>"
~~~

Publish that issue definition through the canonical coordination PR before
claiming it. Then use the state machine in order (a blocked issue leaves the
machine only after its named blockers are resolved):

~~~text
open -> planned -> ready -> running -> verifying -> done
                         \-> blocked -> planned/ready/running
~~~

Typical status and blocker mutations are:

~~~bash
uv run --frozen bmp-lab set-status <issue-id> \
  --event-id <planned-event-id> --actor <actor> --status planned
uv run --frozen bmp-lab set-status <issue-id> \
  --event-id <ready-event-id> --actor <actor> --status ready
uv run --frozen bmp-lab set-status <issue-id> \
  --event-id <running-event-id> --actor <actor> --status running
uv run --frozen bmp-lab block <issue-id> \
  --event-id <block-event-id> --actor <actor> --blocker-id <blocker-id> \
  --category external --summary "<what is blocked>" \
  --recovery-action "<concrete recovery>" \
  --unblock-condition "<observable condition>"
uv run --frozen bmp-lab resolve-blocker <issue-id> \
  --event-id <resolve-event-id> --actor <actor> \
  --blocker-id <blocker-id>
uv run --frozen bmp-lab renew <issue-id> \
  --event-id <renew-event-id> --actor <actor> \
  --lease-id <lease-id> --ttl-seconds 14400
~~~

Commit and publish each event to the canonical coordination ref. A local
event, an Issue comment, or an `@mention` is not cross-host ownership proof.
Before handoff, append a checkpoint; after the next operator claims the scope,
release the old lease. To close a work item, use this exact order:

~~~bash
uv run --frozen bmp-lab set-status <issue-id> \
  --event-id <verifying-event-id> --actor <actor> --status verifying
uv run --frozen bmp-lab review <issue-id> \
  --event-id <review-event-id> --actor PoorOtterBob \
  --verdict approved --summary "<criterion-complete review>" \
  --accept-criterion <criterion-id> \
  --evidence <review-artifact>
uv run --frozen bmp-lab release <issue-id> \
  --event-id <release-event-id> --actor <actor> --lease-id <lease-id>
uv run --frozen bmp-lab set-status <issue-id> \
  --event-id <done-event-id> --actor <actor> --status done
~~~

An approved lab review is a work-item acceptance record, not a benchmark
claim. A documentation-only issue therefore needs documentation evidence and
checks, not a fabricated report or ledger result. A run or result issue has
the additional report, standalone-verifier, and ledger requirements in the
next sections.

## 6. Data And Result Records

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

## 7. Experiment Bundle

An experiment is a mergeable declaration, not a mutable global spreadsheet.
Create one directory:

~~~text
experiments/<id>/
  bundle.json       # frozen BMP declaration and identities
  PLAN.md           # question, hypothesis, factors, budget, stop rule
~~~

`bundle.json` and `PLAN.md` are the files generated by `bmp-collab scaffold`.
An optional hand-written `README.md` may improve navigation, but it is not a
required bundle contract and must not duplicate this guide.

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

## 8. Run, Verify, And Publish

Compile the exact declaration, verify all identities, and use a new durable
record root for every execution. `.runs/` is scratch and is never the only
copy. The repository helper below is an explicit pre-submit check for a real
descendant of the supplied project root; it must run before `bmp-run` and the
proposed directory must not already exist:

~~~bash
uv run --frozen python \
  skills/experiment-infrastructure/scripts/check_run_root.py \
  <project-root> <project-root>/records/<experiment-id>/<run-id> --require-new
~~~

The helper intentionally rejects roots outside `project-root`. If a registered
backend uses a separate durable NAS root, use that backend's trusted-root
check, record the root locator and digest in the bundle/checkpoint, and state
why this project-descendant helper was not applicable. Do not present a
scratch `.runs/` path as durable evidence. Do not run concurrent writers
against one root.

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

For a declared experiment, first publish the issue lifecycle and lease in this
order: `planned`, then `ready`, then acquire or confirm an active lease and set
the issue to `running`. The preflight command requires the primary issue to be
`running` with that matching lease; a `planned` or `ready` issue must not launch
a run. The minimum operator sequence after those prerequisites is:

~~~bash
uv run --frozen bmp-lab set-status <issue-id> \
  --event-id <running-event-id> --actor <lease-holder> --status running
uv run --frozen bmp-collab preflight <experiment-id> \
  --actor <lease-holder>
uv run --frozen bmp-compile path/to/experiment.toml \
  | tee <existing-durable-evidence-dir>/compile.json
uv run --frozen bmp-lab link-run <issue-id> \
  --event-id <run-planned-event-id> --actor <actor> \
  --run-id <run-id> --state planned \
  --record-root <fresh-record-root> \
  --manifest-digest <sha256>
uv run --frozen bmp-lab link-run <issue-id> \
  --event-id <run-running-event-id> --actor <actor> \
  --run-id <run-id> --state running \
  --record-root <fresh-record-root> \
  --manifest-digest <sha256>
uv run --frozen bmp-run path/to/experiment.toml \
  --record-root <fresh-record-root>
uv run --frozen bmp-verify-report \
  <persisted-report-path>
uv run --frozen bmp-lab link-run <issue-id> \
  --event-id <terminal-event-id> --actor <actor> \
  --run-id <run-id> --state finished \
  --record-root <fresh-record-root> \
  --manifest-digest <sha256> \
  --report <persisted-report-path>
uv run --frozen bmp-collab ledger --format json
~~~

The preflight command above executes the bundle's registered preflight and
requires the primary lab issue to be `running` with a live matching lease;
adding `--dry-run` only prints its argv and never substitutes for this gate. The
compile command prints canonical resolved plans; retain that output in an
already-created durable evidence directory as a content-addressed artifact.
There is no `--manifest-out` option.

Publish the `running` link through the canonical coordination path before
launch and use a new event ID for the terminal transition. Never jump a new
run directly to `finished`. Select the actual persisted `claim_report.json` or
`observation_report.json`; a finished link requires `--report`, and its bytes
must pass `bmp-verify-report`. If execution does not finish validly, record the
matching `failed`, `invalid`, or `cancelled` terminal state and preserve its
artifacts instead of inventing a finished report. Replace placeholders only
after the bundle, lease, manifest, and fresh-root checks pass. Save command
output and return codes in the checkpoint; do not paste secrets.

The verifier must check report path, digest, size, schema, denominator,
terminal states, evaluator identity, and all claim gates. Logs are diagnostics,
not metrics. A nonzero process is not automatically invalid and a zero exit is
not success. If a run stops unexpectedly, preserve its artifacts and record
the terminal state; never rerun into the same root or overwrite the report.

## 9. Checkpoint, Recovery, And Shift Handoff

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

Create the checkpoint through `bmp-lab`; do not hand-edit an event JSON. Each
`--resume-arg` is one argv element, and each `--artifact` is an existing file
whose bytes are hashed by the command. The clean-tree form is:

~~~bash
uv run --frozen bmp-lab checkpoint <issue-id> \
  --event-id <checkpoint-event-id> --actor <actor> \
  --resume-arg=uv --resume-arg=run --resume-arg=--frozen \
  --resume-arg=bmp-lab --resume-arg=recover --resume-arg=<issue-id> \
  --next-action "<one concrete next action>" \
  --require-env=CUDA_VISIBLE_DEVICES \
  --artifact <durable-evidence-file>
~~~

Include `--experiment <experiment-id>` and `--record-root
<fresh-record-root>` when the checkpoint belongs to a run. If the worktree is
dirty, add a reviewed UTF-8 patch and preserve the dirty paths captured by the
command:

~~~bash
uv run --frozen bmp-lab checkpoint <issue-id> \
  --event-id <dirty-checkpoint-event-id> --actor <actor> \
  --resume-arg=git --resume-arg=status --resume-arg=--short --resume-arg=--branch \
  --next-action "<one concrete next action>" \
  --patch <reviewed-patch-file> \
  --artifact <patch-or-evidence-file>
~~~

Never put environment values, credentials, authenticated URLs, or an
unreviewed patch in a checkpoint. Retry an uncertain write with the same event
ID and arguments; use a new event ID for a new intent.

### Runtime limitations

These are current implementation limits, not guarantees that the operating
guide can waive:

- `bmp-lab`'s local file lock and atomic event creation do not form a
  cross-machine distributed lock; a pushed event is observable coordination,
  not an atomic remote lease.
- The record-root non-empty check and later creation have a TOCTOU window, and
  the Pipeline has no cross-process exclusive root lock. Keep roots fresh and
  avoid concurrent writers.
- `events.jsonl` uses an unlocked read-modify-write append, and Pipeline atomic
  writes use a fixed temporary filename; preserve partial bytes and do not
  share a writer destination.
- There is no write-ahead lifecycle ledger for provider side effects. A process
  death after a provider request and before durable completion evidence may
  make a retry repeat a call or charge.
- Checkpoint recovery supports only the implemented completed-parent prefix;
  multi-case checkpoint identity is not implemented and fails closed.
- The checked-in Terminal-Bench, SWE-bench, and first-wave benchmark protocols
  currently use `checkpoint_policy=disabled`.

These limits must appear in a checkpoint or handoff when they affect a run.

## 10. Failure And Stop Rules

Stop and report a blocker when repository, base, scope, lease, dependency,
manifest, image digest, provider activation, verifier, runtime, network, or
artifact identity cannot be proven. Also stop when a disconnected terminal may
still have a live job, when a root is not fresh/durable, or when a historical
summary lacks case-level provenance.

Classify outcomes as Supported, Refuted, Inconclusive, or Invalid. Do not
repair a failed run by editing evidence or ledger rows. Make a new versioned
experiment and run identity for a changed implementation.

## 11. Quick Templates

### Branch declaration

~~~text
role: implementer | reviewer | coordinator | operator
issue: <GitHub issue / bmp-lab issue>
base: <origin/main SHA>
branch/worktree: <name / absolute worktree path>
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

For a repository, documentation, or infrastructure change: the exact reviewed
head is on the intended base; required checks pass; no unrun gate is hidden;
the immutable lab chain and checkpoint are present; the approved review is
recorded; the lease is released; and the next operator can recover without
private context. Reports, verifiers, and result-ledger links are required only
when the issue actually runs or publishes a benchmark result.

For an experiment or result publication, add the fresh durable record root,
case-level evidence, persisted report, standalone verifier result, and
`bmp-verify-report -> bmp-lab link-run -> ledger` linkage. Never mark a
documentation issue complete by inventing those artifacts.

## 12. Command Index

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
