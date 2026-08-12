# MagentaBench Laboratory Operations

This document defines the recoverable collaboration control plane for
MagentaBench. Use it to coordinate people and agents, preserve resumable work,
and reconstruct decisions after an interrupted machine or session.

## 1. Purpose and Boundary

`bmp-lab` maintains an immutable issue definition plus a hash-chained event
ledger. Replaying that ledger derives the current owner, status, lease,
blockers, checkpoint, linked runs, and review. Repeating the same mutation with
the same `event-id` and identical request is an idempotent no-op; reusing that
ID for a different request fails. A fork, missing sequence, changed immutable
record, or previous-revision mismatch also fails closed.

This is a collaboration control plane, not an execution transaction manager.
It serializes cooperating writers that use the same local ledger and records
recovery intent. It does **not** make benchmark execution, model/provider API
calls, external side effects, or billing exactly-once. A lease is not a
cross-machine distributed lock.

## 2. Sources of Truth

| Question | Authoritative source |
| --- | --- |
| Who owns active work, what is blocked, and where is its latest checkpoint? | `lab/`, reduced by `bmp-lab status` or `bmp-lab show` |
| What experiments and readiness gates are planned? | `docs/EXPERIMENT_MATRIX.md` and the immutable experiment definitions |
| Did a benchmark run happen and pass its gates? | Persisted report, record index, referenced bytes, and the standalone verifier |
| What result may currently be claimed? | Verified evidence plus review recorded in `EVIDENCE.md` |

Do not maintain a second hand-edited global progress board. `bmp-lab status`
derives the live board from issue and event records. Conversely, a lab issue
being `done` does not prove a benchmark claim and does not update
`EVIDENCE.md`; it only proves that the issue's collaboration acceptance and
review rules were satisfied.

## 3. Repository Layout

The default ledger root is `<project-root>/lab`:

~~~text
lab/
  README.md
  .lab.lock                         # local mutex; ignored by Git
  issues/
    <issue-id>/
      issue.json                    # immutable definition
      events/
        <event-id>.json             # immutable canonical event
~~~

Issue and event JSON are machine-owned records. Create and mutate them only
through `bmp-lab`; never edit, rename, delete, resequence, or copy them between
chains by hand. Commit the durable records to Git. Never place credentials,
tokens, private URLs, or secret values in issue text, notes, arguments, or
artifact locators.

The ignored `.runs/` tree is scratch storage. It may be linked while diagnosing
a run, but it is not durable publication storage and `bmp-lab doctor` will warn
about such references.

## 4. Quickstart

Run commands from the repository root. `uv` uses the locked environment; on
this host, bootstrap it with the mirror command in the experiment runbook.

~~~bash
uv run bmp-lab init
uv run bmp-lab doctor
uv run bmp-lab status

uv run bmp-lab open tb-images \
  --title "Restore pinned Terminal-Bench images" \
  --objective "Make exact task images available by immutable digest." \
  --actor agent-name \
  --priority p0 \
  --write-path registries/backends \
  --resource terminal-bench-images \
  --acceptance "images=Exact image digests are recorded"

uv run bmp-lab claim tb-images \
  --event-id claim-agent-name \
  --actor agent-name \
  --holder agent-name \
  --lease-id lease-agent-name \
  --ttl-seconds 14400

uv run bmp-lab set-status tb-images \
  --event-id start-agent-name \
  --actor agent-name \
  --status running
~~~

Use stable, unique event IDs that identify the intended operation. If command
delivery is uncertain, retry the exact request with the same event ID. Inspect
the issue before choosing a new ID; a new ID means a new event.

Use `--project-root PATH` when invoking the CLI elsewhere, or `--lab-root PATH`
only for an intentionally separate ledger. Splitting one team's work across
multiple lab roots produces multiple, uncoordinated control planes.

## 5. Issue Lifecycle

Open an issue with a narrow objective, explicit repository-relative write
paths and logical resources, dependencies, and testable acceptance criteria.
The definition is immutable. If its objective, scope, dependencies, or criteria
must materially change, open a replacement issue and record the relationship
in a note instead of rewriting `issue.json`.

The normal path is:

~~~text
open -> planned -> ready -> running -> verifying -> done
                         \-> blocked -> ready/running
~~~

`cancelled` and `done` are terminal. `block` records a category, summary, and
concrete recovery action and moves the issue to `blocked`. When available,
also record expected and observed behavior, structured reproduction argv, exit
code, content-addressed evidence, an external issue reference, and an explicit
unblock condition. Resolving the blocker does not guess the next workflow
status, so append the appropriate status event explicitly.

~~~bash
uv run bmp-lab block tb-images \
  --event-id block-image-inspect \
  --actor agent-name \
  --blocker-id image-missing \
  --category infrastructure \
  --summary "Pinned image inspection failed." \
  --expected "The exact pinned image is locally inspectable." \
  --observed "Docker returned No such image." \
  --reproduce-arg docker \
  --reproduce-arg image \
  --reproduce-arg inspect \
  --reproduce-arg alexgshaw/regex-log:20251031 \
  --exit-code 1 \
  --recovery-action "Restore the exact image and retain its immutable ID." \
  --unblock-condition "Inspection succeeds for the exact pinned reference"
