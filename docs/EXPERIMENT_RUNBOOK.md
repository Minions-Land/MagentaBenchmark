# MagentaBench Experiment Runbook

This is the operational entry point after a server interruption. It is written
for the current repository state, not for an assumed clean checkout. The
runbook separates recovery, infrastructure validation, exploratory execution,
and claim publication. A failed gate stops promotion to the next stage.

## 1. Operating Rules

1. Freeze writers before measuring the tree. Do not run cleanup, reset, or
   another broad test process while another agent is editing or testing.
2. Never infer a result from a command that has no retained output. Record the
   command, UTC timestamp, return code, and tree commit together.
3. Use a new record root for every execution. Resume is allowed only when the
   persisted checkpoint and all identity digests match.
4. Keep secrets outside evidence. Store only provider identity and credential
   value SHA-256, never the credential value itself.
5. "exploratory" means diagnostic evidence. It must not be presented as a
   score, comparison, leaderboard value, or model claim.
6. "claim" is an output state, not an intention. It is permitted only after
   every primitive gate and standalone verifier pass from persisted bytes.
7. Use the `lab/` ledger for active ownership, blockers, write scopes, run
   linkage, and recovery checkpoints. Its `done` state is not benchmark proof.

## 2. Recovery After Interruption

The current worktree may contain the only surviving implementation and local
evidence. Before any cleanup, make an external recovery bundle. The destination
must be outside the repository and must be reviewed for secrets before sharing.

~~~bash
RECOVERY=/mnt/aliyunsb/aralacai/magentabench-recovery-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$RECOVERY"
git rev-parse HEAD > "$RECOVERY/head.txt"
git status --porcelain=v2 --branch > "$RECOVERY/status.txt"
git diff --binary > "$RECOVERY/worktree.diff"
git diff --stat > "$RECOVERY/worktree.stat"
git ls-files --others --exclude-standard -z > "$RECOVERY/untracked.lst"
tar --null --files-from="$RECOVERY/untracked.lst" --use-compress-program=gzip \
  -cf "$RECOVERY/untracked.tar.gz"
tar -czf "$RECOVERY/runs.tar.gz" .runs
find "$RECOVERY" -type f -print0 | sort -z | xargs -0 sha256sum > "$RECOVERY/SHA256SUMS"
~~~

Do not run "git clean", "git reset --hard", or "git checkout --" as part of
recovery. The ignored ".runs/" directory contains historical and partial
evidence in the current environment; it must be preserved before any rotation.
After the bundle is inspected, create a recovery branch and stage only reviewed
files. A checkpoint is not durable until it is copied to protected off-host
storage.

## 3. P0 Checkpoint Gate

The checkpoint is usable only when all of these have a recorded return code:

~~~bash
uv run bmp-lab doctor
uv run bmp-lab status --format json
git status --porcelain=v2 --branch
git diff --check
uv sync --frozen --extra test
uv run --extra test pytest -q
PYTHONDONTWRITEBYTECODE=1 uv run python -m compileall -q MagentaBench plugins tests
bash scripts/audit_hcp_boundary.sh
~~~

If `bmp-lab doctor` returns nonzero, stop new shared or expensive experiments
until the ledger is reconciled. Review warnings as well, especially an expired
or otherwise missing live lease on active work, a dirty checkpoint, an artifact
that points only into scratch `.runs/`, or an external locator whose bytes
cannot be verified locally. A finished report available only through an
external locator is an error, not a verified run. `bmp-lab status` is the
collaboration board; it is neither an experiment claim gate nor evidence that a
reported run occurred.
`scripts/preflight_experiment.sh` also matches the experiment path against lab
issues. A matching `open`, `planned`, or `blocked` issue, or active work without
a live lease, stops preflight before execution. Resolve and review the recorded
conditions; do not bypass the gate by deleting or editing ledger records.

