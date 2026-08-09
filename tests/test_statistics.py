from __future__ import annotations

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import StatisticalAnalysisPlan, StatisticalAnalysisReceipt
from MagentaBench.schemas.statistics import PairedScore, analyze_paired_scores


def _plan(**updates: object) -> StatisticalAnalysisPlan:
    payload: dict[str, object] = {
        "paired_unit": ("case_id", "repetition"),
        "minimum_repetitions": 2,
        "holdout_required": True,
        "holdout_split": "test",
    }
    payload.update(updates)
    return StatisticalAnalysisPlan.model_validate(payload)


def test_paired_analysis_records_variance_ci_holdout_and_family_control() -> None:
    plan = _plan(multiple_comparison_method="bonferroni", family_size=2, family_id="fam-1")
    result = analyze_paired_scores(
        plan,
        metric="reward",
        observations=(
            PairedScore({"case_id": "case-1", "repetition": 0}, 0.0, 0.5),
            PairedScore({"case_id": "case-1", "repetition": 1}, 0.0, 1.0),
        ),
        evaluation_splits=("test", "test"),
        allow_no_holdout=False,
    )

    assert result.valid
    assert result.errors == ()
    assert result.receipt is not None
    receipt = result.receipt
    assert receipt.observed_pair_count == 2
    assert receipt.sample_variance == pytest.approx(0.125)
    assert receipt.point_estimate == pytest.approx(0.75)
    assert receipt.confidence_interval is not None
    assert receipt.effective_alpha == pytest.approx(0.025)
    assert receipt.holdout_verified is True


def test_paired_analysis_fails_closed_for_missing_holdout_or_repetition() -> None:
    result = analyze_paired_scores(
        _plan(),
        metric="reward",
        observations=(
            PairedScore({"case_id": "case-1", "repetition": 0}, 0.0, 1.0),
        ),
        evaluation_splits=("train",),
        allow_no_holdout=False,
    )

    assert not result.valid
    assert result.receipt is not None
    assert any("holdout" in error for error in result.errors)
    assert any("repetitions" in error or "variance" in error for error in result.errors)


def test_statistical_receipt_rejects_partial_variance_projection() -> None:
    with pytest.raises(ValidationError, match="all-or-none"):
        StatisticalAnalysisReceipt(
            plan_digest="a" * 64,
            metric="reward",
            paired_unit=("case_id", "repetition"),
            repetition_field="repetition",
            observed_pair_count=2,
            observed_unit_count=1,
            observed_min_repetitions=2,
            pairing_digest="b" * 64,
            variance_method="sample_variance_v1",
            sample_variance=0.1,
            standard_error=None,
            ci_method="normal_approximation_v1",
            confidence_level=0.95,
            effective_alpha=0.05,
            point_estimate=0.5,
            confidence_interval=None,
            holdout_split="test",
            holdout_verified=True,
            multiple_comparison_method="none",
            family_size=1,
        )
