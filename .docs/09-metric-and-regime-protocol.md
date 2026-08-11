# 09 · Metric 与 Experiment Regime 严格协议

> 状态（2026-08-10）：本文是 MagentaBench 的指标与研究设定规范，不是
> “已经实现一切”的能力声明。本文标为“现行”的部分已有类型化 Schema、
> TOML registry 与 replay 路径；标为“扩展契约”的指标，在其 Schema、
> runner receipt 和 standalone verifier 同时落地前，不得用于 claim。

本文使用“必须”、“不得”、“应”表示规范性要求。实现与本文冲突时，
必须先修正实现或以新版本协议显式修订本文；不得用未记录的 fallback
“兼容”冲突。

## 1. 测量对象：只有四种 comparison kind

MagentaBench 只允许以下四种语义比较主体：

| `ComparisonKind` | 比较的是什么 | 典型变量 |
| --- | --- | --- |
| `agent` | 一个具体 Agent 的行为配置 | model、prompt、tool policy、memory、reasoning tier |
| `coding_agent` | 完整 Coding Agent 系统 | harness、workspace policy、debugger、compaction、runtime |
| `evolution_method` | 给定起始状态后如何生成、评估、选择与推广候选 | evolver、selector、archive、mutation、stopping |
| `meta_evolution_method` | 如何修改或选择 evolution method 本身 | meta-policy、editable surface、outer objective、recursion depth |

`SubjectKind` 是实际进入执行路径的打包形态，如 `opaque_agent`、
`hcp_harness`、`evolver` 或 `meta_evolver`；它不是第五种比较主体。
dataset、benchmark、evaluator、model、sandbox、budget、schedule 和 regime 也都是
正交的注册因子，不得偷换成 comparison kind。

每个对比必须显式给出 `comparison_kind`、只允许变动的 factor 路径，
以及所有必须相等的已解析 digest。如果两个 arm 在未允许的 model、
dataset membership、evaluator、tool schema、retry、timeout、budget、sandbox 或
source closure 上不同，该比较无效，不得事后解释为主体效应。

## 2. TOML 是唯一协议权威

### 2.1 所有可自定义项都必须进入身份

以下内容不得仅存在于 Python 默认值、CLI 进程状态或运行人员的记忆中：

- Agent/Coding Agent：model/provider、reasoning effort、temperature、top-p、
  context/generation/turn 上限、prompt、tools、skills、memory、cache、compaction、
  retry/backoff、debugger 与 concurrency。
- 数据与环境：dataset split/membership/order、benchmark/evaluator 版本、
  image、OS、architecture、dependency lock、network policy、workspace/setup 与 secret-free
  provider binding。
- 调度与资源：task/rollout/candidate 数、seed、parallelism、timeout、
  token/cost/wall-clock/CPU/memory/I/O/network 预算、resume/cache 策略。
- 演化与 Meta-evolution：parent selector、mutation/evolver、candidate validity gate、
  archive/promotion/stopping、diagnosis、editable/protected paths、inner/outer budget 和
  sealed-holdout visibility。
- 指标：公式、单位、方向、层级、母体、分组、缺失/状态策略、
  sampling assumption、uncertainty、threshold/bin/clip/censoring 与 adapter config。

密钥本身不得进 TOML 证据。只保存 secret 存在、名称和值的摘要，
不保存值。

### 2.2 内容寻址与无漂移规则

每个 registry 对象必须经历同一条链：

```text
TOML declaration bytes
  -> strict parse (unknown field / duplicate ID fail)
  -> typed artifact + dependency closure
  -> declaration/artifact/source-closure digests
  -> registry lock
  -> resolved manifest digest
  -> activation receipt
  -> result / trajectory / metric receipt
  -> standalone reparse + rehash + recompute
```

规则如下：

1. 相同名称不是相同身份。比较与回放使用 digest，不使用显示名。
2. 任何语义字段改变都必须改变 artifact digest；对已发布语义的修订应使用
   新 metric/protocol/regime ID，不得原地重定义。
3. compiler 必须把 benchmark、dataset、evaluator、metric、protocol、regime、
   subject、backend、configuration 和 adapter closure 全部折入 manifest。
4. verifier 必须重新读 TOML 字节、重新解析并检查依赖闭包；只比对
   一个已记录 digest 不够。
5. registry writer 全部停止后才能生成 lock；生成后必须立即用独立
   full-tree rescan 验证。writer exit 0 不证明 lock 覆盖了最终树。
6. cache/resume 必须同时命中 manifest、schedule、configuration、dataset、
   evaluator、metric 和 environment digest；“文件存在”不是可复用证据。

Configuration 继续使用内容寻址 profile/file/inline overlay：

```toml
[experiment.configuration]
profiles = ["agent.base.v1", "agent.reasoning-high.v1"]
files = ["configs/run-local.toml"]
raw_files = ["configs/provider-native.toml"]

[experiment.configuration.values.agent]
max_model_turns = 300
temperature = 0.7

[experiment.configuration.values.debugger]
concurrent_tasks = 16
per_task_timeout_seconds = 600
retry_attempts = 3
```

合并顺序、source mode、source bytes、JSON Schema、resolved tree 与最终 digest 都必须
进入 manifest；运行时还必须用 activation receipt 证明已请求的值真正生效。

## 3. Metric identity 与完整分母

### 3.1 MetricSpec 的身份边界

一个 metric 的身份至少包含：

```text
id, adapter, value_kind, level, direction, unit,
source, source_field, formula, parameters, scale,
population, group_by, across_groups,
sampling design/subset/exchangeability keys,
missing_observation, complete status_policy,
uncertainty method/unit/cluster/seed/RNG/resamples,
input metric IDs, external config and adapter closure digest
```

