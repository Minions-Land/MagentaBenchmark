# 05 · 当前状态：已建立、未建立、被阻塞

> **状态修订（2026-08-08）**：`SubjectKind` 的 manifest 推导、父 run/子 attempt
> lineage、`RecordIndex`、独立报告校验、checkpoint 字节绑定、环境路径无关身份、
> exploratory 显式 `isolation_valid`、配置/adapter 契约与 HCP sidecar 字节绑定已提交
> 到当前 BMP 代码历史；本地工作树另有配置 composition、外部 override、adapter
> import-closure、Magenta v0.1.22 adapter 与 evolution evidence 改动。最终树验证为 289 tests passed、HCP
> boundary audit 0 violation；**没有任何真实 benchmark 证据因此变成有效 claim**。
> Magenta checkout 已用 `PoorOtterBob` 从 canonical GitHub 更新到本地 merge `5ee759a1`；当前
> Magenta 本地提交 `f0eb2f49` 增加了可消费的中性 HCP assembly sidecar，另有本地测试
> 提交 `c9f8f678`；这些提交均未 push。源码已离线构建为 v0.1.22，但 canonical
> assembly digest/dependency closure 仍未由 HCP 提供，因此 `magenta_hcp` 的
> component/ablation 能力继续 fail closed。

本文档决定接手者从哪继续。

## 代码提交历史（当前以 `73f4706` 为 HEAD）

```
73f4706 Implement flexible configuration and evolution evidence
54ad012 Add Magenta v0.1.22 configuration adapter
08d124d Bind configuration and HCP sidecar bytes
6b9cc7f Open BMP configuration and benchmark adapter contracts
aff5632 Harden BMP lineage, ordering, and standalone verification
db9a171 Add content-addressed case-set adapter registry
e41944e Bind network observations to resolved policy
551b5cc Require exact exploratory evidence coverage
5a40182 Track reproducible environment manager
7ab4ca1 Close test override evidence paths
beadc49 Add gate vacuity mutation tests
68a75b6 Require positive isolation observations
8985596 Assert retained checkpoint executions are loaded
0f8212a Reuse retained checkpoint executions on resume
01fd30e Bind checkpoint loads to ancestor evidence
e89f59e Require plan completeness in scoring and isolation gates
bf25d77 Fix scoring gate vacuous pass; evidence AOSE whole_harness deactivation
5399ba3 Grant test override to Harbor native parsing fixtures
dc536d4 Order compiler diagnostics by contract boundary
233e612 Record subprocess reset activation
27cff87 Fix Harbor and subprocess test clusters
d65a37b Make experiment contrasts authoritative
c11e583 Complete item 15 rename: private pure stages, unblock collection
9a5102c Phase 3b: close identity, substitution, and reachability defects
cb78b69 Clarify blocked schedule identity diagnostics
18cc4be Harden AOSE dry-run attempt isolation
e19b04a Phase 3a: ClaimScope/RunPurpose + compile-time attribution gates
7c2d378 Initial commit: BMP Phase 0-2 implementation
```

当前 BMP 工作树已干净；本轮 BMP 代码改动已本地提交到 `73f4706`。Magenta 的对应 HCP
改动已本地提交为 `f0eb2f49`，且明确未 push。

## 已建立（有代码 + 有测试）

**Phase 0 · 契约层**
- 5 个核心 BMP 契约、11+ JSON Schema、HCP 边界法（`docs/governance/bmp-boundary-law.md`，246 行）、conformance bench

**Phase 1 · 环境与执行基质**
- `EnvManager` 内容寻址 venv 缓存
- subprocess backend：密钥擦除、隔离 workspace、可执行文件 SHA-256
- 密钥防御

**Phase 2 · 真实 backend**
- Harbor 0.20.0：原生 `JobResult`/`TrialResult` 解析、`TimingInfo` 阶段分类
- AOSE Docker 零成本 dry-run，10/10 观测

**Phase 3a · 归因类型**
- `ClaimScope`（10 值）、`RunPurpose`、`RunReport = ObservationReport | ClaimReport` 判别联合、编译期门

