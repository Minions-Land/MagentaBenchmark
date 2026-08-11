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

1. the protocol impact item in the PR template checked;
2. a current approved review (the `protocol-review` workflow checks this); and
3. a CODEOWNER approval enforced by the protected `main` branch.

Do not merge a lab-only review as a substitute for protocol-owner approval.
Shared files such as `pyproject.toml`, `uv.lock`, and `registries/registry.lock.toml`
should be rebased and revalidated after every concurrent merge.

## Required GitHub settings

Repository administrators should protect `main` with:

- required pull request reviews, including CODEOWNERS;
- dismissal of stale approvals after new commits;
- the always-present required status checks `MagentaBench required gate` and
  `Protocol review required gate`; those gates route work internally by path
  and cannot remain pending merely because a path class was not selected;
- no force pushes or branch deletion;
- conversation resolution before merge.

The current CODEOWNERS entries use the repository's active collaborators. Move
them to dedicated GitHub teams when those teams are created, keeping the path
split and review requirements unchanged.

## Secret and evidence rule

Never put credentials in PR bodies, issue records, workflow logs, or lab
checkpoints. Use credential names and digests only. A protocol PR must include
the exact focused test command and state whether any result is exploratory or
claim-eligible; a CI green status never upgrades an experiment result.