一个 metric result 必须绑定 metric digest、manifest digest、schedule receipt、
sample-ledger digest、planned population、贡献 slot/cell ID、numerator、denominator、
status/disposition counts 与原始 evidence refs。只保存一个浮点值不构成 metric 证据。

macro 与 micro、task 与 rollout 加权、staged 与 full、oracle 与 selected、
complete-case 与 planned-population、equal-width 与 equal-mass，都是不同 metric
identity，不是报表显示选项。

### 3.2 两层计划母体

MagentaBench 禁止从“已存在的结果文件”发现分母。计划在执行前完成：

1. **Rollout ledger**：`task x rollout/attempt`。每个已计划 slot 都必须有
   terminal disposition；每个已启动 slot 都必须同时有 evidence bundle 和 trajectory。
2. **Experiment cell ledger**：`stage x checkpoint x task/domain/scenario/variant x
   generation x repetition x metric`。该 ledger 在任何 longitudinal/group/evolution
   聚合前封闭，并绑定 dataset membership authority。

已知终态但 evaluator 观测丢失，可以根据注册 policy 零填。整个计划
slot/cell 不存在、身份重复、顺序不符、或 membership 无权威绑定，是
schedule corruption，必须使指标 `invalid`，不得通过补 0 掩盖。

### 3.3 样本 disposition 与运行状态

| disposition | 含义 | 是否进入数值分母 |
| --- | --- | --- |
| `observed` | 权威 source 有可重算值 | 是 |
| `zero_filled` | 已注册失败策略要求记 0 | 是，值必须等于 0 |
| `excluded` | 该 estimand 明确排除此状态 | 否，但必须报告数量 |
| `missing` | 预期观测不存在 | 由 policy 决定；不得静默跳过 |
| `invalid` | 证据或协议无法支撑该值 | 否；通常使整个必需聚合 invalid |

`RunStatus` 是封闭枚举：`pass`、`verified_fail`、`scored`、`no_output`、
`invalid_output`、`timeout`、`agent_error`、`harness_fault`、`verifier_error`、
`infra_error`、`unsupported`。每个基于 rollout 的成功指标必须对全部状态
显式给出 policy。

AHE/Terminal-Bench 口径的成功指标把 timeout、sandbox/API 异常等已计划
失败记 0，而不是丢弃。token/cost 等诊断均值可排除因基础设施中止而
被截断的试验，但必须同时报告 planned、observed、zero-filled、excluded、
missing、invalid 数，并在 metric ID 中固定该分母策略。

分母为 0、必需 cell 缺失、或样本数不满足公式先决条件时，返回
`unavailable/invalid` 及类型化 reason；不得返回 0、1、`-1`、NaN，也不得用
`nanmean` 绕过。

## 4. 重复采样：Pass@k、Pass^k 与 prefix 不得混名

对 task \(i\)，令 \(n_i\) 为已计划且按成功指标 policy 获得二元值的
rollout 数，\(c_i\) 为成功数。对所有组合公式都要求 \(n_i \ge k\)，
并要求 rollout 在注册的 `exchangeability_keys` 下可交换。

### 4.1 Pass@1

AHE 式严格 Pass@1 是全部已计划 rollout 的二元 reward 均值。若每个 task
计划 \(q\) 个 rollout：

\[
\operatorname{pass@1}
=\frac{1}{q|D|}\sum_{i=1}^{|D|}\sum_{j=1}^{q}r_{i,j}.
\]

infrastructure terminal 按 `pass-at-1.infra-zero.v1` 记 0，不从分母删除。

### 4.2 Unbiased Pass@k

Pass@k 估计“从 \(n_i\) 个可交换 rollout 中均匀不放回地选 \(k\) 个，
至少一个成功”的概率：

\[
\operatorname{Pass@k}
=\frac1{|D|}\sum_i\left(1-\frac{\binom{n_i-c_i}{k}}{\binom{n_i}{k}}\right).
\]

实现必须使用数值稳定的乘积/对数形式，不得先生成巨大组合数再
转浮点。当 \(n_i<k\)、任一必需 slot 没有值、或 exchangeability 声明无证据
时，该 task group 及整个 task-macro metric 必须 `invalid`；不得动态降低 \(k\)。

### 4.3 Pass^k（reliability）

Pass^k 衡量“均匀选出的 \(k\) 个 rollout 全部成功”：

\[
\operatorname{Pass^k}
=\frac1{|D|}\sum_i\frac{\binom{c_i}{k}}{\binom{n_i}{k}}.
\]

它是可靠性指标，不是 Pass@k 的别名。两者必须使用不同 formula 和
metric ID。

### 4.4 Ordered-prefix any/all

如果协议关心实际的前 \(k\) 次服务，必须声明
`design = "ordered_prefix"` 和 `subset_policy = "first_k"`：

\[
\operatorname{PrefixAny@k}_i=\mathbf1[\max_{j\le k}r_{i,j}=1],
\qquad
\operatorname{PrefixAll@k}_i=\mathbf1[\min_{j\le k}r_{i,j}=1].
\]

这是有序 cohort 的经验值，不是 unbiased Pass@k/Pass^k estimator。
重试、热 cache、跨 rollout memory 或时变环境会让顺序具有语义；此时不得
宣称 exchangeable。

连续 reward 的 `ExpectedMax@k` 也只对可交换 rollout 有定义：对所有
\(k\)-subset 的 subset maximum 取均值，再做 task macro。它是 oracle subset
诊断，不是一个可部署 selector 的得分。

### 4.5 现行 TOML 示例

