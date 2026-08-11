from __future__ import annotations

from math import log
from pathlib import Path

import pytest

import MagentaBench.schemas as public_schemas
from MagentaBench.schemas import research_metrics
from MagentaBench.schemas.json_schema import schema_documents
from MagentaBench.schemas.models import ArtifactRef, RunStatus, StrictModel
from MagentaBench.schemas.regime import (
    ExperimentCellCoordinates,
    ExperimentCellDisposition,
    ExperimentCellObservation,
    PlannedMetricCell,
    build_experiment_cell_ledger,
    build_experiment_cell_plan,
)
from MagentaBench.schemas.research_metrics import (
    CalibrationAnalysisPlan,
    CalibrationObservation,
    CandidateYieldObservation,
    CandidateYieldPlan,
    EvolutionBudgetCheckpoint,
    EvolutionCurveObservation,
    EvolutionCurvePlan,
    GeneralizationAnalysisPlan,
    MetaEvolutionArmObservation,
    MetaEvolutionPairPlan,
    ParentChildPair,
    ParentDeltaObservation,
    ParentDeltaPlan,
    ProgressCheckpoint,
    ResearchMetricDirection,
    ResearchMetricDisposition,
    SelectorAnalysisPlan,
    SelectorProbabilityObservation,
    TrajectoryAnalysisPlan,
    TrajectoryProgressObservation,
    analyze_calibration,
    analyze_candidate_yield,
    analyze_evolution_curve,
    analyze_generalization,
    analyze_meta_evolution_pairs,
    analyze_parent_deltas,
    analyze_selector_distribution,
    analyze_trajectory_progress,
)


def _artifact(path: Path, digest: str = "a" * 64) -> ArtifactRef:
    return ArtifactRef(path=str(path.resolve()), sha256=digest, size_bytes=2)


def test_research_metric_public_api_and_json_schemas_are_complete() -> None:
    for name in research_metrics.__all__:
        assert name in public_schemas.__all__
        assert getattr(public_schemas, name) is getattr(research_metrics, name)

    expected_model_schemas = {
        "CalibrationAnalysisPlan": "calibration-analysis-plan",
        "CalibrationMetricsReceipt": "calibration-metrics-receipt",
        "CalibrationObservation": "calibration-observation",
        "CandidateYieldObservation": "candidate-yield-observation",
        "CandidateYieldPlan": "candidate-yield-plan",
        "EvolutionBudgetCheckpoint": "evolution-budget-checkpoint",
        "EvolutionCurveMetricsReceipt": "evolution-curve-metrics-receipt",
        "EvolutionCurveObservation": "evolution-curve-observation",
        "EvolutionCurvePlan": "evolution-curve-plan",
        "GeneralizationAnalysisPlan": "generalization-analysis-plan",
        "GeneralizationMetricsReceipt": "generalization-metrics-receipt",
        "MetaEvolutionArmObservation": "meta-evolution-arm-observation",
        "MetaEvolutionPairPlan": "meta-evolution-pair-plan",
        "ParentChildPair": "parent-child-pair",
        "ParentDeltaObservation": "parent-delta-observation",
        "ParentDeltaPlan": "parent-delta-plan",
        "ProgressCheckpoint": "progress-checkpoint",
        "ResearchMetricContribution": "research-metric-contribution",
        "ResearchMetricReceipt": "research-metric-receipt",
        "SelectorAnalysisPlan": "selector-analysis-plan",
        "SelectorMetricsReceipt": "selector-metrics-receipt",
        "SelectorProbabilityObservation": "selector-probability-observation",
        "TrajectoryAnalysisPlan": "trajectory-analysis-plan",
        "TrajectoryMetricsReceipt": "trajectory-metrics-receipt",
        "TrajectoryProgressObservation": "trajectory-progress-observation",
    }
    public_model_names = {
        name
        for name in research_metrics.__all__
        if isinstance(getattr(research_metrics, name), type)
        and issubclass(getattr(research_metrics, name), StrictModel)
    }
    assert set(expected_model_schemas) == public_model_names
    documents = schema_documents()
    for model_name, document_name in expected_model_schemas.items():
        assert documents[document_name]["title"] == model_name


