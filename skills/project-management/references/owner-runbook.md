# Owner Runbook

## A. 建立基础设施

在 worker 进入项目前，owner 依次完成：

```text
[ ] authorized project/output root 与所有权
[ ] source/data/adapter/依赖 revision 与 dirty state
[ ] 固定环境、模型/API 注入、proxy/no-proxy、GPU/CPU、磁盘下限
[ ] job slot、endpoint quota、GPU occupancy、retry/timeout 上限
[ ] 共享源码树 fingerprint（pre）
[ ] unit、health、owner smoke
[ ] ENVIRONMENT_MANIFEST.json + READY marker + manifest hash
```

manifest 只记录环境变量名和能力标识，不记录密钥值。若 infra 发生变化，撤销 READY，创建 successor contract 和新 Run ID。

bundled script、目录树和命令仅是参考适配器。优先识别项目原生工具，在 `READY` 前按人类已确认的实验上下文调整；冻结的是本项目最终生效的协议和证据接口，不是脚本模板本身。`READY` 后只有会改变实验或证据语义的调整才触发 successor；无语义的文档/展示变更按普通 revision 管理。

## B. 分发

每个 `HANDOFF.md` 必须包含：角色、WP ID、source/contract identity、in-scope/out-of-scope、输入、精确命令、写根、资源上限、预期产物、验收、stop rules、问题频道和回传格式。worker 不需要寻找 owner 的聊天上下文即可开始。

## C. 监控

owner 使用固定间隔的只读检查。下面的 `ps`/`df` 是 Unix-like 环境示例，不是固定工具；实际命令应按本项目的操作系统、scheduler、服务接口和合同调整：

- `ps -eo pid,ppid,etime,args`：匹配完整命令和 WP 归属；
- 稳定 `STATUS.md`/heartbeat：阶段、完成数、最后成功 cell、错误数；
- `df`/GPU/API quota：不越过 contract 的 floor/concurrency；
- 结果目录：只看新增文件和计数，不修改作业。

未知 SSH 状态先查询 durable job/status；只有确认完整命令、PID、父子关系和所有权后才能停止。

## D. 收集与 review

worker `DELIVERED` 后，owner 先运行 layout/receipt/bundle verifier，再按仓库政策请求 review。review 的必改项必须有 ID、owner、due date 和 acceptance check。advisory review 不等于最终批准；只有仓库指定的 accountable reviewer 接受后，包才可以进入集成报告。在 MagentaBenchmark 中该最终 reviewer 仅为 `PoorOtterBob`。