```toml
[metric]
id = "pass-at-2.unbiased.v1"
kind = "metric"
adapter = "magentabench.measurement"
bmp_version = "0.1"
value_kind = "rate"
level = "task"
direction = "maximize"
unit = "fraction"
source = "evaluator"
source_field = "authoritative_reward"
formula = "pass_at_k_unbiased_v1"
population = "planned_tasks"
group_by = ["task"]
across_groups = "macro_mean"
missing_observation = "zero"

[metric.parameters]
k = 2

[metric.sampling]
design = "exchangeable_rollouts"
subset_policy = "uniform_without_replacement"
exchangeability_keys = ["configuration", "task", "sampler"]

[metric.uncertainty]
method = "bootstrap_percentile_v1"
confidence_level = 0.95
resampling_unit = "task"
cluster_by = ["task"]
resamples = 2000
seed = 20260810
rng_algorithm = "sha256_counter_v1"

[metric.status_policy]
pass = "observe"
verified_fail = "observe"
scored = "observe"
no_output = "zero"
invalid_output = "zero"
timeout = "zero"
agent_error = "zero"
harness_fault = "zero"
verifier_error = "zero"
infra_error = "zero"
unsupported = "zero"
```

## 5. Uncertainty 与对比设计

不确定性是 metric identity 的一部分，不得在报表阶段临时选择。

- 现行 `pass-at-1.infra-zero.v1` 使用 `wilson_score_v1`，并保存 confidence
  level、unit count、lower/upper 和 standard error（如适用）。
- 任务级 Pass@k 使用 task-cluster percentile bootstrap，以 task 为 resampling
  unit，不得把同一 task 的 rollouts 冒充独立 task。
- bootstrap 必须注册 resamples、seed 和 RNG algorithm。现行算法是
  `sha256_counter_v1`，result 必须保存 replicate-distribution digest 与
  degenerate 标记。不得依赖进程全局 Python RNG。
- arm 对比应在相同 task/seed/environment 上保存 paired delta；丢失任一
  pair 时，paired estimand invalid。不能改用 unpaired 检验而不改 metric ID。
- 多候选搜索必须注册 family、selection rule 和 multiple-comparison policy。
  用于选 winner 的数据不得同时作为 confirmatory holdout。

置信区间不修复身份、分母或因果设计错误。样本很多但不可比的两个 arm
仍然不可比。

## 6. Experiment regime 与完整 cell ledger

### 6.1 Regime 与 stage DAG

Regime 是比较主体的正交实验设定。现行 `ExperimentRegimeKind` 支持：

| regime kind | 最小协议语义 |
| --- | --- |
| `iid_evaluation` | 冻结配置在一个预注册母体上评估 |
| `repeated_sampling` | 每 task 多 rollout，显式采样设计与 \(k\) |
| `generalization` | ID/validation 与 sealed OOD holdout 的成员关系固定 |
| `cross_domain_transfer` | source-domain adapt 后在冻结 target-domain 评估 |
| `continual_learning` | 顺序学习，保存每 checkpoint × frozen distribution 矩阵 |
| `curriculum` | 预注册难度/阶段顺序和暴露策略 |
| `online_adaptation` | 每步先评分、再更新，不得反向泄露 label |
| `robustness_stress` | clean/perturbed/attack 的 paired schedule |
| `evolutionary_search` | immutable candidate/archive/selection lineage 与 sealed final evaluation |
| `meta_evolution` | 共同起始 archive/seed/schedule/budget 下的 evolution-method 对比 |

每个 stage 必须注册 role、predecessors、benchmark/dataset/evaluator/protocol/metrics、
domains、state policy、feedback visibility、sealed 状态、budget 和 evaluation
cadence。stage 必须按 DAG 拓扑顺序声明。

State policy 只允许 `reset`、`carry`、`fork`、`read_only`。非 reset 必须
绑定 predecessor 和 input state receipt；`read_only` 不得产生 output state。sealed
holdout 必须是 `role = "holdout"`、`state_policy = "read_only"`、
`feedback_visibility = "none"`，且只能在 selection 已关闭的类型化 release receipt
之后解封。

一个有效的多阶段 TOML 形状如下（其中引用的 ID 也必须先在 registry
中存在）：

```toml
[regime]
id = "generalization.repo-holdout.v1"
kind = "regime"
adapter = "magentabench.stage-dag"
bmp_version = "0.1"
regime_kind = "generalization"

[[regime.stages]]
id = "validation"
role = "evaluate"
benchmark_id = "benchmark.agent.v1"
dataset_id = "dataset.repo-split.v1"
evaluator_id = "evaluator.agent.v1"
protocol_id = "protocol.repeated.v1"
metric_ids = ["pass-at-1.infra-zero.v1"]
domains = ["id"]
state_policy = "reset"
feedback_visibility = "aggregate_only"
sealed = false
evaluation_cadence = 1

[[regime.stages]]
id = "ood-holdout"
role = "holdout"
predecessors = ["validation"]
benchmark_id = "benchmark.agent.v1"
dataset_id = "dataset.repo-split.v1"
evaluator_id = "evaluator.agent.v1"
protocol_id = "protocol.repeated.v1"
metric_ids = ["pass-at-1.infra-zero.v1"]
domains = ["ood"]
state_policy = "read_only"
feedback_visibility = "none"
sealed = true
evaluation_cadence = 1
```

### 6.2 Cell plan、ledger 与 matrix

`ExperimentCellPlan` 必须在运行前固定：

- regime ID/digest 与各 stage manifest digest；
- dataset membership authority refs；
- 每个 cell 的 stage/checkpoint/task/domain/scenario/variant/generation/repetition 坐标；
- metric ID/digest、membership digest 和 weight。

`ExperimentCellLedger.observations` 必须与 plan cell ID 完全相等且顺序一致，
并对 observed/zero-filled/excluded/missing/invalid 计数完全对账。
`MetricCellMatrix` 必须包含注册 row × column 的有序笛卡尔积；任一
checkpoint/task/domain cell 缺失都不得对剩余值做 observed-only 聚合。

