# External Lessons: Team Collaboration Workflow

This note distills reusable collaboration practices observed in an external
agent-evaluation project. It intentionally keeps process knowledge only. It
does not reproduce that project's algorithms, prompts, source code, private
paths, logs, or credentials.

Related material: [benchmark operations](EXTERNAL_SKILLER_BENCHMARK_OPERATIONS.md)
and the [numeric baseline declaration](external-evidence/skiller_tau2_baselines.json).

## What A Work Package Must Answer

Every work package should be executable without a private conversation and
reviewable without reading raw logs. Publish one versioned contract containing
both layers:

| Layer | Required information |
| --- | --- |
| Agent-executable | entry point, variables, paths, command, expected artifacts, checks, stop rules |
| Human-reviewable | source and purpose, scope/non-scope, resources, risks, duration, evidence limits, escalation channel |

Both layers bind to the same input revision and acceptance criteria. A command
proves that an operation can start; it does not prove that the result is
complete or correct.

## Project Skeleton And Work Packages

Keep a small public project skeleton and distribute bounded work packages:

```text
project skeleton
  -> infrastructure and shared code
  -> work-package/event contracts
  -> independent execution and review
  -> integrated evidence and handoff
```

The skeleton owns interfaces, environment capabilities, resource policy,
templates, and the current routing page. A work package owns one experiment,
one migration, or one reviewable deliverable. Do not create a long-lived copy
of the shared skeleton inside each worker's directory.

Each package records:

```text
work_package_id:
purpose:
source_revision:
in_scope:
out_of_scope:
inputs_and_dependencies:
DRI:
reviewer:
expected_duration:
start_conditions:
completion_conditions:
artifacts:
risks_and_stop_rules:
question_channel:
```

Only packages with frozen interfaces and independent evidence may run in
parallel. Shared mutable state, ordered online memory, or a version dependency
requires serial execution.

## Ownership And Review

Use one directly responsible owner (DRI) per package and one accountable
decision owner. A multi-party effort normally needs:

- a coordinator for the project contract, dependencies, and current route;
- a package owner for facts and artifacts;
- an integration owner for versions and end-to-end checks;
- an independent reviewer for evidence and acceptance;
- a decision owner for scope, resource, and risk trade-offs.

Two parties may combine roles, but execution and acceptance should remain
separate checks. A chat assignment is not ownership; ownership begins when the
durable issue/lease records the scope.

## Lifecycle And Feedback

Use a small explicit state machine:

```text
DRAFT -> READY -> IN_PROGRESS -> INTERNAL_REVIEW -> DELIVERED
       -> EXTERNAL_REVIEW -> ACCEPTED -> CLOSED
                         \-> REVISION_REQUIRED -> IN_PROGRESS
any open state -> BLOCKED
```

Separate progress from results. “12/456 complete” is progress, not an accepted
metric. A review records facts, evidence, required corrections, optional
improvements, and a due date. Corrections, additions, suggestions, and scope
changes must be separate categories; a scope change is not disguised as a
small documentation request.

Never overwrite raw evidence or a delivered version. A revision points to its
predecessor and records the feedback that caused the change. Keep one current
route; archive superseded material rather than leaving several competing
“current” files.

## Sentinel Checks

Use cheap sentinels before expensive interpretation:

1. **Identity sentinel**: repository, branch, source revision, input revision,
   and effective configuration match the contract.
2. **Boundary sentinel**: write scope, secret boundary, and artifact location
   are authorized; unrelated processes and directories are read-only.
3. **Smoke sentinel**: the smallest representative operation succeeds and
   emits the expected structured fields.
4. **Completeness sentinel**: planned slots, unique IDs, duplicates, missing
   values, and terminal states are checked before computing a rate.
5. **Mechanism/evidence sentinel**: required fingerprints, verifier receipts,
   hashes, and provenance are present before discussing a score.

Fail closed at the first required sentinel. Classify infrastructure failure,
invalid setup, incomplete execution, verifier failure, and algorithmic failure
separately.

## Handoff And Questions

A handoff starts with the current conclusion and one next action, then lists
the exact evidence, verified and unverified boundaries, recovery command or
entry point, owner, and prohibitions. A useful question includes:

```text
work package and context:
confirmed facts and evidence:
uncertain alternatives:
side-effect-free checks already performed:
decision requested:
whether work is blocked:
```

Asking is a quality gate, not a process failure. When a resource limit,
interface meaning, or deletion boundary is unclear, stop and ask instead of
guessing.

## Information Boundary

Keep credentials, authenticated URLs, raw conversations, private source,
machine-specific paths, and unrestricted logs out of public documentation.
Use variables or project-relative locators in reusable templates. Public
numeric summaries must state their source, denominator, conditions, and
limitations, and must not imply an independently reproduced claim.
