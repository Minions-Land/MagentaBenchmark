# 01 · MagentaBench 是什么

## 一句话

MagentaBench 是 **BMP（Benchmark 侧协议）** 的实现：一套让真实 agent 在真实 benchmark 上运行、并且对"这个数字到底测了什么"负责到底的测量框架。BMP 属于 Benchmark；Magenta 智能体自己的组件协议是 HCP（Harness Component Protocol）。

## 它要解决的问题

跑 benchmark 本身不难。难的是让结果**可归因**。考虑一个常见场景：

> 换用新模型后，某 agent 在 Terminal-Bench 上从 42% 提升到 51%。

这句话在没有额外约束时几乎没有信息量。9 个百分点可能来自：模型能力、harness 的 prompt 改动、工具集变化、超时放宽、重试次数、评测机的负载、乃至任务顺序。更糟的情况是，如果 agent 的工作目录里意外混进了 benchmark 的 `solution/` 目录，那么 51% 测的是"能不能读文件"，而报告会显示一切正常。

MagentaBench 的立场是：**一个测量结果必须携带足以确定它测了什么的证据，否则系统拒绝产出结论。**

这导出了整个项目的形态。绝大部分代码不是"执行 benchmark"，而是回答：

- 这次运行的身份由什么构成？改动其中任何一项，身份是否必然改变？
- 声明（declaration）与观测（observation）是否被区分？
- 缺少某类证据时，系统是关门还是猜测？
- 报告里的每个哈希，是否都有一个真实读取并校验它的地方？

## 名词

| 术语 | 含义 |
| --- | --- |
| **subject** | 被测对象。一个 agent（Codex CLI、Claude Code、Magenta），或用于自检的 fake |
| **benchmark** | 任务集合 + 验证器（Terminal-Bench 2.1、BiomniBench-DA） |
| **backend** | 执行基质。`fake` / `subprocess` / `aose-docker` / `harbor` |
| **protocol** | 一次实验的方法学：重复次数、候选选择、状态重置、并行度、检查点策略 |
| **manifest** | 上述四者解析后的不可变编译产物，内容寻址 |
| **ClaimScope** | 这次运行**允许声称什么**。十个值，见下 |
| **RunPurpose** | `exploratory`（探索）或 `claim`（主张）。两者的门标准不同 |
| **gate** | 对证据的判定。`execution_valid` / `protocol_valid` / `isolation_valid` / `scoring_valid` / `statistics_valid` / `claim_eligible` |

## ClaimScope：归因的类型化

`MagentaBench/schemas/models.py:1054`

```
component        单个 HCP 组件的贡献
whole_harness    整个 harness 作为一体
model            仅模型变化，harness 固定
checkpoint       同一模型不同检查点
evolver          自动优化器（harness 优化）
meta_evolver     优化优化器的东西
schedule         调度策略
ablation         消融
hyperparameter   超参
conformance      系统自检（不是关于任何真实 agent 的主张）
```

关键设计：**scope 在编译期校验，不在报告期。** 如果某个 scope 所需的证据类在运行时不存在，编译直接失败并指名缺什么：

```
claim scope 'whole_harness' requires missing evidence class
<...>; runtime support is not active
```

当前 `_ACTIVE_SCOPES = frozenset({ClaimScope.conformance})`（`runner/compiler.py:374`），且进一步收窄到已被真实 Pipeline 跑通的那一个 adapter 元组。

**为什么只剩 conformance**：见 [`05-current-state.md`](05-current-state.md)。简短版本 —— 唯一存在的真实运行产物是一次 AOSE 零成本 dry-run，其 subject entrypoint 是 `/usr/bin/true`，证据包里 `status=no_output`、`verifier_evidence=null`、`output_refs=[]`。没有任何完整产物能支撑 `whole_harness` 处于激活状态。这是基于产物证据的判定，不是推测。

## 目标状态

按优先级：

1. **单个真实元组走通全链路。** Terminal-Bench 2.1 + Harbor + CLI agent，产出一份能通过全部 exploratory 门的 `ObservationReport`。这是当前最大的空缺 —— 迄今为止没有任何真实证据包走过这条链。
2. **gold 隔离可验证。** TB2.1 的 task 目录里 `solution/` 与 `tests/` 和公开题面并列。必须做到 workspace **按构造只含公开内容**，而不是挂载后过滤；并以运行后重新哈希证明没有 verifier-only 内容出现过。
3. **归因对比。** 双臂实验：只变模型（`model` scope）与整体替换（`whole_harness` scope），两者的门标准不同且都必须诚实。
4. **`claim` purpose 可达。** 现在结构性不可达 —— `gates.py` 的统计分支在非确定性 conformance 下必然关门，因为真实实验统计尚未实现。这是正确的关门行为，且位于其他所有 claim 工作的上游。
5. **evolver / meta_evolver。** `EvolutionRunEvidence`、候选/transition
   ledger、内容寻址 refs 和递归 parent verifier 已实现；真正的 claim 仍要求
   external execution capability、完整 digest binding 与 claim-ready provenance。

## 明确不做的事

- **不做 benchmark 自身的执行引擎。** 复用 Harbor、benchmark 自带的 verifier 与容器定义。
- **不为不存在过的数据形状写兼容层。**
- **不做任何掩盖契约不匹配的 fallback。** 无法识别的数据必须响亮失败。
- **不在部分实现的能力上留半成品脚手架。** 要么完整，要么在激活处显式拒绝并说明原因。当前 case-set registry 已支持普通多 case lineage；checkpoint 多 case 因旧 ledger 键空间不足，仍在 activation 处明确拒绝。