def _generalization_ledger(
    tmp_path: Path, *, missing_cell: str | None = None
):
    membership = _artifact(tmp_path / "membership.json", "a" * 64)
    result = _artifact(tmp_path / "metric.json", "e" * 64)
    definitions = (
        ("cell-id-v1", "id", "scenario-1", "variant-1", 1.0),
        ("cell-id-v2", "id", "scenario-1", "variant-2", 0.8),
        ("cell-ood-v1", "ood", "scenario-2", "variant-1", 0.6),
        ("cell-ood-v2", "ood", "scenario-2", "variant-2", 0.4),
    )
    cells = tuple(
        PlannedMetricCell(
            cell_id=cell_id,
            coordinates=ExperimentCellCoordinates(
                stage_id="evaluate",
                checkpoint_id="final",
                task_id=f"task-{index}",
                domain_id=domain_id,
                scenario_id=scenario_id,
                variant_id=variant_id,
            ),
            metric_id="score",
            metric_digest="b" * 64,
            membership_digest=membership.sha256,
        )
        for index, (cell_id, domain_id, scenario_id, variant_id, _) in enumerate(
            definitions, start=1
        )
    )
    plan = build_experiment_cell_plan(
        regime_id="generalization-test",
        regime_digest="c" * 64,
        stage_manifest_digests={"evaluate": "d" * 64},
        membership_refs=(membership,),
        cells=cells,
    )
    observations = tuple(
        ExperimentCellObservation(
            cell_id=cell_id,
            disposition=(
                ExperimentCellDisposition.missing
                if cell_id == missing_cell
                else ExperimentCellDisposition.observed
            ),
            value=None if cell_id == missing_cell else value,
            terminal_status=None if cell_id == missing_cell else RunStatus.scored,
            reason=(
                "planned cell did not produce a score"
                if cell_id == missing_cell
                else None
            ),
            metric_result_ref=None if cell_id == missing_cell else result,
        )
        for cell_id, _, _, _, value in definitions
    )
    return build_experiment_cell_ledger(plan, observations)


def test_generalization_metrics_use_every_planned_membership_cell(
    tmp_path: Path,
) -> None:
    plan = GeneralizationAnalysisPlan(
        metric_id="score",
        metric_digest="b" * 64,
        group_axis="domain",
        distribution_axis="domain",
        in_distribution_values=("id",),
        out_of_distribution_values=("ood",),
    )
    result = analyze_generalization(_generalization_ledger(tmp_path), plan)
    assert result.group_macro.value == pytest.approx(0.7)
    assert result.worst_group.value == pytest.approx(0.5)
    assert result.id_ood_gap.value == pytest.approx(0.4)
    assert result.scenario_goal_completion.value == pytest.approx(0.6)
    assert result.group_macro.planned_count == 4
    assert result.group_macro.observed_count == 4
    assert result.group_macro.canonical_digest()

    missing = analyze_generalization(
        _generalization_ledger(tmp_path, missing_cell="cell-ood-v2"), plan
    )
    assert missing.group_macro.state.value == "invalid"
    assert missing.group_macro.value is None
    assert missing.group_macro.missing_count == 1
    assert missing.group_macro.planned_count == 4
    assert missing.group_macro.contributing_ids == (
        "cell-id-v1",
        "cell-id-v2",
        "cell-ood-v1",
    )


