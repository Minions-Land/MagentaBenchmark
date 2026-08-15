# Memory Baseline Report

This reporting program covers LoCoMo, LongMemEval, SpreadsheetBench,
ALFWorld, and the public-minimal AppWorld release. HotpotQA, NarrativeQA,
hidden AML data, and controlled datasets are outside this batch.

`capability-matrix.json` distinguishes coding-agent adaptations, frozen
artifact adapters, paper-native runners, service dependencies, unsupported
combinations, and blocked methods. Runnable means that an adapter can launch;
it does not imply paper equivalence or support for every benchmark.

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

Real run declarations and local resource paths belong in deployment-specific
configuration outside this public repository. Keep the model, endpoint,
prompt, tools, budgets, evaluator, seed, reset, and retry policy fixed within
each comparison block. Use fresh method state and a fresh record root for every
new execution.
