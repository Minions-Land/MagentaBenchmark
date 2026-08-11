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
