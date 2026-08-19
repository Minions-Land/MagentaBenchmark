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
- State: complete | incomplete | invalid | infrastructure-failed
- Evidence class: reproduced | external-declaration | incomplete | invalid | infrastructure-failure
- One-sentence conclusion:

## Frozen protocol
- Benchmark/data/split/task order:
- Agent and simulator/evaluator model:
- Temperature/timeout/retry/concurrency:
- Seeds/repetitions/budget/threshold:
- No-resume/no-rerun policy:

## Results and denominator
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
- Independent review:

## Fidelity and instrumentation
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
- Results:
- Logs:
- Command/provenance:
- Hashes:
- Owner and one next action:
```

没有 denominator、unique-cell 对账、commit、原始路径或 claim class 的 receipt 不得标 `complete/reproduced`。
