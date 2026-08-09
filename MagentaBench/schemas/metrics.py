"""Replayable evaluation of TOML-registered metrics over planned rollouts."""

from __future__ import annotations

from collections import Counter
from math import isclose
from pathlib import Path
from typing import Any, Callable

from .models import (
    ArtifactRef,
    EvidenceBundle,
    MetricArtifact,
    MetricComputationState,
    MetricFormula,
    MetricMissingDisposition,
    MetricResult,
    MetricSample,
    MetricSampleDisposition,
    MetricSource,
    MetricStatusDisposition,
    RolloutTrajectory,
    RunStatus,
    ScheduleActivationReceipt,
    ResolvedBmpManifest,
)

import hashlib


def _artifact_ref(path: str | Path) -> ArtifactRef:
    resolved = Path(path).resolve()
    content = resolved.read_bytes()
    return ArtifactRef(
        path=str(resolved),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _success(manifest: ResolvedBmpManifest, value: float) -> bool:
    binding = manifest.authoritative_metric_binding
    operator = binding.success_operator
    threshold = binding.success_threshold
    tolerance = binding.absolute_tolerance
    if operator is None or threshold is None:
        raise ValueError("binary metric requires a registered success rule")
    if operator == "eq":
        return isclose(value, threshold, rel_tol=0.0, abs_tol=tolerance)
    if operator == "gte":
        return value >= threshold - tolerance
    if operator == "lte":
        return value <= threshold + tolerance
    if operator == "gt":
        return value > threshold - tolerance
    if operator == "lt":
        return value < threshold + tolerance
    upper = binding.success_upper_bound
    if operator == "range" and upper is not None:
        return threshold - tolerance <= value <= upper + tolerance
    raise ValueError(f"unsupported success operator {operator!r}")


def _read_bundle(path: str, resolve_path: Callable[[str], Path]) -> EvidenceBundle:
    return EvidenceBundle.model_validate_json(resolve_path(path).read_bytes())


def _read_trajectory(
    bundle: EvidenceBundle,
    resolve_path: Callable[[str], Path],
) -> RolloutTrajectory | None:
    if bundle.trajectory_ref is None:
        return None
    return RolloutTrajectory.model_validate_json(
        resolve_path(bundle.trajectory_ref.path).read_bytes()
    )


def _missing_disposition(
    policy: MetricMissingDisposition,
) -> tuple[MetricSampleDisposition, float | None]:
    if policy == MetricMissingDisposition.zero:
        return MetricSampleDisposition.zero_filled, 0.0
    if policy == MetricMissingDisposition.exclude:
        return MetricSampleDisposition.excluded, None
    return MetricSampleDisposition.invalid, None


def _status_disposition(
    artifact: MetricArtifact,
    status: RunStatus,
) -> MetricStatusDisposition:
    return artifact.metric.status_policy.get(
        status.value,
        MetricStatusDisposition.observe,
    )


def compute_metric_results(
    manifest: ResolvedBmpManifest,
    manifest_digest: str,
    receipt: ScheduleActivationReceipt,
    receipt_path: str | Path,
    *,
    schedule_receipt_ref: ArtifactRef | None = None,
    resolve_path: Callable[[str], Path] = Path,
) -> tuple[MetricResult, ...]:
    """Compute every selected metric with an explicit sample for every slot."""

    attempt_by_id = {attempt.attempt_id: attempt for attempt in receipt.attempts}
    status_counts: Counter[RunStatus] = Counter()
    slots: list[dict[str, Any]] = []
    next_index_by_case: dict[str, int] = {}
    for allocation in receipt.budget_ledger.attempt_allocations:
        fallback_index = next_index_by_case.get(allocation.case_id, 0)
        next_index_by_case[allocation.case_id] = fallback_index + 1
        attempt = attempt_by_id.get(allocation.attempt_id)
        if attempt is None:
            status = RunStatus.infra_error
            bundle = None
            trajectory = None
            attempt_index = fallback_index
            bundle_ref = None
            trajectory_ref = None
        else:
            status = attempt.status
            attempt_index = attempt.attempt_index
            if attempt.evidence_bundle_ref is None:
                bundle = None
                trajectory = None
                bundle_ref = None
                trajectory_ref = None
            else:
                bundle_ref = attempt.evidence_bundle_ref
                bundle = _read_bundle(bundle_ref.path, resolve_path)
                trajectory = _read_trajectory(bundle, resolve_path)
                trajectory_ref = bundle.trajectory_ref
        status_counts[status] += 1
        slots.append(
            {
                "attempt_id": allocation.attempt_id,
                "case_id": allocation.case_id,
                "attempt_index": attempt_index,
                "status": status,
                "bundle": bundle,
                "trajectory": trajectory,
                "bundle_ref": bundle_ref,
                "trajectory_ref": trajectory_ref,
            }
        )

    planned_count = len(slots)
    task_count = len(receipt.budget_ledger.case_allocations)
    schedule_ref = (
        _artifact_ref(Path(receipt_path))
        if schedule_receipt_ref is None
        else schedule_receipt_ref
    )
    results: list[MetricResult] = []
    result_by_id: dict[str, MetricResult] = {}

    def result(
        artifact: MetricArtifact,
        samples: list[MetricSample],
        *,
        value: float | None,
        state: MetricComputationState,
        reason: str | None = None,
        numerator: float | None = None,
        denominator: float | None = None,
    ) -> MetricResult:
        dispositions = Counter(sample.disposition for sample in samples)
        item = MetricResult(
            metric_id=artifact.metric.id,
            metric_digest=artifact.artifact_digest,
            manifest_digest=manifest_digest,
            parent_run_id=manifest.metadata.run_id,
            schedule_receipt_ref=schedule_ref,
            state=state,
            value=value,
            reason=reason,
            planned_rollout_count=planned_count,
            task_count=task_count,
            rollouts_per_task=receipt.declared_rollouts_per_case,
            observed_count=dispositions[MetricSampleDisposition.observed],
            zero_filled_count=dispositions[MetricSampleDisposition.zero_filled],
            excluded_count=dispositions[MetricSampleDisposition.excluded],
            missing_count=dispositions[MetricSampleDisposition.missing],
            invalid_count=dispositions[MetricSampleDisposition.invalid],
            numerator=numerator,
            denominator=denominator,
            input_metric_ids=artifact.metric.inputs,
            status_counts=dict(status_counts),
            samples=tuple(samples),
        )
        results.append(item)
        result_by_id[item.metric_id] = item
        return item

    for artifact in manifest.metrics:
        metric = artifact.metric
        samples: list[MetricSample] = []

        if metric.source == MetricSource.derived:
            input_results = [result_by_id.get(metric_id) for metric_id in metric.inputs]
            template = next(
                (item for item in input_results if item is not None),
                None,
            )
            if template is None:
                for slot in slots:
                    samples.append(
                        MetricSample(
                            attempt_id=slot["attempt_id"],
                            case_id=slot["case_id"],
                            attempt_index=slot["attempt_index"],
                            status=slot["status"],
                            disposition=MetricSampleDisposition.invalid,
                            evidence_bundle_ref=slot["bundle_ref"],
                            trajectory_ref=slot["trajectory_ref"],
                        )
                    )
                result(
                    artifact,
                    samples,
                    value=None,
                    state=MetricComputationState.invalid,
                    reason="metric dependencies were not computed before this metric",
                )
                continue
            samples = list(template.samples)
            if any(
                item is None
                or item.state != MetricComputationState.complete
                or item.value is None
                for item in input_results
            ):
                result(
                    artifact,
                    samples,
                    value=None,
                    state=MetricComputationState.unavailable,
                    reason="one or more registered metric inputs lack an aggregate value",
                )
                continue
            values = [float(item.value) for item in input_results if item is not None]
            if metric.formula == MetricFormula.successes_per_million_tokens_v1:
                numerator = values[0] * 1_000_000.0
                denominator = values[1]
            else:
                numerator = values[0]
                denominator = values[1]
            if denominator <= 0:
                result(
                    artifact,
                    samples,
                    value=None,
                    state=MetricComputationState.unavailable,
                    reason="registered metric denominator is zero",
                    numerator=numerator,
                    denominator=denominator,
                )
            else:
                result(
                    artifact,
                    samples,
                    value=(numerator / denominator) * metric.scale,
                    state=MetricComputationState.complete,
                    numerator=numerator,
                    denominator=denominator,
                )
            continue

        if metric.formula == MetricFormula.completed_per_hour_v1:
            for slot in slots:
                launched = slot["bundle"] is not None
                samples.append(
                    MetricSample(
                        attempt_id=slot["attempt_id"],
                        case_id=slot["case_id"],
                        attempt_index=slot["attempt_index"],
                        status=slot["status"],
                        disposition=(
                            MetricSampleDisposition.observed
                            if launched
                            else MetricSampleDisposition.excluded
                        ),
                        value=1.0 if launched else None,
                        evidence_bundle_ref=slot["bundle_ref"],
                        trajectory_ref=slot["trajectory_ref"],
                    )
                )
            elapsed = receipt.budget_ledger.global_elapsed_wall_seconds
            completed = float(sum(sample.value or 0.0 for sample in samples))
            if elapsed <= 0:
                result(
                    artifact,
                    samples,
                    value=None,
                    state=MetricComputationState.unavailable,
                    reason="schedule wall-clock duration is zero",
                    numerator=completed,
                    denominator=elapsed,
                )
            else:
                result(
                    artifact,
                    samples,
                    value=completed * 3600.0 / elapsed,
                    state=MetricComputationState.complete,
                    numerator=completed,
                    denominator=elapsed,
                )
            continue

        for slot in slots:
            status = slot["status"]
            bundle: EvidenceBundle | None = slot["bundle"]
            trajectory: RolloutTrajectory | None = slot["trajectory"]
            disposition = _status_disposition(artifact, status)
            value: float | None = None

            if disposition == MetricStatusDisposition.zero:
                sample_disposition = MetricSampleDisposition.zero_filled
                value = 0.0
            elif disposition == MetricStatusDisposition.exclude:
                sample_disposition = MetricSampleDisposition.excluded
            elif disposition == MetricStatusDisposition.invalidate:
                sample_disposition = MetricSampleDisposition.invalid
            else:
                raw: int | float | None = None
                if metric.source == MetricSource.evaluator and bundle is not None:
                    verifier = bundle.verifier_evidence
                    if verifier is not None:
                        raw = verifier.metrics.get(
                            manifest.authoritative_reward_metric
                        )
                    if raw is not None and metric.formula == MetricFormula.pass_at_1_v1:
                        raw = 1.0 if _success(manifest, float(raw)) else 0.0
                elif metric.source == MetricSource.usage and bundle is not None:
                    usage = bundle.usage
                    raw = None if usage is None else getattr(usage, metric.source_field or "", None)
                elif metric.source == MetricSource.trajectory and trajectory is not None:
                    raw = getattr(trajectory.usage, metric.source_field or "", None)
                if raw is None:
                    sample_disposition, value = _missing_disposition(
                        metric.missing_observation
                    )
                else:
                    sample_disposition = MetricSampleDisposition.observed
                    value = float(raw) * metric.scale
            samples.append(
                MetricSample(
                    attempt_id=slot["attempt_id"],
                    case_id=slot["case_id"],
                    attempt_index=slot["attempt_index"],
                    status=status,
                    disposition=sample_disposition,
                    value=value,
                    evidence_bundle_ref=slot["bundle_ref"],
                    trajectory_ref=slot["trajectory_ref"],
                )
            )

        if metric.formula == MetricFormula.direct_v1:
            result(
                artifact,
                samples,
                value=None,
                state=MetricComputationState.complete,
            )
            continue
        invalid_count = sum(
            sample.disposition == MetricSampleDisposition.invalid
            for sample in samples
        )
        included = [
            float(sample.value)
            for sample in samples
            if sample.value is not None
        ]
        if invalid_count:
            result(
                artifact,
                samples,
                value=None,
                state=MetricComputationState.invalid,
                reason="one or more required metric observations are unavailable",
            )
        elif not included:
            result(
                artifact,
                samples,
                value=None,
                state=MetricComputationState.unavailable,
                reason="metric population contains no included observations",
            )
        else:
            numerator = sum(included)
            denominator = float(len(included))
            aggregate = (
                numerator
                if metric.formula == MetricFormula.sum_v1
                else numerator / denominator
            )
            result(
                artifact,
                samples,
                value=aggregate,
                state=MetricComputationState.complete,
                numerator=numerator,
                denominator=(
                    None if metric.formula == MetricFormula.sum_v1 else denominator
                ),
            )

    return tuple(results)


__all__ = ["compute_metric_results"]
