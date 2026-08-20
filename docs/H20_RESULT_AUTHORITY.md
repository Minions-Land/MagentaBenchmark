# H20 Result Authority And Handoff

This page is the H20/NAS handoff for historical benchmark evidence. It is an
inventory and routing contract, not a leaderboard and not a second progress
board. The canonical repository state for this handoff is the isolated branch
`chore/results-inventory-h20-v1` at base `4db369370d997ee0109d357a9c64ac8d4579ade8`.

## Authority Table

The first two rows below point to typed, content-addressed imports already in
this repository. Their records are the machine-readable per-run source for the
ledger; `reports/benchmark_task_matrices.json` is the scan-friendly per-case
projection. Every historical row remains `claim_eligible=false`.

| Benchmark | H20/source identity | Structured records | Current evidence | Binding gaps | Safe next action |
| --- | --- | --- | --- | --- | --- |
| CMTBench | `Minions-Land/MinionsOS2-Bench@150fa100ead4ab51acdfc24ed246a8c5b2141466`, tree `3deaec22a778564ae37cbea396765268f959fee5` | [`imports/minionsos2-cmtbench-150fa10`](../imports/minionsos2-cmtbench-150fa10): 8 run records, `test` split, 50 planned cases per run | Adopted accuracy is retained with correct, unresolved/parser-invalid and denominator fields; the primary regrade CSV is pinned as blob `88fc2f...`, 221376 bytes, SHA-256 `cde0aa20311f255fcc4892d69ec0b58702d16f8e27473481276c2cdad4cdcbad` | No published raw per-case outputs, model/source revision, runtime/image identity, durable BMP record root, or standalone BMP replay; no raw MinionsOS2 checkout is present on H20 | Keep the legacy projection. If promotion is needed, materialize the exact source bytes into a fresh root and create a new snapshot/run with official evaluator replay. |
| BiomniBench DA | `Minions-Land/AOSEBench@def4dae7520807d254612b3590eb32b9aa977924`, tree `50e8fe57a14d8f4c89b8357ab91827fe8bfe60ee` | [`imports/aosebench-biomnibench-da-def4dae7`](../imports/aosebench-biomnibench-da-def4dae7) (source descriptor SHA-256 `7cfd1cf9...`): 14 typed runs (12 `legacy-evaluated`, 2 Magenta `candidate` partial runs) | 50-task DA denominator, judge verdict/score, missing and invalid counts, and negative/no-output states are retained; candidate Magenta medium/xhigh runs retain terminal counts and config digests but intentionally no metrics | Historical records are not BMP runs; model/source commit, runtime/image, durable record root and standalone report replay are not bound. H20's raw judge summary is therefore not a ledger metric | Treat legacy rows as comparison evidence and Magenta rows as candidate-only. Re-run through a preregistered BMP bundle before any claim. |
| NatureBench | `Minions-Land/AOSEBench-NatureBench@4b512029f3ad37746502ce377e4fcc2027fd46db`, `NatureBranch`, tree `e11636f88a5d74e9cb4dcaa06518b9a3a71c87ea` | [`imports/aosebench-naturebench-4b51202`](../imports/aosebench-naturebench-4b51202): 31-case declarations and aggregate reference records | Pinned NatureBranch contains no completed result CSV; declarations expose `not-observed`, null metrics, and `claim_eligible=false`; source descriptor SHA-256 is `8e054ded30897f7ef7a44bf26f8535db24773040c40c7dd0a45386716756377e` | H20 dirty CSV is outside the pinned tree and lacks run ID, source/model commit, config/manifest/evaluator digests, record root and per-case evidence; the materialized namespace records a private/license blocker | Do not import or overwrite the CSV. Freeze it externally as a candidate only after owner review, then materialize and verify a new immutable snapshot. |
| BioML-Bench | `Minions-Land/MinionsOS_Paper` revision hint `48173d1` (not a fixed local snapshot) | Matrix declaration only (`bioml-bench` rows) | `external-unavailable`, no score or denominator claim | No immutable source, evaluator, run, or artifact bytes | Keep declaration-only. Locate an authorized public/fixed source before creating records. |
| BiomeBench | No authoritative H20/NAS source located in this audit | No typed records | No result is asserted | Benchmark identity, dataset/split, evaluator, and source revision are all unresolved | Open a separate source-discovery issue; do not infer that “BiomeBench” means BiomniBench DA or BioML-Bench. |
| SWE-bench Verified | No Verified source or run located | [`reports/benchmark_task_matrices.json`](../reports/benchmark_task_matrices.json) contains one local SWE-bench **Lite (not Verified)** probe row from repository snapshot `a913967a05fba7277f64a72694f5f868f36a3c4` | The Astropy probe is exploratory and repository-local; it is not a SWE-bench Verified result | No Verified dataset revision, official Verified evaluator, model/source binding, or claim report | Keep the row explicitly Lite/exploratory. A Verified run needs its own frozen bundle, official verifier and fresh record root. |

