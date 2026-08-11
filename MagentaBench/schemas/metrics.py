"""Replayable evaluation of TOML-registered metrics over planned rollouts."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from math import floor, isclose, prod, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable

from .models import (
    ArtifactRef,
    EvidenceBundle,
    MetricArtifact,
    MetricAcrossGroupAggregation,
    MetricComputationState,
    MetricFormula,
    MetricGroupKey,
    MetricGroupResult,
    MetricMissingDisposition,
    MetricResult,
    MetricSample,
    MetricSampleDisposition,
    MetricSource,
    MetricStatusDisposition,
    MetricUncertaintyMethod,
    MetricUncertaintyResult,
    RolloutTrajectory,
    RunStatus,
    ScheduleActivationReceipt,
    ResolvedBmpManifest,
)

import hashlib


def estimate_pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased HumanEval estimator for at least one success in ``k`` draws."""

    if n < 1 or c < 0 or c > n or k < 1 or k > n:
        raise ValueError("pass@k requires 0 <= c <= n and 1 <= k <= n")
    if n - c < k:
        return 1.0
    # Equivalent to 1 - C(n-c,k)/C(n,k), but avoids converting enormous
    # Python integers to float for large rollout populations.
    return 1.0 - prod(
        1.0 - (k / denominator)
        for denominator in range(n - c + 1, n + 1)
    )


def estimate_pass_power_k(n: int, c: int, k: int) -> float:
    """Reliability estimator for all ``k`` draws succeeding (Pass^k)."""

    if n < 1 or c < 0 or c > n or k < 1 or k > n:
        raise ValueError("Pass^k requires 0 <= c <= n and 1 <= k <= n")
    if c < k:
        return 0.0
    return prod((c - index) / (n - index) for index in range(k))


def expected_max_at_k(values: list[float], k: int) -> float:
    """Expected maximum of a uniform size-``k`` subset without replacement."""

    n = len(values)
    if n < 1 or k < 1 or k > n:
        raise ValueError("expected-max@k requires 1 <= k <= number of values")
    ordered = sorted(values)
    return sum(
        value
        * (k / index)
        * prod(
            (index - offset) / (n - offset)
            for offset in range(k)
        )
        for index, value in enumerate(ordered, start=1)
        if index >= k
    )


def quantile_linear(values: list[float], quantile: float) -> float:
    """NumPy-compatible linear quantile over a non-empty finite population."""

    if not values or quantile < 0.0 or quantile > 1.0:
        raise ValueError("linear quantile requires values and 0 <= q <= 1")
    ordered = sorted(values)
    position = quantile * (len(ordered) - 1)
    lower_index = floor(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )


