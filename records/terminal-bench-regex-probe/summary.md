# Terminal-Bench exploratory probe: regex-log

Date: 2026-08-08 (Asia/Shanghai)

The historical probe activated one Terminal-Bench 2.1 task, staged a
solution-free task view, validated a native Harbor 0.20.0 job, entered the
Docker task container, ran the `nop` integration subject, and invoked the
official verifier. The exact loader and execution-adapter implementation bytes
from that run were not retained and are therefore not asserted by `probe.json`.

The verifier installed Ubuntu packages and then attempted to download `uv`
0.9.5 from GitHub. That transfer failed, leaving `/root/.local/bin/env` and
`uvx` unavailable. Harbor nevertheless emitted reward `0.0` without an
exception. BMP classifies this probe as `verifier_failure` and does not treat
that reward as an Agent score.

`probe.json` conforms to `bmp-integration-probe-v1`. Verification rehashes the
retained task/verifier sources, `nop` executable, Harbor executable, public
input, secret-free Harbor projection, and failure excerpt. The projection's
`retention_note` records an explicitly unverifiable raw-result hash; the raw
bytes were not retained. This remains exploratory and is not a claim-ready
benchmark run.
