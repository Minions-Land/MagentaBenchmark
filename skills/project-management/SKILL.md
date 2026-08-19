---
name: project-management
description: 端到端管理研究、benchmark 和多智能体 coding-agent 项目。用于项目 owner 建立基础设施、冻结实验合同、生成工作包、分发执行者、监控资源与进度、收集严格格式回执、组织 review/feedback、交接和收尾；也用于执行者拿着 handoff 在已准备好的服务器上运行自己的工作包。包含边界、资源、完整性、parity、provenance 和 claim 哨兵。
---

# Project Management

把一次对话变成可恢复、可审查、可并行的项目系统。默认分为 `OWNER`、`WORKER`、`ADVISORY_REVIEWER` 三种角色；先识别角色，再读取对应参考资料。仓库自己的权限、状态机和最终审核规则优先；在 MagentaBenchmark 中，`bmp-lab` 和 GitHub Issue/PR 是共享事实来源，`PoorOtterBob` 是唯一 accountable final reviewer。

## 1. 先定项目边界

1. 把用户授权的目录固定为 `<PROJECT_ROOT>`，所有 run 和交付物必须落在其内。
2. 读取 `AGENTS.md`、`README.md`、当前路由、active contract 和最新 handoff；旧聊天、旧 status、`agent.md` 不能覆盖新鲜证据。
3. 记录源码/数据/依赖身份、dirty state、owner、写边界、磁盘下限、资源配额和问题频道。
4. 不复制或打印 `.env`、token、私钥、原始对话、个人数据或不必要的私有绝对路径。

## 2. 先完成人类对齐，再进入管理

项目默认由人类与 owner coding agent 共同初始化。人类先通过对话、现有文档或其他上下文提供或确认科学目标、要跑的对象、对照、实验细节、资源授权、风险偏好和验收口径；agent 负责检查当前事实、暴露缺口、提出选项并把已获得的上下文写成 `PROJECT_CHARTER` 和 active contract。已经清楚给出的信息直接落盘，不重复盘问；只追问会实质改变范围、资源或验收的缺口。

当关键科学选择、资源权限或验收阈值缺失时，保持 `HUMAN_ALIGNMENT`/`DRAFT`，列出需要人类回答的精确问题。不要替人类猜测方法、模型、数据、seed、预算、指标或结论门槛。只有现有上下文已经构成明确的人类确认，或人类补充确认 charter/contract 后，才进入 infra preparation、work-package 分发和正式监控。使用 `references/project-intake-template.md` 固化这次对齐。

## 3. OWNER 模式：一次性搭好，之后分发

项目 owner 必须在 worker 上线前完成以下动作；worker 不安装、不升级、不猜测性修复基础设施：

1. **读取人类已确认的 charter**：把目标、非目标、方法、实验协议、授权和未决项绑定到同一版本；未确认项不能静默转成默认值。
2. **建立 infra**：准备锁定环境、依赖、数据缓存、模型/API 注入、GPU/CPU/存储配额和 proxy/no-proxy 规则；写 `infra/ENVIRONMENT_MANIFEST.json`，健康检查通过后写唯一 `infra/READY`。
3. **冻结 contract**：写明 benchmark/dataset revision、任务/拆分/顺序、模型、温度、timeout、retry、seed、预算、阈值、分母、并发、重跑政策、输出命名和 stop rules。缺一项就保持 `BLOCKED`。
4. **生成 work package**：每包有一个 DRI、一个 accountable owner、仓库政策允许的 review route、输入/输出接口、预计耗时、命令、资源上限和完成条件。把 `HANDOFF.md` 写成 coding agent 可直接执行的指令。
5. **预跑 owner smoke**：用与 worker 完全相同的入口跑最小代表样例，保存命令、版本、日志和产物 hash；不要把 smoke 数字当正式结果。
6. **分发**：给每个 worker 独占的 `<RUN_ROOT>/<WORK_PACKAGE_ID>/` 和 contract 定义的唯一输出身份。共享源码、环境、合同和结果目录在运行期间只读。
7. **监控**：按固定间隔读取稳定的 `STATUS.md`/heartbeat、进程完整命令、完成计数、错误率、磁盘、GPU/API 配额；状态检查不得修改作业。SSH 断开后先查询 durable 状态，不能盲目重启。
8. **收集与 review**：只接收 `DELIVERED` 包；验证 receipt、parity、唯一 cell、hash 和负结果，再按仓库政策请求 review。reviewer 记录事实、证据、必改项、owner、截止时间和验收条件；advisory review 不得冒充最终批准。
9. **收尾/交接**：生成集成 receipt、当前路由、决策 memo、未验证边界和下一步唯一动作；旧路线归档并指向 successor。

