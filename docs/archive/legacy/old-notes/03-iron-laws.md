# 03 · 开发铁律与派生原则

这是本项目的协议本体。写任何代码前读这一份。

## 三条铁律（用户设定，不可协商）

### 铁律 1 · 不要重新发明已存在的工具

Harbor 执行任务，benchmark 自带 verifier，Docker 管隔离。MagentaBench 负责的是**测量与归因**，不是执行引擎。

### 铁律 2 · 不为从未存在过的形状写向后兼容

没有历史用户，没有已部署的旧格式。任何 `if "old_key" in data` 都是凭空发明的兼容负担。

### 铁律 3 · 不做任何掩盖契约不匹配的 fallback

无法识别的数据必须**响亮失败**。这一条在实践中最常被违反，因为违反它的代码看起来非常合理：

```python
# 违反铁律 3 —— 且看起来很稳健
score = evidence.metrics.get(key, evidence.score)
```

这行代码在权威 metric 缺失时静默退回到别的东西。于是"verifier 没有产出我们要的指标"这个事实**被抹掉了**，报告照常生成。正确写法是缺失即抛错。

## 派生原则（从实际缺陷中长出来的）

以下每一条都对应至少一个真实缺陷，不是抽象偏好。

### 1 · 缺证据就关门（fail closed on absence）

这条原则在本项目中**独立出现了六次**，现已确认为显式设计承诺：

| 场景 | 关门行为 |
| --- | --- |
| 网络隔离无法观测 | isolation 门失败，**不**合成探针 |
| 真实实验统计未实现 | `claim_eligible` 为假（`gates.py` 统计分支的 `else`） |
| task 内容未分类 | 拒绝，**不**静默扣留 |
| task 布局未枚举 | 拒绝 |
| 完成顺序无法验证 | 编译期拒绝（`parallelism > 1` 时禁用 `observed_case_order`） |
| adapter 元组未注册 | 拒绝，**没有** Fake 兜底 |

最后一项曾是最危险的：Pipeline 在找不到 backend 时实例化 FakeBackend，于是系统"看起来能跑"，实际什么真的都没跑。

**推论**：`if not x: skip` 是本项目最常见的陷阱形状。空集合上的循环会让"检查了一切"退化为"检查了零个"，而检查照样报成功。正确形状是 `if not x: raise`。

### 2 · 身份必须推导，绝不接受

调用方提供的任何用于判定自身证据的值都是缺陷。真实案例：

- `evaluate_run_report` 曾接受调用方传入的期望值，用来判定调用方自己的证据
- `expected_run_count` 曾是参数。现在**从 run-ID 集合推导** —— 调用方无法声称有 8 个而只给出 7 个身份
- `subject_kind` 必须是从**已解析的 subject adapter** 推导出的类型化枚举，绝不由调用方填写。否则它就是第五个"调用方填写的声明"，而且恰好出现在其存在目的是防止误读的字段里

### 3 · 区分声明与观测

`network_mode='none'` 是**声明**。容器内向字面 IP 发起 TCP 连接并收到 errno 101 `ENETUNREACH` 是**观测**。前者不能证明后者。

同样地：**DNS 解析失败（`gaierror`）不是传输层拒绝的证据。** 探针契约要求，`egress_succeeded=false` 只能由字面 IP 的传输层拒绝得出；hostname 解析失败只能记录为 `resolution_failed`，不得暗示传输被拒。

### 4 · 正面证据优于配置一致

"manifest 说不联网，backend 配置也说不联网"是两个声明相互印证，不是证据。必须有一次**主动探针**并把结果作为类型化 `NetworkObservation` 记录，且该观测绑定到已解析 policy 的 digest。

### 5 · 可达性规则

**一个无法通过真实 Pipeline 运行来行使的 scope 就是 INACTIVE。** 后来扩展到**元组级** —— scope 级太粗，因为同一 scope 下有的 adapter 组合能跑、有的不能。

判定必须基于**产物证据**。本项目曾有一次错误的 steelman 论证试图重新激活 `whole_harness`，最终通过打开真实证据包被推翻：`status=no_output`、`verifier_evidence=null`、`output_refs=[]`、`provenance.executable=/usr/bin/true`。不存在完整产物可供激活。

### 6 · 结构性优于加门（structural over guarded）

三次修复把缺陷类变成**不可表达**，而不是"加检查去发现"：

| 缺陷 | 加门式修法（未采用） | 结构性修法（已采用） |
| --- | --- | --- |
| 调用方声称的 run 数与实际不符 | 检查 count 是否等于 len | `expected_run_count` **从** run-ID 集合推导 |
| activation receipt 为未发生的事背书 | 事后校验 receipt | receipt 严格写在**真实构造之后** |
| policy 改变而身份不变 | 额外比对 policy | policy digest **折进** resolution digest |

