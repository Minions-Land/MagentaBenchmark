# 05 · 当前状态：已建立、未建立、被阻塞

> **当前树修订（2026-08-09）**：本文只保留当前可复核事实，不再内嵌会漂移的
> 历史 HEAD、测试数或早期“从未接入 TB2.1”等交接快照。本修订固化了下述实现；
> 能力边界仍以本页复核命令和实际产物为准，不以工作树是否干净作证明。

## 当前事实

- Terminal-Bench 2.1 已有内容寻址 loader、solution/gold 排除和 Harbor 0.20.0
  原生 backend；一个 `regex-log` case 已真实进入 Docker 和官方 verifier。
- SWE-bench `astropy__astropy-6938` 保留了一次手工观测的 exploratory probe 和
  candidate patch；早期摘要里提到的 Codex/model 与 focused-test 计数只存在于历史
  叙述，当前 probe schema 不接受未绑定的这类元数据，retained refs 不能独立重放这些事实。
  该运行也未经过 BMP Pipeline 的
  model/provider activation，因此不能成为 claim。
- Terminal-Bench probe 的 verifier 下载 `uv` 失败，随后 `uvx` 不存在；该结果已
  标准化为 `IntegrationProbeRecord(outcome=verifier_failure)`，不能计作 Agent
  失败或 Terminal-Bench 分数。
- `custom` case order 已从 `explicit_case_ids` 分离，使用项目内 SHA-256/size
  绑定的 JSON order artifact，并在编译、loader、scheduler、case-set、gate 和
  standalone verifier 重新校验。
- `ExperimentContrast` 同时支持旧 subject-ID arm 和通用
  `factor_path/control_value/treatment_value` arm；统计计划/收据支持配对单位、
  样本方差、正态 CI、holdout 与 Bonferroni，并由 `bmp-verify-report` 独立重算。
- HCP authority receipt 已绑定 Magenta commit
  `78e2998f5bb78aa029c5cfe6f9508777f307679d`、治理文档、sidecar contract bytes
  和 BMP boundary audit；`bmp-verify-authority` 可独立通过。BMP 仍不拥有 HCP。

## 仍未达到 claim-ready

- 通用 `ModelActivationReceipt`、`ProviderBinding`、execution capability 声明、
  Pipeline 补全以及 claim/standalone gate 已接通；真实模型缺少 native observation
  会明确记录为 `unobserved`。尚未有真实 benchmark 产出 matched receipt 加可观测
  token/cost 的完整记录，所以仍没有正向 model claim。
- evolver/meta-evolver 已有 deterministic local adapter 贯通 Pipeline 与 standalone
  verifier，但这只是 conformance runtime；通用系统的 search evaluator 与 sealed
  holdout evaluator 仍需各自的生产 adapter 提供。
- 真实 Terminal-Bench verifier 依赖必须固定或在容器镜像中预装，之后才可能产生
  第一份完整 exploratory `ObservationReport`。当前没有 claim-ready benchmark
  report，也没有可发布 leaderboard 数字。
- 统计计划已实现，但正式 claim 仍要求真实 activation、隔离、评分、重复和
  holdout 证据全部通过；不要把单元测试或 exploratory probe 当成统计结论。

## 已建立且会 fail closed 的主链路

- 配置 registry 支持 TOML CRUD、profile 继承、envelope/raw 外部文件、inline/CLI
  override、深合并、JSON Schema 校验、ownership 与 composition replay；最终值、来源字节
  和 adapter import closure 均进入 manifest identity。
- benchmark loader、backend factory、execution adapter 均可由 digest-bound
  `AdapterCapability` TOML 注册；未声明的 backend read-set、subject adapter、model
  activation 或 state-reset policy 不会被猜测。
- case-set、schedule、attempt、budget、network、checkpoint、report 与 record index 均有
  内容寻址收据；Pipeline 和 standalone verifier 分别重放，任何缺失或漂移都会使相关门
  失败。
- case 顺序支持 fixed、seeded random、random、explicit 和内容寻址 custom strategy；
  custom artifact 与 observed order 在 loader、scheduler、gate 和 verifier 全部交叉绑定。
- 对比支持 subject-ID arms 与任意声明的 `factor_path` arms；统计计划与收据从原始 verifier
  score 重算，不信任报告内自报 effect。
- deterministic evolution adapter 已执行 generate/feedback/revise/select/terminate 全生命周期，
  分离 search/holdout authority，并把 candidate、transition、evaluation 和预算写入可递归验证
  的 evidence/receipt；它不是对任意演化框架的完成声明。
- `IntegrationProbeRecord` 已统一 SWE-bench 与 Terminal-Bench 探针记录；
  `bmp-verify-probe` 只验证记录中实际绑定的 retained bytes 与可重算 identity，拒绝
  未绑定的摘要、usage、network 声明，不把历史叙述升级成执行证明。两份当前记录都明确是
  exploratory。

## 当前阻塞与下一步

1. 为 Terminal-Bench verifier 固定 `uv` 依赖或把它预装进镜像，重跑少量 case，产出第一份
   完整 exploratory `ObservationReport`；不得把依赖下载失败记成 Agent 分数。
2. 把真实 provider/model activation、usage 可观测性和 credential-free provenance 接到生产
   runtime；只有 schema 或 manifest 声明不构成 activation 证据。
3. 为真实 evolver/meta-evolver 接入对应 execution adapter，沿用已验证的 search/holdout
   authority 分离；所有候选、transition、查询和预算必须保留 lineage。
4. 补齐仍缺失的 claim scope 证据；`ResolutionBandReceipt`、sidecar 或单个 probe 都不能单独
   让 `claim_eligible` 为真。
5. Magenta checkout 只允许本地 commit，绝不 push；MagentaBench 也只做精确 staging，避免
   把真实探针的临时/敏感产物误纳入版本库。

## 复核命令

```bash
uv run --extra test pytest -q
schema_tmp=$(mktemp -d)
uv run python -c 'import sys; from MagentaBench.schemas import write_json_schemas; write_json_schemas(sys.argv[1])' "$schema_tmp"
diff -qr MagentaBench/schemas/json "$schema_tmp"
rm -r "$schema_tmp"
uv run python -m compileall -q MagentaBench plugins tests
bash scripts/audit_hcp_boundary.sh
uv run bmp-verify-probe records/swebench-astropy-6938-probe/probe.json
uv run bmp-verify-probe records/terminal-bench-regex-probe/probe.json
uv run bmp-verify-authority docs/authority/magenta-hcp-authority.json
git diff --check
```
