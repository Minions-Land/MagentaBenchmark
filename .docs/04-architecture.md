# 04 · 架构与契约链路

> 本页的行号和提交引用是 Planner 基线（`db9a171`）。当前实现已增加
> `RecordIndex`/standalone verifier、父 run/子 attempt lineage、显式 exploratory
> isolation 结果及 checkpoint save 字节验证；不要把基线行号当作当前源码定位。

本文档适用于要动代码的人。

## 目录结构（15,516 行 Python，37 个模块文件）

```
MagentaBench/
├── schemas/              # 契约模型，11 个 schema 模块
│   ├── models.py         # ClaimScope / RunPurpose / RunStatus / ScoringKind ...
│   ├── manifest.py       # ExperimentManifest 等
│   ├── evidence.py       # ObservationReport / ClaimReport
│   ├── execution.py      # TrialResult / JobResult
│   └── ...
├── runner/               # 执行与编译，9 个模块
│   ├── compiler.py       # ManifestCompiler（入口，_ACTIVE_SCOPES 在此）
│   ├── pipeline.py       # Pipeline（调度 backend、resume）
│   ├── scheduler.py      # 预算与计划分配（Phase 3b，刚完成审计）
│   ├── gates.py          # evaluate_run_report 与六个门
│   ├── backend/          # 四个 backend（fake / subprocess / aose / harbor）
│   ├── env/              # EnvManager（内容寻址 venv 缓存）
│   └── adapter_registry.py  # 内容寻址 case-set 与 adapter 注册（db9a171）
├── adapters/             # benchmark / subject / task / verifier 适配层
│   ├── benchmarks/       # aosebench.py
│   ├── subjects/         # cli_agent.py
│   └── fake/             # 用于自检的 fake 适配
├── conformance/          # Phase 0 conformance fixture
└── tests/                # 20 个测试文件，全 pytest

registries/               # TOML 声明（backend / benchmark / protocol / subject）
├── backends/             # fake / subprocess / aose / harbor / harbor-shim
├── benchmarks/           # aosebench-biomnibench-da / fake-exact
├── protocols/            # fake-deterministic / subprocess-deterministic / harbor-shim-conformance / ...
└── subjects/             # fake-control / fake-treatment / aose-dryrun-true / ...

records/                  # 运行产物（135KB / 43 文件），见 07-records-guide.md
docs/governance/          # bmp-boundary-law.md（246 行）
```

## 核心链路：从 manifest 到 report

```
┌─────────────────┐
│ TOML registry   │
│ + user manifest │
└────────┬────────┘
         │ ManifestCompiler.compile()
         │   - 解析 backend / benchmark / protocol / subject
         │   - 检查 scope / purpose / kind 矩阵（compiler.py:600-626）
         │   - 生成内容寻址的 CompiledManifest
         ↓
┌─────────────────┐
│ CompiledManifest│  (不可变，digest 寻址)
└────────┬────────┘
         │ Pipeline.execute()
         │   - adapter_registry 解析 case-set
         │   - 实例化 backend（fake / subprocess / aose / harbor）
         │   - 调用 backend.execute_trial() 或 .execute_job()
         │   - 写 ScheduleActivationReceipt 与执行 receipt
         ↓
┌─────────────────┐
│ evidence_bundle │  CompletedRun 列表
│ (per case/run)  │   - TrialResult / JobResult
└────────┬────────┘   - provenance: executable SHA-256, timestamps, etc.
         │            - NetworkObservation (if available)
         │ evaluate_run_report()
         │   - 六个门逐一判定
         │   - 推导 expected_run_count、authoritative metric
         ↓
┌─────────────────┐
│ ObservationReport  或  ClaimReport │
│ - gates: {execution_valid, protocol_valid, isolation_valid,
│           scoring_valid, statistics_valid, claim_eligible}
│ - 每个门带 valid: bool + reason: str
└─────────────────┘
```

## 六个门的语义（`gates.py`，12 个函数）

每个门返回 `GateResult(valid: bool, reason: str | None)`。所有门的输入是**同一个 `CompletedRun` 列表**，不依赖调用方传入的期望值。

