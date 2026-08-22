# H20 Result Authority

This page is the H20/NAS evidence inventory for historical benchmark assets.
It records source identity, recoverability, and claim boundaries; it is not a
leaderboard, a progress board, or a second execution procedure. Repository
workflow is defined once in [`OPERATING_GUIDE.md`](OPERATING_GUIDE.md).

The baseline publication decision is GitHub Issue #135, with its task-level
view in PR #136. Issue #159 is the later H20 catalog import decision recorded
below; it extends the typed inventory without changing the baseline claim
boundary. Every row in either view is explicitly `claim_eligible=false`; only a standalone
BMP report followed by `bmp-verify-report`, `bmp-lab link-run`, and the ledger
can derive a claim.

## H20 catalog snapshot (Issue #159)

The authoritative H20/NAS input is the validated catalog resolved by the
operator as `$BMP_H20_RESULTS_ROOT`. The variable must name the read-only
catalog root outside the public checkout; its host-specific value is not part
of the repository contract. The catalog is external evidence, not a Git
checkout. The accepted inventory is 197 files and 4,355,698 bytes with catalog
digest
`6eaacb5c6b9437dfd00a7fdae1b64da888e4ff512ec8712739568a4cfa153b90`.
Only the 145 structured fact files and four schemas are consumed; generated
views, README files, `.audit`, and views are not evidence inputs.

The sanitized, typed snapshot is retained in Git history at commit
`4dd8c0bd7786899434d1d01c625df6a9f5205ba1`, tree
`0bcf574c47f50a1cb27296b6202ec601449f9610`, blob
`89dd5d9e1250a289c3e5547bac911e7a3cc7198e`, SHA-256
`39cf16dbd2337768c6c0c5e6f02b8aa32ec677782375b59db302eed3a580bfa0`,
703,813 bytes. The working tree intentionally does not retain the snapshot
bytes; `git cat-file blob 89dd5d9e...` recovers the exact input. The final
`source.json` and every imported record point to that content-addressed Git
object, never to an absolute NAS path.

The import adds 30 candidate owner records and 2,360 unit results (2,390
records):

| Benchmark | Candidate owner envelopes | Unit rows | Unit statuses |
| --- | ---: | ---: | --- |
| BiomniBench-DA | 10 | 1,500 | 1,119 success; 381 verified-fail |
| CMTBench | 8 | 800 | 214 success; 574 verified-fail; 12 invalid-output |
| SWE-bench Verified | 12 | 60 | 47 success; 11 verified-fail; 2 no-output |
| NatureBench | 0 (existing records reused) | 0 | aggregate/declaration only |

Each of the 30 owners is a `candidate` identity envelope with `metrics=[]`. It
binds the source run and complete experiment conditions referenced by its unit
records, but it emits no ledger observation and carries no aggregate result.
The final legacy ledger therefore has 2,779 observations: 419 existing
observations plus 2,360 new unit rows. The paper task table has the same 2,360
new unit rows.

All 2,390 imported records have `claim_eligible=false`. The 2,360 unit results
are `legacy-evaluated` with `verification_status=unverified`; the 30 candidate
owners do not contain metrics or verification claims. CMT/Biomni unit rows are
derived non-claim views; SWE unit rows retain the historical official-harness
class and code commit
`174590db9b51b61ace9270dbf1f24d4364c6c640`. Unit denominators are always one;
negative, invalid, and no-output outcomes remain explicit. No raw answers,
prompts, gold data, traces, logs, host paths, commands, credentials, or report
contents are copied.

## Authority Table

| Benchmark | Fixed source and records | Observed state | Binding gap | Safe next action |
| --- | --- | --- | --- | --- |
| CMTBench | H20 catalog task matrix: `Minions-Land/MagentaBenchmark@3d799a7a4cb274d863a8d00f60048fb2cbc10985`, tree `62597410d42b95eab3cef47f98187c9a89f9f880`; source report SHA-256 `ca29ec764092b99e16226f35e05328e461d0eaea4df6879d7c675edd0570565f`, 179065 bytes | 8 owning runs, 800 unit rows; adopted verdicts and invalid outcomes are preserved | No BMP standalone report or live runtime binding | Keep as typed historical projection; materialize a fresh BMP run before promotion |
| BiomniBench DA | H20 catalog task matrix: `Minions-Land/MagentaBenchmark@3d799a7a4cb274d863a8d00f60048fb2cbc10985`, tree `62597410d42b95eab3cef47f98187c9a89f9f880`; source report SHA-256 `ca29ec764092b99e16226f35e05328e461d0eaea4df6879d7c675edd0570565f`, 179065 bytes | 10 owning runs, 1500 unit rows; scores, failures, and explicit non-claim outcomes remain visible | Historical judge summaries are not BMP reports and do not bind a live record root | Compare only as legacy evidence; re-run through a preregistered bundle for claims |
| NatureBench | Existing typed import `Minions-Land/AOSEBench-NatureBench@4b512029f3ad37746502ce377e4fcc2027fd46db`; no new H20 task rows | Existing 8 runs and 12 aggregate observations are reused | No new task-level H20 result source | Keep existing declarations/aggregates; do not duplicate them |
| BioML-Bench | `Minions-Land/MinionsOS_Paper` revision hint `48173d1`; no immutable source snapshot or publication decision | 8 declaration rows, `external-unavailable`, no numeric result | Dataset, evaluator, and source bytes are unresolved | Keep declaration-only; open a separate source-discovery issue |
| BiomeBench | No authoritative H20/NAS source, dataset revision, or evaluator located | No rows or result asserted | Identity is unresolved and must not be inferred from BiomniBench or BioML-Bench | Open a separate discovery issue; do not create a metric row |
| SWE-bench Verified | H20 catalog official-harness subset: 5 fixed instances from population 500; dataset digest `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd`; evaluator digest `748fe0b199e5212e413089631b6b7dbd06973a8a813be16ca399eb373c0d3835` | 12 owning runs, 60 unit rows; denominator is five per run, with 47 success, 11 verified-fail, and 2 no-output | Historical reports are not current BMP claims; no fresh Supervisor binding | Keep as non-claim historical evidence; a new claim needs a frozen bundle, official verifier, and fresh root |