这个 ledger 是 IID、generalization、continual、evolution 和 stress metric 的共同
adapter 边界；各领域只定义如何从完整母体约减，不能自己扫描结果目录。

## 7. 指标族协议

本节给出 MagentaBench 支撑整条 Agent 研究轨迹的统一指标语言。
P0 是产生相应 claim 前的最小集；P1 是高价值诊断；P2 是研究型扩展。
未落地的公式属于扩展契约，不得仅在 notebook 中临时计算后进入 claim。

### 7.1 IID、效率、吞吐与失败

P0 必须同时报告品质、成本和状态：

- authority reward、Pass@1/重复采样 metric；
- prompt/completion/reasoning/total tokens、model/tool calls、retries、tool errors；
- wall time、CPU seconds、peak memory、I/O bytes、network ingress/egress、金额；
- mean 与预注册 p50/p95，完整 terminal-status distribution；
- successes per million tokens/cost/wall-second 与 completed per hour。

AHE 的 token 效率口径为：

\[
\operatorname{Succ/Mtok}
=\frac{\operatorname{pass@1}\times10^6}
{\operatorname{mean\ tokens\ per\ trial}}.
\]

如 token mean 排除被截断 infra trial，必须另报该分母 coverage，并始终
把 Pass@1 与 Tokens 两个原始轴一起展示。零 token/cost/time 分母是 invalid，
不得返回无穷大效率。

P1 应增加 failure/status entropy、retry burst、tool-error taxonomy、queue wait、
critical-path utilization 和 throughput-under-concurrency curve。状态熅必须声明 log base、
是否归一化以及完整状态集，不得先合并“其他失败”。

### 7.2 Generalization 与 transfer

对由 dataset authority 预注册的 groups \(D_g\)，令 \(x_i\in[0,1]\)：

\[
\mu_g=\frac1{|D_g|}\sum_{i\in D_g}x_i,
\quad
\operatorname{GroupMacro}=\frac1{|G|}\sum_g\mu_g,
\quad
\operatorname{WorstGroup}=\min_g\mu_g.
\]

\[
\operatorname{ID\!\to\!OOD\ Gap}=\mu_{ID}-\mu_{OOD}.
\]

gap 保留符号，不取绝对值。空 group、缺失成员、从实际响应反推 group，
都必须 invalid。repository/domain/time/composition 标签与 group weight 都进
metric identity。

AppWorld-compatible metric 必须区分：

\[
\operatorname{TGC}=100\frac1{|D|}\sum_i success_i,
\qquad
\operatorname{SGC}=100\frac1{|G|}\sum_g\min_{i\in D_g}success_i.
\]

SGC 表示同一 scenario 的所有预注册 variant 都成功。它不得在单 task
上命名为 SGC，也不得对 supplied dictionary 中恰好出现的 variant 取 min。

P1 包括 lower-tail CVaR、paired transfer delta、compositional-depth curve、时间/域距离
衰减和 unseen-tool/schema extrapolation。CVaR 的 \(\alpha\)、离散取整规则和 group
weight 必须注册。

### 7.3 Continual learning、curriculum 与 online adaptation

令 \(A_{t,j}\in[0,1]\) 为完成第 \(t\) 次更新后，在第 \(j\) 个冻结
distribution 上的注册 metric；\(A_{0,j}\) 为学习前 baseline。P0 必须保存
完整 \(A[t,j]\) 矩阵，然后才能计算：

\[
AA_T=\frac1T\sum_{j=1}^{T}A_{T,j},
\]

\[
BWT_T=\frac1{T-1}\sum_{j=1}^{T-1}(A_{T,j}-A_{j,j}),
\]

\[
FWT_T=\frac1{T-1}\sum_{j=2}^{T}(A_{j-1,j}-A_{0,j}),
\]

\[
Forget_T=\frac1{T-1}\sum_{j=1}^{T-1}
\max\!\left(0,\max_{t=j,\ldots,T-1}A_{t,j}-A_{T,j}\right).
\]

\(T=1\) 时只有 \(AA\) 有效。任一计划 matrix cell 缺失时，BWT/FWT/Forget 不得
在剩余 cell 上重算。每个 \(A_{t,j}\) 自身仍必须使用 planned-rollout
分母。evaluation stage 必须冻结 memory/state，评估时继续学习会改变 estimand。

online accuracy 必须使用 **pre-update** 分数：

\[
OnlineAcc_T=\frac1T\sum_{t=1}^{T}x_t^{pre-update}.
\]

P1 包括 retention per Mtok、memory interference、checkpoint stability 和跨 skill transfer；
P2 包括 joint-training oracle intransigence 和 causal transfer。学习 token 为 0 时
retention-efficiency invalid，并必须一起报告原始 retention 和 token 轴。

### 7.4 Evolution

对 lineage node \(v\)、parent \(p(v)\) 和 sealed-validation score \(S(v)\)，P0 包括：

\[
\Delta_v=S(v)-S(p(v)),
\quad
B_t=\max\left(S(root),\max_{v:time(v)\le t}S(v)\right),
\]

\[
FinalGain_T=B_T-S(root).
\]

对预注册 candidate 数 \(N_p\) 和阈值 \(\delta\)：

\[
CandidateYield_\delta=
\frac1{N_p}\sum_{v\in planned}
\mathbf1[valid(v)\land\Delta_v>\delta].
\]

在共同计算预算 \(C\) 下：

\[
AUBCGain_C=\frac1C\int_0^C(B(c)-S(root))\,dc.
\]

\(B(c)\) 必须使用预注册的左连续 best-so-far 和 abort 后延伸策略。compile/
schema/smoke/eval 失败的 candidate 仍占 planned yield 分母并记 0；根本未
dispatch 且没有显式 disposition 则 run invalid。

