# SWE-bench exploratory probe: astropy__astropy-6938

Date: 2026-08-08 (Asia/Shanghai)

This is an exploratory integration probe, not a BMP claim-ready report. It
checks the local dataset/image/evaluator tuple, activates one case through the
SWE-bench loader, and makes one real Codex API-backed attempt. No credential,
container filesystem, or repository checkout is retained here.

## Frozen inputs

- Dataset: `HarnessX/benchmarks/swebench/swebench_lite_test.json`
- Dataset SHA-256: `b91794a6779207e6904d103280141657a6a23b529911d20bdc5e3b9bad3ba007`
- Dataset source commit: `bf5f199ee65034d55db0c536e582f1e7c8abf669`
- Instance: `astropy__astropy-6938`
- Image tag: `sweb.eval.x86_64.astropy__astropy-6938:latest`
- Image ID: `sha256:a64e48c6ff94271d86498cf991b41d40f0e3bf33537f7adc6c740c0f26e641e9`
- Candidate patch SHA-256: `89421ea1cce0281f27942094468a8c9677ab87b1987915eb8e1ee2517d3fb25e`

The candidate patch is retained as `candidate.patch`. The gold patch is not
retained in this record and was not exposed to the agent.

## Evaluator calibration

The hidden `test_patch` was read from the pinned dataset and applied only
inside a disposable container. The focused verifier command was:

```bash
python -m pytest -q \
  astropy/io/fits/tests/test_checksum.py::TestChecksumFunctions::test_ascii_table_data \
  astropy/io/fits/tests/test_table.py::TestTableFunctions::test_ascii_table
```

Results on fresh containers:

| Candidate state | Result |
| --- | --- |
| Base image + hidden test patch | `2 failed` |
| Base image + hidden test patch + dataset gold patch | `2 passed` |
| Base image + hidden test patch + Codex candidate patch | `2 passed` |

The baseline failures were the expected unchanged checksum/data sum and the
missing `D` exponent. Both pass after either the gold or candidate patch.

## Real agent attempt

The source tree was copied from the frozen image into
`/tmp/bmp-swe-codex-6938`. Codex was invoked non-interactively with workspace
write isolation:

```bash
codex exec \
  --ephemeral \
  --color never \
  --sandbox workspace-write \
  -C /tmp/bmp-swe-codex-6938 \
  -o /tmp/bmp-swe-codex-6938/codex-final.txt \
  "Solve SWE-bench instance astropy__astropy-6938 ..."
```

Observed runtime identity and usage:

- Codex CLI: `0.144.4`
- Model: `gpt-5.6-sol`
- Provider: `custom`
- Reasoning effort: `high`
- Session ID: `019fe1fa-327e-7f41-bd59-bc330402f6a0`
- Wall time: `290.3 s`
- Tokens reported by Codex: `111,311`
- Agent-modified files: one (`astropy/io/fits/fitsrec.py`)

## Independent candidate scoring

A second fresh container was created by immutable image ID. The hidden test
patch was applied first, then `candidate.patch` was streamed from the host, and
the focused verifier command above was executed. It completed with:

```text
2 passed, 1 warning in 0.52s
```

The evaluation container was disposable. This record demonstrates a valid
single-case benchmark contact, but it does not yet supply BMP network
observation, generic model activation, full execution-adapter provenance, or
claim-ready statistics.
