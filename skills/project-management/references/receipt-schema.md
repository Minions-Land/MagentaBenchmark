# Receipt Schema

每个工作包交付两层证据：run bundle 和短 receipt。项目 contract 必须先给 `PATH_SCHEMA`、`RESULT_SCHEMA`、`UNIQUE_KEY` 和 `RECEIPT_NAME` 赋值；本文件不固定项目名、方法名、domain、trial 或目录前缀。

## Run bundle

```text
<RUN_ROOT>/
├── logs/<LOG_NAME>
├── provenance/
│   ├── run-contract.json
│   ├── command.sh
│   ├── environment.json
│   └── parity/
├── results/<RESULT_PATH_FROM_SCHEMA>
├── STATUS.md
└── SHA256SUMS.txt
```

目录名必须由 contract 预先登记。不同 logical cell 是否共用结果文件由 `RESULT_SCHEMA` 明确；raw artifact 用 exclusive create。

## Short receipt

文件名：`deliverables/<RECEIPT_NAME>.md`，旁边必须有 `<RECEIPT_NAME>.sha256`。外部 baseline 项目可以把 `RECEIPT_NAME` 设为 `RESULTS_<METHOD>`，其他项目使用自己的稳定命名。

```markdown
# <RECEIPT_NAME>

## Conclusion
- State: complete | incomplete | not-run | invalid | infrastructure-failed
- Evidence class: reproduced | external-declaration | incomplete | invalid | infrastructure-failure
- Claim eligible: true | false
- One-sentence conclusion:

## Frozen protocol
- Benchmark/data/split/task order:
- Agent and simulator/evaluator model:
- Temperature/timeout/retry/concurrency:
- Seeds/repetitions/budget/threshold:
- Resume/retry/no-rerun policy:

## Results and denominator
- Expected cells:
- Unique cells:
| Domain/method | Passed | Expected | Rate |
| --- | ---: | ---: | ---: |

## Sentinel checks
- Boundary/privacy:
- Identity:
- Resource:
- Schema/interface:
- Smoke:
- Parity/mechanism:
- Completeness (expected/actual/unique/missing/duplicate/error):
- Provenance:
- Accountable review (or advisory finding plus pending final review):

## Fidelity and instrumentation
- Source commit:
- Artifact SHA256:
- Official repository/commit:
- Parameters and source:
- Adaptation changes and reasons:
- Method-specific events/fingerprint:
- Embedding/model/temperature choices:

## Cost, deviations and limits
- Task inference cost:
- Memory/build/maintenance cost:
- Retries/restarts:
- Unrun checks and limitations:

## Evidence and next action
- Owner:
- Final reviewer:
- Review state: approved | pending | changes-requested
- Final review HEAD:
- Results:
- Logs:
- Command/provenance:
- Hashes:
- Owner and one next action:
```

`complete/reproduced` 必须填写非 N/A 的 expected/unique、40-hex source
commit、artifact SHA-256、owner 和仓库 accountable final review；
`external-declaration` 必须写 `Claim eligible: false`。所有 receipt 都必须
有内容匹配的 `<RECEIPT_NAME>.sha256` sidecar。没有这些证据不得标成
`complete/reproduced`。

本仓库的 reference validator 只做结构和摘要检查，不自行授予 GitHub
approval。校验 `complete/reproduced` 时，调用者还必须把受信任的当前 head
作为 `--expected-review-head <40-HEX>` 传入；脚本核对 receipt 中的 exact
head，而 GitHub required gate 继续负责认证 `PoorOtterBob` 的最终审核。
