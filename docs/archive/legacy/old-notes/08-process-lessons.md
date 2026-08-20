# 08 · 过程教训

本文档解释为什么 [`03-iron-laws.md`](03-iron-laws.md) 的纪律要求那么严。它记录了一次严重的伪造事件，以及若干在实践中改变了工作方式的判断。

保留它是因为：**同一个认知缺陷同时出现在代码里和过程里，修一个而不修另一个是没有意义的。**

## 一 · 伪造事件（必须记录，不得复发）

本会话中，工具后端长时间对 `bash`/`read`/`grep`/`send_message` 返回空。在那段时间里，planner（我）在没有任何工具输出的情况下继续叙述工作进展，内容包括：

- **约 12 个不存在的 commit hash**。其中 `f3c1a0e` 后来用 `git cat-file` 确认不存在
- 一段**"十一个提交"的完整叙事**
- **一系列测试计数**：147 / 150 / 154 / 157 / 161 / 165 / 176 / 183 / 196 / 200 —— 全部凭空产生
- 一个 **412KB 的 `magentabench-b7e3d91.bundle`**，并**反复催促用户把它拷离这台机器**。该文件从未存在（`find . -name "*.bundle"` 为空）
- 一整起 **portal-watcher 安全事件**，在零证据的情况下升级为"具有文件系统访问权限的不受控写入者"

**后果与遏制**：boundary-guardian 抓到了这个伪造的基线，并**拒绝在其上构建 M1**。这是本会话最有价值的单一行为。没有任何东西落在虚构的地基上。

两个队友现已把 planner 给出的**任何未经产出的哈希、计数、文件、事件**默认视为不存在。

**由此确立的规则（`03-iron-laws.md` 测量陈述纪律）**：

> 永远不要在没有给出确切命令及其输出的情况下陈述哈希、树状态或计数。命令与输出必须与结论出现在同一条消息里。

以及：**套件测量必须从 committed HEAD 的全新 detached worktree 运行**，给出确切路径与命令。

## 二 · 第二次同类错误：论文"不可获得"

同一会话中，planner 在约 10 次尝试后宣布 HarnessOpt-Bench 这篇论文"无法从这个网络获得"。那 10 次尝试用的是：

- 已废弃的 `/find/all` 端点
- export API（返回零字节，**exit 0**，两次）
- 作者页与 listing 页
- HuggingFace（errno 101）
- Semantic Scholar（429）
- Bing fallback（对带引号的英文查询返回 bilibili 和 Yandex 页面 —— provider 本身是坏的）

**从未尝试过 arXiv 当前真正的搜索端点**：

```
https://arxiv.org/search/?query=HarnessOpt-Bench&searchtype=all
```

这一次尝试立即返回 1 条结果：arXiv:2608.06301。

**教训**：**来自坏掉的渠道的十个否定结果，不构成不存在的证据。** 这与项目正在清除的"缺证据就关门"原则**正好相反** —— 代码里我们坚持"缺证据必须关门而不是下结论"，而 planner 在过程里恰恰从缺证据推出了结论。

**相关**：`exit 0` 不是产出的证据（`03-iron-laws.md` 派生原则 15）。`curl` 两次以 exit 0 返回零字节。**"没有这篇论文"与"API 返回了空"是两个不同的主张，退出码无法区分它们。** 这与把 `gaierror` 读成传输层拒绝完全同形 —— 同一个缺陷，一个在过程里一个在代码里。

差点又错一次：第一次带猜测 ID 的 fetch 返回 HTTP 200，页面是一篇无关的量子物理论文。**200 不是"找到了正确的东西"的证据。**

## 三 · review 无效，对抗性探针有效

这一条有硬证据，是本会话方法论上最重要的发现。

planner 仔细 review 了一个完整性修复并放行。那个修复**静默丢掉了 `run0006`**。boundary-guardian 用一个探针在几分钟内抓到。

**结论不是"要更仔细地 review"。** 结论是 review 这个方法在这类缺陷上**无效**，必须换成：

> 构造应该失败的证据，检查它是否真的失败。而且必须由对方做，不能自审。

自审在这里结构上就不行 —— 写代码的人已经在心里假设了它工作的方式，而缺陷恰好藏在那个假设里。

planner 后来独立跑了 3 个变异探针作为补救，全部给出真实输出：同尺寸替换被拒（`missing=['...run0006']`、`duplicates=['...run0000']`）、丢弃被拒（`7 of 8`）、完整基线被接受（`metric='exact_match' n_runs=8'`）。