演化必须保持三本不可变账本：

1. candidate ledger：生成了什么，包括 invalid/rejected；
2. archive ledger：哪些通过 validity/evidence gate 并可见；
3. selection receipt：完整可选母体、变换分数、probability/weight、tie-break、
   RNG algorithm/seed/state、选择结果与 fallback reason。

candidate 只有在 source/base commit、tracked/untracked patch、environment、import/compile/
schema/smoke 命令及 stdout/stderr/error 都由 `CandidateValidityGateReceipt` 绑定后，
才能进入 staged evaluation。diagnosis、selection、checkpoint 和 container setup 都是
预算操作，不得从 outer-loop cost 消失。

P1 包括两个分开 ID 的 descendant-growth：root-relative signed gain 与
edge-relative gain；P2 包括 archive hypervolume、oracle-parent regret 与 causal lineage
credit。注释与实现公式不同时，不得沿用注释中的名称。

### 7.5 Meta-evolution

完整 evolution-method configuration \(m\) 必须包含 selector、evolver/mutation、
archive、stopping、metric、visibility、editable surface 和 budget 的闭包 digest。
对共同 initial archive、seed、task schedule 和 compute budget 的 replicate \(r\)，
令 \(G_{m,r}=FinalGain\)：

\[
MetaUtility(m)=\frac1R\sum_rG_{m,r},
\]

\[
MetaDelta(m,m_0)=\frac1R\sum_r(G_{m,r}-G_{m_0,r}),
\]

\[
MetaSuccess_\delta(m)=\frac1R\sum_r\mathbf1[G_{m,r}>\delta].
\]

吞吐不同的方法使用共同预算下的 `MetaAUBC`。inner evolution cost、outer/meta
search cost 与 total cost 必须分开保存。缺失任一 paired replicate 时
`MetaDelta` invalid；不得把不同起始 archive 的独立 run 后处理成 paired comparison。

P1 的 `MetaRegret` 是注册 method set 内的 post-hoc oracle 诊断，不是 deployed
performance。P2 包括 recursion-depth marginal gain、self-modification causal ablation
和跨代 stability/scaling law。

### 7.6 Trajectory 品质与首达时间

令 \(p_t\in[0,1]\) 为固定 horizon \(H\) 上，由注册 milestone evaluator 从
原始 trajectory 重算的 progress。P0 包括：

\[
FinalProgress=p_H,
\qquad
AUPC_H=\frac1H\sum_{t=1}^{H}p_t.
\]

对阈值 \(\theta\)：

\[
T_\theta=\min\{t:p_t\ge\theta\}.
\]

未达到阈值的轨迹是 **right-censored at \(H\)**，不是成功样本的 \(T=H\)，
也不得被丢弃。censoring 需要专用 typed receipt、event/censor time、
horizon authority 和 survival/aggregation rule；在该 receipt 落地前，只能报告原始
censored counts，不得宣称 mean time-to-success。

早停后按注册规则 carry forward 至固定 \(H\)，不得缩短 horizon。\(H=0\)、
无 milestone、或仅 guardrail milestone 时主 progress metric unavailable，不得默认为 1。
milestone DAG mapping 必须声明 strict one-to-one 还是 nondecreasing reuse。

P1 使用未做 max-so-far 的 raw progress 计算：

\[
RegressionMass=\sum_{t=1}^{H}\max(0,p_{t-1}-p_t).
\]

如果只保存上升 milestone，该值不可计算，不得默认为 0。P2 包括
trajectory behavior/edit distance、alternative-path coverage 和 milestone causal attribution。

### 7.7 Tool-use 品质

对 attempted tool calls \(A\)，P0 分开报告：

\[
ToolNameValidity=\frac{N_{known\ tool}}{|A|},\quad
ArgumentValidity=\frac{N_{schema\ valid}}{|A|},\quad
ExecutionSuccess=\frac{N_{executed\ successfully}}{|A|}.
\]

还必须保存 read/write precision、DB match/mismatch 与 check coverage、auth
succeeded/failed/not-needed/not-checked、termination reason 和 first-critical-source。每个 batch
内 call 有独立 call ID；unknown tool、schema-invalid argument、tool runtime error 和
infrastructure error 不得合并。分母为 0 时 rate unavailable。

P1 对 evaluator 注册 required-call set \(R\) 与 actual calls 做一对一最大匹配：

\[
CallPrecision=\frac{TP}{|A|},
\qquad
CallRecall=\frac{TP}{|R|}.
\]

同时报告 redundant rate、hallucinated-tool rate 和 side-effect precision。argument
normalizer、equivalence predicate、matching algorithm 和 side-effect diff evaluator 都必须
注册。P2 包括 call-order DAG conformance、speculative parallel efficiency 和
repair-after-error quality。

### 7.8 Safety 与 robustness

对相同 task/seed/environment 的 clean/attack pair，令 \(U_i^c,U_i^a\) 为独立 utility
evaluator 的值，\(A_i\) 为独立 attack-goal evaluator 的结果。P0 包括：

\[
CleanUtility=\frac1N\sum_iU_i^c,
\quad
AttackedUtility=\frac1N\sum_iU_i^a,
\]

\[
UtilityDegradation=\frac1N\sum_i(U_i^c-U_i^a).
\]

若 attack goal 有 \(S\) 个成功、\(F\) 个失败、\(Q\) 个未知，\(N=S+F+Q\)：

\[
ASR_{lower}=\frac SN,
\qquad
ASR_{upper}=\frac{S+Q}{N}.
\]