Verify generated JSON schemas in a temporary directory and compare them to
"MagentaBench/schemas/json". Verify the 104-entry registry lock from the
repository root:

~~~bash
uv run python -c 'from MagentaBench.schemas.registry_lock import verify_registry_lock; verify_registry_lock("registries")'
~~~

The lock API is currently explicit; "bmp-compile" and "bmp-run" do not invoke
it automatically. Until a release/claim preflight is added, this command is a
required manual gate. Run the retained evidence checks as well:

~~~bash
uv run bmp-verify-probe records/swebench-astropy-6938-probe/probe.json
uv run bmp-verify-probe records/terminal-bench-regex-probe/probe.json
uv run bmp-verify-authority docs/authority/magenta-hcp-authority.json
~~~

Do not report a full-suite count from a different tree, a stale pytest cache,
or a concurrently running process. The count is valid only with the exact
commit, worktree state, command, and return code.

### Dependency source policy

Choose one source policy before committing "uv.lock":

- upstream PyPI URLs for portable publication; or
- the Aliyun mirror for this network, documented and used consistently in CI
  and bootstrap.

This checkout chooses the Aliyun mirror. Bootstrap with
`UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ uv sync --frozen
--extra test`; the lock records the same source URLs, including the pinned
Harbor 0.20.0 test dependency on Python 3.12+. The mirror is an acceleration
mechanism, not benchmark evidence. A lockfile rewritten only because "uv run"
used a local index must not be silently mixed into a recovery checkpoint. Use
frozen installation for every subsequent run.

## 4. Infrastructure Preflight

Harbor is pinned by the backend declaration. Verify both executable identity and
Docker state before attempting a task:

~~~bash
/root/.local/share/uv/tools/harbor/bin/harbor --version
sha256sum /root/.local/share/uv/tools/harbor/bin/harbor
docker info
docker image inspect alexgshaw/regex-log:20251031
docker image inspect alexgshaw/headless-terminal:20251031
~~~

The expected Harbor version is "0.20.0" and the registered executable digest is
"998eb086b23784f317a336d2cf6d306896ea1cb6fc8998806b2ecacba2ebad7c". The two
Terminal-Bench images are currently absent after the server interruption. Do
not substitute a floating tag or an unrelated local image. Pull or rebuild the
exact image references, then record immutable image digests in the recovery
log.

The regex verifier installs "uv" inside the task container and invokes "uvx".
Host "uvx" availability is not sufficient. Prefer an image with the required
tool preinstalled, or pin a reachable package mirror and retain the build log;
otherwise classify the run as "verifier_failure", never as an agent failure.

## 5. Define the First Experiment Tuple

Before compiling, the experiment owner writes a short preregistration containing
all of the following:

| Field | Required decision |
| --- | --- |
| Benchmark | dataset commit, split, explicit case IDs and official verifier |
| Subject | exact adapter, interface, harness version, and state-reset policy |
| Model/provider | model ID, provider binding, credential name and SHA-256 only |
| Protocol | purpose, case order, repetitions, parallelism, retry policy, budgets |
| Metrics | authoritative score, pass/failure semantics, usage and cost metrics |
| Evidence | native result/trace paths, image and executable digests, record root |

No model, provider, or case-set value may be changed after "bmp-compile" without
creating a new experiment ID and record root. A requested model value is not
activation evidence. A positive run requires a matching
"ModelActivationReceipt"; token/cost fields remain unknown when the native
provider did not observe them.

## 6. Staged Execution

### Stage A: protocol conformance

Run fake, deterministic, subprocess, and repeated-sampling fixtures to verify
compiler, scheduler, evidence lineage, and standalone replay. These runs are
tests of BMP and must remain labelled conformance/exploratory.

### Stage B: infrastructure probe

Compile and run the Terminal-Bench no-op or single-case probe with a fresh
record root. Its purpose is to prove dataset staging, Docker, Harbor, verifier
invocation, and failure classification. It cannot establish agent quality.

