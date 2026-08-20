# 06 · 缺陷分类与审计方法

本会话在 Phase 3b 审计中发现约 45 个结构性缺陷，**零误报**，且**全部位于已通过自身测试的代码中**。这一点值得反复强调：测试通过不构成正确性证据。

本文档给出缺陷的四个类别（用于识别新缺陷）与唯一被证明有效的审计方法。

## 四类结构性缺陷

### 类别 1 · 声明回显（declaration echo）

**形状**：一个字段进入了 identity digest，但没有任何 adapter 读它。

**为什么危险**：digest 制造了完整性的外观。字段变了 digest 变了，看起来"被追踪"，但改动这个字段**对实际行为毫无影响**。于是两个行为完全相同的运行有不同身份，或者更糟 —— 有人以为改了它就改了行为。

**真实案例**：
- `network_mode` 进 digest，无 adapter 读取
- backend defaults 里的 `budget`
- 未加门的 `deterministic_conformance`

**检测方法**：对 report 中每一个 digest 与每一个进入身份的字段，问"哪一行代码读它并据此改变行为"。找不到就是装饰。

### 类别 2 · 行为别名（behavioral aliasing）

**形状**：两个不同的枚举值产生**完全相同的行为**。

**为什么危险**：manifest 声称的方法学与实际执行的方法学不同，而没有任何地方会报错。声明 `candidate_selection=exact` 与声明 `single` 得到同一个结果，于是"我们用的是 exact 选择"这句话是空的。

**真实案例**：
- `candidate_selection` 的 `single` 与 `exact` 都选 attempt 0
- `state_reset` 即使 hook 为 `None` 也照样递增计数器，于是"状态已重置"的计数在什么都没重置时也增长

**检测方法**：对每个枚举，为每个值构造一次运行，断言它们的**可观测行为不同**。若两个值无法通过行为区分，则其中一个是假的。

### 类别 3 · 机制替换（mechanism substitution）

**形状**：调用方传入的值顶替了 manifest 钉住的东西。

**为什么最危险**：这类缺陷让系统**看起来在工作**。

**真实案例**：
- `Pipeline(backend=...)` 无论 manifest 声明什么都实例化 FakeBackend —— 系统"能跑"，实际什么真的都没跑
- `evaluate_run_report` 让调用方提供用来判定**自己证据**的期望值
- `expected_run_count` 曾是参数，调用方可以声称 8 而只给 7 个身份
- `ManifestCompiler` 的并行简化版 `build_resolved_manifest`：零调用方，但绕过全部 `_ACTIVE_SCOPES` 与 tuple 门（**已删除**）

**检测方法**：对每个"被解析的东西"，问"调用方能不能绕过解析直接提供它"。能就是缺陷。特别注意任何接受 `Optional` 依赖注入并带默认值的构造函数。

### 类别 4 · 本地路径进入内容身份

**形状**：相同的字节在不同 checkout 根下算出不同 digest。

**为什么危险**：内容寻址的全部意义是"相同内容 → 相同 id"。混入绝对路径后，同一份证据在另一台机器上身份不同，跨机复现与去重都失效。

**检测方法**：在两个不同路径下 checkout 同一 commit，比较所有 digest。

## 十八项替换清单（6 层，已穷尽）

审计按层系统扫描，每层问"这一层有什么可以被调用方顶替"：

1. **compiler** —— scope 门、tuple 门、registry 解析
2. **execution** —— backend 实例化、workspace 构建、provenance 记录
3. **scheduler / parser** —— 预算分配、attempt 选择、Harbor 解析
4. **evaluation / loader** —— 门的输入、期望值来源、case-set 加载
5. **parallel compiler surface** —— 存在第二条编译路径（已删除）

穷尽的判据不是"找不到更多了"，而是**每一层都被明确检查过并给出结论**。

## 反复出现的陷阱：空即跳过（empty-means-skip）

这是本项目最常见的单一缺陷形状，出现了至少三次。

**形状**：

