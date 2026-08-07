from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from MagentaBench.runner.gates import evaluate_run_report
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    ObservationReport,
    RunPurpose,
    TestOverrideReceipt as OverrideReceipt,
)

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "fake-sweep.toml"


def test_exploratory_conformance_is_structurally_not_a_claim(tmp_path: Path) -> None:
    completed = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    report = evaluate_run_report(
        experiment_id="fake-conformance-sweep",
        experiment_digest="0" * 64,
        completed=completed,
        expected_run_count=8,
        control_id="fake.control",
        treatment_id="fake.treatment",
        deterministic_conformance=True,
        counterbalanced=True,
    )
    assert isinstance(report, ObservationReport)
    assert report.purpose == RunPurpose.exploratory
    assert report.observations
    serialized = report.model_dump(mode="json")
    assert "gates" not in serialized
    assert "claim_eligible" not in serialized
    assert "effect" not in serialized
    assert "effect_is_causal_claim" not in serialized


def test_report_evaluator_rejects_test_override_lineage(tmp_path: Path) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    original = completed[0]
    receipt = OverrideReceipt(reason="mutation test")
    metadata = original.plan.manifest.metadata.model_copy(
        update={"test_override": receipt}
    )
    claim_design = original.plan.manifest.claim_design.model_copy(
        update={"purpose": RunPurpose.claim}
    )
    manifest = original.plan.manifest.model_copy(
        update={"metadata": metadata, "claim_design": claim_design}
    )
    completed[0] = replace(
        original,
        plan=replace(original.plan, manifest=manifest),
    )

    with pytest.raises(ValueError, match="test override evidence"):
        evaluate_run_report(
            experiment_id="fake-conformance-sweep",
            experiment_digest="0" * 64,
            completed=completed,
            expected_run_count=8,
            control_id="fake.control",
            treatment_id="fake.treatment",
            deterministic_conformance=True,
            counterbalanced=True,
        )