\(Q>0\) 时 point ASR invalid，只报 bounds 和明确标注的 complete-case diagnostic。
内部字段必须命名为 `attack_goal_achieved`，不得用含义反转的
`security = true`。infra failure 可使 utility 记 0，但 attack goal 必须为 unknown；
DoS 不得定义为 `not utility`。utility、attack goal、policy violation 和 harmful
side effect 使用四个独立 evaluator 身份。

P1 包括有 causal evidence 的 conditional DoS、robustness ratio 与 attack-family
worst-group；P2 包括 adaptive-attacker budget curve、attack transfer 和 severity-weighted
causal risk。

### 7.9 Calibration

对 success label \(y_i\in\{0,1\}\) 和成功概率 \(q_i\in[0,1]\)，P0 包括：

\[
Brier=\frac1N\sum_i(q_i-y_i)^2,
\qquad
NLL=-\frac1N\sum_i\log p_{i,y_i}.
\]

多分类 Brier 对所有 class 的 squared error 求和。\(p=0\) 的 NLL 为 \(+\infty\)；
如使用 clip，\(\epsilon\) 必须进 metric ID。对固定 bins \(B_b\)：

\[
ECE=\sum_b\frac{|B_b|}{N}
\left|\operatorname{mean}_{i\in B_b}q_i-
\operatorname{mean}_{i\in B_b}y_i\right|.
\]

必须同时报告：

\[
ConfidenceCoverage=\frac{N_{confidence\ observed}}{N_{planned}}.
\]

`model_probability`、`self_reported`、`judge_confidence`、`ensemble_frequency` 是四种
不同 source identity。缺 confidence 不得静默跳过；除非预注册的 estimand 就是
observed-confidence subset，否则主 calibration metric invalid。equal-width/equal-mass、
bin 数/边界/tie、minimum occupancy 和 minimum sample size 都进入 ID，不得根据
观测数据动态减 bin。

P1 包括 selective accuracy/coverage curve 及其面积；P2 包括 held-out
Platt/isotonic recalibration、Brier decomposition 与 continual calibration drift。拟合与
评估必须使用分离数据；全对/全错导致 Platt fit 不可识别时，不得报
post-Platt ECE = 0。

### 7.10 Diversity

对 selection receipt 中 \(N\) 个 eligible parents 的注册概率 \(p_i\)，P0 包括：

\[
SelectionEntropy_{norm}
=-\frac{\sum_i p_i\log p_i}{\log N},
\]

\[
ESS=\frac1{\sum_i p_i^2},
\qquad ESS_{norm}=\frac{ESS}{N},
\]

\[
ParentCoverage=
\frac{|\{parent\ IDs\ selected\}|}{|eligible\ archive|}.
\]

\(N=0\) 时 invalid；\(N=1\) 时 entropy = 0、ESS = 1。没有 probability vector、
candidate ordering、RNG state 和抽样结果的 selection receipt，只能报 observed
frequency，不得声称 selector distribution。

P1 在共同计划 task set 上报告 behavioral disagreement、novelty 与 branching
coverage。缺任一 candidate-task output 时，pairwise metric 必须 invalid 或使用预注册
coverage-subset ID；parse error 不得每个都当作独特行为，否则会虚增 diversity。
P2 包括 niche coverage、AST/patch structural distance、archive breadth/depth 和
multi-objective behavioral hypervolume。representation、normalizer、distance、threshold 和
task weighting 都必须内容寻址。

## 8. 完整 trajectory 是指标的原始证据

每个已启动 planned slot 必须有 append-only、单调 sequence 的 trajectory，至少记录：

- model request/response 的原始 bytes ref、provider/model activation、prompt/completion/reasoning
  tokens、latency、cost、retry 和 typed error；
- tool request/response、call ID、schema validation、stdout/stderr、side effects 和 typed error；
- evaluator request/result、权威 metric key、依赖的 environment/state refs；
- environment setup、image/dependency/source digest、network observation、workspace snapshot/delta；
- timeout、sandbox/API exception、budget debit、checkpoint/resume/cache decision 和 terminal finish；
- 每次 context compaction 的 trigger/mode/strategy/prompt/model、pre/post context digest、
  retained/dropped/summarized message lineage、token/message count、usage/retry/error；
- provider-native trace conversion 的 raw/normalized refs、converter source/schema digest、mapped/
  dropped/unclassified counts、lossy/partial/failed 和 typed error。

raw 轨迹与 normalized projection 必须同时保留。完整 uncompacted history 是独立
权威 artifact；summary 不能覆盖它。compaction、trace conversion、diagnosis 和
debugging 都是被测操作，其 token/time/cost 必须进预算。

未启动 slot 不能伪造 trajectory，但必须在 schedule ledger 中有显式 disposition
和 reason。catch-all exception 只 print、返回字符串或 `None` 而不生成 typed terminal
record，必须 fail closed。

## 9. External metric adapter 边界

内建 algebra 只承载稳定、普适的 reducer。group/scenario/evolution 等领域公式可通过
`adapter_kind = "metric_source"` 扩展，但 adapter 不能扩张信任边界。

一个合法 external metric adapter 必须：

1. 有 TOML capability，绑定 entrypoint bytes、local import closure、adapter kind、
   supported source/formula 和 JSON config Schema digest。
2. 只接收 `ExternalMetricAdapterInput`，其中包含 resolved metric artifact、manifest/
   capability digest、完整 `ExperimentCellLedger` **或** `MetricCellMatrix` 及原始 refs。
3. 配置必须显式列出 group/member membership、stage/checkpoint 和 within/across-group
   reducer；不得从有值的响应中发现成员。
4. 输出 `ExternalMetricComputationReceipt`，对账 planned/observed/zero-filled/excluded/
   missing/invalid，保存每组值、所有 contributing cell ID、population/config/
   contribution digest 和 terminal state/reason。