**Phase 3b · 调度器 + 对抗审计（本会话主体）**
- `BudgetAllocation` / `BudgetLedger` / `AttemptAllocation` / `ScheduleActivationReceipt`
- **约 45 个结构性缺陷被发现并修复，零误报，全部位于已通过自身测试的代码中**
- 替换清单穷尽：6 层 18 项
- `ManifestCompiler` 的一个并行简化版本（`build_resolved_manifest`）**被删除** —— 零调用方，绕过全部 scope 与 tuple 门

**Phase 3b 后续加固**
- `551b5cc`：精确 run-ID 集合覆盖、三级 metric 推导、持久化字节校验（三子句）、provenance 身份绑定两条路径
- `e41944e`：`ResolvedNetworkPolicy` / `NetworkPolicySource` / `NetworkBoundary`，观测绑定 policy digest，正面 deny 探针要求
- 内容寻址 case-set adapter registry；普通多 case 执行按
  `parent_run_id × case_id × attempt_id` 写入独立 lineage，checkpoint 多 case
  在 schema 扩展前继续 fail closed
- 配置 registry：TOML 对象内容寻址、CRUD、深合并、envelope/raw 外部文件、inline/CLI
  overlay、JSON Schema 校验与可重放 composition；配置 digest 进入 manifest；`custom`
  benchmark + `AdapterCapability` 支持显式 digest-bound 外部 adapter（含 helper closure）
- 项目级 `registries/adapters/*.toml` discovery：entrypoint 字节 digest、声明字节、兼容 tuple
  都绑定到 manifest，Pipeline/standalone verifier 均拒绝漂移

**benchmark 接入**
- BiomniBench-DA：26 case 已注册，连续 `ScoringKind`，`authoritative_reward_metric="overall"`
- 双臂 `whole_harness` 对比、首次计费运行（3 case，零凭证泄漏）、model-scope 对比（haiku vs sonnet）、harness 对比（Magenta vs Claude）、首次预注册 `purpose=claim` 运行 —— **全部产物现已被当前门拒绝，见 07**

## 未建立（明确的空缺）

### 最重要的一条

**从未有任何真实 benchmark 证据走通过整条链路。** 所有门都只由**构造与变异测试**证明，未由**执行**证明。TB2.1 loader 从未通过 Pipeline 解析过一个 case。

这不是小注脚。它意味着门与真实 backend 输出之间的接触面**完全未经检验**。

### 其他

| 项 | 状态 |
| --- | --- |
| TB2.1 registry | 不存在。写时 `authoritative_reward_metric='reward'` 必须引用**实测观测**，非 leaderboard 源码 |
| gold 隔离契约 | 已规范，未实现。5 个要求 + 6 个变异，承重的那个是"运行中拷入 gold"必须在**运行后重新哈希**处失败，而非在挂载检查处 |
| M3 探针契约 | 已规范，未实现。≥2 个不同种类的类型化探针；`literal_ip` 记录 IP/端口/TCP/结果/errno/错误类；`hostname` 解析失败报 `resolution_failed` **不得**暗示传输拒绝；`egress_succeeded=false` 只能来自字面 IP 传输拒绝；探针代码 digest；字节校验的产物进 `NetworkObservation.evidence_refs`；仅有 `gaierror` 的产物**不能**满足 `active_probe` |
| `subject_kind` 类型化枚举 | 已实现：从已解析 manifest/subject kind 推导，并由 standalone verifier 与索引交叉检查；仍未有真实 benchmark claim |
| `EvolutionRunEvidence` | 已实现：候选/transition ledger、rejected/invalid 保留、content-addressed refs、meta-evolver parent 递归校验；claim 还要求 external execution capability、digest binding 与 provenance ref |
| `ResolutionBandReceipt` | 已由 boundary-guardian 裁定契约形状，未实现。见 `02-upstream-references.md` |
| CMT-Bench | 未接入 |
| `EVIDENCE.md` / `records/RETROACTIVE.md` | 已写；明确当前没有真实 benchmark 成功证据，旧 records 全部是失效反例 |
| Phase 3c | 对比引擎泛化（非 subject vary 轴）、`ProviderBinding`/`CredentialRef` 集成、RunRecord Step 2 |
| Phase 4 | resolution band、held-out split 与更多 evolver/meta-evolver gate 量化 |

## 被阻塞（含原因）

### 1 · 端到端 TB2.1 运行 —— 等用户授权

会在 `records/` 下写 bundle / receipt / report。三个条件已确立：

