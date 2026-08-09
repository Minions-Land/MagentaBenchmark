# 02 · 上游参考：仓库、工具、论文

铁律第一条是"不要重新发明已存在的工具"。本文档记录每一项外部依赖**被采纳了什么**、以及**哪些被明确拒绝采纳**。

## 一、Benchmark 仓库

### Terminal-Bench 2.1

- 本地路径：`/mnt/aliyunsb/aralacai/terminal-bench-2-1`
- 上游：https://github.com/laude-institute/terminal-bench
- 89 个任务

**采纳**：任务定义、容器镜像、verifier 全部原样使用，不做修改。

**已观测（非推断）的 reward 形状** —— 用真实执行观测得到：

```
verifier_result.rewards.reward = 0.0
```

单键，且**即使在失败的 no-op 上 `rewards` 依然非空**。这一点很重要：不能把"`rewards` 有内容"当作"运行成功"。

leaderboard 的解析位置：`leaderboard/src/leaderboard/ci/static_analysis.py:142-145`，读 `verifier_result.rewards.reward`。现有 `registries/benchmarks/terminal-bench-2-1.toml` 已把 `authoritative_reward_metric = "reward"` 绑定为本地协议事实；其依据是**上面那次实际观测**，而不是把 leaderboard 源码当成 verifier 证据 —— 源码是别人对格式的读法，观测才是格式本身。

**已缓存的镜像（digest 钉住，已验证）**：

```
alexgshaw/headless-terminal:20251031
  sha256:eb7e209672bf6cef2785fafd9e13509b10626c327bcc2b37f5bf40ca83eaf3aa  148MB
alexgshaw/regex-log:20251031
  sha256:90101b2e815323a8da20528a1439bebc407eb9761c9c68a3d557730856c878e9  78.1MB
```

两者在 `--network none` 下均可启动，容器内有 `/usr/bin/python3`。

**关键危险 —— 必须让接手者知道**：TB2.1 的 task 目录**把 `solution/` 与 `tests/` 和公开题面放在一起**。任何"把 task 目录平铺挂进 workspace"的做法都会把答案挂进去，然后在运行后把它重新哈希成一份合法证据。这比其他任何缺陷都糟：它产出的证据会**令人信服地证明错误的性质**，并通过每一道门。

而且 TB2.1 **没有机器可读的 public/gold 清单**。所以分类只能是**逐 case 的显式白名单**，未分类内容一律拒绝 —— 不能用黑名单，因为黑名单会把"我们的推断"编码成"完备知识"。

当前 checkout 的 89 个任务均使用 `task.toml` + `instruction.md`，没有旧版
`task.yaml`。loader 把这两类文件按逐 case 白名单归为公开 task contract，把
`tests/` 单独归为 verifier-only refs，并彻底排除 `solution/`、README 与 VCS 元数据。
分类必须逐 case 且各自带 digest，**永远不是 benchmark 级别**；未来出现任何未知
路径都必须 fail closed，不能因当前 89 个 case 的布局而自动公开。

### BiomniBench-DA / AOSEBench

- 任务：`/mnt/aliyunsb/BioAgent/BiomniBench-DA`，26 个任务，case id 形如 `da-1-N`
- registry：`registries/benchmarks/aosebench-biomnibench-da.toml`
- AOSEBench 源 commit：`46d437f2f7a8ef505b8fac95ade12c2d6458a623`
- 镜像：`biomnibench-da-magenta-0.0.22`
- `scoring_kind = "continuous"`，`authoritative_reward_metric = "overall"`

**采纳**：input/output 契约（`/app/instruction.md`、`/app/data:ro` → `/app/trace.md`、`/app/answer.txt`）与 rubric-judge 评测器。

**为何没有被选作第一个真实元组**：它的评测器是 judge 模型，会连带引入 provider、prompt、rubric、成本等一整套需要单独收据的东西。TB2.1 的确定性 verifier 避开了这些。

### CMT-Bench

已在目标中，尚未接入。

## 二、执行工具

