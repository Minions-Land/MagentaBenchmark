"""Deterministic paired statistical analysis shared by runner and verifier."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import sqrt
from statistics import NormalDist, mean
from typing import Any, Mapping, Sequence

from .models import StatisticalAnalysisPlan, StatisticalAnalysisReceipt


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class PairedScore:
    """One control/treatment observation matched on the declared unit."""

    unit_values: Mapping[str, Any]
    control_score: float
    treatment_score: float


@dataclass(frozen=True)
class StatisticalAnalysisResult:
    receipt: StatisticalAnalysisReceipt | None
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return self.receipt is not None and not self.errors


def benchmark_evaluation_split(benchmark: object) -> str | None:
    """Read one explicit adapter-owned evaluation split without guessing.

    Custom benchmark adapters commonly use one of these four configuration
    keys. Conflicting declarations are treated as unbound and therefore fail a
    holdout-required analysis.
    """

    explicit = getattr(benchmark, "evaluation_split", None)
    config = getattr(benchmark, "config", {})
    values = {
        value
        for value in (
            explicit,
            *(
                config.get(key)
                for key in (
                    "evaluation_split",
                    "holdout_split",
                    "task_split",
                    "split",
                )
            ),
        )
        if isinstance(value, str) and value
    } if isinstance(config, Mapping) else ({explicit} if isinstance(explicit, str) else set())
    return next(iter(values)) if len(values) == 1 else None


def analyze_paired_scores(
    plan: StatisticalAnalysisPlan,
    *,
    metric: str,
    observations: Sequence[PairedScore],
    evaluation_splits: Sequence[str | None],
    allow_no_holdout: bool,
) -> StatisticalAnalysisResult:
    """Derive a versioned receipt from paired scores and plan identity.

    Bonferroni is applied to the two-sided normal interval by dividing the
    family alpha. Sample variance uses Bessel's correction over paired
    treatment-minus-control differences.
    """

    errors: list[str] = []
    expected_fields = set(plan.paired_unit)
    entries: list[dict[str, Any]] = []
    seen_pairs: set[bytes] = set()
    repetitions: dict[bytes, set[bytes]] = {}
    base_fields = tuple(
        field for field in plan.paired_unit if field != plan.repetition_field
    )

    for observation in observations:
        values = dict(observation.unit_values)
        if set(values) != expected_fields:
            errors.append(
                "observed pairing fields differ from StatisticalAnalysisPlan.paired_unit"
            )
            continue
        pair_key = _canonical_bytes(values)
        if pair_key in seen_pairs:
            errors.append("paired unit contains duplicate control/treatment observations")
            continue
        seen_pairs.add(pair_key)
        base_key = _canonical_bytes({field: values[field] for field in base_fields})
        repetition_key = _canonical_bytes(values[plan.repetition_field])
        repetitions.setdefault(base_key, set()).add(repetition_key)
        entries.append(
            {
                "unit": {field: values[field] for field in plan.paired_unit},
                "control": observation.control_score,
                "treatment": observation.treatment_score,
                "difference": observation.treatment_score
                - observation.control_score,
            }
        )

    if not entries:
        return StatisticalAnalysisResult(
            receipt=None,
            errors=tuple(dict.fromkeys((*errors, "no paired scores were observed"))),
        )

    entries.sort(key=_canonical_bytes)
    differences = [float(entry["difference"]) for entry in entries]
    observed_min_repetitions = min(len(values) for values in repetitions.values())
    if observed_min_repetitions < plan.minimum_repetitions:
        errors.append(
            "observed repetitions per paired unit are below the declared minimum"
        )

    split_set = set(evaluation_splits)
    if plan.holdout_required:
        holdout_verified = split_set == {plan.holdout_split}
        if not holdout_verified:
            errors.append("benchmark holdout split does not match the analysis plan")
    else:
        holdout_verified = allow_no_holdout
        if not holdout_verified:
            errors.append("non-conformance analysis requires a declared holdout split")

    point_estimate = mean(differences)
    sample_variance: float | None = None
    standard_error: float | None = None
    interval: tuple[float, float] | None = None
    if len(differences) < 2:
        errors.append("sample variance requires at least two paired observations")
    else:
        sample_variance = sum(
            (value - point_estimate) ** 2 for value in differences
        ) / (len(differences) - 1)
        standard_error = sqrt(sample_variance / len(differences))
        family_divisor = (
            plan.family_size
            if plan.multiple_comparison_method == "bonferroni"
            else 1
        )
        effective_alpha = (1.0 - plan.confidence_level) / family_divisor
        critical = NormalDist().inv_cdf(1.0 - effective_alpha / 2.0)
        margin = critical * standard_error
        interval = (point_estimate - margin, point_estimate + margin)

    family_divisor = (
        plan.family_size if plan.multiple_comparison_method == "bonferroni" else 1
    )
    effective_alpha = (1.0 - plan.confidence_level) / family_divisor
    receipt = StatisticalAnalysisReceipt(
        plan_digest=plan.canonical_digest(),
        metric=metric,
        paired_unit=plan.paired_unit,
        repetition_field=plan.repetition_field,
        observed_pair_count=len(entries),
        observed_unit_count=len(repetitions),
        observed_min_repetitions=observed_min_repetitions,
        pairing_digest=hashlib.sha256(_canonical_bytes(entries)).hexdigest(),
        variance_method=plan.variance_method,
        sample_variance=sample_variance,
        standard_error=standard_error,
        ci_method=plan.ci_method,
        confidence_level=plan.confidence_level,
        effective_alpha=effective_alpha,
        point_estimate=point_estimate,
        confidence_interval=interval,
        holdout_split=plan.holdout_split,
        holdout_verified=holdout_verified,
        multiple_comparison_method=plan.multiple_comparison_method,
        family_size=plan.family_size,
        family_id=plan.family_id,
    )
    return StatisticalAnalysisResult(
        receipt=receipt,
        errors=tuple(dict.fromkeys(errors)),
    )


__all__ = [
    "PairedScore",
    "StatisticalAnalysisResult",
    "analyze_paired_scores",
    "benchmark_evaluation_split",
]
