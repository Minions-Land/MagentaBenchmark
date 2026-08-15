# Native Benchmark Adapter

This plugin runs an upstream benchmark driver and its native evaluator as an
opaque process. MagentaBench owns case activation, source digests, the fresh
workspace, the argv/environment boundary, and persisted evidence. The driver
owns model execution and benchmark-native scoring.

The adapter does not import benchmark code and does not rewrite an upstream
method. It is intended for paper-native runners, evaluator scripts, and
coding-agent adaptations that can exchange one JSON result document.

## Registration

A complete declaration uses four registry entries:

- `native-benchmark-loader.v1` resolves the case manifest and content closure.
- `native-process.factory.v1` creates a local process backend.
- `native-benchmark.execution.v1` binds the benchmark, backend, and subject.
- `native-process.local.v1` supplies digest-bound backend defaults.

The subject declaration must provide `launch_argv`. Arguments are passed
directly to `subprocess.Popen`; no shell is involved. Supported placeholders
are `{case_id}`, `{public_input}`, `{output_dir}`, `{workspace}`,
`{dataset_source}`, `{subject_source}`, `{model}`, `{attempt_id}`,
`{max_tokens}`, and `{max_cost}`. Unknown placeholders, conversions, and
format specifiers fail before launch.

## Case Manifest

The dataset `config.case_manifest` points to a JSON file inside the declared
source closure:

```json
{
  "schema_version": "magentabench.native-cases.v1",
  "cases": [
    {
      "id": "case-one",
      "public_input": "public/case-one.json",
      "task_contracts": ["contracts/task.txt"],
      "verifier_contracts": ["contracts/verifier.txt"],
      "allow_internet": false
    }
  ]
}
```

Only the fields shown above are accepted. Case ids must be unique normalized
identifiers. All referenced files must be regular files under the dataset
source, must not pass through symlinks, and must be included by
`content_globs`. The source closure is content-addressed at compile time and
checked again at activation time.

Case order is controlled by the protocol. `fixed`, `explicit`, `custom`, and
`seeded_random` are reproducible; unseeded `random` is retained as an
exploratory choice. A fresh workspace is created for every attempt, so the
protocol's reset policy cannot silently reuse native state.

## Native Result

The driver writes `output/result.json` with this closed top-level contract:

```json
{
  "schema_version": "magentabench.native-result.v1",
  "case_id": "case-one",
  "metrics": {
    "native_score": 0.75,
    "secondary_exact": 1.0
  },
  "usage": {
    "input_tokens": 120,
    "output_tokens": 40,
    "total_tokens": 160,
    "cost": 0.002
  },
  "artifacts": ["answer.json"],
  "trace": "trace.json",
  "model_activation": null,
  "verifier": "official-evaluator-v1"
}
```

Required fields are `schema_version`, `case_id`, and a non-empty `metrics`
object. Metric values must be finite numbers. The authoritative metric named
by the experiment must be present. Binary evaluators apply the registered
success rule and emit `pass` or `verified_fail`; continuous evaluators emit
`scored`. Every metric is retained in `VerifierEvidence.metrics`, including
secondary metrics not named by the MagentaBench experiment.

`usage` is optional. Unknown usage fields are rejected, and the adapter always
sets observed wall-clock time. Artifact and trace paths are relative to the
driver's output directory, are copied into the evidence directory, and are
checked for symlink/path escape and forwarded-secret bytes. A
`model_activation` object is accepted only when its source is
`native_result`; it produces the normal MagentaBench activation receipt.

Malformed JSON, schema drift, missing authoritative metrics, invalid usage,
artifact escape, nonzero exit, timeout, and missing output remain distinct
failure states. They are retained in the evidence instead of being converted
to a score.

## Environment And Network

The backend inherits only `PATH`, `LANG`, `LC_ALL`, and `TZ` by default. Extra
variables must be declared by name in the backend defaults. Values are never
written to a receipt; stdout, stderr, JSON, and copied artifacts are redacted
when they contain a forwarded value.

`native_process` records the benchmark case's network policy, but host egress
is not observed by this backend. The resulting network observation is marked
`unobservable`, and standalone verification therefore keeps the run
exploratory for isolation claims. A native score can still be inspected, but
it must not be presented as evidence of a closed network boundary.

## Conformance

The deterministic fixtures exercise both scoring kinds:

```bash
uv run --frozen --extra test pytest -q tests/test_native_benchmark_adapter.py
uv run --frozen bmp-compile MagentaBench/conformance/experiments/native-benchmark-continuous-smoke.toml
uv run --frozen bmp-compile MagentaBench/conformance/experiments/native-benchmark-binary-smoke.toml
```

Use a fresh record root for every real benchmark run and run the standalone
report verifier against the resulting report. Provider credentials and hidden
benchmark data remain outside this repository.
