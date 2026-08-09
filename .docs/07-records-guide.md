# 07 · 如何读 `records/`

`records/` 同时保存两类东西：历史 AOSE 反例，以及后来加入的真实 exploratory
integration probe。历史反例通不过当前的门，并且必须原样保留；probe 可以通过
`bmp-verify-probe`，但只证明真实接触与失败分类，不能充当 Agent 分数或 claim。

如果你"修复"这些产物让它们通过新门，你就毁掉了它们唯一的用途。

## 布局

```
records/
├── swebench-astropy-6938-probe/       # 真实 API-backed exploratory contact
├── terminal-bench-regex-probe/        # 真实 Docker/verifier contact；verifier failure
├── aose-zero-cost-dryrun-summary.json
├── aose-zero-cost-run-a/
│   ├── 328da78fb3e49b07.../cases/da-1-3/attempt-0001/{container_receipt,evidence_bundle,observation_report,status}.json + container.std{out,err}.log
│   └── f541c9ddc9328a72.../cases/da-1-3/
│       ├── {claim_report,container_receipt,evidence_bundle,status}.json + container.std{out,err}.log
│       └── attempt-0000/{claim_report,container_receipt,evidence_bundle,status}.json + logs
└── aose-zero-cost-run-b/
    └── 1ac338c1838ef690.../cases/da-1-3/attempt-0000/...
```

第二层是 **manifest digest**（内容寻址），第三层是 case，第四层可选 attempt。

## 先读这一个

```bash
cd /mnt/aliyunsb/aralacai/MagentaBench
D=records/aose-zero-cost-run-a/f541c9ddc9328a7283965c509704effd9436fc9b7b280b12bf14847bc06d294c/cases/da-1-3
python3 -m json.tool $D/evidence_bundle.json
python3 -m json.tool $D/claim_report.json
```

### `evidence_bundle.json` 的实际内容

```
status:                  no_output
verifier_evidence:       null
output_refs:             []
trace_ref:               null
provenance.executable:   /usr/bin/true
```

subject registry（`registries/subjects/aose-dryrun-true.toml`）里 `entrypoint = "/usr/bin/true"`、`emits_trace = false`。

**也就是说：这份"真实 benchmark 证据"是容器里跑了一个空操作二进制。** 它成功启动、成功退出、什么都没做。

### `claim_report.json` 的实际内容（逐字节，已复核）

```json
{
 "claim_eligible": false,
 "failure_breakdown": {"no_output": 1},
 "gates": {
  "execution_valid":  {"valid": false, "reason": "execution-invalid statuses or missing cases: no_output",           "evidence_refs": []},
  "isolation_valid":  {"valid": true,  "reason": "allowed-diff compile check and evidence provenance agree",         "evidence_refs": []},
  "protocol_valid":   {"valid": true,  "reason": "resolved schedule and reset policy match the plan",                "evidence_refs": []},
  "scoring_valid":    {"valid": true,  "reason": "every verifiable output has exact-verifier evidence",               "evidence_refs": []},
  "statistics_valid": {"valid": false, "reason": "full real-experiment statistics are not implemented by the fake gate", "evidence_refs": []}
 },
 "lineage": [{
   "case_id": "da-1-3",
   "run_id": "aose-zero-cost-run-a__run0000",
   "evidence_bundle_sha256": "6cd6f667f734d2904cff48200b42ca8f98031eb6966edd8ad39562a53dd60e94"
 }],
 "manifest_digest": "f541c9ddc9328a7283965c509704effd9436fc9b7b280b12bf14847bc06d294c"
}
```

## 这份报告里的三个缺陷

这一个文件同时展示了 [`06-defect-taxonomy.md`](06-defect-taxonomy.md) 里的三类缺陷。**它是整场审计的缩影。**

### 缺陷 1 · `scoring_valid: true` 是空集空过

reason 写着 "every verifiable output has exact-verifier evidence"。而同目录的 bundle 里 `output_refs: []`、`verifier_evidence: null`。

这句话**逻辑上为真** —— 空集上的全称量化恒真。但它作为门的结论是彻底错的：scoring 循环只遍历 execution-valid 的 bundle，`no_output` 一个分支都不匹配，于是在零个元素上报成功。

