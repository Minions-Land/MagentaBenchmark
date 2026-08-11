# Execution Target Profiles

These files describe placement requirements outside BMP protocol semantics.
They do not register a backend and they do not make a result claim-ready. The
selected backend remains the digest-bound id in the BMP experiment TOML;
`uv run bmp-collab modes` reports what is actually registered.

Each mode owns one directory so unrelated adapters can evolve in parallel:

```text
execution-profiles/<mode>/profile.json
```

Validate the JSON shape with `schema.json`, then satisfy every listed identity,
runtime, recovery, and teardown receipt in the backend adapter. Live progress
belongs to the linked `lab/` issue, not to these profiles.
