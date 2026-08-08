# MagentaBench 交接文档

> 状态修订（2026-08-08）：本目录最初记录的是 `db9a171` 的 Planner 基线，其中的
> 171-test 数字和“subject_kind 未实现”等内容是历史快照，不是当前树的结论。当前
> BMP 代码加固已提交到 `08d124d`；本目录只记录交接状态，请以根目录 `EVIDENCE.md`、
> 实际 `git status` 和最后一次
> 测试命令为准。
> BMP 是 MagentaBench 的 Benchmark-side Protocol；HCP 是 Magenta 智能体的 Harness
> Component Protocol。

本目录是渐进式交接文档。按顺序读，每一份都可独立使用。

| 文档 | 内容 | 读它的时机 |
| --- | --- | --- |
| [`01-what-is-magentabench.md`](01-what-is-magentabench.md) | 项目是什么、要解决什么问题、目标状态 | 第一次接触本项目 |
| [`02-upstream-references.md`](02-upstream-references.md) | 参考了哪些仓库、论文、工具，各自被采纳了什么 | 需要判断某个设计的来源与依据 |
| [`03-iron-laws.md`](03-iron-laws.md) | 开发铁律与派生原则（协议本体） | 写任何代码之前 |
| [`04-architecture.md`](04-architecture.md) | 代码结构、契约链路、门（gate）语义 | 要动代码 |
| [`05-current-state.md`](05-current-state.md) | 已建立什么、未建立什么、被什么阻塞 | 接手时判断从哪继续 |
| [`06-defect-taxonomy.md`](06-defect-taxonomy.md) | 已发现的四类结构性缺陷与审计方法 | 做审计或 review |
| [`07-records-guide.md`](07-records-guide.md) | `records/` 里的产物怎么读，为何全部失败且被保留 | 要看 `records/` |
| [`08-process-lessons.md`](08-process-lessons.md) | 过程教训，含一次严重的伪造事件 | 想知道为什么纪律要求这么严 |

配置与外部 benchmark 接入的约束见 [`../docs/governance/bmp-configuration.md`](../docs/governance/bmp-configuration.md)。

## 三十秒摘要

MagentaBench 是一个 benchmark 测量协议（BMP）实现：让真实 agent（Codex CLI / Claude Code / Magenta）跑真实 benchmark（Terminal-Bench 2.1 / BiomniBench-DA / CMT-Bench），并且**只在证据足以支撑时才允许产出结论**。

它的核心不是"跑得动"，而是"跑出来的数字指向它声称的那个东西"。项目里绝大部分代码是在防止一类特定失败：**产出一份看起来完全正确、通过所有检查、但证明了错误性质的报告。**

当前代码状态（HEAD `08d124d`，26 个代码提交，251 测试通过）：
- 契约层、编译期归因门、三个真实 backend、内容寻址证据链 —— 已建立
- **从未有任何真实 benchmark 证据走通过整条链路**。所有门都只由构造与变异测试证明，未由执行证明
- 十个 `ClaimScope` 中九个在编译期被拒绝，各自指名缺失的证据类
- `_ACTIVE_SCOPES = {conformance}`

最后一条不是缺陷，是当前唯一诚实的状态。

## 验证纪律（对接手者同样适用）

本文档中所有数字与哈希均来自实际命令输出。接手时请自行复核：

```bash
cd /mnt/aliyunsb/aralacai/MagentaBench
git log --oneline -- MagentaBench | head   # 当前代码历史以 08d124d 结尾
git status --porcelain | wc -l              # 应为 0
uv run pytest -q                           # 应为 251 passed
uv run python -m compileall -q MagentaBench tests
bash scripts/audit_hcp_boundary.sh         # 应为 0 violation(s), 0 scan error(s)
```

**必须使用 `uv run` 或项目虚拟环境。** 系统 `python3` 是 3.6.8，无法运行本项目。