The source descriptor digest for the approved NatureBench typed projection is
`8e054ded30897f7ef7a44bf26f8535db24773040c40c7dd0a45386716756377e`. The
BiomniBench DA descriptor digest is
`7cfd1cf9a4eb872f9dd97f99115550a14ba80c53fbf3cf58d90b37d1381d1a44`.
These descriptors authorize a typed, non-claim projection only; they do not
authorize copying raw source bytes into this repository.

## Row Contract

The machine-readable matrix is `reports/benchmark_task_matrices.json`. Its
deterministic projector is
`scripts/historical_imports/benchmark_task_matrices_v1.py`; the fail-closed
validator is
`scripts/historical_imports/validate_benchmark_task_matrices.py`.

Each row is one `benchmark/dataset/split/case-or-question` unit and carries a
stable task ID, category, method cells, and explicit false claim eligibility.
When a real run exists, the corresponding typed record must additionally bind
run ID, model/code commit, denominator, official evaluator, BMP/manifest/
dataset/config/evaluator digests, fresh durable `record_root`, report digest
and size, terminal outcome, and evidence tier. Missing, invalid, timeout, and
no-output cases remain in the denominator.

The projector keeps historical import `normalizer_*` fields in their original
role and records its own implementation identity separately. It verifies the
fixed source bytes (size, SHA-256, and Git blob SHA-1) before projecting CMT,
BiomniBench DA, and NatureBench declarations. BioML-Bench and unresolved
BiomeBench remain non-numeric. The SWE-bench Lite row intentionally contains
no narrative timing/token metric; `summary.md` prose is not a result field.

## Quarantine Locators

Private H20 paths are never committed. A digest-only locator means that an
authorized operator may recover the bytes from the NAS inventory; it does not
make them authoritative:

| Candidate | Safe locator | Classification |
| --- | --- | --- |
| NatureBench dirty `opus4.7_medium.csv` | `external://h20-nas/sha256/b6fc9334895295f39faf8859fefca9301cdb9872592e9e3dc5642859f9567259` | 31 rows, no run identity; quarantine-only |
| NatureBench paper PDF | `external://h20-nas/sha256/9617d41ed6e089dd9c8509977e5b60ba4acad0146b0cd69e19f2a85b7ec5ecc2` | context only, not an evaluator/report |
| BiomniBench DA candidate directory | `external://h20-nas/unbound/biomnibench-da-candidate` | no single immutable tree digest; preserve, do not import |
| SWE-bench Verified input parquet | `external://h20-nas/sha256/bb5b123d29ce70107cc0951cf444894241c570a11d76aec452332c65b01e06d8` | input-only, no result |
| SWE-bench third-party parity summary | `external://h20-nas/sha256/68339c5b8f43b7f71f7ec89693344b27ce4857b83800c14ae3b60b76e1fa0d17` | aggregate lead without per-case report |
| Deleted/dirty CMT forensic checkout | `external://h20-nas/unbound/minionsos2-bench-forensic-candidate` | incomplete checkout; not source authority |

Generated regeneration/import worktrees are intermediate copies. They must
not replace a pinned source or a typed record without exact source, tree, and
record-ID comparison.

## Boundary

Use the single procedure in [`OPERATING_GUIDE.md`](OPERATING_GUIDE.md) for
bundle creation, lease/status transitions, fresh record roots, verification,
handoff, and ledger linkage. Supervisor logs, shell exit codes, receipts, and
narrative summaries are operational evidence only; they never become metrics.
Changing code, evaluator, dataset, or configuration creates a new version and
new run identity. Old records remain bound to their original snapshots.
