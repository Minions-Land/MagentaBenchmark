from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.evidence import artifact_ref, atomic_write_json, sha256_file
from MagentaBench.runner.gates import evaluate_run_report
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    ComparisonKind,
    ObservationReport,
    RolloutTrajectory,
    RunPurpose,
    ScheduleActivationReceipt,
    TestOverrideReceipt as OverrideReceipt,
)

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "fake-sweep.toml"
_UNSET = object()
EXPECTED_RUN_IDS = tuple(
    f"fake-conformance-sweep__run{index:04d}" for index in range(8)
)


def _evaluate(completed, *, expected_run_ids=EXPECTED_RUN_IDS):
    return evaluate_run_report(
        completed=completed,
        expected_run_ids=expected_run_ids,
    )


def _rewrite_metric_evidence(
    item,
    *,
    authoritative_metric: str | None = None,
    metrics: dict[str, float] | None = None,
    score=_UNSET,
):
    manifest = item.plan.manifest
    if authoritative_metric is not None:
        evaluator_spec = manifest.evaluator.evaluator
        bindings = tuple(
            binding.model_copy(
                update={"source_key": authoritative_metric}
            )
            if binding.authoritative
            else binding
            for binding in evaluator_spec.metrics
        )
        evaluator_spec = evaluator_spec.model_copy(update={"metrics": bindings})
        evaluator = manifest.evaluator.model_copy(
            update={"evaluator": evaluator_spec, "artifact_digest": "0" * 64}
        )
        evaluator = evaluator.model_copy(
            update={"artifact_digest": evaluator.canonical_digest()}
        )
        manifest = manifest.model_copy(update={"evaluator": evaluator})
    plan = replace(item.plan, manifest=manifest)

    evidence = item.case.bundle.verifier_evidence
    assert evidence is not None
    evidence = evidence.model_copy(
        update={
            "metrics": evidence.metrics if metrics is None else metrics,
            "score": evidence.score if score is _UNSET else score,
        }
    )
    provenance = item.case.bundle.provenance.model_copy(
        update={"manifest_digest": plan.manifest_digest}
    )
    bundle = item.case.bundle.model_copy(
        update={"verifier_evidence": evidence, "provenance": provenance}
    )

    # A manifest/evaluator mutation changes the trajectory identity too. Keep
    # the persisted rollout evidence closed so the exploratory gate exercises
    # the intended evaluator binding rather than reporting unrelated lineage
    # drift.
    if bundle.trajectory_ref is not None:
        trajectory_path = Path(bundle.trajectory_ref.path)
        trajectory = RolloutTrajectory.model_validate_json(
            trajectory_path.read_bytes()
        )
        events = tuple(
            event.model_copy(
                update={
                    "details": {
                        **dict(event.details),
                        "metrics": dict(evidence.metrics),
                        "score": evidence.score,
                    }
                }
            )
            if event.kind.value == "evaluator_response"
            else event
            for event in trajectory.events
        )
        trajectory = trajectory.model_copy(
            update={
                "manifest_digest": plan.manifest_digest,
                "evaluator_digest": plan.manifest.evaluator.artifact_digest,
                "provenance": provenance,
                "verifier_evidence": evidence,
                "events": events,
            }
        )
        atomic_write_json(trajectory_path, trajectory)
        bundle = bundle.model_copy(update={"trajectory_ref": artifact_ref(trajectory_path)})
    atomic_write_json(item.case.bundle_path, bundle)
    bundle_ref = artifact_ref(item.case.bundle_path)
    case = replace(
        item.case,
        bundle=bundle,
        bundle_digest=sha256_file(item.case.bundle_path),
    )

    metric = plan.manifest.evaluator.evaluator.authoritative_metric.source_key
    attempts = tuple(
        attempt.model_copy(
            update={
                "evidence_bundle_ref": bundle_ref,
                "reward_metric": metric if metric in evidence.metrics else None,
                "reward_value": evidence.metrics.get(metric),
            }
        )
        for attempt in item.schedule_receipt.attempts
    )
    receipt = ScheduleActivationReceipt.model_validate(
        item.schedule_receipt.model_copy(
            update={"attempts": attempts}
        ).model_dump(mode="json")
    )
    atomic_write_json(item.schedule_receipt_path, receipt)
    return replace(
        item,
        plan=plan,
        case=case,
        schedule_receipt=receipt,
        schedule_receipt_sha256=sha256_file(item.schedule_receipt_path),
    )