## 4. WORKER 模式：拿 handoff 即可开工

worker 上线后的固定顺序：


1. 只读执行 `check_project --agent` 或等价 preflight；看到 `infra/READY` 缺失、manifest/hash 不匹配、资源不足或合同过期，立即交回 owner。
2. 阅读自己的 `HANDOFF.md`，确认 `WP_ID`、`RUN_ID`、写根、命令、预期产物和禁止事项；不要读取无关 worker 的私有上下文。
3. 通过 identity/schema/unit/smoke 门后，才启动 qualification/full run。每次实质性变化新建 Run ID；只有 frozen contract 明确允许、durable state 身份一致且先查询当前 job/run 后才可 resume。禁止用 resume 填补看过结果后的缺口，也禁止挑 seed、删结果或为好看分数重跑。
4. 只写自己的 work-package 状态、日志、provenance、结果和 receipt；共享代码需要修复时先 `BLOCKED`，由 owner 决策并创建隔离分支/新合同。
5. 运行结束先对账完整性，再写 contract 指定的 `<RECEIPT_NAME>.md` 和同名 `.sha256`；原始结果、日志、命令和 hash 不粘贴进长文档，只链接路径。
6. 将状态置为 `DELIVERED`，给 owner 一个短 handoff：结论、已验证、未验证、首个失败点、恢复命令、证据路径和下一动作。

## 5. ADVISORY_REVIEWER 模式

尽可能由未实现该变更的人先检查证据再评价算法，但是否要求角色分离由当前仓库政策决定。advisory reviewer 只能报告 findings；最终批准必须来自仓库指定的 accountable reviewer：

- contract/effective config 是否一致；
- source、依赖、数据、模型和适配 commit 是否可追溯；
- expected/actual/unique/missing/duplicate/error 是否对账；
- parity、mechanism fingerprint、injection/更新事件是否存在；
- 分子/分母、阈值、成本拆分和未运行项是否明确；
- receipt、相邻路径和 SHA256 是否可读且未泄漏秘密。

只能在 required sentinels 全部通过后把 evidence class 标成 `reproduced`；否则使用 `incomplete`、`invalid`、`infrastructure-failure` 或 `external-declaration`。`not-run`、`verifier-failure` 和 `algorithmic-failure` 是单独的运行状态/原因，不能冒充 evidence class；有效且验证完成的算法失败仍可是 `complete/reproduced`。

## 6. 哨兵模型（硬门，fail closed）

按下列顺序执行。任一硬哨兵失败，停止后续昂贵运行并记录首个失败点。

| 哨兵 | 检查 | 最低证据 |
| --- | --- | --- |
| Boundary/Privacy | 写入、密钥、源码、进程和删除边界 | authorized root、secret scan、owner |
| Identity | 实际 source/data/model/dependency/config/seed | commit、dirty state、manifest hash |
| Resource | CPU/GPU/API/磁盘/并发/配额 | owner manifest、job/PID、capacity snapshot |
| Schema/Interface | adapter、输入输出字段、命令和路径 | parser/unit/contract test |
| Smoke | 一例真实执行和 terminal state | 结构化结果、stdout/stderr、exit class |
| Parity/Mechanism | 固定模型、参数、顺序、重试及方法事件 | parity table、fingerprint/instrument |
| Completeness | 预期槽、唯一 key、缺失、重复、错误和终态 | verifier 输出、计数表 |
| Provenance | 命令、版本、输入 hash、PID、重试、产物 hash | `provenance/`、`SHA256SUMS.txt` |
| Review/Claim | accountable review 和负边界 | review record、claim class、next decision |

进程退出码 0 只证明进程结束，不证明任何其他哨兵。

## 7. 路径与格式合同

以下是推荐路由，不是跨项目写死的目录。owner 应在 `HUMAN_ALIGNMENT` 阶段根据现有仓库、框架和人类上下文建立映射，并在 active contract 中声明本项目实际采用的路径：

