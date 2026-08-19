# Human Alignment / Project Intake

在创建 infra 或 work package 前，由人类与 owner coding agent 共同确认。先从当前对话和已有材料提取已经给出的内容；不要要求人类重复填写。只把会改变范围、资源、协议或验收的真实缺口列为问题。不适用的字段写 `N/A + 原因`，不能静默猜默认值。

```markdown
# PROJECT_CHARTER <VERSION>

## Human-approved objective
- Goal:
- Non-goals:
- Why now / intended decision:

## Project objects
- Methods/arms/components to build or run:
- Inputs/datasets/benchmarks:
- Required comparisons:
- Known official implementations or sources:

## Experiment choices
- Model/evaluator/simulator:
- Task/split/order/repetition semantics:
- Seeds/budget/threshold/metrics/statistics:
- Fairness/parity rules:
- Allowed adaptations and prohibited changes:

## Authority and resources
- Authorized project/write roots:
- Human owner and decision channel:
- Credentials/data/model access mechanism:
- CPU/GPU/API/storage/concurrency limits:
- Deletion, retry, resume and retention policy:

## Deliverables and review
- PATH_SCHEMA / RESULT_SCHEMA / UNIQUE_KEY:
- Receipt and handoff names:
- Worker/reviewer assignment policy:
- Acceptance gates and stop rules:

## Human confirmation
- Alignment evidence (conversation/document/decision locator):
- Confirmed version/date:
- Confirmed decisions:
- Open decisions that remain BLOCKED:
```

Agent 可以建议选项和风险，但不得把未确认的科学/资源选择伪装成项目事实。若人类已经在同一任务中明确给出答案，记录该答案就是确认，不再制造额外表单门槛。
