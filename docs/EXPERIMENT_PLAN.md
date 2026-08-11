# MagentaBench 实验计划

本文是服务器中断后的单一执行入口。它把开发、实验、审计和发布拆成
可重复的阶段；任何阶段没有满足退出条件，都不能把结果升级为 claim。

## 目标与边界

MagentaBench 负责 BMP（Benchmark-side Protocol）：实验身份、分母、执行
策略、指标、证据和可重放验证。Magenta 的 HCP、模型供应商账号、真实
benchmark 的官方 verifier 仍由各自适配器负责。不要在 BMP 中复制 HCP，
也不要用历史日志补造缺失 receipt。

当前事实：

- 确定性 fake/conformance 运行用于验证协议实现，不是模型排名证据。
- Terminal-Bench、SWE-bench 等目录中的 probe 是 exploratory；只有通过完整
  Pipeline、独立 verifier 和全部 claim gates 的报告才能发布。
- 外部 metric adapter、regime 多 stage 执行和 sealed-holdout 内容绑定在
  完成前应保持 disabled，不能作为正式实验输入。

## 阶段门

| 阶段 | 目的 | 必须产物 | 退出条件 |
| --- | --- | --- | --- |
| P0 复原 | 固定代码、依赖和 registry | Git commit、`uv.lock`、测试日志 | `pytest`、`compileall`、HCP boundary audit 全部通过 |
| P1 协议 smoke | 验证编译、调度、报告重放 | fresh `.runs/`、manifest、report、record index | `bmp-verify-report` 独立通过；无 secret、无未声明 adapter |
| P2 单 case 集成 | 连接真实 benchmark/verifier | probe 或 exploratory report、容器/依赖 digest | verifier 失败必须按 taxonomy 记录，不得计为 agent 失败 |
| P3 重复采样 | 固定 case 分母并测不确定性 | preregistered protocol、完整 attempt ledger、metric receipts | planned slots 全部有终态；缺失/invalid 明确计入分母 |
| P4 研究 regime | 泛化、持续学习、演化等 stage DAG | stage activation、state lineage、cell plan/ledger、holdout release | 每个 stage 可独立重放；sealed holdout 证据绑定 membership bytes |
| P5 发布 claim | 生成可引用结论 | claim report、statistics receipt、verification log | 五个 validity gates 全部 positive；否则只发布 exploratory |

## 每次实验的固定流程

1. 只修改 registry 或 experiment TOML，先运行 `bmp-compile`，保存输出的
   manifest digest。修改后不得复用旧 record root。
   执行前使用 bundle-aware preflight（必须由当前 lease holder 授权）：
   `uv run --frozen bmp-collab preflight <experiment-id> --actor <lease-holder> --dry-run`。
   它会 fail closed 检查主 issue 状态、依赖、必需环境变量、execution profile、
   registry lock、编译 manifest、`compileall` 和 patch whitespace；底层 shell
   检查也要求显式的 `BMP_LAB_ACTOR` 与 `BMP_LAB_ISSUE_ID` 绑定。
2. 用新的 UTC 目录运行 `bmp-run`。记录 root 不得放入凭据、prompt secret
   或未授权的工作区文件。
3. 用 `bmp-verify-report`（以及报告中声明的 verifier）从磁盘 bytes 重算
   digest、attempt、metric 和 gate。验证失败即停止发布。
4. 检查 `purpose`、`claim_blockers`、failure taxonomy、planned denominator
   和 holdout 状态。只有 `purpose=claim` 且 gates 全部有效才可写入论文/榜单。
5. 运行仓库级审计并精确 staging：

   ```bash
   uv run --extra test pytest -q
   uv run python -m compileall -q MagentaBench plugins tests
   bash scripts/audit_hcp_boundary.sh
   git diff --check
   git status --short
   ```

## 实验矩阵（第一轮）

第一轮只做能在当前环境完成的最小闭环：

- **Conformance**：`fake-sweep.toml`、`deterministic-evolution-smoke.toml`，
  验证 schema/compiler/pipeline/standalone replay。
- **Terminal-Bench probe**：`terminal-bench-regex-smoke.toml`，只验证镜像、
  loader、官方 verifier 接触和失败分类；先把 `uv`/`uvx` 固定到镜像后再跑。
- **真实模型**：在 provider activation receipt、token/cost usage 和网络隔离
  可观测前保持 disabled。schema 声明不等于 activation 证据。
- **Regime/research metrics**：先用 fake stage fixtures 做 contract tests；
  外部指标、跨 stage state carry 和 sealed holdout 未完成前不进入 claim。

## 服务器中断恢复

恢复时先检查 `git status --short`、`git log -1`、`uv.lock` 和 record root。
不要删除未知目录；把无法验证的旧产物标为 retroactive/negative example。
重新执行 P0，再从最后一个有完整 manifest digest 的阶段开始。每个实验
使用独立的 record root，并把命令、UTC 时间和 commit 写入交接记录。

## 发布与远端

canonical remote：`origin` 指向 GitHub；受限网络 fetch 使用
`mirror=https://gitclone.com/github.com/Minions-Land/MagentaBenchmark.git`。
镜像不提供写权限。推送前确认 `git diff --check`、无秘密文件和目标分支，
再使用已认证的 `git push origin <branch>`。推送失败时保留本地 commit 和
准确错误信息，不改写历史或强推。