~~~bash
uv run bmp-compile MagentaBench/conformance/experiments/terminal-bench-regex-smoke.toml
uv run bmp-run MagentaBench/conformance/experiments/terminal-bench-regex-smoke.toml \
  --record-root /path/to/immutable-artifacts/tb-infra-<utc>
~~~

### Stage C: one real-agent case

Do not start a sweep until one real subject has a registered execution
capability, provider binding, native activation evidence, observable usage, and
an official verifier result. The first real run is exploratory and should use a
single pre-registered case. Check the persisted report with
"bmp-verify-report"; retain all infra, agent, verifier, and usage outcomes,
including failures.

### Stage D: repeated sampling

Only after Stage C is reproducible, freeze the case denominator and repetitions
in a protocol. Every planned slot needs a terminal attempt state. Missing,
invalid, timeout, and verifier-failure attempts remain in the denominator and
are not silently filtered. Use the preregistered metric and uncertainty
receipts; do not select repetitions after seeing outcomes.

### Stage E: claim review

The release owner reviews the report, record index, and standalone verifier from
a clean environment. Publish only if identity, execution, isolation/network,
scoring, usage, lineage, and statistical gates are all positive. A report with
an unobserved model activation, missing image digest, incomplete verifier, or
unverifiable reference remains exploratory.

## 7. Artifact Layout

Use an immutable external root, for example:

~~~text
<artifact-root>/
  <experiment-id>/<utc>-<manifest-digest>/
    command.txt
    environment.txt
    plan.json
    records/...
    report.json
    aggregate.json
    verification.txt
  index.json
~~~

"index.json" must bind the experiment ID, manifest digest, commit, command,
UTC timestamps, artifact paths, and SHA-256 values. Keep ".runs/" only as a
recoverable working copy. Never overwrite a completed record root; create a new
one for a rerun.

## 8. Promotion Decision

At the end of each stage, write one of these explicit decisions:

- "pass": all stage exit conditions satisfied;
- "exploratory-only": evidence is useful but one or more claim gates are absent;
- "blocked": an external dependency or missing adapter prevents execution;
- "failed": the experiment executed and produced a classified failure.

The experiment matrix defines the stable inventory and readiness conditions.
Record the live owner, blocker, checkpoint, stage decision, and record/report
link as immutable lab events; do not turn the matrix into a hand-maintained
multi-writer status board. A promotion is valid only when the linked persisted
report, record index, referenced evidence, and standalone verifier support it.
Do not replace a blocked or exploratory entry with a score narrative.

## 9. Idempotency and Recovery Boundary

`bmp-lab` provides replayable collaboration state, idempotently retryable event
requests, and serialization for cooperating processes that share one local
ledger. It does not make Pipeline execution, model/provider calls, external
side effects, or billing exactly-once. Its local lease is not a cross-machine
distributed lock; see `docs/LAB_OPERATIONS.md` for the required canonical Git
workflow.

The current Pipeline also has these recovery limits:

- the record root has no cross-process exclusive lock, and its non-empty check
  has a time-of-check/time-of-use window;
- `events.jsonl` is an unlocked read-modify-write operation, while Pipeline
  `atomic_write_bytes` uses a fixed `<name>.tmp` path;
- no attempt-level write-ahead lifecycle ledger surrounds provider launch;
- checkpoint recovery supports only a limited completed parent-run prefix, and
  multi-case checkpoint identity fails closed;
- checked-in Terminal-Bench, SWE-bench, and first-wave protocols currently use
  `checkpoint_policy=disabled`; and
- a crash after request launch but before durable completion can cause a retry
  to repeat a provider call and its charge.

Use a fresh record root for each new execution and reuse one only for an
explicitly validated resume. Never run concurrent writers against it; retain
all partial artifacts and inspect provider-side request/billing history before
retrying an interrupted real-model attempt.