def _artifact_ref(path: str | Path) -> ArtifactRef:
    resolved = Path(path).resolve()
    content = resolved.read_bytes()
    return ArtifactRef(
        path=str(resolved),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _sample_ledger_digest(samples: list[MetricSample]) -> str:
    payload = [
        {
            "attempt_id": sample.attempt_id,
            "case_id": sample.case_id,
            "attempt_index": sample.attempt_index,
            "status": sample.status.value,
            "disposition": sample.disposition.value,
            "value": sample.value,
        }
        for sample in samples
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _variance(values: list[float], *, sample: bool) -> float:
    if not values:
        raise ValueError("variance requires observations")
    if sample and len(values) < 2:
        raise ValueError("sample variance requires at least two observations")
    mean = sum(values) / len(values)
    denominator = len(values) - 1 if sample else len(values)
    return sum((value - mean) ** 2 for value in values) / denominator


def _reduce_scalar_values(
    formula: MetricFormula,
    values: list[float],
    *,
    quantile: float | None = None,
) -> tuple[float, float | None, float | None]:
    """Apply one built-in scalar reducer and retain ratio-style evidence."""

    if not values:
        raise ValueError("metric reduction requires observations")
    numerator = sum(values)
    denominator = float(len(values))
    if formula == MetricFormula.sum_v1:
        return numerator, numerator, None
    if formula == MetricFormula.minimum_v1:
        return min(values), None, None
    if formula == MetricFormula.maximum_v1:
        return max(values), None, None
    if formula == MetricFormula.median_v1:
        return quantile_linear(values, 0.5), None, None
    if formula == MetricFormula.quantile_linear_v1:
        if quantile is None:
            raise ValueError("quantile metric reduction lacks its registered quantile")
        return quantile_linear(values, quantile), None, None
    if formula == MetricFormula.variance_population_v1:
        return _variance(values, sample=False), None, None
    if formula == MetricFormula.variance_sample_v1:
        return _variance(values, sample=True), None, None
    if formula == MetricFormula.standard_deviation_population_v1:
        return sqrt(_variance(values, sample=False)), None, None
    if formula == MetricFormula.standard_deviation_sample_v1:
        return sqrt(_variance(values, sample=True)), None, None
    # ``mean_v1`` and ``pass_at_1_v1`` are both arithmetic means over their
    # explicitly registered populations.
    return numerator / denominator, numerator, denominator


def _sha256_counter_index(
    *, seed: int, replicate: int, draw: int, population_size: int
) -> int:
    """Versioned rejection-sampled index independent of Python's RNG state."""

    if population_size < 1:
        raise ValueError("bootstrap population must be non-empty")
    space = 1 << 256
    limit = space - (space % population_size)
    counter = 0
    while True:
        payload = (
            f"bmp-sha256-counter-v1:{seed}:{replicate}:{draw}:{counter}"
        ).encode("ascii")
        value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
        if value < limit:
            return value % population_size
        counter += 1


def _uncertainty_result(
    artifact: MetricArtifact,
    *,
    sample_values: list[float],
    group_values: list[float],
) -> MetricUncertaintyResult | None:
    spec = artifact.metric.uncertainty
    if spec is None:
        return None
    units = sample_values if spec.resampling_unit == "rollout" else group_values
    if not units:
        return None
    if spec.method == MetricUncertaintyMethod.wilson_score_v1:
        n = len(units)
        successes = sum(units)
        if any(value not in {0.0, 1.0} for value in units):
            raise ValueError("Wilson uncertainty requires binary rollout values")
        proportion = successes / n
        alpha = 1.0 - spec.confidence_level
        z = NormalDist().inv_cdf(1.0 - alpha / 2.0)
        z_squared = z * z
        denominator = 1.0 + z_squared / n
        center = (proportion + z_squared / (2.0 * n)) / denominator
        radius = (
            z
            * sqrt(
                proportion * (1.0 - proportion) / n
                + z_squared / (4.0 * n * n)
            )
            / denominator
        )
        return MetricUncertaintyResult(
            method=spec.method,
            confidence_level=spec.confidence_level,
            resampling_unit=spec.resampling_unit,
            unit_count=n,
            lower=max(0.0, center - radius),
            upper=min(1.0, center + radius),
            standard_error=sqrt(proportion * (1.0 - proportion) / n),
        )

    assert spec.resamples is not None and spec.seed is not None
    estimates = [
        sum(
            units[
                _sha256_counter_index(
                    seed=spec.seed,
                    replicate=replicate,
                    draw=draw,
                    population_size=len(units),
                )
            ]
            for draw in range(len(units))
        )
        / len(units)
        for replicate in range(spec.resamples)
    ]
    alpha = 1.0 - spec.confidence_level
    standard_error = (
        sqrt(_variance(estimates, sample=True)) if len(estimates) > 1 else 0.0
    )
    return MetricUncertaintyResult(
        method=spec.method,
        confidence_level=spec.confidence_level,
        resampling_unit=spec.resampling_unit,
        unit_count=len(units),
        lower=quantile_linear(estimates, alpha / 2.0),
        upper=quantile_linear(estimates, 1.0 - alpha / 2.0),
        standard_error=standard_error,
        resamples=spec.resamples,
        seed=spec.seed,
        rng_algorithm=spec.rng_algorithm,
        replicate_distribution_digest=hashlib.sha256(
            json.dumps(
                estimates,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        degenerate=len(set(estimates)) == 1,
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
        groups: tuple[MetricGroupResult, ...] = (),
        uncertainty: MetricUncertaintyResult | None = None,
    ) -> MetricResult:
        dispositions = Counter(sample.disposition for sample in samples)
        item = MetricResult(
            metric_id=artifact.metric.id,
            metric_digest=artifact.artifact_digest,
            sample_ledger_digest=_sample_ledger_digest(samples),
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
            groups=groups,
            uncertainty=uncertainty,
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
            elif metric.formula == MetricFormula.difference_v1:
                numerator = values[0] - values[1]
                denominator = None
                result(
                    artifact,
                    samples,
                    value=numerator * metric.scale,
                    state=MetricComputationState.complete,
                    numerator=numerator,
                )
                continue
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
                    if raw is not None and metric.formula in {
                        MetricFormula.pass_at_1_v1,
                        MetricFormula.pass_at_k_unbiased_v1,
                        MetricFormula.pass_power_k_v1,
                        MetricFormula.empirical_any_at_k_v1,
                        MetricFormula.empirical_all_at_k_v1,
                    }:
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
        elif metric.formula in {
            MetricFormula.variance_sample_v1,
            MetricFormula.standard_deviation_sample_v1,
        } and len(included) < 2:
            result(
                artifact,
                samples,
                value=None,
                state=MetricComputationState.unavailable,
                reason="sample dispersion requires at least two observations",
            )
        elif metric.formula in {
            MetricFormula.pass_at_k_unbiased_v1,
            MetricFormula.pass_power_k_v1,
            MetricFormula.empirical_any_at_k_v1,
            MetricFormula.empirical_all_at_k_v1,
            MetricFormula.expected_max_at_k_v1,
        }:
            k = metric.parameters.k if metric.parameters is not None else None
            if k is None:
                result(
                    artifact,
                    samples,
                    value=None,
                    state=MetricComputationState.invalid,
                    reason="registered k parameter is missing",
                )
                continue
            by_case: dict[str, list[MetricSample]] = defaultdict(list)
            for sample in samples:
                by_case[sample.case_id].append(sample)
            groups: list[MetricGroupResult] = []
            task_values: list[float] = []
            group_failure: str | None = None
            for case_id in sorted(by_case):
                case_samples = sorted(
                    by_case[case_id], key=lambda sample: sample.attempt_index
                )
                values = [sample.value for sample in case_samples]
                group_payload = {MetricGroupKey.task: case_id}
                attempt_ids = tuple(sample.attempt_id for sample in case_samples)
                if any(value is None for value in values):
                    group_failure = (
                        f"task {case_id!r} lacks a value for every planned rollout"
                    )
                    groups.append(
                        MetricGroupResult(
                            group=group_payload,
                            state=MetricComputationState.invalid,
                            reason=group_failure,
                            attempt_ids=attempt_ids,
                            included_count=sum(value is not None for value in values),
                        )
                    )
                    continue
                numeric = [float(value) for value in values]
                n = len(numeric)
                if n < k:
                    group_failure = f"task {case_id!r} has n={n}, below registered k={k}"
                    groups.append(
                        MetricGroupResult(
                            group=group_payload,
                            state=MetricComputationState.invalid,
                            reason=group_failure,
                            attempt_ids=attempt_ids,
                            included_count=n,
                        )
                    )
                    continue
                successes = sum(value == 1.0 for value in numeric)
                if metric.formula == MetricFormula.pass_at_k_unbiased_v1:
                    group_value = estimate_pass_at_k(n, successes, k)
                elif metric.formula == MetricFormula.pass_power_k_v1:
                    group_value = estimate_pass_power_k(n, successes, k)
                elif metric.formula == MetricFormula.empirical_any_at_k_v1:
                    group_value = float(any(value == 1.0 for value in numeric[:k]))
                elif metric.formula == MetricFormula.empirical_all_at_k_v1:
                    group_value = float(all(value == 1.0 for value in numeric[:k]))
                else:
                    group_value = expected_max_at_k(numeric, k)
                task_values.append(group_value)
                groups.append(
                    MetricGroupResult(
                        group=group_payload,
                        state=MetricComputationState.complete,
                        value=group_value,
                        attempt_ids=attempt_ids,
                        included_count=n,
                        population_count=n,
                        success_count=(
                            None
                            if metric.formula
                            == MetricFormula.expected_max_at_k_v1
                            else successes
                        ),
                        subset_size=k,
                    )
                )
            if group_failure is not None:
                result(
                    artifact,
                    samples,
                    value=None,
                    state=MetricComputationState.invalid,
                    reason=group_failure,
                    groups=tuple(groups),
                )
            else:
                result(
                    artifact,
                    samples,
                    value=sum(task_values) / len(task_values),
                    state=MetricComputationState.complete,
                    numerator=sum(task_values),
                    denominator=float(len(task_values)),
                    groups=tuple(groups),
                    uncertainty=_uncertainty_result(
                        artifact,
                        sample_values=[float(sample.value) for sample in samples],
                        group_values=task_values,
                    ),
                )
        else:
            quantile = (
                metric.parameters.quantile
                if metric.parameters is not None
                else None
            )
            if metric.group_by:
                unsupported_keys = sorted(
                    key.value
                    for key in metric.group_by
                    if key != MetricGroupKey.task
                )
                if unsupported_keys:
                    result(
                        artifact,
                        samples,
                        value=None,
                        state=MetricComputationState.invalid,
                        reason=(
                            "schedule metric source lacks typed coordinates for "
                            f"group keys: {unsupported_keys}"
                        ),
                    )
                    continue
                by_task: dict[str, list[MetricSample]] = defaultdict(list)
                for sample in samples:
                    by_task[sample.case_id].append(sample)
                groups: list[MetricGroupResult] = []
                group_values: list[float] = []
                group_failure: str | None = None
                for case_id in sorted(by_task):
                    task_samples = sorted(
                        by_task[case_id], key=lambda sample: sample.attempt_index
                    )
                    task_values = [
                        float(sample.value)
                        for sample in task_samples
                        if sample.value is not None
                    ]
                    if not task_values:
                        group_failure = (
                            f"task {case_id!r} contains no included observations"
                        )
                        groups.append(
                            MetricGroupResult(
                                group={MetricGroupKey.task: case_id},
                                state=MetricComputationState.unavailable,
                                reason=group_failure,
                                attempt_ids=tuple(
                                    sample.attempt_id for sample in task_samples
                                ),
                                included_count=0,
                            )
                        )
                        continue
                    if metric.formula in {
                        MetricFormula.variance_sample_v1,
                        MetricFormula.standard_deviation_sample_v1,
                    } and len(task_values) < 2:
                        group_failure = (
                            f"task {case_id!r} has fewer than two included observations"
                        )
                        groups.append(
                            MetricGroupResult(
                                group={MetricGroupKey.task: case_id},
                                state=MetricComputationState.unavailable,
                                reason=group_failure,
                                attempt_ids=tuple(
                                    sample.attempt_id for sample in task_samples
                                ),
                                included_count=len(task_values),
                            )
                        )
                        continue
                    group_value, group_numerator, group_denominator = (
                        _reduce_scalar_values(
                            metric.formula,
                            task_values,
                            quantile=quantile,
                        )
                    )
                    group_values.append(group_value)
                    groups.append(
                        MetricGroupResult(
                            group={MetricGroupKey.task: case_id},
                            state=MetricComputationState.complete,
                            value=group_value,
                            attempt_ids=tuple(
                                sample.attempt_id for sample in task_samples
                            ),
                            included_count=len(task_values),
                            numerator=group_numerator,
                            denominator=group_denominator,
                        )
                    )
                if group_failure is not None:
                    result(
                        artifact,
                        samples,
                        value=None,
                        state=MetricComputationState.unavailable,
                        reason=group_failure,
                        groups=tuple(groups),
                    )
                    continue
                if metric.across_groups == MetricAcrossGroupAggregation.minimum:
                    aggregate = min(group_values)
                    aggregate_numerator = None
                    aggregate_denominator = None
                else:
                    aggregate_numerator = sum(group_values)
                    aggregate_denominator = float(len(group_values))
                    aggregate = aggregate_numerator / aggregate_denominator
                result(
                    artifact,
                    samples,
                    value=aggregate,
                    state=MetricComputationState.complete,
                    numerator=aggregate_numerator,
                    denominator=aggregate_denominator,
                    groups=tuple(groups),
                    uncertainty=_uncertainty_result(
                        artifact,
                        sample_values=included,
                        group_values=group_values,
                    ),
                )
                continue

            aggregate, numerator, denominator = _reduce_scalar_values(
                metric.formula,
                included,
                quantile=quantile,
            )
            result(
                artifact,
                samples,
                value=aggregate,
                state=MetricComputationState.complete,
                numerator=numerator,
                denominator=denominator,
                uncertainty=_uncertainty_result(
                    artifact,
                    sample_values=included,
                    group_values=[],
                ),
            )

    return tuple(results)


__all__ = [
    "compute_metric_results",
    "estimate_pass_at_k",
    "estimate_pass_power_k",
    "expected_max_at_k",
    "quantile_linear",
]
