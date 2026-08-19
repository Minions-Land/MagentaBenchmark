# Handoff: <WORK_PACKAGE_ID> / <DELIVERABLE_ID>

> 这份文件应能直接作为 coding agent 的启动提示；不要要求 agent 猜路径、模型、seed 或资源。所有尖括号字段都必须由 owner 按本项目实际入口替换，不能把未映射的示例命令直接交给 worker。

## 角色与边界

- Role: `WORKER`
- Work package ID / DRI / reviewer:
- Authorized root: `<PROJECT_ROOT>`
- Shared code/environment: read-only
- Worker write root: `<RUN_ROOT>/<WORK_PACKAGE_ID>/`
- Question channel: `<OWNER_CHANNEL>`

## 当前结论与下一动作

结论：<当前 verified/unverified 状态>

下一动作：<一条可复制命令，由 worker 执行>

## 必读

1. `<PROJECT_CURRENT_ROUTE>`
2. `<WORK_PACKAGE_CONTRACT>`
3. `<PROJECT_RULES_DOC>`（项目存在时）
4. 本 handoff

## Owner 已准备好的 infra

```bash
<PROJECT_NATIVE_PREFLIGHT_COMMAND>
```

这里必须填写 owner 已验证过的项目原生命令或适配命令；skill 不要求存在特定脚本名。失败就停止并回传：命令、首个错误、manifest 路径；不要 `pip install`、`uv sync`、换模型、换 seed 或改共享代码。

## 执行入口

```bash
RUN_ID=<FROZEN_RUN_ID>
RUN_ROOT=<PROJECT_ROOT>/runs/$RUN_ID/<WORK_PACKAGE_ID>
<EXACT_COMMAND>
```

先写 command/provenance，再运行。输出使用新的、此前不存在的路径；禁止 `--resume` 和覆盖。

## 验收与交付

1. 通过 identity/schema/unit/smoke/资源哨兵；
2. 对账 expected、actual、unique、missing、duplicate、error 和 terminal state；
3. 生成 contract 指定的 `<PROJECT_ROOT>/deliverables/<RECEIPT_NAME>.md` 与 `.sha256`；
4. 状态置为 `DELIVERED`，回传 verified、not verified、首个失败点、证据路径、限制和下一动作。

## 禁止事项

- 不删失败结果、不挑 seed、不为分数重跑；
- 不把未完整运行称为 score；
- 不复制密钥、私有 prompt、raw trace 到 receipt；
- 不停止未知进程；
- 不修改 frozen contract。任何范围变化先 `BLOCKED` 并请求 owner decision。
