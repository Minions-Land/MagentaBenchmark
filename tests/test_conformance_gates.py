from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from MagentaBench.runner.gates import CompletedRun, evaluate_claim
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import GateName, RunStatus


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "fake-sweep.toml"


def _evaluate(items, *, expected=8, counterbalanced=True):
    return evaluate_claim(
        experiment_id="fake-conformance-sweep",
        experiment_digest="0" * 64,
        completed=items,
        expected_run_count=expected,
        control_id="fake.control",
        treatment_id="fake.treatment",
        deterministic_conformance=True,
        counterbalanced=counterbalanced,
    )


def test_each_claim_gate_can_independently_block_eligibility(tmp_path: Path) -> None:
    successful = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)

    incomplete = _evaluate(successful[:-1], expected=8)
    assert not incomplete.gates[GateName.execution_valid].valid
    assert not incomplete.claim_eligible

    first = successful[0]
    protocol = first.plan.manifest.execution.protocol
    assert protocol is not None
    bad_protocol = protocol.model_copy(update={"state_reset": "never"})
    bad_execution = first.plan.manifest.execution.model_copy(
        update={"protocol": bad_protocol}
    )
    bad_manifest = first.plan.manifest.model_copy(update={"execution": bad_execution})
    protocol_items = [
        CompletedRun(plan=replace(first.plan, manifest=bad_manifest), case=first.case),
        *successful[1:],
    ]
    protocol_report = _evaluate(protocol_items)
    assert not protocol_report.gates[GateName.protocol_valid].valid
    assert not protocol_report.claim_eligible

    provenance = first.case.bundle.provenance.model_copy(
        update={"backend_digest": "drifted"}
    )
    drifted_bundle = first.case.bundle.model_copy(update={"provenance": provenance})
    isolation_items = [
        CompletedRun(plan=first.plan, case=replace(first.case, bundle=drifted_bundle)),
        *successful[1:],
    ]
    isolation_report = _evaluate(isolation_items)
    assert not isolation_report.gates[GateName.isolation_valid].valid
    assert not isolation_report.claim_eligible

    verifier_failed = first.case.bundle.model_copy(update={"status": RunStatus.verifier_error})
    scoring_items = [
        CompletedRun(plan=first.plan, case=replace(first.case, bundle=verifier_failed)),
        *successful[1:],
    ]
    scoring_report = _evaluate(scoring_items)
    assert not scoring_report.gates[GateName.scoring_valid].valid
    assert not scoring_report.claim_eligible

    statistics_report = _evaluate(successful, counterbalanced=False)
    assert not statistics_report.gates[GateName.statistics_valid].valid
    assert not statistics_report.claim_eligible
    assert statistics_report.effect is not None
    assert statistics_report.effect.point_estimate == 1.0
    assert statistics_report.effect_is_causal_claim is False