### Harbor 0.20.0

- registry：`registries/backends/harbor-020.toml`（`id = "harbor.0.20.0"`）
- 可执行文件：`/root/.local/share/uv/tools/harbor/bin/harbor`
- 已钉住的 SHA-256：`998eb086b23784f317a336d2cf6d306896ea1cb6fc8998806b2ecacba2ebad7c`

**采纳**：原生 `JobResult` / `TrialResult` 解析、`TimingInfo` 阶段分类。**没有**重写它的执行逻辑。

**两个已知约束，都已在编译期加门**：

1. **`observed_case_order` 仅在 `parallelism=1` 时有效。** Harbor 记录的是**完成顺序**，不是提交顺序。编译期守卫读的是**已解析的 protocol**，不是调用方传入的值。
2. **测试用的解析放宽必须显式标记。** `allow_test_parse` / `allow_test_override` 会写进一个类型化的 `TestOverrideReceipt`，并使 `evaluate_run_report` 拒绝被标记的血统。测试 fixture 不能变成 claim 证据。

### AOSE Docker

`registries/backends/aose-docker-immutable.toml`。已完成 10/10 零成本 dry-run 观测。

**从它身上学到的教训（已成为设计承诺）**：这个 backend 早期用 `network_mode='none'` 作为"网络隔离已达成"的证据。这是**声明冒充观测** —— 配置项与观测结果是两件事。现在的规则是：**永远不要为无法观测出口流量的 adapter 合成探针，而应让 isolation 门失败。**

### subprocess

`registries/backends/subprocess-echo.toml`。含密钥擦除、隔离 workspace、可执行文件 SHA-256 记录。

## 三、论文参考

### HarnessOpt-Bench（Scale AI）

- arXiv:2608.06301，全文 https://arxiv.org/html/2608.06301v1
- 作者：Ursekar, Shanker, Maurya, Yasser, Kalmath, Chatrath, Yuan Xue

这是唯一被详细研读的外部方法学参考。它评测的是"模型能否优化一个 agent harness"，与我们的 `evolver` scope 直接对应。

**采纳一：resolution band（分辨带）。**

他们在约 5–6 runs/cell 的样本量下**不报告置信区间、不做显著性检验**。做法是：对同一候选、同一批 case 重复打分，取**中位差异**，作为该任务的分辨带。原文：

> a descriptive threshold: differences smaller than the band are treated as unresolved, not as formal significance-test results

这直接解开了我们一直误认为被"完整推断统计设计"阻塞的统计门。而它可信的原因在于**他们让分辨带约束自己的结论**，三次：模型-vs-harness 的 1.8× 差距被注明只是"勉强"超过分辨带；§5.4 中 20 对里 11 对超过分辨带但方向不一致，因此把聚合近似平局报告为异质效应而非一致小效应；§5.3 用"好四到五个分辨带"作为幅度单位。

**boundary-guardian 的裁定（覆盖了我最初的提议）**：分辨带必须是**独立的类型化证据契约 `ResolutionBandReceipt`**，被统计门消费但绝不与之混同；它**本身不能**让 `claim_eligible` 或因果 `statistics_valid` 为真。理由：分辨带衡量"仪器能分辨多细"，推断统计估计"差异是否真实"，把前者塞进后者的门就是让一个性质冒充相邻性质 —— 正是本项目整天在清除的替代缺陷类。我提出了那个替代，裁定推翻了它。

最小字段集：不可变的候选/subject/benchmark/case-set 身份；权威 metric；K≥2 的重复协议；逐次 bundle ref 与重算得到的具名分数；精确的差异构造与中位绝对差异；归一化尺度定义；runner/verifier/pipeline digest；实测预算扣减。**所有值从已验证字节推导，绝不接受调用方输入。** 缺 K、case/metric 身份不匹配、ref 不完整、分数为空，一律关门。