~~~

Entering `running` or `verifying` requires the actor to hold the active lease.
Entering `done` requires all of the following:

- every blocker is resolved;
- the recorded lease is explicitly released (expiry alone is not release);
- a recovery checkpoint exists; and
- an approved review covers every declared acceptance criterion.

Those conditions govern work-item completion only. Benchmark promotion still
depends on the report and evidence gates in `docs/EXPERIMENT_RUNBOOK.md`.

Prefer immutable, content-addressed artifacts for review evidence. If a
completed or cancelled issue cites a repository-relative source file and that
file later changes, `bmp-lab doctor` accepts the snapshot only when the same
path has the exact recorded digest and size in the complete current `HEAD`
first-parent history. Side branches, tags, remote-tracking refs, and merge
second parents cannot supply the snapshot. Active issues still require the
current bytes to match. Missing current paths, absolute paths, external URIs,
and symlinks never use this fallback. Shallow clones, a mismatched Git top
level, and timed-out or malformed Git inspection fail closed; required CI jobs
therefore fetch full history.

## 6. Lease and Write-Scope Workflow

Before editing or starting a shared resource:

1. Fetch the canonical Git coordination ref and run `bmp-lab doctor`.
2. Inspect `bmp-lab status` and `bmp-lab show <issue-id>`.
3. Claim the issue. The lease captures its immutable path/resource scope plus
   the current Git HEAD and branch.
4. Publish the claim event to the canonical remote ref and confirm that push
   succeeded before treating it as visible to another host.
5. Renew before expiry, checkpoint before interruptible work, and release when
   leaving the scope.

An unowned issue may be self-claimed or self-assigned by a contributor. A
non-owner cannot use that bootstrap rule to assign or acquire a lease on behalf
of somebody else; delegation must come from the creator or current owner.

~~~bash
uv run bmp-lab renew tb-images \
  --event-id renew-agent-name-01 \
  --actor agent-name \
  --lease-id lease-agent-name \
  --ttl-seconds 14400

uv run bmp-lab release tb-images \
  --event-id release-agent-name \
  --actor agent-name \
  --lease-id lease-agent-name
~~~

On the same machine, `fcntl` plus immutable atomic file creation serializes CLI
access to one lab root, and a claim rejects overlap with another active
path/resource scope. A platform without POSIX `fcntl` support fails closed
instead of silently using only a process-local lock. On different machines,
neither the local lock nor an
unpushed event is visible. Git synchronization is a team protocol, not a
distributed mutex: even a pushed lease is an observable claim record, not an
atomic remote lock. Push rejection or a concurrent update requires fetching,
rerunning `doctor`, and reevaluating the claim before work continues.

An expired lease cannot be renewed. Use `recover`, inspect the last holder and
checkpoint, coordinate as needed, and claim a new lease with a new ID. Ordinary
notes do not erase the expired lease from the recovery trail; explicitly
release the current recorded lease before completing an issue.

## 7. Checkpoint and Recovery

A checkpoint captures Git HEAD and branch, all dirty paths, a structured resume
argument vector, required environment-variable **names**, the next action, and
content-addressed artifact references. If the worktree is dirty, provide a
reviewed UTF-8 patch with `--patch`; preserve untracked files separately and
reference any required recovery artifacts with repeated `--artifact` options.
Never put credential values in the resume arguments or patch.

For a clean tree:

~~~bash
uv run bmp-lab checkpoint tb-images \
  --event-id checkpoint-agent-name-01 \
  --actor agent-name \
  --resume-arg uv \
  --resume-arg run \
  --resume-arg bmp-run \
  --resume-arg MagentaBench/conformance/experiments/terminal-bench-regex-smoke.toml \
  --resume-arg=--record-root \
  --resume-arg /protected/artifacts/tb-regex-001 \
  --record-root /protected/artifacts/tb-regex-001 \
  --require-env PROVIDER_API_KEY \
  --next-action "Verify image digests, then start the single-case probe"
~~~

An argument beginning with `--` must use the
`--resume-arg=--flag-name` form so `argparse` does not consume it as a
`bmp-lab` option. `--require-env` records only a name; it never captures the
value.

After an interruption:

~~~bash
uv run bmp-lab doctor
uv run bmp-lab show tb-images
uv run bmp-lab recover tb-images
~~~

`recover` verifies and prints a recovery plan. It never executes the resume
command, restores files, exports variables, or calls a provider. The operator
must verify the commit, patch, artifact digests, environment, scope, and
record-root freshness before manually executing anything.

## 8. Run Linkage and Evidence Boundary

Link an operational run as its state changes. Use a fresh event ID for every
transition; a `finished` link requires a persisted report reference.

~~~bash
uv run bmp-lab link-run tb-images \
  --event-id run-tb-regex-running \
  --actor agent-name \
  --run-id tb-regex-001 \
  --state running \
  --record-root /protected/artifacts/tb-regex-001

