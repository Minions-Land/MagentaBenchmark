"""Strict checks for metadata-only experiment-regime protocol templates.

Loading a template and compiling its registry dependency closure establish only
protocol identity.  They do not establish runtime orchestration, stage
activation receipts, complete cell ledgers, replayability, or claim readiness.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas import (
    ExperimentRegimeKind,
    ExperimentRegimeSpec,
    ExperimentStageRole,
    StageFeedbackVisibility,
    StageStatePolicy,
    load_experiment_regime_spec,
)


ROOT = Path(__file__).parents[1]
REGIME_ROOT = ROOT / "registries/regimes"

CATALOG = {
    "generalization-protocol-template-v1.toml": (
        ExperimentRegimeKind.generalization,
        {
            "id-validation": (),
            "ood-holdout": ("id-validation",),
        },
    ),
    "cross-domain-transfer-protocol-template-v1.toml": (
        ExperimentRegimeKind.cross_domain_transfer,
        {
            "source-baseline": (),
            "source-adapt": ("source-baseline",),
            "target-validation": ("source-adapt",),
            "target-holdout": ("source-adapt", "target-validation"),
        },
    ),
    "continual-learning-protocol-template-v1.toml": (
        ExperimentRegimeKind.continual_learning,
        {
            "initial-evaluate": (),
            "learn-domain-a": ("initial-evaluate",),
            "evaluate-after-a": ("learn-domain-a",),
            "learn-domain-b": ("learn-domain-a", "evaluate-after-a"),
            "evaluate-after-b": ("learn-domain-b",),
        },
    ),
    "curriculum-protocol-template-v1.toml": (
        ExperimentRegimeKind.curriculum,
        {
            "train-easy": (),
            "train-medium": ("train-easy",),
            "train-hard": ("train-medium",),
            "curriculum-evaluate": ("train-hard",),
        },
    ),
    "online-adaptation-protocol-template-v1.toml": (
        ExperimentRegimeKind.online_adaptation,
        {
            "pre-update-score-1": (),
            "adapt-after-score-1": ("pre-update-score-1",),
            "pre-update-score-2": ("adapt-after-score-1",),
            "adapt-after-score-2": (
                "adapt-after-score-1",
                "pre-update-score-2",
            ),
            "frozen-final-evaluate": ("adapt-after-score-2",),
        },
    ),
    "robustness-stress-protocol-template-v1.toml": (
        ExperimentRegimeKind.robustness_stress,
        {
            "clean-reference": (),
            "perturbation-stress": ("clean-reference",),
            "adversarial-stress": ("clean-reference",),
            "paired-summary": (
                "perturbation-stress",
                "adversarial-stress",
            ),
        },
    ),
    "evolutionary-search-protocol-template-v1.toml": (
        ExperimentRegimeKind.evolutionary_search,
        {
            "candidate-search": (),
            "staged-validation": ("candidate-search",),
            "promotion-selection": (
                "candidate-search",
                "staged-validation",
            ),
            "sealed-final-holdout": ("promotion-selection",),
        },
    ),
    "meta-evolution-protocol-template-v1.toml": (
        ExperimentRegimeKind.meta_evolution,
        {
            "meta-method-search": (),
            "inner-evolution-runs": ("meta-method-search",),
            "meta-validation": ("inner-evolution-runs",),
            "meta-selection": (
                "meta-method-search",
                "inner-evolution-runs",
                "meta-validation",
            ),
            "sealed-meta-holdout": ("meta-selection",),
        },
    ),
}


def _load_catalog():
    return {
        filename: load_experiment_regime_spec(REGIME_ROOT / filename)
        for filename in CATALOG
    }


def test_protocol_template_catalog_covers_every_non_fixture_regime_kind() -> None:
    specs = _load_catalog()
    fixture_kinds = {
        ExperimentRegimeKind.iid_evaluation,
        ExperimentRegimeKind.repeated_sampling,
    }
    assert {spec.regime_kind for spec in specs.values()} == (
        set(ExperimentRegimeKind) - fixture_kinds
    )
    assert all(spec.id.endswith(".protocol-template.v1") for spec in specs.values())
    assert all(spec.adapter == "magentabench.stage-dag" for spec in specs.values())


def test_protocol_template_dags_have_exact_registered_topology() -> None:
    for filename, spec in _load_catalog().items():
        expected_kind, expected_topology = CATALOG[filename]
        assert spec.regime_kind == expected_kind
        assert {
            stage.id: stage.predecessors for stage in spec.stages
        } == expected_topology

        positions = {stage.id: index for index, stage in enumerate(spec.stages)}
        assert all(
            positions[predecessor] < positions[stage.id]
            for stage in spec.stages
            for predecessor in stage.predecessors
        )
        assert all(stage.budget is not None for stage in spec.stages)


def test_protocol_template_state_feedback_and_sealing_are_fail_closed() -> None:
    specs = _load_catalog()
    sealed_kinds = {
        ExperimentRegimeKind.generalization,
        ExperimentRegimeKind.cross_domain_transfer,
        ExperimentRegimeKind.evolutionary_search,
        ExperimentRegimeKind.meta_evolution,
    }
    for spec in specs.values():
        sealed_stages = [stage for stage in spec.stages if stage.sealed]
        assert bool(sealed_stages) == (spec.regime_kind in sealed_kinds)
        for stage in spec.stages:
            if not stage.predecessors:
                assert stage.state_policy == StageStatePolicy.reset
            if stage.state_policy == StageStatePolicy.fork:
                assert len(stage.predecessors) == 1
            if stage.state_policy in {
                StageStatePolicy.carry,
                StageStatePolicy.read_only,
            }:
                assert stage.predecessors
            if stage.sealed:
                assert stage is spec.stages[-1]
                assert stage.role == ExperimentStageRole.holdout
                assert stage.state_policy == StageStatePolicy.read_only
                assert stage.feedback_visibility == StageFeedbackVisibility.none

    robustness = specs["robustness-stress-protocol-template-v1.toml"]
    stress_stages = [
        stage for stage in robustness.stages if stage.role == ExperimentStageRole.stress
    ]
    assert len(stress_stages) == 2
    assert all(stage.state_policy == StageStatePolicy.fork for stage in stress_stages)
    assert {stage.predecessors for stage in stress_stages} == {("clean-reference",)}

    online = specs["online-adaptation-protocol-template-v1.toml"]
    online_positions = {stage.id: index for index, stage in enumerate(online.stages)}
    assert online_positions["pre-update-score-1"] < online_positions["adapt-after-score-1"]
    assert online_positions["pre-update-score-2"] < online_positions["adapt-after-score-2"]

    continual = specs["continual-learning-protocol-template-v1.toml"]
    learning_stages = [
        stage for stage in continual.stages if stage.role == ExperimentStageRole.adapt
    ]
    assert len(learning_stages) == 2
    assert {domain for stage in learning_stages for domain in stage.domains} == {
        "domain-a",
        "domain-b",
    }


def test_protocol_template_invariant_drift_is_rejected() -> None:
    specs = _load_catalog()

    generalization = specs[
        "generalization-protocol-template-v1.toml"
    ].model_dump(mode="json")
    generalization["stages"][-1]["feedback_visibility"] = "aggregate_only"
    with pytest.raises(ValidationError, match="sealed holdout forbids feedback"):
        ExperimentRegimeSpec.model_validate(generalization)

    curriculum = specs["curriculum-protocol-template-v1.toml"].model_dump(
        mode="json"
    )
    curriculum["stages"][1]["predecessors"] = []
    with pytest.raises(
        ValidationError,
        match="carry/read_only state requires a predecessor",
    ):
        ExperimentRegimeSpec.model_validate(curriculum)

    robustness = specs[
        "robustness-stress-protocol-template-v1.toml"
    ].model_dump(mode="json")
    robustness["stages"][1]["predecessors"] = [
        "adversarial-stress"
    ]
    with pytest.raises(ValidationError, match="topologically ordered"):
        ExperimentRegimeSpec.model_validate(robustness)


def test_protocol_templates_compile_dependency_closure_but_are_not_run_evidence() -> None:
    compiler = Compiler(ROOT)
    for spec in _load_catalog().values():
        artifact = compiler._regime_artifact(spec.id)
        expected_dependencies = {
            ("benchmark", stage.benchmark_id)
            for stage in spec.stages
        } | {
            ("dataset", stage.dataset_id)
            for stage in spec.stages
        } | {
            ("evaluator", stage.evaluator_id)
            for stage in spec.stages
        } | {
            ("protocol", stage.protocol_id)
            for stage in spec.stages
        } | {
            ("metric", metric_id)
            for stage in spec.stages
            for metric_id in stage.metric_ids
        }
        assert {
            (dependency.registry_kind, dependency.id)
            for dependency in artifact.dependencies
        } == expected_dependencies
        assert artifact.artifact_digest == artifact.canonical_digest()

        # This object binds declarations only.  Claim readiness additionally
        # requires runtime stage receipts, complete ledgers, and replay.
        artifact_payload = artifact.model_dump(mode="json")
        assert "stage_activation_receipts" not in artifact_payload
        assert "cell_ledgers" not in artifact_payload
        assert "claim_ready" not in artifact_payload
