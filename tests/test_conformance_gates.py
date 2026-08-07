from __future__ import annotations

from pathlib import Path

from MagentaBench.runner.gates import evaluate_run_report
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import ObservationReport, RunPurpose

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