- **(A)** `subject_kind` 类型化枚举从已解析 subject adapter 推导（已实现，须随最终回归复核）
- **(B)** 与已跟踪 fixture 路径不冲突 —— 已验证，只有 `records/.gitkeep` 被跟踪
- **(C)** 框架预先说定：**任何门触发都是一次成功的探针**，不是放宽该门的理由

零成本条件：两个已缓存镜像、no-op agent、断网、无模型调用。

### 2 · 无 git remote

`git remote -v` 为空（`wc -l` → 0）。26 个提交**只存在于这台机器**。没有 bundle 文件。远端去向是用户决定。

### 3 · `claim_eligible` 对任何真实运行结构性不可达

`gates.py:516` 的 `else` 分支在 `deterministic_conformance=False` 时必然触发，记录 "full real-experiment statistics are not implemented by the fake gate"。这是**正确的关门行为**，且位于其他所有 claim 工作的上游。

`ResolutionBandReceipt` 不解除这个阻塞 —— boundary-guardian 明确裁定分辨带本身不能让 `claim_eligible` 或因果 `statistics_valid` 为真。它能支撑的是**第一份真实的 exploratory ObservationReport**。

### 4 · 仍未实现的 ClaimScope 编译期被拒

各自指名缺失的证据类。`component` 与 `ablation` 已有中性 `magenta_hcp`
sidecar 接口，但 HCP 尚未提供 canonical assembly digest 与 dependency
closure，仍由 `compiler.py` 和 claim gate fail closed。`evolver` /
`meta_evolver` 已有独立 `EvolutionRunEvidence`，但没有 external execution
capability 或 claim-ready provenance 时仍然 fail closed。

### 5 · `observed_case_order` 仅在 `parallelism=1` 有效

Harbor 记录完成顺序而非提交顺序。编译期守卫读**已解析的 protocol**，不读调用方传入值。

### 6 · TB2.1 无机器可读 public 清单

gold 分类只能是逐 case 显式白名单 + 未分类拒绝。

## 首次真实执行的九个预判失败模式（boundary-guardian）

接手者做端到端运行时会撞上这些。**每一个触发都是成功的探针。**

| # | 失败模式 | 状态 |
| --- | --- | --- |
| 1 | 容器内无法发起字面 IP 连接 | **已由观测关闭** —— 镜像内有 `/usr/bin/python3`，`--network none` 下字面 IP TCP 连接返回 errno 101 `ENETUNREACH`（传输层拒绝，非 `gaierror`）|
| 2 | Harbor 运行后 workspace 路径是否可读 | **部分关闭** —— workspace 在 `/app`，agent 阶段后被复制到宿主 trial 目录。**复制与 verifier 写入的先后顺序无法在不执行的情况下解决** |
| 3 | 无害的 harness 写入触发运行后重新哈希 | 预期接触点。需要一份**声明并 digest 的无害写入白名单**，且只能从**实际观测到的写入**构建，绝不能从读代码预先构建 |
| 4 | 超时时 rewards 被填充但无 authoritative key | 必须成为 `failure_breakdown` 中的终局失败，**绝不**是一个 metric 不可验证的已评分运行 |
| 5 | Harbor 完成顺序不确定 | **已由 parallelism 守卫关闭** |

**若模式 2 触发**，正确结论是该元组**无法产出 gold 隔离证据**、证据包 isolation 门失败 —— **不是**改成条件性重新哈希。那又是一次"空即跳过"。

## 建议的继续顺序

1. **等授权后**：M3a gold 隔离 loader（5 要求 6 变异），然后 M3b 探针契约。
2. **`ResolutionBandReceipt`**（boundary-guardian 已持有设计）。
3. **TB2.1 registry + 首个真实元组走通全链路**。这是解锁其余一切的那一步。

## 复核命令

```bash
cd /mnt/aliyunsb/aralacai/MagentaBench
git log --oneline -- MagentaBench | head      # 当前代码历史以 73f4706 结尾
git status --porcelain | wc -l                  # 0
git status --porcelain --ignored | grep -E "^!!.*\.py$" | grep -vE "venv|__pycache__"   # 空 = 无隐藏源码
uv run pytest -q                                # 289 passed
uv run python -m compileall -q MagentaBench tests
bash scripts/audit_hcp_boundary.sh              # 0 violation(s), 0 scan error(s)
find records -type f | wc -l                    # 43
```