5. standalone verifier 必须重新校验 source closure/config schema/capability，并从同一
   ledger 重算结果。

通用 cell adapter 的 metric 形状如下：

```toml
[metric]
id = "worst-domain.repo-holdout.v1"
kind = "metric"
adapter = "magentabench.cell-metrics"
bmp_version = "0.1"
value_kind = "rate"
level = "experiment"
direction = "maximize"
unit = "fraction"
source = "regime"
source_field = "experiment_cell_ledger"
formula = "external_adapter_v1"
population = "domains"
group_by = ["domain"]
across_groups = "minimum"
missing_observation = "invalidate"

[metric.config]
group_axis = "domain"
member_axis = "task"
stage_id = "ood-holdout"
checkpoint_id = "final"
within_group = "mean"
across_groups = "minimum"

[metric.config.members_by_group]
repo-a = ["task-a1", "task-a2"]
repo-b = ["task-b1", "task-b2"]
```

adapter 不得引入第五种 comparison kind、改写 status policy、自行降低
\(k\)/bin/group 数、丢弃失败 cell、动态下载未固定代码，或只返回一个值而
不返回 receipt。任一情况都必须在 compile 或 verify 时拒绝。

## 10. 必须拒绝的指标替代

| 被拒绝的做法 | 正确协议 |
| --- | --- |
| 用 staged score 冒充 full benchmark score | 两个 metric ID、两个分母、两份 receipt；staged 只能控制 promotion |
| 用 oracle best@n 冒充可部署性能 | `oracle-best@n` 与 `selector-selected@n` 分开；selected 必须绑定 selector input/probability/RNG/decision receipt |
| all-failed best-of-n 不出报告 | 保留所有失败，确定性选一个 evidence representative，selection 标 invalid，仍按计划母体报成功指标 |
| 未首达的轨迹丢弃或当作 \(T=H\) 成功 | 保存 right-censor flag/time/horizon，使用注册 survival/censoring estimand |
| 按已返回/非空 prediction 计算分母 | 先读 schedule/cell plan，每 slot 显式 disposition |
| infra exception 从 Pass@1/Pass@k 删除 | 已计划失败零填，另报 infra status/count |
| 根据实际完成数降低 \(k\) | \(n<k\) invalid，不改 estimand |
| 把 prefix-any 命名为 unbiased Pass@k | 声明 ordered prefix 并使用独立 metric ID |
| 把 Pass^k 命名为 Pass@k | 分别报“至少一个成功”与“全部成功” |
| 缺 matrix/group/bin member 后对剩余值取均值/min | 必需母体不完整即 invalid，只能用事先注册的 subset estimand |
| 空分母返回 `-1`、0、1 或 NaN | `unavailable/invalid` + typed reason |
| 报告 ECE 时静默跳过无 confidence 样本 | 主 metric invalid，另报 ConfidenceCoverage |
| 全对/全错时记 post-Platt ECE=0 | calibration fit invalid，不伪造完美校准 |
| 把 infra failure 记为 attack success | utility 可零填，attack goal 必须 unknown |
| 从 archive snapshot 重建 selector 概率 | 使用原始 immutable selection receipt |
| 只留 normalized trace | raw bytes + converter closure + completeness/error receipt 同时保留 |
| 只因 JSON/cache 文件存在就 resume | 重新校验全部 lineage digest |

## 11. 固定研究来源与“吸收/拒绝”原则

以下是本协议的固定审计快照，不是浮动的“最新 main”。日后更新来源
必须保留新 commit 的独立审计记录，不得原地改写这些证据的含义。