The CMTBench, BiomniBench DA, and NatureBench source snapshots are private or
license-undetected upstream material represented here only by the approved
typed-results projection (`Minions-Land/MagentaBenchmark#85`, approved by
`PoorOtterBob`). The task-level publication view is governed separately by
`Minions-Land/MagentaBenchmark#135`; neither approval makes the imported rows
BMP claims or authorizes copying raw source bytes.

## Required Row Contract

Do not flatten these inventories into prose or a single score. A result unit is
one `benchmark/dataset/split/case-or-question/run` tuple. The typed record or
future BMP report must retain, at minimum:

| Field | Required meaning |
| --- | --- |
| benchmark and dataset | Stable benchmark ID, dataset revision/content digest, and split |
| case/question | Case or question ID; planned denominator includes missing, timeout, invalid and verifier-failure slots |
| run | Immutable run/experiment ID and a fresh durable `record_root` |
| subject | Method/model ID, source/code commit, interface and configuration digest |
| evaluator | Official verifier/evaluator identity and digest, plus its observed outcome |
| result | Metric value and unit, success/failure/invalid/no-output state, numerator and denominator, and uncertainty when applicable |
| provenance | Manifest/config/dataset/evaluator digests, immutable artifact locator, exact size and SHA-256 |
| publication state | `standalone_verification`, evidence tier, comparability limits, and derived `claim_eligible` (never supplied by an import summary) |

The CMTBench and BiomniBench DA imports already implement this shape for
aggregate historical runs and preserve unresolved or missing outcomes. The
task matrix adds one row per case/task but intentionally omits private answers,
prompts, traces, logs, credentials and machine-private paths. It is therefore a
projection, not a substitute for a report graph.

## H20 Candidate Quarantine

The following files were observed read-only and are **not** authoritative
results:

- `<H20-NAS>/BioAgent/NatureBench/opus4.7_medium.csv`, 1,763 bytes,
  SHA-256 `b6fc9334895295f39faf8859fefca9301cdb9872592e9e3dc5642859f9567259`.
  It has 31 case rows but no run/provenance bindings and is an untracked file
  in dirty checkout `19fe7d9204838832801d120ab0bc446fa9d48c26`.
- `<H20-NAS>/BioAgent/NatureBench/NatureBench_arXiv_2606.24530.pdf`,
  7,473,331 bytes, SHA-256
  `9617d41ed6e089dd9c8509977e5b60ba4acad0146b0cd69e19f2a85b7ec5ecc2`.
  It is also untracked and is paper context, not a bound evaluator/report
  artifact.
- `<H20-NAS>/BioAgent/BiomniBench-DA/BiomniBench-DA_result/`, including
  the Magenta analysis and task summaries. These are useful source leads, but
  the directory is not a Git snapshot and its summaries are not standalone
  BMP reports. Preserve it; do not copy private raw artifacts into this repo.
  The tracked candidate records are `biomnibench-da-magenta-medium` and
  `biomnibench-da-magenta-xhigh` (both `terminal_state=partial`,
  `claim_eligible=false`); an external judge summary exists for both 50-task
  cohorts, but its values are deliberately not emitted as imported metrics
  because source/model/runtime/record-root binding is open.

Generated Nature regeneration directories under
`<H20-NAS>/aralacai/naturebench-regeneration-*/` and
`<H20-NAS>/aralacai/naturebench-import-worktree-*/` are intermediate copies,
not a new source of truth. They must not replace the tracked import without an
exact source and record-ID comparison.

## Operational Chain

For a new or re-materialized result, use this order:

```text
freeze source/dataset/split/cases/model/evaluator/config
  -> create experiment bundle and BMP manifest
  -> check_run_root --require-new (durable NAS root)
  -> bmp-run (one run ID per fresh root)
  -> bmp-verify-report (exact persisted bytes)
  -> bmp-lab link-run (coordination only)
  -> bmp-collab ledger (derive observations and claim gates)
```

The ledger is the only source that may derive claim eligibility. Historical
imports remain non-claim even when a source summary reports a successful score.
Supervisor logs, shell exit codes, and narrative analysis are operational
evidence, never benchmark metrics.

Use NAS-local scratch for command output so the root filesystem is not used:

```bash
export TMPDIR=<H20-NAS>/aralacai/magentabench-inventory-evidence-<UTC>/tmp
export UV_CACHE_DIR=<H20-NAS>/aralacai/magentabench-inventory-evidence-<UTC>/uv-cache
uv run --frozen bmp-collab validate-imports
uv run --frozen bmp-collab ledger --table observations
```

If a source, evaluator, model revision, or artifact byte changes, create a new
snapshot directory and run identity. Never reinterpret an old record under a
new implementation. No Supervisor or benchmark runtime was started for this
handoff; those gates remain explicitly unrun until an authorized, leased,
digest-bound execution is scheduled.
