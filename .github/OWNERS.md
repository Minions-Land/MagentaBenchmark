# GitHub Collaboration Policy

This file explains the intent behind `CODEOWNERS` and the required repository
settings. The lab ledger coordinates active work; GitHub review protects the
repository surface. They are complementary controls.

## Single Review Owner

`PoorOtterBob` is the single accountable GitHub reviewer for this repository.
Other collaborators can implement work, run experiments, report findings, and
leave advisory comments, but their approval is never a required merge
condition. This deliberately keeps the current lab's decision path short
while retaining immutable issue, lease, checkpoint, commit, CI, and evidence
records.

## Merge classes

### Lab/control-plane changes

Changes limited to `lab/`, `MagentaBench/lab/`, the lab operation tests, and
the collaboration workflows may merge in parallel when they touch disjoint
issue/event paths. Every such pull request must run `bmp-lab doctor`; a clean
ledger and a non-overlapping lease are required before merge.

### BMP protocol changes

Changes under `MagentaBench/adapters/`, `MagentaBench/schemas/`,
`MagentaBench/runner/`, `plugins/`, `registries/`, `pyproject.toml`, or
`uv.lock` are protocol changes even when a PR is described as a refactor. They
require:

1. the protocol impact item in the PR template checked; and
2. either a current `PoorOtterBob` approval or an authorized `PoorOtterBob`
   exact-head self-review (the `protocol-review` workflow checks this).

Do not merge a lab-only review as a substitute for `PoorOtterBob` final
approval or self-review.
Shared files such as `pyproject.toml`, `uv.lock`, and `registries/registry.lock.toml`
should be rebased and revalidated after every concurrent merge.

## Required GitHub settings

Repository administrators should protect `main` with:

- pull requests and the always-present required status checks. The general
  approval count remains zero: a `PoorOtterBob`-authored PR cannot approve
  itself through GitHub's review API, so the review workflow provides the
  attributable self-review gate instead;
- dismissal of stale approvals after new commits;
- the always-present required status checks `MagentaBench required gate`,
  `PoorOtterBob review required gate`, `Protocol review required gate`, and
  `Execution profile required gate`;
  these gates cannot remain pending merely because a path class was not
  selected;
- no force pushes or branch deletion;
- conversation resolution before merge.

The general branch rule does not remove the BMP protocol boundary. A pull
request that changes a protected BMP path must still satisfy the dedicated
`BMP protocol review gate`. For a PR authored by someone else, that means a
current approval from `PoorOtterBob`; for a PR authored by `PoorOtterBob`, it
means the exact-head self-review attestation in the PR body. The latter is
deliberately attributable and is not reported as an independent review.

`CODEOWNERS` intentionally routes every path to `PoorOtterBob`, including
documentation, lab records, and workflows. Review requests to other
collaborators are optional notifications only.

## Secret and evidence rule

Never put credentials in PR bodies, issue records, workflow logs, or lab
checkpoints. Use credential names and digests only. A protocol PR must include
the exact focused test command and state whether any result is exploratory or
claim-eligible; a CI green status never upgrades an experiment result.