| 门 | 含义 | 关键实现 | 位置 |
| --- | --- | --- | --- |
| `execution_valid` | 每个 run 都成功执行且有可验证产出 | `status=no_output` 拒绝；`len(items) != expected_run_count` 拒绝；**expected 从 run-ID 集合推导** | `gates.py:651` |
| `protocol_valid` | 执行符合声明的方法学 | 检查 receipt binding、case-set binding，并绑定每个 case 的 selected attempt | `gates.py` `_receipt_binding_errors` |
| `isolation_valid` | 网络隔离可验证 | 需要 **每个 item** 都有类型化 `NetworkObservation`；绑定到 policy digest；当前在 `exploratory` 上会看有无 observation，`claim` 上严格要求 | `gates.py:579` `_network_policy_errors` |
| `scoring_valid` | 评分真实且与声明一致 | **L1**：从 `manifest.benchmark.authoritative_reward_metric` 获取；**L2**：所有 run 一致同意；**L3**：有 `verifier_evidence.metrics` 时，`metrics[key] == score` 精确相等（不用 `isclose`）；**空集拒绝**（曾经在 `output_refs=[]` 上空过） | `gates.py:135` `_exploratory_metric_scores` |
| `statistics_valid` | 统计推断支持因果主张 | **结构性不可达** —— `gates.py:516` 的 `else` 分支在 `deterministic_conformance=False` 时必然触发，记录 "full real-experiment statistics are not implemented by the fake gate"。这是正确的关门，且在其他所有 claim 工作上游 | `gates.py:651` |
| `claim_eligible` | 前五个门全绿 + `purpose=claim` | 逻辑合取 | `gates.py:833` |

**关键设计**：`_score()` 返回裸 `evidence.score`（`gates.py:122-124`），不做任何变换。`_exploratory_metric_scores()` 是唯一读 `verifier_evidence.metrics` 的地方，且在 L3 用 `metrics[auth_key] == derived_score` 精确检查 —— 如果 backend 产出的 metric 与从 reward 推导的分数不等，**必须拒绝**。

**`expected_run_count` 的推导位置**：`gates.py:641-664`，从 run-ID 集合做集合差分得到覆盖情况，count 是**推导值**不是参数。

## 编译期归因门（`compiler.py:600-626`）

`ClaimScope` 与 `RunPurpose` 有一个编译期矩阵。九个 scope 目前不在 `_ACTIVE_SCOPES` 中，会触发：

```python
raise CompilationError(
    f"claim scope {scope.value!r} requires missing evidence class "
    f"{proof_type}; runtime support is not active"
)
```

`proof_type` 对每个 scope 不同，例如 `checkpoint` 要 `CheckpointRunEvidence`、`evolver` 要 `EvolutionRunEvidence`。这些类型**目前都不存在**，因此编译就会失败。

**为什么这样设计**：防止系统在证据机制缺失的情况下产出看起来合法的 claim。编译期拒绝优于运行后发现报告不可信。

## backend 接口（`runner/backend/__init__.py`）

每个 backend 必须实现 `execute_trial()` 或 `execute_job()`，返回 `TrialResult` / `JobResult`。四个已实现的 backend：

| backend | 用途 | 关键约束 |
| --- | --- | --- |
| `fake` | 自检 fixture，返回固定 `exact_match` 分数 | 绝不回显 benchmark 的 metric |
| `subprocess` | 本地子进程，有密钥擦除 | 记录 executable SHA-256 |
| `aose-docker` | AOSE 容器，已完成 10/10 零成本 dry-run | 曾用 `network_mode='none'` 做隔离，已废弃 |
| `harbor` | Harbor 0.20.0，原生解析其 `JobResult` | `observed_case_order` 仅在 `parallelism=1` 时有效（编译期守卫）；测试解析放宽须显式标记 `allow_test_override` |

**Harbor 的两个特殊收据**：`TimingInfo` 阶段分类（agent / verifier / overhead）；`verifier_rewards` 从 `verifier_result.rewards` 提取，若有多个 key 且无 authoritative key 则返回 `None` 并让 scoring 门失败（`harbor.py:146-167`）。

## adapter_registry（`db9a171` 刚落地）

`runner/adapter_registry.py` 提供内容寻址的 case-set 加载与 adapter 查找。关键类型：