def _rewrite_receipt(item, **updates):
    receipt = ScheduleActivationReceipt.model_validate(
        item.schedule_receipt.model_copy(update=updates).model_dump(mode="json")
    )
    atomic_write_json(item.schedule_receipt_path, receipt)
    return replace(
        item,
        schedule_receipt=receipt,
        schedule_receipt_sha256=sha256_file(item.schedule_receipt_path),
    )


def _rewrite_provenance(item, **updates):
    provenance = item.case.bundle.provenance.model_copy(update=updates)
    bundle = item.case.bundle.model_copy(update={"provenance": provenance})
    atomic_write_json(item.case.bundle_path, bundle)
    bundle_ref = artifact_ref(item.case.bundle_path)
    case = replace(
        item.case,
        bundle=bundle,
        bundle_digest=sha256_file(item.case.bundle_path),
    )
    attempts = tuple(
        attempt.model_copy(update={"evidence_bundle_ref": bundle_ref})
        for attempt in item.schedule_receipt.attempts
    )
    receipt = ScheduleActivationReceipt.model_validate(
        item.schedule_receipt.model_copy(
            update={"attempts": attempts}
        ).model_dump(mode="json")
    )
    atomic_write_json(item.schedule_receipt_path, receipt)
    return replace(
        item,
        case=case,
        schedule_receipt=receipt,
        schedule_receipt_sha256=sha256_file(item.schedule_receipt_path),
    )


def _rewrite_persisted_bundle_only(item, **provenance_updates):
    provenance = item.case.bundle.provenance.model_copy(update=provenance_updates)
    persisted_bundle = item.case.bundle.model_copy(update={"provenance": provenance})
    atomic_write_json(item.case.bundle_path, persisted_bundle)
    bundle_ref = artifact_ref(item.case.bundle_path)
    case = replace(
        item.case,
        bundle_digest=sha256_file(item.case.bundle_path),
    )
    attempts = tuple(
        attempt.model_copy(update={"evidence_bundle_ref": bundle_ref})
        for attempt in item.schedule_receipt.attempts
    )
    receipt = ScheduleActivationReceipt.model_validate(
        item.schedule_receipt.model_copy(
            update={"attempts": attempts}
        ).model_dump(mode="json")
    )
    atomic_write_json(item.schedule_receipt_path, receipt)
    return replace(
        item,
        case=case,
        schedule_receipt=receipt,
        schedule_receipt_sha256=sha256_file(item.schedule_receipt_path),
    )


def test_exploratory_conformance_is_structurally_not_a_claim(tmp_path: Path) -> None:
    completed = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    report = evaluate_run_report(
        completed=completed,
        expected_run_ids=EXPECTED_RUN_IDS,
    )
    assert isinstance(report, ObservationReport)
    assert report.purpose == RunPurpose.exploratory
    assert report.observations
    assert report.isolation_valid is False
    assert any("NetworkObservation missing" in item for item in report.isolation_reasons)
    serialized = report.model_dump(mode="json")
    assert "gates" not in serialized
    assert "claim_eligible" not in serialized
    assert "effect" not in serialized
    assert "effect_is_causal_claim" not in serialized


def test_exploratory_report_rejects_protocol_digest_drift(tmp_path: Path) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    completed[0] = _rewrite_receipt(completed[0], protocol_digest="0" * 64)
    with pytest.raises(ValueError, match="protocol_digest"):
        _evaluate(completed)


def test_exploratory_report_rejects_invalid_schedule_receipt(tmp_path: Path) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    completed[0] = _rewrite_receipt(
        completed[0],
        schedule_valid=False,
        mismatch_reasons=("injected invalid schedule",),
    )
    with pytest.raises(ValueError, match="injected invalid schedule"):
        _evaluate(completed)


def test_exploratory_report_rejects_provenance_identity_drift(
    tmp_path: Path,
) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    completed[0] = _rewrite_provenance(
        completed[0],
        manifest_digest="0" * 64,
        benchmark_digest="1" * 64,
        subject_digest="2" * 64,
        backend_digest="3" * 64,
    )
    with pytest.raises(ValueError, match="evidence integrity failed") as error:
        _evaluate(completed)
    message = str(error.value)
    for expected in (
        "manifest digest drift",
        "benchmark digest drift",
        "subject digest drift",
        "backend digest drift",
    ):
        assert expected in message


def test_exploratory_report_rejects_corrupt_persisted_receipt(tmp_path: Path) -> None:
    completed = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    completed[0].schedule_receipt_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="persisted schedule receipt"):
        _evaluate(completed)


def test_exploratory_report_rejects_persisted_bundle_model_drift(
    tmp_path: Path,
) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    completed[0] = _rewrite_persisted_bundle_only(
        completed[0], manifest_digest="0" * 64
    )
    with pytest.raises(
        ValueError, match="persisted evidence bundle differs from in-memory bundle"
    ):
        _evaluate(completed)