```text
<PROJECT_ROOT>/
├── infra/ENVIRONMENT_MANIFEST.json
├── infra/READY
├── work-packages/<WORK_PACKAGE_ID>/{CONTRACT.md,HANDOFF.md,STATUS.md}
├── runs/<RUN_ID>/<WORK_PACKAGE_ID>/{logs,provenance,results,STATUS.md,SHA256SUMS.txt}
├── deliverables/<RECEIPT_NAME>.md
├── deliverables/<RECEIPT_NAME>.sha256
└── reviews/<RUN_ID>-<WORK_PACKAGE_ID>-review.md
```

项目先在 contract 中声明 `PATH_SCHEMA` 和 `RESULT_SCHEMA`，再创建任何输出。需要多 domain/多 trial 时可采用 `<DOMAIN>_<METHOD>_k<K>/results.json`，但这只是模板变量，不是全局固定格式。不同逻辑单元是否拆分文件也由 schema 冻结。所有结果文件使用 exclusive create，不能覆盖。

### 脚本与模板是参考适配器

- 脚本不是项目管理能力本体。真正的硬约束是本项目已经确认的角色、边界、合同、门禁、证据和验收语义；脚本可以不存在、被替换或只采用其中一部分。
- 不假设任何项目都存在同名 `check_project.py`、runner、目录或状态文件；先复用项目原生入口，再按需改写或替换 bundled script。
- 不要为了套用示例脚本而重构一个已有项目。先把示例中的变量、路径、schema 和状态映射到本项目；无法证明等价时，保留 `DRAFT/BLOCKED`，而不是直接运行示例命令。
- `READY` 前，owner 可以正常调整脚本、命令、目录和 verifier，以适配已经对齐的人类需求；把最终有效接口、测试和证据记录进本项目 contract/manifest 即可。
- `READY` 或正式 run 开始后，只在变更会影响实验协议、结果语义、完整性判定或证据可复现性时撤销 READY、建立 successor 和新 Run ID。纯文档措辞、展示格式或无语义监控调整不机械触发新实验版本。
- 通用 skill 只携带保守的参考脚本和模板，不把某个项目的算法、绝对路径、命令或结果 schema 复制成全局规则。

## 8. Receipt 最低字段

每个工作包按 contract 定义的 receipt 名称交付一份短 receipt（详细模板见 `references/receipt-schema.md`）：

- `state`、一行结论和 evidence class；
- benchmark/data split/task order、模型/模拟器、temperature、timeout、retry、seed、预算、阈值；
- 分子/分母、expected/actual/unique/missing/duplicate/error；
- parity、机制/注入/更新 fingerprint；
- source/adapter/依赖 commit 与参数来源；
- 任务推理成本与记忆构建/维护成本分开；
- 原始结果、日志、命令、provenance、hash 的相对/授权路径；
- retries/deviations、未运行检查、限制、owner 和唯一下一动作。

完整性必须先于指标；没有完整分母不得发布百分比、排名或因果结论。

## 9. 状态与反馈

使用 `HUMAN_ALIGNMENT -> DRAFT -> READY -> IN_PROGRESS -> INTERNAL_REVIEW -> DELIVERED -> EXTERNAL_REVIEW -> ACCEPTED -> CLOSED`；任意开放态可转 `BLOCKED/CANCELLED`。scope/resource 变化是新 decision，不是静默编辑旧合同。每次 revision 指向 predecessor 和触发它的 feedback。

## 10. 子 skill 路由

把 `project-management` 视为整套项目管理能力的总入口。只路由到本仓库实际提交的 skill 和权威文档，不假设缺失的 sibling skill：

| 需要 | 读取 |
| --- | --- |
| MagentaBenchmark 权限、Issue/PR、lease、handoff | [`../../docs/GITHUB_DEVELOPMENT.md`](../../docs/GITHUB_DEVELOPMENT.md) |
| 项目边界、生命周期、work package | 本 skill 与 `references/owner-runbook.md` |
| owner infra、资源和秘密 | `../experiment-infrastructure/SKILL.md` |
| 长跑、监控、receipt | `../benchmark-operations/SKILL.md` |
| 完整性哨兵/claim | `../experiment-integrity/SKILL.md` |
| 当前实验与状态 | MagentaBenchmark `experiments/`、`bmp-lab` 和 generated ledger |

需要具体格式时直接读取本 skill 的 references；不要一次性加载整个技能集合。
