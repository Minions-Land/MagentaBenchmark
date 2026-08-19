# Memory Baseline Report

This reporting program covers LoCoMo, LongMemEval, SpreadsheetBench,
ALFWorld, and the public-minimal AppWorld release. HotpotQA, NarrativeQA,
hidden AML data, and controlled datasets are outside this batch.

`capability-matrix.json` distinguishes coding-agent adaptations, frozen
artifact adapters, paper-native runners, service dependencies, unsupported
combinations, and blocked methods. Runnable means that an adapter can launch;
it does not imply paper equivalence or support for every benchmark.
[`BASELINE_MATRIX.md`](BASELINE_MATRIX.md) is the reviewable 48-family by
five-benchmark view of the same stable inventory. It is not a live progress
board.

Generate the complete local report from one or more independently verified
MagentaBench reports:

```bash
uv run --frozen python tools/memory_baseline_report/render_report.py \
  --report /path/to/observation_report.json \
  --output-json /path/to/memory-baselines.json \
  --output-html /path/to/memory-baselines.html
```

The renderer runs standalone report verification before reading evidence. It
unions every native verifier metric, registered BMP metric, and observed usage
field across reports. Missing, unsupported, and blocked cells remain labelled
with their reason and are never displayed as zero.

Every verified result row remains in the JSON and HTML, including failures and
rows with tool errors. `completion_eligible` is true only when the report is
protocol-valid, the attempt reached a scoring terminal status, and
`usage.tool_errors` is observed as zero. Nonzero or unobserved tool errors are
retained with explicit `completion_exclusions`; consumers must not infer
completion from score presence alone.

Real run declarations and local resource paths belong in deployment-specific
configuration outside this public repository. Keep the model, endpoint,
prompt, tools, budgets, evaluator, seed, reset, and retry policy fixed within
each comparison block. Use fresh method state and a fresh record root for every
new execution.
