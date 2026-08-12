# Execution Target Profiles

These files describe placement requirements outside BMP protocol semantics.
They do not register a backend and they do not make a result claim-ready. The
selected backend remains the digest-bound id in the BMP experiment TOML;
`uv run bmp-collab modes` reports what is actually registered.

Each mode owns one directory so unrelated adapters can evolve in parallel:

```text
execution-profiles/<mode>/profile.json
```

Each profile declares the workspace lifecycle and network policy plus every
required identity, runtime, recovery, and teardown receipt. Run
`uv run --frozen bmp-collab validate` and `uv run --frozen bmp-collab modes`;
they validate `schema.json` and reject drift from backend registrations,
verifier boundaries, or linked `lab/` work. Live progress belongs to the linked
issue, not to these profiles.

Profiles may also declare an argv-only, read-only host readiness probe. A probe
checks prerequisites for the placement; it must not pull, build, inspect, or
execute a benchmark image, and success never registers a backend or raises the
profile's evidence ceiling. Apptainer currently provides this host probe:

```bash
uv run --frozen python scripts/check_execution_profiles.py apptainer
```

Set `APPTAINER_BIN`, `APPTAINER_CACHEDIR`, `APPTAINER_TMPDIR`, and
`MAGENTABENCH_ARTIFACT_ROOT` to inspect a particular installation. Optionally
set `MAGENTABENCH_APPTAINER_IMAGE` to check whether an expected image path is
present and pass `--require-gpu` when GPU visibility is a host prerequisite.
Only variable names and local metadata are reported; image identity remains a
future backend receipt.