def test_evolution_parent_yield_and_common_budget_curve_are_distinct() -> None:
    parent_plan = ParentDeltaPlan(
        pairs=(
            ParentChildPair(pair_id="pair-1", parent_id="p-1", child_id="c-1"),
            ParentChildPair(pair_id="pair-2", parent_id="p-2", child_id="c-2"),
        )
    )
    parent_observations = (
        ParentDeltaObservation(
            pair_id="pair-1",
            disposition=ResearchMetricDisposition.observed,
            parent_score=0.5,
            child_score=0.7,
        ),
        ParentDeltaObservation(
            pair_id="pair-2",
            disposition=ResearchMetricDisposition.observed,
            parent_score=0.8,
            child_score=0.7,
        ),
    )
    parent = analyze_parent_deltas(parent_plan, parent_observations)
    assert parent.value == pytest.approx(0.05)

    missing_parent = analyze_parent_deltas(parent_plan, parent_observations[:1])
    assert missing_parent.state.value == "invalid"
    assert missing_parent.missing_count == 1

    yield_plan = CandidateYieldPlan(
        candidate_ids=("candidate-1", "candidate-2", "candidate-3"),
        yield_definition="promoted",
    )
    candidate_yield = analyze_candidate_yield(
        yield_plan,
        tuple(
            CandidateYieldObservation(
                candidate_id=candidate_id,
                disposition=ResearchMetricDisposition.observed,
                yielded=yielded,
            )
            for candidate_id, yielded in zip(
                yield_plan.candidate_ids, (True, False, True), strict=True
            )
        ),
    )
    assert candidate_yield.value == pytest.approx(2 / 3)

    curve_plan = EvolutionCurvePlan(
        checkpoints=tuple(
            EvolutionBudgetCheckpoint(checkpoint_id=f"budget-{budget}", budget=float(budget))
            for budget in (1, 2, 3, 4)
        ),
        common_budget_limit=4.0,
        baseline_score=0.5,
    )
    curve = analyze_evolution_curve(
        curve_plan,
        tuple(
            EvolutionCurveObservation(
                checkpoint_id=checkpoint.checkpoint_id,
                disposition=ResearchMetricDisposition.observed,
                score=score,
            )
            for checkpoint, score in zip(
                curve_plan.checkpoints, (0.6, 0.55, 0.8, 0.75), strict=True
            )
        ),
    )
    assert curve.best_so_far_final.value == pytest.approx(0.8)
    assert curve.final_gain.value == pytest.approx(0.3)
    assert curve.aubc.value == pytest.approx(0.625)
    assert curve.aubc.parameters_digest == curve.final_gain.parameters_digest


def test_meta_evolution_requires_the_same_complete_pair_keys() -> None:
    plan = MetaEvolutionPairPlan(
        pair_keys=("seed-1", "seed-2"),
        control_arm_id="control",
        treatment_arm_id="treatment",
    )
    observations = tuple(
        MetaEvolutionArmObservation(
            pair_key=pair_key,
            arm_id=arm_id,
            disposition=ResearchMetricDisposition.observed,
            score=score,
        )
        for pair_key, control, treatment in (
            ("seed-1", 0.5, 0.7),
            ("seed-2", 0.7, 0.6),
        )
        for arm_id, score in (("control", control), ("treatment", treatment))
    )
    paired = analyze_meta_evolution_pairs(plan, observations)
    assert paired.value == pytest.approx(0.05)
    assert paired.planned_count == 2

    incomplete = analyze_meta_evolution_pairs(plan, observations[:-1])
    assert incomplete.state.value == "invalid"
    assert incomplete.missing_count == 1
    assert incomplete.value is None


def test_trajectory_metrics_preserve_right_censoring_and_fail_closed() -> None:
    plan = TrajectoryAnalysisPlan(
        trajectory_ids=("trajectory-1", "trajectory-2"),
        checkpoints=(
            ProgressCheckpoint(checkpoint_id="step-0", coordinate=0.0),
            ProgressCheckpoint(checkpoint_id="step-1", coordinate=1.0),
            ProgressCheckpoint(checkpoint_id="step-2", coordinate=2.0),
        ),
        threshold=0.8,
    )
    values = {
        "trajectory-1": (0.0, 0.5, 1.0),
        "trajectory-2": (0.2, 0.6, 0.55),
    }
    observations = tuple(
        TrajectoryProgressObservation(
            trajectory_id=trajectory_id,
            checkpoint_id=checkpoint.checkpoint_id,
            disposition=ResearchMetricDisposition.observed,
            progress=value,
        )
        for trajectory_id in plan.trajectory_ids
        for checkpoint, value in zip(
            plan.checkpoints, values[trajectory_id], strict=True
        )
    )
    result = analyze_trajectory_progress(plan, observations)
    assert result.final_progress.value == pytest.approx(0.775)
    assert result.aupc.value == pytest.approx(0.49375)
    assert result.regression_mass.value == pytest.approx(0.025)
    assert result.time_to_threshold.value == pytest.approx(2.0)
    assert result.time_to_threshold.observed_count == 1
    assert result.time_to_threshold.censored_count == 1
    assert result.time_to_threshold.contributions[1].censored

    incomplete = analyze_trajectory_progress(plan, observations[:-1])
    assert incomplete.aupc.state.value == "invalid"
    assert incomplete.aupc.missing_count == 1
    assert incomplete.aupc.planned_count == 2