uv run bmp-lab link-run tb-images \
  --event-id run-tb-regex-finished \
  --actor agent-name \
  --run-id tb-regex-001 \
  --state finished \
  --record-root /protected/artifacts/tb-regex-001 \
  --report /protected/artifacts/tb-regex-001/report.json
~~~

The link is an index, not proof. `doctor` checks locally available artifact
digests and invokes the standalone report verifier for a finished report.
One run ID remains bound to one record root and one non-null manifest digest;
another run ID cannot reuse that root. The same run may be linked from multiple
issues only with those identities unchanged.
An external locator for an ordinary recovery artifact produces a warning
because its bytes are not available locally. A finished report that is only an
external locator makes `doctor` fail: it cannot be treated as verified until
the bound bytes are locally available to the standalone verifier. A successful
check still does not turn an exploratory run into a claim; apply every gate in
the experiment runbook and review `EVIDENCE.md` separately.

To complete a work item, move it to `verifying`, append a review over all
declared criterion IDs, release the lease, and then set `done`:

~~~bash
uv run bmp-lab review tb-images \
  --event-id review-tb-images \
  --actor agent-name \
  --verdict approved \
  --summary "Pinned image identities and checks are present." \
  --accept-criterion images \
  --evidence /protected/artifacts/tb-images/verification.txt
~~~

## 9. Multi-Host Git Workflow

The team must designate one canonical remote branch/ref for `lab/` records.
For every mutation, use this sequence:

1. Fetch and fast-forward from the canonical ref.
2. Run `bmp-lab doctor` and inspect the relevant issue.
3. Append exactly one intended CLI mutation.
4. Review and commit the new immutable record without folding unrelated work
   into it.
5. Push immediately. If the ref moved, do not force-push; fetch and validate
   the combined state first.
6. Only an event accepted on the canonical ref may be used as cross-host
   coordination input.

Concurrent events on the same issue can each have the same sequence and
previous revision even when Git merges their different filenames cleanly. The
ledger correctly treats that result as a fork. Preserve both branches, select
the canonical history through review, and replay the rejected intention through
the CLI against the canonical state with a new event ID. Never repair a fork by
editing sequence numbers or revision hashes.

## 10. Current Runtime Limitations

The lab ledger and the benchmark Pipeline have separate durability properties.
The ledger does not remove these current Pipeline limitations:

- a record root has no cross-process exclusive lock; the non-empty-directory
  check and later creation have a time-of-check/time-of-use window;
- `events.jsonl` uses an unlocked read-modify-write append;
- Pipeline `atomic_write_bytes` uses one fixed `<name>.tmp` path, so concurrent
  writers to the same destination can interfere;
- there is no attempt-level write-ahead lifecycle ledger that durably records
  provider side effects before and after launch;
- checkpoint recovery supports only a limited completed parent-run prefix;
- multi-case checkpoint identity is not implemented and fails closed;
- the checked-in Terminal-Bench, SWE-bench, and first-wave benchmark protocols
  use `checkpoint_policy=disabled`; and
- if a process dies after a provider request is launched but before durable
  completion evidence is written, retrying may repeat the call and its charge.

Therefore, use a fresh record root for each new execution and reuse one only
for an explicitly validated resume. Avoid concurrent Pipeline writers,
preserve partial bytes, and manually inspect provider-side request/billing
records before retrying an interrupted real-model attempt.

## 11. Failure Handling

- If `doctor` returns nonzero, freeze new shared or expensive experiments. Save
  the current Git refs and recovery bundle before investigating.
- For a fork, gap, or revision drift, do not mutate ledger JSON. Reconcile on a
  preserved branch and replay intended operations through the CLI.
- For a missing or digest-drifted checkpoint artifact, restore the exact bytes
  or append a new checkpoint that references the replacement; never alter the
  old event.
- For an active issue with no live lease, run `recover`, contact the prior
  holder if possible, and claim a new lease before resuming.
- For uncertain command delivery, retry the identical request and event ID.
  `changed: false` confirms that the original immutable event already exists.
- For an interrupted provider call, first inspect retained local evidence and
  provider-side request history. Do not assume a CLI retry is free or unique.

## 12. Operator Checklist

Before work:

- fetch the canonical coordination ref;
- run `bmp-lab doctor` and review every warning;
- inspect `status`, dependencies, blockers, checkpoint, and write scope;
- expect experiment preflight to stop on a matching `open`, `planned`, or
  `blocked` issue, or when active work has no live lease;
- claim and publish the lease before touching shared scope;
- choose a fresh record root for any execution.

Before pausing or handing off:

- retain partial artifacts and a reviewed dirty-tree patch when needed;
- append a checkpoint with exact resume argv, environment names, and next step;
- record blockers with recovery actions and link every launched run;
- commit and push the new ledger events and relevant work;
- renew the lease or release it explicitly.

Before declaring completion:

- rerun `doctor` and the relevant code/evidence checks;
- verify persisted reports from referenced bytes;
- resolve blockers, checkpoint, and obtain criterion-complete review;
- release the lease before setting the issue to `done`;
- update `EVIDENCE.md` only through an independent claim review.