**这就是"空即跳过"陷阱。** 已修（`bf25d77`），并给 `scoring_valid` 与 `isolation_valid` 都加了计划完整性要求（`e89f59e`）：`len(items) != expected_run_count` 直接拒绝，且 reason 必须带正面证明的计数。

### 缺陷 2 · `isolation_valid: true` 是声明相互印证

reason：**"allowed-diff compile check and evidence provenance agree"**。

两个东西"agree"。它们是什么？一个编译期检查与一份 provenance 记录 —— **两个声明**。没有任何一次实际的出口流量探测。这个 backend 当时用 `network_mode='none'` 作为隔离证据，而配置项不是观测。

已修（`68a75b6` 要求正面隔离观测，`e41944e` 把观测绑定到已解析 policy 的 digest）。现在必须有类型化 `NetworkObservation`，且 `egress_succeeded=false` 只能来自字面 IP 的传输层拒绝。

### 缺陷 3 · 每一道门的 `evidence_refs` 都是 `[]`

包括三个**通过**的门。

一道门声称某个性质成立、同时引用零份证据 —— 这就是装饰性完整性的定义。字段存在、结构完整、digest 齐全，而没有任何东西承重。

## 为什么这批产物现在全部失败

AOSE 的 `claim_report` 在**三个独立理由**上被当前门拒绝：

1. 缺 `NetworkObservation`（`e41944e` 要求每个 item 都有）
2. 空集 scoring 空过（`bf25d77` + `e89f59e`）
3. metric 被标为 `exact_match`，而 benchmark registry 声明的是 `overall`（`aosebench-biomnibench-da.toml` 里 `authoritative_reward_metric = "overall"`）

第 3 条特别值得注意：它是**独立交叉检查生效的实例**。FakeBackend 产出自己的原生键 `exact_match`，与 benchmark 声明的 `overall` 不符，于是三级 metric 推导的 L1/L2 层直接抓到。如果当初让 fake 回显 benchmark 声明的 metric，这个错误声明就会**自我印证**（铁律派生原则 13）。

## 保留策略

**这些历史 AOSE 文件不得修改。** 它们的用途是对门本身的反证：

> 如果新的门接受了这些产物，说明新的门是坏的。

这是一个可重复的检验。任何改动门的变更，都可以拿这批产物验证门没有退化。若有人为了"清理"而修好它们，这个检验就永久消失了。

同理，`records/` 下仍然**没有任何 claim-ready 产物**。两份新 probe 证明 SWE-bench
和 Terminal-Bench 的真实 integration contact，但都没有走通带完整 activation、隔离、
评分、统计与 standalone report replay 的 claim 链路。

## 关于其他早期实验

会话记录里提到过若干"已完成"的实验：双臂 `whole_harness` 对比、首次计费运行（3 case，零凭证泄漏）、model-scope 对比（haiku vs sonnet）、harness 对比（Magenta vs Claude）、首次预注册 `purpose=claim` 运行。

**这些产物同样通不过当前的门。** 追溯性失效清扫已完成 —— 结论是 `records/` 下**每一个**产物都失败。任何引用这些实验作为能力证据的说法都必须先复核产物本身。

## `whole_harness` 为何被停用（基于产物，非推测）

`_ACTIVE_SCOPES` 一度被论证应该重新包含 `whole_harness`。这个论证通过**打开真实证据包**被推翻 —— 就是上面那份 `evidence_bundle.json`：`status=no_output`、`verifier_evidence=null`、`output_refs=[]`、`provenance.executable=/usr/bin/true`。

**不存在任何完整产物可供 `whole_harness` 激活。** planner 自己的 steelman 论证在事实层面是错的，已撤回。

这是"可达性规则"的一次实际应用：判定必须基于产物证据，不基于对代码能力的推断。

## 复核命令

```bash
cd /mnt/aliyunsb/aralacai/MagentaBench
find records -type f | sort
git ls-files records
uv run bmp-verify-probe records/swebench-astropy-6938-probe/probe.json
uv run bmp-verify-probe records/terminal-bench-regex-probe/probe.json
```