```python
for item in items:
    if not item.metrics:
        continue          # ← 缺陷
    check(item)
```

或者更隐蔽的版本 —— 循环本身就在空集合上：

```python
for item in [i for i in items if i.execution_valid]:
    verify_scoring(item)
# 如果所有 item 都是 no_output，这个循环跑零次，然后报"全部通过"
```

**真实案例（最严重的那个）**：`scoring_valid` 断言"每个可验证产出都有精确 verifier 证据"，而在 `no_output` 的运行上这个集合是**空的**。于是门在 `output_refs=[]` 上返回 `valid=True`，reason 写着"every verifiable output has exact-verifier evidence"。产物见 `07-records-guide.md`。

**正确形状**：`if not x: raise`。以及给门加**计划完整性**要求：`len(items) != expected_run_count` 直接拒绝，且 reason 字符串里带**正面证明的计数**（"验证了 8 个中的 8 个"，而不是"没有发现问题"）。

## 唯一被证明有效的审计方法

**构造应该失败的证据，检查它是否真的失败。而且必须由对方做，不能自审。**

这有硬证据。我（planner）仔细 review 并放行了一个完整性修复，它**静默丢掉了 `run0006`**。boundary-guardian 用一个探针在几分钟内抓到了。结论不是"要更仔细地 review"，而是**review 这个方法本身在这类缺陷上无效**，必须换成对抗性探针。

### 三条常设实践

- **(a) 双问法**：对每一道门，同时问两件事 —— 它检查的东西对不对；**以及已记录的任何东西是否与它的结论矛盾**。第二问抓到了 `scoring_valid` 的空集空过：门说"全部有 verifier 证据"，而同目录的 `evidence_bundle.json` 里 `verifier_evidence=null`。
- **(b) 探针转测试**：任何找到真实缺陷的探针，在**同一个变更内**成为永久测试。`test_gate_vacuity.py`（`beadc49`）与 `test_adapter_registry.py`（`db9a171`，9 变异）都是这么来的。
- **(c) 移除测试**：逐个禁用每个前置条件，确认**恰好是它自己的测试失败**。若禁用某个前置条件后没有测试失败，那个前置条件没有承重；若失败的是别人的测试，说明测试与它要保护的性质错配。

### 变异测试的具体标准

一个变异测试要有意义，必须满足：

1. 变异是**最小的** —— 只禁用一个前置条件
2. 断言的是**恰好一个**指定测试失败
3. 变异后的证据在其他方面**完全合法** —— 否则失败可能来自别的原因

反例：把整个 `NetworkObservation` 删掉会让多个门失败，这不能证明 isolation 门在检查观测的**内容**。

## 已完成的追溯性失效清扫

**`records/` 下的每一个产物现在都通不过当前的门。** 这是有意为之，是对门本身的反证。AOSE 的 `claim_report` 在**三个独立理由**上失败：

1. 缺 `NetworkObservation`
2. 空集 scoring 空过
3. metric 被标为 `exact_match` 而 benchmark 声明的是 `overall`

产物被**原样保留、不做修改**，作为反证证据。若有人"修复"这些产物使其通过新门，就毁掉了这个检验。

## 给接手者的检查清单

改动任何门或身份逻辑时，逐项确认：

- [ ] 新增的每个字段，是否有代码读它并据此改变行为？（类别 1）
- [ ] 新增枚举的每个值，行为是否可区分？（类别 2）
- [ ] 是否存在调用方绕过解析直接提供该值的路径？（类别 3）
- [ ] digest 是否只依赖内容，不依赖本地路径？（类别 4）
- [ ] 循环是否可能在空集合上跑零次并报成功？（空即跳过）
- [ ] 缺失时是 `raise` 还是 `continue`？
- [ ] 是否有一个变异测试证明这个前置条件承重？（规则 c）
- [ ] 拒绝是在**执行之前**还是报告期触发？（执行后才抓到，forbidden 调用已经发出）
- [ ] 这个修复是**结构性**的（缺陷不可表达）还是**加门式**的（缺陷被检查）？优先前者
