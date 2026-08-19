# Sentinel Contract

## Hard sentinel table

| ID | Sentinel | Evidence | Failure class |
| --- | --- | --- | --- |
| S0 | Boundary/privacy | authorized root、secret scan、write log | `invalid_boundary` |
| S1 | Identity | source/data/model/dependency/config hash | `invalid_identity` |
| S2 | Resource | owner manifest、capacity、PID/slot、disk floor | `infrastructure_failure` |
| S3 | Schema/interface | parser、contract/unit、expected fields | `invalid_setup` |
| S4 | Smoke | representative terminal result | `smoke_failure` |
| S5 | Parity/mechanism | effective config、task order、retry、fingerprint/events | `configuration_drift` / `method_not_active` |
| S6 | Completeness | expected vs actual unique cells、duplicates、missing/error | `incomplete` |
| S7 | Provenance | command、versions、input/result hashes、retry history | `unreproducible` |
| S8 | Independent review | reviewer record、claim boundary、negative result | `verifier_failure` |

S0--S5 未通过时不进入 full run；S6--S8 未通过时不计算 headline metric 或做因果声明。

## Online/stateful methods

冻结 task order、trial semantics、state reset/shared policy、checkpoint/state hash 和 idempotency key。每个下一状态的 `state_before_hash` 必须匹配上一提交的 `state_after_hash`；未知网络结果先查 durable commit，不盲目 replay。

