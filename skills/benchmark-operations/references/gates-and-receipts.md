# Gates And Receipts

## Gate Evidence

| Gate | Minimum evidence | Failure class |
| --- | --- | --- |
| Identity | source/dataset revisions, effective config, clean or declared tree | invalid identity |
| Schema/interface | parser, manifest, adapter, and contract checks | invalid setup |
| Smoke | one representative case with expected tool/result fields | infrastructure or adapter |
| Parity | model, temperature, timeout, retry, seed, task order, and trial count | configuration drift |
| Mechanism | required events, injections, fingerprints, or verifier receipts | method not active |
| Qualification | small fixed sample and stable termination | readiness failure |
| Full run | all planned cells and retained raw artifacts | incomplete run |
| Review | independent completeness and metric calculation | verifier failure |

Do not silently downgrade a failed hard gate to a warning. If a policy allows
continuation, record the exception, approver, and effect on claim eligibility.

## Receipt Template

```markdown
# <RUN_ID> Receipt

## Conclusion
- State: complete | incomplete | invalid | infrastructure-failed
- One-sentence result and evidence class:

## Frozen protocol
- Source and dataset revisions:
- Subject and model identities:
- Tasks, order, repetitions, seeds:
- Metric, threshold, numerator/denominator:
- Resource limits and retry policy:

## Checks
- Identity:
- Parity:
- Mechanism/fingerprint:
- Completeness:
- Independent metric review:

## Evidence
- Results:
- Logs:
- Provenance/command:
- Hashes:

## Deviations and limits
- Retries or restarts:
- Unrun checks:
- Known limitations:
- Next action and owner:
```

External numbers that were not replayed through the current harness must be
marked as externally declared and not claim-eligible. Keep their source,
denominator, conditions, and limitations; do not copy private implementation
details or trajectories.