def test_calibration_main_metrics_invalidate_when_planned_confidence_is_missing() -> None:
    plan = CalibrationAnalysisPlan(
        prediction_ids=("prediction-1", "prediction-2"),
        bin_edges=(0.0, 0.5, 1.0),
    )
    observations = (
        CalibrationObservation(
            prediction_id="prediction-1",
            outcome_disposition=ResearchMetricDisposition.observed,
            outcome=1,
            confidence_disposition=ResearchMetricDisposition.observed,
            confidence=0.9,
        ),
        CalibrationObservation(
            prediction_id="prediction-2",
            outcome_disposition=ResearchMetricDisposition.observed,
            outcome=0,
            confidence_disposition=ResearchMetricDisposition.observed,
            confidence=0.2,
        ),
    )
    result = analyze_calibration(plan, observations)
    assert result.brier.value == pytest.approx(0.025)
    assert result.nll.value == pytest.approx(-(log(0.9) + log(0.8)) / 2)
    assert result.ece.value == pytest.approx(0.15)
    assert result.confidence_coverage.value == 1.0

    missing_confidence = observations[1].model_copy(
        update={
            "confidence_disposition": ResearchMetricDisposition.missing,
            "confidence": None,
            "confidence_reason": "provider omitted confidence",
        }
    )
    incomplete = analyze_calibration(plan, (observations[0], missing_confidence))
    assert incomplete.brier.state.value == "invalid"
    assert incomplete.nll.state.value == "invalid"
    assert incomplete.ece.state.value == "invalid"
    assert incomplete.confidence_coverage.state.value == "complete"
    assert incomplete.confidence_coverage.value == 0.5
    assert incomplete.confidence_coverage.missing_count == 1


def test_selector_entropy_and_ess_cover_zero_probability_candidates() -> None:
    plan = SelectorAnalysisPlan(
        candidate_ids=("candidate-1", "candidate-2", "candidate-3")
    )
    probabilities = (0.5, 0.3, 0.2)
    result = analyze_selector_distribution(
        plan,
        tuple(
            SelectorProbabilityObservation(
                candidate_id=candidate_id,
                disposition=ResearchMetricDisposition.observed,
                probability=probability,
            )
            for candidate_id, probability in zip(
                plan.candidate_ids, probabilities, strict=True
            )
        ),
    )
    assert result.entropy.value == pytest.approx(
        -sum(probability * log(probability) for probability in probabilities)
    )
    assert result.effective_sample_size.value == pytest.approx(
        1 / sum(probability**2 for probability in probabilities)
    )
    assert result.entropy.planned_count == 3

    invalid = analyze_selector_distribution(
        plan,
        (
            SelectorProbabilityObservation(
                candidate_id="candidate-1",
                disposition=ResearchMetricDisposition.observed,
                probability=0.8,
            ),
            SelectorProbabilityObservation(
                candidate_id="candidate-2",
                disposition=ResearchMetricDisposition.observed,
                probability=0.3,
            ),
            SelectorProbabilityObservation(
                candidate_id="candidate-3",
                disposition=ResearchMetricDisposition.zero_filled,
                probability=0.0,
            ),
        ),
    )
    assert invalid.entropy.state.value == "invalid"
    assert invalid.entropy.value is None
