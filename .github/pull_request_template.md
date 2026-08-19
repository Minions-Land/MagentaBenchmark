## Change summary

<!-- Link the relevant immutable lab issue, for example: `Closes #` is not a
lab event. Use the issue id and keep the event in lab/. -->

Lab issue ID:

### Change class

- [ ] Lab/control-plane or recovery records only (eligible for parallel merge when scopes are disjoint)
- [ ] Adapter, registry, experiment, or evidence tooling
- [ ] BMP protocol surface (`MagentaBench/schemas`, `MagentaBench/runner`, or shared contracts)
- [ ] Documentation/workflow only

### BMP protocol review

- [ ] I have requested PoorOtterBob review (required when core BMP paths change and I am not PoorOtterBob)
- [ ] I completed PoorOtterBob self-review (only PoorOtterBob may check this)
- [ ] This PR has no BMP protocol impact

For a PoorOtterBob self-review, replace the placeholder below with the current
full PR head SHA. A new push requires updating this line.

Protocol self-review HEAD: `<full head SHA>`

### Repository review authority

- [ ] I requested PoorOtterBob review (required when I am not PoorOtterBob)
- [ ] I completed PoorOtterBob final review/self-review (only PoorOtterBob may check this)

For a PoorOtterBob-authored PR, replace the placeholder below with the current
full PR head SHA. A new push requires updating this line. Other collaborators
may comment or submit advisory findings, but they are not required approvers.

Final review HEAD: `<full head SHA>`

Explain any protocol impact, including schema, identity, denominator, verifier,
checkpoint, or claim-gate changes. Do not use a lab-owner approval as a protocol
approval. A PoorOtterBob self-review is an explicit, attributable attestation
and is not represented as an independent review.

## Verification

Commands run (include the mirror/index policy when dependencies are installed):

```text
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ uv sync --frozen --extra test
uv run --frozen --extra test pytest -q <focused tests>
uv run --frozen python -m compileall -q MagentaBench plugins tests
bash scripts/audit_hcp_boundary.sh
uv run bmp-lab doctor
```

Observed result and return code:

## Evidence and safety

- [ ] No credential value, provider token, or secret-bearing URL is included.
- [ ] New or changed artifacts have stable references and SHA-256 values.
- [ ] Existing records and historical probes were not edited in place.
- [ ] Any benchmark output remains explicitly `exploratory` unless all claim gates and standalone verification pass.
- [ ] If this changes a shared path, I rebased after the latest canonical merge and reran the relevant checks.