- `CaseSetArtifact`：不可变 case 列表 + digest
- `BenchmarkLoader`：根据 `benchmark.adapter` 返回 loader
- `ExecutionAdapter`：根据 `(benchmark.adapter, backend.adapter, subject.interface)` 返回执行适配
- `Backend` factory：根据 `backend.adapter` 返回工厂

普通多 case 执行已实现：一个 parent schedule receipt 可包含多个 case，报告为每个
`(parent_run_id, case_id, attempt_id)` 写入独立 lineage；RecordIndex 仍只保存一次
parent manifest。checkpoint ledger 的旧 completion map 仍以 parent run 为键，因此
checkpoint 多 case 在 schema 扩展前会在 activation 处明确拒绝。

## EnvManager（Phase 2）

`runner/env/manager.py`。内容寻址 venv 缓存：给定 `requirements.txt` 或 lockfile，返回 digest 与已准备好的 venv 路径。避免重复创建。

## 关键文件行号参考（截至 `db9a171`）

| 位置 | 内容 |
| --- | --- |
| `gates.py:122-124` | `_score()` 返回裸 `evidence.score` |
| `gates.py:135` | `_exploratory_metric_scores()` 三级 metric 推导 |
| `gates.py:178` | `_receipt_binding_errors()` |
| `gates.py:516` | 统计分支关门 `else` |
| `gates.py:590-594` | `test_override` 拒绝被标记血统 |
| `gates.py:641-664` | 集合差分覆盖 + 推导 `expected_run_count` |
| `compiler.py:337` | `_ACTIVE_SCOPES = frozenset({ClaimScope.conformance})` |
| `compiler.py:374` | `_ACTIVE_SCOPES` 定义 |
| `compiler.py:424` | 唯一的 `allow_test_override` 检查站 |
| `compiler.py:426` | `PipelineAdapterActivationReceipt` 写入 |
| `compiler.py:447`/`633` | scope 不在 `_ACTIVE_SCOPES` 的拒绝位置 |
| `compiler.py:600-626` | kind / purpose / scope 矩阵 |
| `compiler.py:644-658` | 候选选择门 |
| `models.py:135` | `ResourceSpec.allow_internet` |
| `models.py:234` | `NetworkObservation.declared_allow_internet` |
| `models.py:329` | `authoritative_reward_metric` 定义 |
| `models.py:935` | `SUBJECT_KIND_SCOPE_MATRIX`（`opaque_agent` 不含 conformance）|
| `models.py:1054` | `ClaimScope` 枚举十值 |
| `models.py:1105` | `VerifierEvidence.metrics` |
| `models.py:1474+` | `validate_schedule_receipt` |
| `pipeline.py:491` | resume 时 backend 交换检查 |
| `pipeline.py:541` | `receipt_path` 推导 |
| `harbor.py:146-167` | `_verifier_rewards()` / `_verifier_score()` 关门逻辑 |
| `harbor.py:418` | `metrics=_verifier_rewards(verifier)` |
| `harbor.py:808` | 传 `authoritative_reward_key` |

## 测试组织（20 个文件）

```
test_schemas.py                 986 行，schema 验证
test_conformance_gates.py       conformance 门全流程
test_conformance_pipeline.py    Pipeline 自检路径
test_gate_vacuity.py            6 个变异：scoring / completeness / 双 missing
test_adapter_registry.py        9 个 adapter 变异（db9a171）
test_runner_harbor.py           Harbor 原生解析 14 clusters
test_runner_subprocess.py       subprocess backend
test_aose_docker_dryrun.py      10/10 AOSE 零成本观测
...
```

**重要的那个**：`test_gate_vacuity.py`（`beadc49`）。6 个变异证明门在被构造的缺陷证据上确实失败：scoring 空集、completeness 丢掉一个 run、双 missing（NetworkObservation 与 verifier-score）。每个变异对应一个真实抓到的缺陷。

## 当前测试计数与运行方式

```bash
cd /mnt/aliyunsb/aralacai/MagentaBench
.venv311/bin/python -m pytest -q   # 171 passed in ~75s
```

**必须用 `.venv311/bin/python`**，系统 `python3` 是 3.6.8。

最近一次全套：`171 passed in 75.54s`（干净 HEAD `db9a171`，由 runtime-builder 从全新 detached worktree `/mnt/aliyunsb/aralacai/MagentaBench-verify-db9a171` 运行）。
