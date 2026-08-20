# H20 Result Authority

This page is the H20/NAS evidence inventory for historical benchmark assets.
It records source identity, recoverability, and claim boundaries; it is not a
leaderboard, a progress board, or a second execution procedure. Repository
workflow is defined once in [`OPERATING_GUIDE.md`](OPERATING_GUIDE.md).

The publication decision is GitHub Issue #135. Its task-level view is PR #136.
Every row in that view is explicitly `claim_eligible=false`; only a standalone
BMP report followed by `bmp-verify-report`, `bmp-lab link-run`, and the ledger
can derive a claim.

## Authority Table

| Benchmark | Fixed source and records | Observed state | Binding gap | Safe next action |
| --- | --- | --- | --- | --- |
| CMTBench | `Minions-Land/MinionsOS2-Bench@150fa100ead4ab51acdfc24ed246a8c5b2141466`, tree `3deaec22a778564ae37cbea396765268f959fee5`; `experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/per_answer_regrade.csv`, blob `88fc2f305f97ef1fcaab247602eb20c948c17945`, 221376 bytes, SHA-256 `cde0aa20311f255fcc4892d69ec0b58702d16f8e27473481276c2cdad4cdcbad` | 50 task rows, 8 methods; adopted verdicts and unresolved outcomes are preserved | No BMP run, model/source commit, runtime identity, durable record root, or standalone report | Keep as a typed historical projection; materialize a fresh BMP run before promotion |
| BiomniBench DA | `Minions-Land/AOSEBench@def4dae7520807d254612b3590eb32b9aa977924`, tree `50e8fe57a14d8f4c89b8357ab91827fe8bfe60ee`; default summary blob `0e5d4e04c830b60d1a54b5f4f0171e1c96382c5f`, 35972 bytes, SHA-256 `af5b16706bf758b79c090ecab73df46d3a23cc1defe6917839a5871b7dd54f5d`; xhigh blob `68e7f39dd8692c904a3a8cc775b5bef2f50b0648`, 37959 bytes, SHA-256 `1d587f28659a5fc026f8d0b6c14e6ef361991484d24d22cf59725afeee117373` | 50 task rows, 10 method/regime columns; scores, no-output, and failures remain visible | Historical judge summaries are not BMP reports and do not bind model/runtime/record root | Compare only as legacy evidence; re-run through a preregistered bundle for claims |
| NatureBench | `Minions-Land/AOSEBench-NatureBench@4b512029f3ad37746502ce377e4fcc2027fd46db`, tree `e11636f88a5d74e9cb4dcaa06518b9a3a71c87ea`; `manifests/tasks.tsv`, blob `f5821c63e3575fefb501c6e02d3c5cfeddf91cfb`, 5829 bytes, SHA-256 `24d667df9d14a433a460a6115e6c4c24e8d6b7186cc2c528be648c199f7dfc09`; `task-set/cellomics.txt`, blob `18c02dafc5759479c2d91904402b6ce3099e1cd9`, 589 bytes, SHA-256 `10020a26ab2c5d510a916a72db668123396555ec5352661d79f856370f084ac2` | 31 declaration rows; no completed result file exists at the pinned snapshot | H20 dirty CSV has no run/provenance binding and remains quarantine-only | Do not import the CSV; freeze and verify a new source/run if an owner authorizes it |
| BioML-Bench | `Minions-Land/MinionsOS_Paper` revision hint `48173d1`; no immutable source snapshot or publication decision | 8 declaration rows, `external-unavailable`, no numeric result | Dataset, evaluator, and source bytes are unresolved | Keep declaration-only; open a separate source-discovery issue |
| BiomeBench | No authoritative H20/NAS source, dataset revision, or evaluator located | No rows or result asserted | Identity is unresolved and must not be inferred from BiomniBench or BioML-Bench | Open a separate discovery issue; do not create a metric row |
| SWE-bench Verified | H20 input lead: HF revision `03e151cf5560b1af6a4363c6a9d766deaaea6b56`, `test`, 500 cases; parquet SHA-256 `bb5b123d29ce70107cc0951cf444894241c570a11d76aec452332c65b01e06d8`, 2480309 bytes | Input-only snapshot; no predictions or official report. The checked-in row is a one-case SWE-bench Lite exploratory probe, not Verified | No Verified model/source binding, evaluator output, run ID, record root, or per-case report | Keep both leads non-claim; a Verified run needs a frozen bundle, official verifier, and fresh root |

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
