# External Baseline Pattern

外部方法 baseline 采用“宽容但不失真”的合同：

1. 使用官方实现并记录 commit SHA；必要适配逐条列出，无法忠实运行就 `BLOCKED`，不交削弱版。
2. 下游模型、模拟器、temperature、timeout、retry、seed、task order、k 和阈值与主表逐字对齐；缺失默认值时引用官方 example/config，不能看结果后调参。
3. 离线构建方法只用预注册 train split；在线方法保持原生更新方式但固定 task 顺序，并显式披露测试期自适应。
4. embedding/检索模型统一；任务推理 token 与记忆构建/维护 token 分开统计。
5. 先完成 domain A 并通过 parity/completeness，再启动 domain B；只有预注册的 durable resume policy 才能恢复同一 Run ID，不得用 resume 填补结果缺口、挑 seed 或为好看数字重跑。
6. 每个方法交付 contract 指定的 receipt + `.sha256`。`<DOMAIN>_<METHOD>_k<K>` 和每域独立 `results.json` 是可选模板；只有项目 contract 明确选择时才使用。
7. Receipt 必须写 numerator/denominator、task-trial unique cells、缺失/重复 reward、parity、机制 instrumentation、官方 commit、适配改动、绝对/授权路径和失败原因。

这些规则来自外部 baseline handoff 的可迁移流程；原文中的主机、账号、私有路径和具体模型值不能复制进通用 skill。
