# GitHub Collaboration Policy

This file explains the intent behind `CODEOWNERS` and the required repository
settings. The lab ledger coordinates active work; GitHub review protects the
protocol surface. They are complementary controls.

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
2. either a current listed-owner review or an authorized author self-review
   (the `protocol-review` workflow checks this).

Do not merge a lab-only review as a substitute for protocol-owner approval.
Shared files such as `pyproject.toml`, `uv.lock`, and `registries/registry.lock.toml`
should be rebased and revalidated after every concurrent merge.

## Required GitHub settings

Repository administrators should protect `main` with:

- pull requests and the always-present required status checks. This repository
  currently requires zero general approvals so the author may merge after the
  checks pass; an author must not manufacture an independent review;
- dismissal of stale approvals after new commits;
- the always-present required status checks `MagentaBench required gate`,
  `Protocol review required gate`, and `Execution profile required gate`;
  these gates cannot remain pending merely because a path class was not
  selected;
- no force pushes or branch deletion;
- conversation resolution before merge.

The general branch rule does not remove the BMP protocol boundary. A pull
request that changes a protected BMP path must still satisfy the dedicated
`BMP protocol review gate`. That gate accepts a current approval from one of
the listed protocol owners, or an exact-head self-review attestation by the
authorized author `PoorOtterBob`. The latter is deliberately attributable and
is not reported as an independent review. `CODEOWNERS` continues to route
review requests and document path ownership; it is not a claim that every
documentation or lab PR needs another person's approval.

The current CODEOWNERS entries use the repository's active collaborators. Move
them to dedicated GitHub teams when those teams are created, keeping the path
split and protocol review requirements unchanged.

## Secret and evidence rule

Never put credentials in PR bodies, issue records, workflow logs, or lab
checkpoints. Use credential names and digests only. A protocol PR must include
the exact focused test command and state whether any result is exploratory or
claim-eligible; a CI green status never upgrades an experiment result.