| 来源 | 固定 commit | 吸收 | 拒绝或加强约束 |
| --- | --- | --- | --- |
| [HumanEval](https://github.com/openai/human-eval) | `6d43fb980f9fee3c892a914eda09951f772ad10d` | unbiased Pass@k 的组合估计 | 不允许从实际结果发现 \(n\)；必须绑定状态和采样假设 |
| [τ-bench](https://github.com/sierra-research/tau-bench) | `59a200c6d575d595120f1cb70fea53cef0632f6b` | 交互式 task/user-agent 评估与重复试验需求 | 不接受 observed-only denominator 或模糊的多次成功命名 |
| [τ²-bench](https://github.com/sierra-research/tau2-bench) | `668d3bcd135c02aa3438f987ef45735b7c163ee3` | read/write、DB、auth、termination 等 tool-use 原子证据 | 拒绝过滤 infra 后动态降 \(k\)；`not_checked` 不得消失 |
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | `6c5b888f955235e865f6c3dda6d9d9bbf1fe849a` | 类型化 eval log、usage、model/tool event 与 scorer 组合 | MagentaBench 额外要求 planned-slot 母体、完整 raw refs 和 standalone replay |
| [Agentic Harness Engineering](https://github.com/china-qijizhifeng/agentic-harness-engineering) | `8b2a55d97590363fe50c3cc6b5e833b020a4bb4c` | Pass@1 infra-zero、token/cost 分轴、配置快照、retry/timeout、per-call span、compaction 与 trace conversion | 加强为 pre/post context + message lineage receipt；不接受 best-effort trace drop 或不明确 token 分母 |
| [Hyperagents](https://github.com/facebookresearch/Hyperagents) | `59a68f672dfb92c74aeb7e61535d776fb36e172d` | parent-child archive、staged/full 分离、candidate diversity/penalty、patch hash、transfer/growth 分析、pre-eval validity gate | 拒绝无 receipt 的全局 RNG、从 archive 重建 selection、注释/公式混名和不完整候选进 archive |
| [DGM](https://github.com/jennyzzt/dgm) | `a565fd2d1dca504ef5104a7cc0f3bdc4ab9b4fd2` | archive/parent-child lineage、staged/full evaluation（`DGM_outer.py:221-300`）和 candidate-index best-so-far curve（`analysis/plot_progress.py:17-67`） | 拒绝 parent-selection 的无 receipt 全局 RNG（`DGM_outer.py:50-150`）、compile exception skip/dynamic full threshold（`DGM_outer.py:152-219`）、submitted/observed denominator（`utils/evo_utils.py:43-80`）和 filename-only resume |
| [ADAS](https://github.com/ShengranHu/ADAS) | `2702bee8fefda42255efc5be9f60e3bd3db96ae4` | architecture archive、search/debug（`_mgsm/search.py:145-240`）、validation-test 分离（`_mgsm/search.py:243-275`）和 planned-data 设计（`_mgsm/search.py:278-332`） | 拒绝 skipped failures、`n -= 1` 式动态分母、只保留格式化 CI、未绑定 bootstrap RNG（`_mgsm/utils.py:88-133`）和被注释掉的 cost accounting |
| [OpenEvolve](https://github.com/codelion/openevolve) | `411fb59c886c18704caaffb611e17cf9e7d824d2` | island/QD/MAP-Elites/feature archive、artifact/trace/checkpoint 设计（`configs/default_config.yaml:1-178`）与 component RNG seed（`controller.py:64-127`） | 拒绝缺 metric 时回退到 numeric average（`process_parallel.py:704-760`）、optional trace、path-only resume 和可变 YAML 身份 |
| [AppWorld](https://github.com/StonyBrookNLP/appworld) | `a072b7a86e7c1d5b1d7175659d750ebb9b79f10a` | requirement-all-pass success、TGC、scenario-min SGC 和 dataset membership | 拒绝单 task SGC 以及对实际返回 variant 取 min |
| [Avalanche](https://github.com/ContinualAI/avalanche) | `eb075be393e1f458b2c352514ff6c17b5a2c0f4e` | continual-learning 的 accuracy/forgetting/transfer 概念 | Agent 设定必须加上完整 checkpoint × distribution × rollout ledger |
| [LifelongAgentBench](https://github.com/caixd-220529/LifelongAgentBench) | `d6f19b42eb358d9150379f0c68c2985c5a867520` | 顺序 sample order、session/state persistence、typed outcome/status | 拒绝只汇总已持久化 session、空分母 `-1` 和缺失 \(A[t,j]\) 矩阵 |
| [ToolSandbox](https://github.com/apple/ToolSandbox) | `165848b9a78cead7ca7fe7c89c688b58e6501219` | milestone/minefield、guardrail、trajectory DAG mapping | 拒绝无 milestone=1、effective-turn 缩分母和未声明的 snapshot reuse |
| [AgentBoard](https://github.com/hkust-nlp/AgentBoard) | `bb7255e2daf1989069a186dad9e53f70680961db` | subgoal progress、partial-progress event 与 carry-forward 需求 | 拒绝 observed-only task mean、就地修改输入轨迹和未类型化 tool error |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | `089ed468cf3ed0322acc66b0211f26d9d90dbf60` | normal utility 与 injection-goal 分开评估、trace-first evidence | 拒绝把 attack success 叫 `security=true`、infra=>attack 以及 DoS=`not utility` |
| [HELM](https://github.com/stanford-crfm/helm) | `63754d05db6f874e41a395880fb573890a13e791` | ECE、selective coverage/accuracy 和 calibration metadata | 拒绝静默跳过缺 confidence 样本、小样本只 warning 和 post-Platt ECE=0 fallback |
| [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness) | `671b1550ff9541341a3d2cdff16ee7a224c34298` | Brier、probability projection、bootstrap/SE 基础 | 拒绝未注册 seed 的 bootstrap、缺 subtask 只 warning 后继续聚合 |

“吸收”只表示使用上游已验证的抽象、公式或工程边界；不表示复制其
失败语义。上游 README、代码注释、论文表格和实际实现不一致时，必须
以固定 commit 的源码与可重算产物为证据，并把差异编码成不同 metric ID。

MagentaBench 比上游更严的统一底线是：**计划母体、完整轨迹、内容寻址
身份、类型化失败、密封 holdout 和 standalone replay 不得被 adapter 放宽。**

## 12. 实现状态与 claim gate

当前树已有的现行机制包括：

- 严格 `MetricSpec`、内建 reducer algebra、完整 status/missing policy 和内容寻址
  metric artifact；
- Pass@1、unbiased Pass@k、Pass^k、ordered-prefix any/all、ExpectedMax@k；
- Wilson 与 deterministic task-cluster bootstrap receipt；
- experiment-regime TOML、stage DAG、dependency artifact、stage activation 与 sealed-holdout
  release receipt；
- 完整 experiment cell plan/ledger/matrix，以及 AA/BWT/FWT/max-history forgetting 约减；
- source-closed external metric adapter boundary 和通用 complete-membership group reducer；
- context compaction、trace conversion 和 candidate validity gate 的 typed receipt。

以下仍是本文已定义但需继续落地的扩展契约：oracle-best/selector-selected
独立 receipt、right-censored time-to-success、failure entropy、正式 group/SGC/CVaR
metric TOML、calibration、trajectory AUPC/regression、evolution AUBC/yield/parent delta、
meta paired delta、tool/safety 原子指标和 diversity receipt。

对这些扩展，“公式已写在文档”、“adapter 能算”或“notebook 有数”都不是
claim 资格。只有当以下条件全部满足时才可用于对外结论：

```text
versioned TOML metric/regime
+ strict Schema
+ planned population ledger
+ typed runtime receipts
+ complete raw evidence/trajectory
+ deterministic runner recomputation
+ standalone reparse/rehash/recomputation
+ adversarial missing/tamper/denominator tests
= claim-eligible metric
```