**未采纳其一处欠规范 —— 不要继承。** 他们用 K=2 测差异，held-out 打分用 K=3，文中只说把中位差异"carry to the K=3 scale"，**没有给出这个换算的算术**。两次单次打分的中位绝对差异与两个三次均值之差不在同一尺度上（后者方差更小）。裁定：换算必须显式且可推导；若无法给出有原则的换算，收据就记录**实测的 K** 并拒绝声称跨 K 可比。一个无法验证的换算藏在完整性收据里，比一个claim更窄的分辨带更糟。

**采纳二：靠"沙箱里根本没有"实现隔离。** 他们的 split 是 20/40/40 互斥、由 committed manifest 钉住、且"split generator 逐字节校验 committed tree"。test 分区在搜索期间**完全不可访问**，由受信服务器在候选提名后评测。原文：

> Because held-out data, provider credentials, and budget enforcement are absent from the optimizer's sandbox, these restrictions are properties of the execution environment rather than instructions the optimizer is expected to follow.

这与 boundary-guardian 拒绝 `network_mode='none'` 的理由**独立地撞到同一个原则**：环境属性 vs 期望被遵守的指令。

**但他们的保证只在 setup 时是结构性的**，我没有找到任何运行后校验证明 optimizer 的 workspace 从未**包含过** held-out 字节。逐字节校验发生在 split 生成时，无法区分"从未存在"与"存在过后被删除"。我们的运行后重新哈希覆盖了这个 case。写 EVIDENCE.md 时应记录这个差别 —— 不是宣称我们更优，而是两种机制能区分的东西确实不同。

**采纳三：过程量一律从产物重算。** 原文：every quantity we report about an optimizer's process is recomputed from its own execution trace and from the evaluator's record of which evaluations it ran。这与我们"`expected_run_count` 从 run-ID 集合推导"同一原则。

**采纳四：预算是成本向量而非标量。** `c_j ∈ R^d`，`Σc_j ≤ B` 按分量约束：每分区 100 次评测调用、每分区四次完整 case pass、外加总 target-token 上限。他们的发现是 **"case passes, not evaluation calls, bind"** —— 真正卡住的是一个不显眼的分量。标量预算会把这个事实藏起来。这对我们的 `BudgetLedger` 直接相关。

其嵌套隔离：optimizer 与 target 的推理经由同一网关计量，但 **optimizer 自身的推理被计量而不设上限**，是刻意如此，以估计"当 optimizer 的推理不是稀缺资源时可达到什么"。两层执行，一个强制点，分别归因。

**值得引用的一处克制**：§5.3 标题为 "Visible validation scores are optimistic"，而他们写道该图 "cannot distinguish selection-induced overfitting from a validation–test mismatch, so we claim only that the visible best score is optimistic"。证据同时符合两种机制、无法分离，于是只声称证据支持的部分。这与我们对 "DNS 解析失败 ≠ 传输层拒绝" 的处理是同一种纪律，来自独立团队。日后若有人认为我们过于严格，这是外部先例。

**`EvolutionRunEvidence` 所需字段（从其协议导出）**：钉住的 seed digest 与不可变路径集；互斥 split manifest 及其逐字节校验的生成过程；作为数据的分区披露策略；带分量上限与实测扣减的成本向量；作为内容寻址 commit 链的完整候选序列；绑定到其中某一 commit 的提名；含 K 与逐 case 次数的服务端 held-out 评测收据；以及作为一等字段的分辨带，使任何被报告的增益自带其不可分辨阈值。

## 四、HCP（Harness Component Protocol）

`docs/governance/bmp-boundary-law.md`（246 行）。BMP 与 HCP 的边界法，含 HCP 不变量、禁止构造、接口契约、六个门的正确性规则、失败保留规则、以及可运行审计脚本规范。

**当前阻塞**：Magenta 已导出中性 HCP assembly sidecar；但 HCP 尚未提供
`canonicalAssemblyDigest` 和 `dependencyFileClosure`，因此需要完整 assembly
身份的 `component` 与 `ablation` scope 仍被阻塞（`compiler.py:452` 检查
`subject.sidecar_ref`，并由 claim gate 对缺失证据 fail closed）。
