# Experiment Matrix

This matrix is the current planning source for MagentaBench. "Runnable" means
the checked-in protocol can execute with its declared subject and backend; it
does not mean that the result is a real-model score. "Exploratory" means the
artifact may be retained and independently verified but cannot support a claim.
"Blocked" means the missing dependency or capability must be resolved first.

## Current Inventory

| Experiment or artifact | Purpose | Current state | What it proves | Allowed output |
| --- | --- | --- | --- | --- |
| fake-sweep.toml | BMP conformance | Runnable | factor expansion, scheduling, fake evidence and report replay | Conformance test only |
| fake-taxonomy.toml | BMP conformance | Runnable | failure taxonomy and gate behavior | Conformance test only |
| subprocess-echo-smoke.toml | BMP conformance | Runnable | subprocess boundary and evidence retention | Conformance test only |
| repeated-sampling-smoke.toml | Sampling contract | Runnable | fixed denominator, attempts and uncertainty receipts on a fake subject | Protocol test only |
| deterministic-evolution-smoke.toml | Evolution conformance | Runnable | deterministic candidate lineage and search/holdout ordering | Conformance test only |
| deterministic-meta-evolution-smoke.toml | Meta-evolution conformance | Runnable | nested deterministic lineage and budget receipts | Conformance test only |
| terminal-bench-nop-smoke.toml | TB infrastructure probe | Compiles; runtime depends on Docker | loader, task staging, Harbor invocation and failure classification with no agent | Exploratory infrastructure evidence |
| terminal-bench-regex-smoke.toml | TB single-case probe | Compiles; currently blocked in this host | real task content and official verifier path once images/dependencies exist | Exploratory only; no agent score |
| records/terminal-bench-regex-probe | Historical TB probe | Independently verified | one retained Harbor attempt and a verifier dependency failure | Exploratory failure evidence |
| records/swebench-astropy-6938-probe | Historical SWE-bench probe | Independently verified | bound input/candidate bytes and exploratory taxonomy | Exploratory only; no model claim |
| aose-zero-cost-run-a/b.toml | Negative fixture | Intentionally inactive | compile-time rejection for missing production capabilities | Negative test |
| harbor-shim-smoke.toml | Negative fixture | Intentionally inactive | no accidental shim fallback | Negative test |
| registries/regimes/* templates | Regime contracts | Contractual only | schema/identity rules for stage, cell and holdout artifacts | Contract tests only |
| External metric adapter | Metric contract | Registered/tested directly | adapter identity and source contract | Not a Pipeline result yet |
| Real Codex/Claude/Magenta subject | First real experiment | Blocked | Magenta subject and execution capability are registered; provider binding, activation/usage evidence, and Docker/verifier gates remain | No run |
| Any purpose=claim experiment | Publication | None exists | No claim-ready result currently exists | Forbidden until all gates pass |

## Readiness Conditions

A real Terminal-Bench experiment is ready only when all of the following rows are
green in the same compiled manifest:

| Gate | Required evidence |
| --- | --- |
| Dataset | Terminal-Bench 2.1 checkout at commit 5c8eadf1f393183288fa08b8f73ca9a469cc5e00, split and case order bound |
| Images | Exact task image tags resolve to immutable Docker digests; no floating replacement |
| Harbor | Harbor 0.20.0 executable and registered SHA-256 match |
| Verifier | Task-container uv/uvx path succeeds, or the image contains a pinned equivalent |
| Subject | Digest-bound subject declaration and matching Terminal-Bench interface |
| Execution | Capability declares the exact benchmark/backend/subject tuple and real-model activation source |
| Provider | Secret-free ProviderBinding and credential SHA-256; no secret values in TOML or records |
| Activation | Native result or adapter receipt proves activated provider/model matches the request |
| Usage | Provider/harness token and cost usage is observable; unknown is not coerced to zero |
| Isolation | State reset, workspace namespace, network observation and image provenance are retained |
| Scoring | Official verifier result maps to reward.authoritative.v1 with success threshold 1.0 |
| Lineage | Every planned attempt, case, output, log and report index is content-addressed |
| Verification | bmp-verify-report and the standalone report verifier pass from persisted bytes |

A missing row makes the run exploratory or blocked. It cannot be repaired by
editing the final report.

## Planned First Wave

1. **Checkpoint:** preserve the interrupted worktree and decide the dependency
   source policy for uv.lock.
2. **Infrastructure:** restore the exact Terminal-Bench images and make the
   verifier's in-container uvx dependency deterministic.
3. **Activation:** register one real subject and one execution capability. Start
   with a single provider/model so native activation and usage can be audited.
4. **Pilot:** run one pre-registered Terminal-Bench case as exploratory evidence.
   Re-run only with a new record root; retain all failures.
5. **Sampling:** after a successful pilot is reproducible, preregister the case
   denominator and repetition count, then run repeated sampling with the
   authoritative reward metric and uncertainty receipts.
6. **Claim review:** only a purpose=claim manifest with positive identity,
   activation, isolation, scoring, usage, lineage, statistics, and standalone
   gates may be published.

The pilot subject, provider, model, repetitions, budget, and case ID are still
deliberate decisions for the experiment owner. The matrix must be updated with
those exact values before compilation; do not infer them from historical prose.

## Deferred Research Regimes

Generalization, cross-domain transfer, continual learning, curriculum,
online adaptation, robustness, evolution, and meta-evolution templates are
kept as contracts while the Pipeline lacks complete stage-DAG orchestration and
stage activation receipts. They are not first-wave experiments. Implement and
verify the stage runtime only after the single-case real benchmark path,
repeated sampling, and evidence publication are stable.

## Evidence Labels

Use exactly one label in the run index:

- conformance: protocol implementation fixture; never a benchmark claim;
- exploratory: real or historical execution with one or more claim gates absent;
- blocked: no valid execution because a dependency/capability is missing;
- claim: all gates and independent verification are positive.

The current matrix contains no valid claim row.
