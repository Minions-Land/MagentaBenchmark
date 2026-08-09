from __future__ import annotations

from pathlib import Path

import pytest

from MagentaBench.runner.evidence import artifact_ref
from MagentaBench.schemas.compiler import canonical_digest
from MagentaBench.schemas.evolution import (
    EvolutionDiagnosisReceipt,
    EvolutionFailureSample,
    EvolutionFailureSamplingReceipt,
    MetaEvolutionEditPolicy,
    MetaEvolutionEditReceipt,
)


def _ref(root: Path, name: str, content: str):
    path = root / name
    path.write_text(content, encoding="utf-8")
    return artifact_ref(path)


def _sampling(root: Path) -> EvolutionFailureSamplingReceipt:
    success_ref = _ref(root, "success.json", '{"status":"success"}')
    failure_ref = _ref(root, "failure.json", '{"status":"infra_error"}')
    population = (
        EvolutionFailureSample(
            item_id="rollout-success",
            candidate_id="candidate-a",
            outcome="success",
            eligible=False,
            included=False,
            inclusion_probability=0.0,
            evidence_refs=(success_ref,),
        ),
        EvolutionFailureSample(
            item_id="rollout-failure",
            candidate_id="candidate-b",
            outcome="infra_error",
            eligible=True,
            included=True,
            inclusion_probability=1.0,
            evidence_refs=(failure_ref,),
        ),
    )
    return EvolutionFailureSamplingReceipt(
        sampling_id="failure-sampling-0000",
        policy_id="failure-sampling.all-failures.v1",
        population=population,
        population_digest=canonical_digest(
            [item.identity_data() for item in population]
        ),
        selected_item_ids=("rollout-failure",),
        sample_size=1,
        sampling_algorithm="all_failures",
        reason="diagnose every observed failure",
    )


def test_failure_sampling_retains_successes_and_infrastructure_failures(
    tmp_path: Path,
) -> None:
    receipt = _sampling(tmp_path)
    assert len(receipt.population) == 2
    assert receipt.selected_item_ids == ("rollout-failure",)

    payload = receipt.model_dump(mode="json")
    payload["population"] = payload["population"][1:]
    with pytest.raises(ValueError, match="population digest drift"):
        EvolutionFailureSamplingReceipt.model_validate(payload)


def test_diagnosis_binds_sample_component_and_io(tmp_path: Path) -> None:
    sampling = _sampling(tmp_path)
    component_ref = _ref(tmp_path, "diagnoser.py", "# diagnoser\n")
    prompt_ref = _ref(tmp_path, "prompt.json", '{"failure":"infra_error"}')
    result_ref = _ref(tmp_path, "diagnosis.json", '{"root_cause":"sandbox"}')
    receipt = EvolutionDiagnosisReceipt(
        diagnosis_id="diagnosis-0000",
        failure_sampling_id=sampling.sampling_id,
        sampled_item_id=sampling.selected_item_ids[0],
        candidate_id="candidate-b",
        diagnosis_component_digest=component_ref.sha256,
        prompt_ref=prompt_ref,
        result_ref=result_ref,
        failure_evidence_refs=(component_ref,),
        budget_event_id="budget-diagnosis-0000",
        root_cause="sandbox infrastructure exception",
        retry_recommended=True,
    )
    assert receipt.result_ref == result_ref


def test_meta_evolution_edit_policy_protects_holdout_and_registry_paths(
    tmp_path: Path,
) -> None:
    policy = MetaEvolutionEditPolicy(
        policy_id="meta-edit.evolver-prompt.v1",
        editable_paths=("evolver.prompt", "evolver.selection"),
        protected_paths=("registry", "dataset.holdout", "evaluator.holdout"),
        feedback_visibility=("search_scores", "failure_diagnostics"),
    )
    receipt = MetaEvolutionEditReceipt(
        edit_id="meta-edit-0000",
        policy=policy,
        policy_digest=policy.canonical_digest(),
        target_before_digest="1" * 64,
        target_after_digest="2" * 64,
        changed_paths=("evolver.prompt.system",),
        prompt_ref=_ref(tmp_path, "meta-prompt.json", "{}"),
        patch_ref=_ref(tmp_path, "meta-patch.json", "[]"),
        result_ref=_ref(tmp_path, "meta-result.json", "{}"),
        budget_event_id="budget-meta-agent-0000",
    )
    assert receipt.sealed_holdout_access_count == 0

    payload = receipt.model_dump(mode="json")
    payload["changed_paths"] = ["dataset.holdout.tasks"]
    with pytest.raises(ValueError, match="non-editable path"):
        MetaEvolutionEditReceipt.model_validate(payload)