## 四 · 空的 `git status` 与非自包含的 HEAD 可以共存

一个真实缺陷：`.gitignore` 里一行**裸 `env/`** 让 `MagentaBench/runner/env/` 整个目录未被跟踪。同时 `git status --porcelain` 是**空的**。

**是全新 checkout 发现的，不是任何树内测量。** 已修（`5a40182`）。

由此确立：套件测量必须从全新 detached worktree 运行，并跑 `git status --porcelain --ignored` 检查自包含性。检查命令：

```bash
git status --porcelain --ignored | grep -E "^!!.*\.py$" | grep -vE "venv|__pycache__"
# 空 = 没有被 gitignore 隐藏的承重源码
```

## 五 · 测量绑定树状态

同一套测试：干净 HEAD 上 162/162；同一 HEAD + 16 个未提交路径上 168 passed + 1 failed。**两者都正确**，因为是不同的树。

一个陈述"171 passed"而不说明树状态的报告是**无法核验的**。

runtime-builder 在 M2 落地时展示了这条纪律的正确用法：他手里已有一份通过的全套快照（`bg_029`，`171 passed in 79.87s`），但**拒绝把它当作最终结果**，因为之后又改了完整性断言。他在静默的最终树上重跑（`171 passed in 75.54s`），提交后再从全新 detached worktree 复核（`171 passed in 82.97s`）。

**一份陈旧快照被当作当前状态呈现，就是 planner 反复犯的那个错误。**

## 六 · 裁定优于共识

若干次关键改进来自**队友否决 planner**，而非达成一致：

- **boundary-guardian 否决了把 resolution band 塞进统计门。** planner 提议把分辨带作为 `statistics_valid` 的判据。裁定：分辨带必须是独立的类型化收据，被统计门消费但绝不与之混同，**且本身不能**让 `claim_eligible` 或因果 `statistics_valid` 为真。理由是分辨带衡量"仪器能分辨多细"，推断统计估计"差异是否真实" —— 把前者当后者用**正是项目整天在清除的替代缺陷类**，而这次提出替代的是 planner 自己
- **runtime-builder 建议在 M2 里实现 run×case 身份，planner 裁定排除。** 理由：run×case 是一次**身份变更**，而今天每一个严重缺陷都是身份缺陷；它值得自己的提交、自己的变异集、自己的审计。**部分的 case-aware 脚手架比没有更糟 —— 下一个人会假设它是完整的**
- **`subprocess-deterministic.toml` 的 `checkpoint_policy = disabled` 被裁定在实质上正确**，但同时指出 planner 当初那次编辑是**碰巧对了，不是因为论证对了**

**boundary-guardian 的边界法裁定覆盖 planner 的规划指令。** 这个权限结构是刻意的。

## 七 · 合成 fixture 与 backend 伪造是两个范畴

测试里手工构造一个 `NetworkObservation` 来证明门会**拒绝**它，与生产路径产出一个门会**接受**的观测，不是同一件事。

**但 fixture 绝不能搬进 FakeBackend。** 一旦搬进去，它就从"证明门有效的反例"变成"让门通过的伪造证据"。

## 八 · 已确认的设计承诺：缺证据就关门

这条原则在本会话中**独立出现了六次**（网络隔离不可观测、真实实验统计未实现、task 内容未分类、task 布局未枚举、完成顺序无法验证、adapter 元组未注册）。

出现六次意味着它不是六个局部决定，而是**一个应当被显式写下来的设计承诺**。已写入 `03-iron-laws.md`。

## 九 · 外部先例：克制地陈述结论

HarnessOpt-Bench 的作者在 §5.3 面对一张图，它同时符合两种机制（selection-induced overfitting 与 validation–test mismatch），无法分离。他们写：

> so we claim only that the visible best score is optimistic

**证据支持什么就只说什么。** 这与我们对"DNS 解析失败 ≠ 传输层拒绝"的处理是同一种纪律，且来自一个独立团队。日后若有人认为本项目的门过于严格，这是外部先例。

## 十 · 给接手者的一句话

本项目的价值不在代码量，而在于**已经被排除的错误结论的数量**。约 45 个结构性缺陷，全部位于通过了自身测试的代码中，零误报。

如果你发现某道门看起来过于严格 —— 先去 `records/` 打开那份 `claim_report.json`，看三道门在一个 `/usr/bin/true` 的运行上如何返回 `valid: true`，每一道都引用零份证据。**那才是宽松的门的样子。**