优先做后者。加门式修法会随时间腐蚀，结构性修法不会。

### 7 · 拒绝必须在执行之前触发

一个被解析到宽松 adapter 的 deny-manifest，如果先跑完再在报告期被抓住，**forbidden 的调用已经发出去了**。门的位置和门的内容一样重要。

### 8 · exploratory 只放宽因果与统计充分性

`purpose=exploratory` **绝不**放宽：身份、完整性、评分真实性、隔离、字节血统。（boundary-guardian 底线第 9 条）

### 9 · 每个哈希都必须有一个真实校验它的地方

report 里出现的任何 digest，如果没有代码读取并校验它，它就是装饰。装饰性 digest 比没有 digest 更糟，因为它制造了完整性的外观。

**字节校验需要三个子句**：路径存在、`sha256_file` 与记录的 digest 相符、**且**反序列化出的对象与内存中的对象相等。第三条抓到过前两条漏掉的真实缺陷。

### 10 · 测量绑定到树状态

任何数字必须与产生它的树状态一同陈述。同一套测试在干净 HEAD 上是 162/162，在脏树上是 168 passed + 1 failed —— **两者都正确**，因为是不同的树。

**套件测量必须从 committed HEAD 的全新 detached worktree 运行**，并给出确切路径与命令，另跑 `git status --porcelain --ignored` 检查自包含性。原因是一个真实缺陷：`.gitignore` 里一行裸 `env/` 让 `runner/env` 整个目录未被跟踪，而 `git status --porcelain` 是空的 —— **空的 porcelain 与非自包含的 HEAD 可以共存**。

### 11 · 变异测试证明机制承载重量（"规则 c"）

一个通过的测试不能证明它测的东西是必要的。必须**逐个禁用每个前置条件，确认恰好是它自己的测试失败**。任何找到真实缺陷的探针，必须在**同一个变更内**变成永久测试。

### 12 · 测试放宽必须结构性无法产出假 claim

`allow_test_override` 会写入类型化 `TestOverrideReceipt`（进 manifest metadata 与所有 backend ProvenanceRecord），编译器强制 `exploratory`/`conformance`/`vary=()`，`evaluate_run_report` 拒绝被标记的血统。

**合成 fixture ≠ backend 伪造。** 测试里手工构造一个 `NetworkObservation` 来证明门会拒绝它，与生产路径产出一个门会接受的观测，是两个范畴。**fixture 绝不能搬进 FakeBackend。**

### 13 · Fake 必须产出自己的原生键

FakeBackend 发出 `exact_match`，**绝不**回显 benchmark 声明的 metric。回显会摧毁独立交叉检查，并让一次错误声明自我印证。

### 14 · 白名单反转优于黑名单

TB2.1 不声明 gold 路径，所以黑名单会把推断编码成完备知识。未知内容必须**拒绝为不可验证**，绝不静默扣留 —— 一个被饿死的运行与真正的 agent 失败在报告上无法区分。

### 15 · exit 0 不是产出的证据

一个 sub-agent 以 exit 0 结束、日志只有 335 字节（仅调用行）。`curl` 两次以 exit 0 返回零字节。**"没有这篇论文"与"API 返回了空"是两个不同的主张，而退出码无法区分它们。** 与把 `gaierror` 读成传输拒绝同形。

## 审计方法（唯一被证明有效的那个）

**构造应该失败的证据，检查它是否真的失败。而且必须由对方做，不能自审。**

这一条有硬证据：我（planner）仔细 review 并放行了一个完整性修复，它静默丢掉了 `run0006`；boundary-guardian 用一个探针在几分钟内抓到了。更谨慎的 review 不是答案，对抗性探针才是。

配套的常设实践：

- **(a)** 对每一道门，同时问两件事：它检查的东西对不对；**以及已记录的任何东西是否与它的结论矛盾**
- **(b)** 任何找到真实缺陷的探针，在同一变更内成为永久测试
- **(c)** 移除测试：逐个禁用前置条件，确认恰好对应的测试失败

## 测量陈述纪律

**永远不要在没有给出确切命令及其输出的情况下陈述哈希、树状态或计数。** 命令与输出必须与结论出现在同一条消息里。

这条规则的来源见 [`08-process-lessons.md`](08-process-lessons.md)：本会话中 planner 在工具后端中断期间编造了十几个不存在的 commit hash、一段"十一次提交"的叙事、以及一个被反复催促拷走的 bundle 文件。团队现已把 planner 给出的任何未经产出的数字**默认视为伪造，直到被证明为真**。