def test_exploratory_report_rejects_incomplete_plan(tmp_path: Path) -> None:
    completed = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    with pytest.raises(ValueError, match="7 of 8 planned runs"):
        _evaluate(completed[:-1])


def test_exploratory_report_rejects_same_size_run_substitution(
    tmp_path: Path,
) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    missing_id = completed[-1].plan.manifest.metadata.run_id
    duplicate_id = completed[0].plan.manifest.metadata.run_id
    completed[-1] = completed[0]
    with pytest.raises(ValueError, match="plan coverage mismatch") as error:
        _evaluate(completed)
    message = str(error.value)
    assert missing_id in message
    assert duplicate_id in message
    assert "duplicates=" in message


def test_exploratory_report_uses_declared_authoritative_metric(
    tmp_path: Path,
) -> None:
    completed = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    remetriced = tuple(
        _rewrite_metric_evidence(
            item,
            authoritative_metric="overall",
            metrics={"overall": item.case.bundle.verifier_evidence.score},
        )
        for item in completed
    )
    report = _evaluate(remetriced)
    assert report.observations[0].metric == "overall"
    assert report.observations[0].value == 0.5


def test_exploratory_report_rejects_metric_disagreement(tmp_path: Path) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    score = completed[0].case.bundle.verifier_evidence.score
    completed[0] = _rewrite_metric_evidence(
        completed[0],
        authoritative_metric="overall",
        metrics={"overall": score},
    )
    with pytest.raises(ValueError, match="metric differs across included runs"):
        _evaluate(completed)


def test_exploratory_report_rejects_missing_authoritative_metric(
    tmp_path: Path,
) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    completed[0] = _rewrite_metric_evidence(
        completed[0], metrics={"unrelated": 0.5}
    )
    with pytest.raises(ValueError, match="is missing from verifier evidence"):
        _evaluate(completed)


def test_exploratory_report_rejects_metric_score_disagreement(
    tmp_path: Path,
) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    original_score = completed[0].case.bundle.verifier_evidence.score
    conflicting_score = original_score + 5e-13
    completed[0] = _rewrite_metric_evidence(
        completed[0], metrics={"exact_match": conflicting_score}
    )
    with pytest.raises(ValueError, match="disagrees with verifier score"):
        _evaluate(completed)


def test_claim_report_identity_and_contrast_accept_no_caller_overrides(
    tmp_path: Path,
) -> None:
    pipeline_result = Pipeline(ROOT, tmp_path).run(EXPERIMENT)
    registered_subject = Compiler(ROOT)._subject_artifact("fake.nonfake")
    completed = tuple(
        replace(
            item,
            plan=replace(
                item.plan,
                manifest=item.plan.manifest.model_copy(
                    update={
                        "claim_design": item.plan.manifest.claim_design.model_copy(
                            update={
                                "comparison_kind": ComparisonKind.coding_agent,
                                "purpose": RunPurpose.claim,
                                "intervention_factor_id": "conformance.fake-subject",
                            }
                        ),
                        "subject": registered_subject,
                    }
                ),
            ),
        )
        for item in pipeline_result.runs
    )

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        evaluate_run_report(
            completed=completed,
            expected_run_ids=EXPECTED_RUN_IDS,
            experiment_id="forged-experiment",  # type: ignore[call-arg]
        )

    index_ref = artifact_ref(pipeline_result.report_path.parent / "record_index.json")
    report = evaluate_run_report(
        completed=completed,
        expected_run_ids=EXPECTED_RUN_IDS,
        record_index_ref=index_ref,
    )

    assert report.experiment_id == "fake-conformance-sweep"
    assert report.record_index_ref == index_ref
    assert report.effect is not None
    assert report.effect.point_estimate == 1.0


def test_exploratory_report_rejects_execution_valid_without_score(
    tmp_path: Path,
) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    completed[0] = _rewrite_metric_evidence(
        completed[0], metrics={}, score=None
    )
    with pytest.raises(ValueError, match="execution-valid bundle lacks verifier score"):
        _evaluate(completed)


def test_exploratory_report_rejects_empty_metrics_for_scored_evidence(
    tmp_path: Path,
) -> None:
    completed = list(Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs)
    completed[0] = _rewrite_metric_evidence(completed[0], metrics={})
    with pytest.raises(ValueError, match="verifier metrics are empty"):
        _evaluate(completed)


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
            completed=completed,
            expected_run_ids=EXPECTED_RUN_IDS,
        )
