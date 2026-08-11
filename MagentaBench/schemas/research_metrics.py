"""Fail-closed research metrics for Agent and RSI experiments.

This module deliberately keeps the metric algebra separate from registry and
runner integration.  Every calculator consumes a pre-declared population,
aligns observations to that population, and emits an immutable receipt that
retains disposition counts and content-addressed lineage.  Missing observations
are never removed from a denominator.

The formulas are intentionally small and source-closed so the runner and a
standalone verifier can call the same pure functions.
"""

from __future__ import annotations

from bisect import bisect_left
from enum import Enum
from math import log
import re
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field, field_validator, model_validator

from .compiler import canonical_digest
from .models import (
    ID_PATTERN,
    SHA256_PATTERN,
    MetricComputationState,
    StrictModel,
)
from .regime import ExperimentCellDisposition, ExperimentCellLedger


class ResearchMetricDisposition(str, Enum):
    """Disposition of one unit in a pre-declared research population."""

    observed = "observed"
    zero_filled = "zero_filled"
    excluded = "excluded"
    missing = "missing"
    invalid = "invalid"
    censored = "censored"


class ResearchMetricDirection(str, Enum):
    maximize = "maximize"
    minimize = "minimize"


class ResearchMetric(str, Enum):
    generalization_group_macro_v1 = "generalization_group_macro.v1"
    generalization_worst_group_v1 = "generalization_worst_group.v1"
    generalization_id_minus_ood_gap_v1 = (
        "generalization_id_minus_ood_gap.v1"
    )
    generalization_scenario_goal_completion_v1 = (
        "generalization_scenario_goal_completion.v1"
    )
    evolution_parent_delta_macro_v1 = "evolution_parent_delta_macro.v1"
    evolution_best_so_far_final_v1 = "evolution_best_so_far_final.v1"
    evolution_final_gain_v1 = "evolution_final_gain.v1"
    evolution_candidate_yield_v1 = "evolution_candidate_yield.v1"
    evolution_aubc_common_budget_step_v1 = (
        "evolution_aubc_common_budget_step.v1"
    )
    meta_evolution_paired_delta_macro_v1 = (
        "meta_evolution_paired_delta_macro.v1"
    )
    trajectory_final_progress_macro_v1 = (
        "trajectory_final_progress_macro.v1"
    )
    trajectory_aupc_trapezoid_macro_v1 = (
        "trajectory_aupc_trapezoid_macro.v1"
    )
    trajectory_regression_mass_macro_v1 = (
        "trajectory_regression_mass_macro.v1"
    )
    trajectory_time_to_threshold_rmst_v1 = (
        "trajectory_time_to_threshold_rmst.v1"
    )
    calibration_brier_v1 = "calibration_brier.v1"
    calibration_binary_nll_clipped_v1 = "calibration_binary_nll_clipped.v1"
    calibration_ece_v1 = "calibration_ece.v1"
    calibration_confidence_coverage_v1 = (
        "calibration_confidence_coverage.v1"
    )
    selector_entropy_nats_v1 = "selector_entropy_nats.v1"
    selector_effective_sample_size_v1 = (
        "selector_effective_sample_size.v1"
    )


class ResearchMetricContribution(StrictModel):
    """One replayable intermediate reduction retained in a metric receipt."""

    contribution_id: str = Field(pattern=ID_PATTERN)
    source_ids: tuple[str, ...] = ()
    value: float
    weight: float = Field(default=1.0, ge=0.0, strict=True)
    censored: bool = False

    @field_validator("source_ids")
    @classmethod
    def source_ids_are_unique_bmp_ids(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("research metric contribution source ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("research metric contribution source ids must be BMP ids")
        return values


class ResearchMetricReceipt(StrictModel):
    """Content-addressed scalar result over a closed planned population."""

    format: Literal["bmp-research-metric-receipt-v1"] = (
        "bmp-research-metric-receipt-v1"
    )
    metric: ResearchMetric
    state: MetricComputationState
    value: float | None = None
    reason: str | None = Field(default=None, min_length=1)
    planned_count: int = Field(ge=1, strict=True)
    observed_count: int = Field(ge=0, strict=True)
    zero_filled_count: int = Field(ge=0, strict=True)
    excluded_count: int = Field(ge=0, strict=True)
    missing_count: int = Field(ge=0, strict=True)
    invalid_count: int = Field(ge=0, strict=True)
    censored_count: int = Field(ge=0, strict=True)
    planned_ids: tuple[str, ...]
    contributing_ids: tuple[str, ...]
    population_digest: str = Field(pattern=SHA256_PATTERN)
    observation_digest: str = Field(pattern=SHA256_PATTERN)
    parameters_digest: str = Field(pattern=SHA256_PATTERN)
    contributions: tuple[ResearchMetricContribution, ...] = ()

    @field_validator("planned_ids", "contributing_ids")
    @classmethod
    def receipt_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("research metric receipt ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("research metric receipt ids must be BMP ids")
        return values

    @model_validator(mode="after")
    def receipt_is_closed_and_coherent(self) -> "ResearchMetricReceipt":
        counts = (
            self.observed_count
            + self.zero_filled_count
            + self.excluded_count
            + self.missing_count
            + self.invalid_count
            + self.censored_count
        )
        if counts != self.planned_count:
            raise ValueError("research metric disposition counts must cover the plan")
        if len(self.planned_ids) != self.planned_count:
            raise ValueError("research metric planned ids must match planned_count")
        planned = set(self.planned_ids)
        contributing = set(self.contributing_ids)
        if not contributing.issubset(planned):
            raise ValueError("research metric contributions must come from the plan")
        if len(self.contributing_ids) != (
            self.observed_count + self.zero_filled_count + self.censored_count
        ):
            raise ValueError(
                "research metric contributing ids must match included dispositions"
            )
        component_ids = [item.contribution_id for item in self.contributions]
        if len(set(component_ids)) != len(component_ids):
            raise ValueError("research metric contribution ids must be unique")
        if any(
            not set(item.source_ids).issubset(contributing)
            for item in self.contributions
        ):
            raise ValueError("research metric intermediate sources must contribute")
        if self.state == MetricComputationState.complete:
            if self.value is None or self.reason is not None:
                raise ValueError("complete research metric requires a value")
        elif self.value is not None or self.reason is None:
            raise ValueError("failed research metric requires a reason and no value")
        if self.state != MetricComputationState.complete and self.contributions:
            raise ValueError("failed research metrics forbid partial reductions")
        return self

    def canonical_digest(self) -> str:
        return canonical_digest(self)


class GeneralizationMetricsReceipt(StrictModel):
    group_macro: ResearchMetricReceipt
    worst_group: ResearchMetricReceipt
    id_ood_gap: ResearchMetricReceipt
    scenario_goal_completion: ResearchMetricReceipt

    @model_validator(mode="after")
    def metrics_match_fields(self) -> "GeneralizationMetricsReceipt":
        expected = (
            (self.group_macro, ResearchMetric.generalization_group_macro_v1),
            (self.worst_group, ResearchMetric.generalization_worst_group_v1),
            (
                self.id_ood_gap,
                ResearchMetric.generalization_id_minus_ood_gap_v1,
            ),
            (
                self.scenario_goal_completion,
                ResearchMetric.generalization_scenario_goal_completion_v1,
            ),
        )
        if any(receipt.metric != metric for receipt, metric in expected):
            raise ValueError("generalization receipt fields contain wrong metrics")
        return self


class EvolutionCurveMetricsReceipt(StrictModel):
    best_so_far_final: ResearchMetricReceipt
    final_gain: ResearchMetricReceipt
    aubc: ResearchMetricReceipt

    @model_validator(mode="after")
    def metrics_match_fields(self) -> "EvolutionCurveMetricsReceipt":
        expected = (
            (
                self.best_so_far_final,
                ResearchMetric.evolution_best_so_far_final_v1,
            ),
            (self.final_gain, ResearchMetric.evolution_final_gain_v1),
            (
                self.aubc,
                ResearchMetric.evolution_aubc_common_budget_step_v1,
            ),
        )
        if any(receipt.metric != metric for receipt, metric in expected):
            raise ValueError("evolution curve receipt fields contain wrong metrics")
        return self


class TrajectoryMetricsReceipt(StrictModel):
    final_progress: ResearchMetricReceipt
    aupc: ResearchMetricReceipt
    regression_mass: ResearchMetricReceipt
    time_to_threshold: ResearchMetricReceipt

    @model_validator(mode="after")
    def metrics_match_fields(self) -> "TrajectoryMetricsReceipt":
        expected = (
            (
                self.final_progress,
                ResearchMetric.trajectory_final_progress_macro_v1,
            ),
            (self.aupc, ResearchMetric.trajectory_aupc_trapezoid_macro_v1),
            (
                self.regression_mass,
                ResearchMetric.trajectory_regression_mass_macro_v1,
            ),
            (
                self.time_to_threshold,
                ResearchMetric.trajectory_time_to_threshold_rmst_v1,
            ),
        )
        if any(receipt.metric != metric for receipt, metric in expected):
            raise ValueError("trajectory receipt fields contain wrong metrics")
        return self


class CalibrationMetricsReceipt(StrictModel):
    brier: ResearchMetricReceipt
    nll: ResearchMetricReceipt
    ece: ResearchMetricReceipt
    confidence_coverage: ResearchMetricReceipt

    @model_validator(mode="after")
    def metrics_match_fields(self) -> "CalibrationMetricsReceipt":
        expected = (
            (self.brier, ResearchMetric.calibration_brier_v1),
            (self.nll, ResearchMetric.calibration_binary_nll_clipped_v1),
            (self.ece, ResearchMetric.calibration_ece_v1),
            (
                self.confidence_coverage,
                ResearchMetric.calibration_confidence_coverage_v1,
            ),
        )
        if any(receipt.metric != metric for receipt, metric in expected):
            raise ValueError("calibration receipt fields contain wrong metrics")
        return self


class SelectorMetricsReceipt(StrictModel):
    entropy: ResearchMetricReceipt
    effective_sample_size: ResearchMetricReceipt

    @model_validator(mode="after")
    def metrics_match_fields(self) -> "SelectorMetricsReceipt":
        expected = (
            (self.entropy, ResearchMetric.selector_entropy_nats_v1),
            (
                self.effective_sample_size,
                ResearchMetric.selector_effective_sample_size_v1,
            ),
        )
        if any(receipt.metric != metric for receipt, metric in expected):
            raise ValueError("selector receipt fields contain wrong metrics")
        return self


_INCLUDED = {
    ResearchMetricDisposition.observed,
    ResearchMetricDisposition.zero_filled,
}

_DISPOSITION_PRECEDENCE = {
    ResearchMetricDisposition.observed: 0,
    ResearchMetricDisposition.zero_filled: 1,
    ResearchMetricDisposition.censored: 2,
    ResearchMetricDisposition.excluded: 3,
    ResearchMetricDisposition.missing: 4,
    ResearchMetricDisposition.invalid: 5,
}


def _ids_are_unique(values: Sequence[str], *, field_name: str) -> None:
    if not values or len(set(values)) != len(values):
        raise ValueError(f"{field_name} must be non-empty and unique")
    if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
        raise ValueError(f"{field_name} must contain valid BMP ids")


def _counts(
    dispositions: Sequence[ResearchMetricDisposition],
) -> dict[ResearchMetricDisposition, int]:
    return {
        disposition: sum(value == disposition for value in dispositions)
        for disposition in ResearchMetricDisposition
    }


def _combined_disposition(
    dispositions: Sequence[ResearchMetricDisposition],
) -> ResearchMetricDisposition:
    if not dispositions:
        return ResearchMetricDisposition.missing
    return max(dispositions, key=_DISPOSITION_PRECEDENCE.__getitem__)


def _receipt(
    *,
    metric: ResearchMetric,
    planned_ids: Sequence[str],
    dispositions: Sequence[ResearchMetricDisposition],
    population_identity: Mapping[str, Any],
    observation_identity: Sequence[Mapping[str, Any]],
    parameters: StrictModel,
    value: float | None = None,
    reason: str | None = None,
    contributions: Sequence[ResearchMetricContribution] = (),
) -> ResearchMetricReceipt:
    if len(dispositions) != len(planned_ids):
        raise ValueError("research metric dispositions must align with planned ids")
    disposition_counts = _counts(dispositions)
    contributors = tuple(
        item_id
        for item_id, disposition in zip(planned_ids, dispositions, strict=True)
        if disposition
        in {
            ResearchMetricDisposition.observed,
            ResearchMetricDisposition.zero_filled,
            ResearchMetricDisposition.censored,
        }
    )
    complete = value is not None and reason is None
    return ResearchMetricReceipt(
        metric=metric,
        state=(
            MetricComputationState.complete
            if complete
            else MetricComputationState.invalid
        ),
        value=value,
        reason=reason,
        planned_count=len(planned_ids),
        observed_count=disposition_counts[ResearchMetricDisposition.observed],
        zero_filled_count=disposition_counts[
            ResearchMetricDisposition.zero_filled
        ],
        excluded_count=disposition_counts[ResearchMetricDisposition.excluded],
        missing_count=disposition_counts[ResearchMetricDisposition.missing],
        invalid_count=disposition_counts[ResearchMetricDisposition.invalid],
        censored_count=disposition_counts[ResearchMetricDisposition.censored],
        planned_ids=tuple(planned_ids),
        contributing_ids=contributors,
        population_digest=canonical_digest(population_identity),
        observation_digest=canonical_digest(list(observation_identity)),
        parameters_digest=canonical_digest(parameters),
        contributions=tuple(contributions) if complete else (),
    )


def _invalid_receipt(
    *,
    metric: ResearchMetric,
    planned_ids: Sequence[str],
    dispositions: Sequence[ResearchMetricDisposition],
    population_identity: Mapping[str, Any],
    observation_identity: Sequence[Mapping[str, Any]],
    parameters: StrictModel,
    reason: str,
) -> ResearchMetricReceipt:
    return _receipt(
        metric=metric,
        planned_ids=planned_ids,
        dispositions=dispositions,
        population_identity=population_identity,
        observation_identity=observation_identity,
        parameters=parameters,
        reason=reason,
    )


def _experiment_disposition(
    value: ExperimentCellDisposition,
) -> ResearchMetricDisposition:
    return ResearchMetricDisposition(value.value)


# ---------------------------------------------------------------------------
# Generalization


GeneralizationAxis = Literal[
    "task", "domain", "scenario", "variant", "generation"
]


class GeneralizationAnalysisPlan(StrictModel):
    """Pre-registered membership semantics for generalization reductions."""

    metric_id: str = Field(pattern=ID_PATTERN)
    metric_digest: str = Field(pattern=SHA256_PATTERN)
    group_axis: GeneralizationAxis
    distribution_axis: GeneralizationAxis = "domain"
    in_distribution_values: tuple[str, ...]
    out_of_distribution_values: tuple[str, ...]
    direction: ResearchMetricDirection = ResearchMetricDirection.maximize

    @model_validator(mode="after")
    def distributions_form_disjoint_declared_sets(self) -> "GeneralizationAnalysisPlan":
        _ids_are_unique(
            self.in_distribution_values, field_name="in-distribution values"
        )
        _ids_are_unique(
            self.out_of_distribution_values, field_name="out-of-distribution values"
        )
        if set(self.in_distribution_values) & set(self.out_of_distribution_values):
            raise ValueError("ID and OOD membership values must be disjoint")
        return self


def _weighted_mean(values: Sequence[tuple[float, float]]) -> float:
    denominator = sum(weight for _, weight in values)
    if denominator <= 0:
        raise ValueError("weighted mean requires positive total weight")
    return sum(value * weight for value, weight in values) / denominator


def analyze_generalization(
    ledger: ExperimentCellLedger,
    plan: GeneralizationAnalysisPlan,
) -> GeneralizationMetricsReceipt:
    """Compute macro, worst-group, ID-OOD gap, and AppWorld-style SGC.

    SGC first computes a weighted mean for each declared scenario/variant cell,
    takes the worse variant within each scenario according to ``direction``,
    and finally macro-averages scenarios.  All four metrics use the entire
    target-metric population from ``ledger.plan``.
    """

    target_indices = [
        index
        for index, cell in enumerate(ledger.plan.cells)
        if cell.metric_id == plan.metric_id
    ]
    if not target_indices:
        raise ValueError("generalization ledger has no cells for the target metric")
    target_cells = [ledger.plan.cells[index] for index in target_indices]
    drifted = [
        cell.cell_id for cell in target_cells if cell.metric_digest != plan.metric_digest
    ]
    if drifted:
        raise ValueError("generalization target metric digest drift")
    observations = [ledger.observations[index] for index in target_indices]
    planned_ids = tuple(cell.cell_id for cell in target_cells)
    dispositions = tuple(
        _experiment_disposition(observation.disposition)
        for observation in observations
    )
    population_identity = {
        "source_plan_digest": ledger.plan.plan_digest,
        "metric_id": plan.metric_id,
        "metric_digest": plan.metric_digest,
        "cells": [cell.model_dump(mode="json") for cell in target_cells],
    }
    observation_identity = [
        observation.model_dump(mode="json") for observation in observations
    ]
    unavailable = any(disposition not in _INCLUDED for disposition in dispositions)

    def invalid(metric: ResearchMetric, reason: str) -> ResearchMetricReceipt:
        return _invalid_receipt(
            metric=metric,
            planned_ids=planned_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            reason=reason,
        )

    if unavailable:
        reason = "one or more required planned generalization cells are unavailable"
        return GeneralizationMetricsReceipt(
            group_macro=invalid(
                ResearchMetric.generalization_group_macro_v1, reason
            ),
            worst_group=invalid(
                ResearchMetric.generalization_worst_group_v1, reason
            ),
            id_ood_gap=invalid(
                ResearchMetric.generalization_id_minus_ood_gap_v1, reason
            ),
            scenario_goal_completion=invalid(
                ResearchMetric.generalization_scenario_goal_completion_v1,
                reason,
            ),
        )

    values_by_id = {
        cell.cell_id: (float(observation.value), cell.weight)
        for cell, observation in zip(target_cells, observations, strict=True)
    }

    group_error: str | None = None
    group_values: dict[str, list[tuple[str, float, float]]] = {}
    try:
        for cell in target_cells:
            group_id = cell.coordinates.axis_value(plan.group_axis)
            value, weight = values_by_id[cell.cell_id]
            group_values.setdefault(group_id, []).append(
                (cell.cell_id, value, weight)
            )
    except ValueError:
        group_error = "one or more planned cells lack the declared group axis"

    if group_error is None:
        group_means = {
            group_id: _weighted_mean(
                [(value, weight) for _, value, weight in entries]
            )
            for group_id, entries in group_values.items()
        }
        group_components = tuple(
            ResearchMetricContribution(
                contribution_id=group_id,
                source_ids=tuple(item_id for item_id, _, _ in entries),
                value=group_means[group_id],
                weight=1.0 / len(group_values),
            )
            for group_id, entries in group_values.items()
        )
        macro_value = sum(group_means.values()) / len(group_means)
        worst_value = (
            min(group_means.values())
            if plan.direction == ResearchMetricDirection.maximize
            else max(group_means.values())
        )
        group_macro = _receipt(
            metric=ResearchMetric.generalization_group_macro_v1,
            planned_ids=planned_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            value=macro_value,
            contributions=group_components,
        )
        worst_group = _receipt(
            metric=ResearchMetric.generalization_worst_group_v1,
            planned_ids=planned_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            value=worst_value,
            contributions=group_components,
        )
    else:
        group_macro = invalid(
            ResearchMetric.generalization_group_macro_v1, group_error
        )
        worst_group = invalid(
            ResearchMetric.generalization_worst_group_v1, group_error
        )

    distribution_entries: dict[str, list[tuple[str, float, float]]] = {}
    distribution_error: str | None = None
    try:
        for cell in target_cells:
            distribution_id = cell.coordinates.axis_value(plan.distribution_axis)
            value, weight = values_by_id[cell.cell_id]
            distribution_entries.setdefault(distribution_id, []).append(
                (cell.cell_id, value, weight)
            )
    except ValueError:
        distribution_error = (
            "one or more planned cells lack the declared distribution axis"
        )
    declared_distributions = {
        *plan.in_distribution_values,
        *plan.out_of_distribution_values,
    }
    if (
        distribution_error is None
        and set(distribution_entries) != declared_distributions
    ):
        distribution_error = (
            "ID/OOD declarations must exactly partition planned distribution values"
        )
    if distribution_error is not None:
        id_ood_gap = invalid(
            ResearchMetric.generalization_id_minus_ood_gap_v1,
            distribution_error,
        )
    else:
        distribution_means = {
            distribution_id: _weighted_mean(
                [(value, weight) for _, value, weight in entries]
            )
            for distribution_id, entries in distribution_entries.items()
        }
        id_value = sum(
            distribution_means[item] for item in plan.in_distribution_values
        ) / len(plan.in_distribution_values)
        ood_value = sum(
            distribution_means[item] for item in plan.out_of_distribution_values
        ) / len(plan.out_of_distribution_values)
        id_ids = tuple(
            cell_id
            for item in plan.in_distribution_values
            for cell_id, _, _ in distribution_entries[item]
        )
        ood_ids = tuple(
            cell_id
            for item in plan.out_of_distribution_values
            for cell_id, _, _ in distribution_entries[item]
        )
        id_ood_gap = _receipt(
            metric=ResearchMetric.generalization_id_minus_ood_gap_v1,
            planned_ids=planned_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            value=id_value - ood_value,
            contributions=(
                ResearchMetricContribution(
                    contribution_id="in-distribution",
                    source_ids=id_ids,
                    value=id_value,
                    weight=1.0,
                ),
                ResearchMetricContribution(
                    contribution_id="out-of-distribution",
                    source_ids=ood_ids,
                    value=ood_value,
                    weight=1.0,
                ),
            ),
        )

    scenario_error: str | None = None
    scenario_variants: dict[
        tuple[str, str], list[tuple[str, float, float]]
    ] = {}
    try:
        for cell in target_cells:
            scenario_id = cell.coordinates.axis_value("scenario")
            variant_id = cell.coordinates.axis_value("variant")
            value, weight = values_by_id[cell.cell_id]
            scenario_variants.setdefault((scenario_id, variant_id), []).append(
                (cell.cell_id, value, weight)
            )
    except ValueError:
        scenario_error = "SGC requires scenario and variant on every planned cell"
    if scenario_error is not None:
        sgc = invalid(
            ResearchMetric.generalization_scenario_goal_completion_v1,
            scenario_error,
        )
    else:
        variant_means = {
            key: _weighted_mean(
                [(value, weight) for _, value, weight in entries]
            )
            for key, entries in scenario_variants.items()
        }
        scenario_ids = tuple(dict.fromkeys(key[0] for key in scenario_variants))
        scenario_values: dict[str, float] = {}
        scenario_sources: dict[str, tuple[str, ...]] = {}
        for scenario_id in scenario_ids:
            variant_values = [
                value
                for (current_scenario, _), value in variant_means.items()
                if current_scenario == scenario_id
            ]
            scenario_values[scenario_id] = (
                min(variant_values)
                if plan.direction == ResearchMetricDirection.maximize
                else max(variant_values)
            )
            scenario_sources[scenario_id] = tuple(
                cell_id
                for (current_scenario, _), entries in scenario_variants.items()
                if current_scenario == scenario_id
                for cell_id, _, _ in entries
            )
        sgc = _receipt(
            metric=ResearchMetric.generalization_scenario_goal_completion_v1,
            planned_ids=planned_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            value=sum(scenario_values.values()) / len(scenario_values),
            contributions=tuple(
                ResearchMetricContribution(
                    contribution_id=scenario_id,
                    source_ids=scenario_sources[scenario_id],
                    value=scenario_values[scenario_id],
                    weight=1.0 / len(scenario_values),
                )
                for scenario_id in scenario_ids
            ),
        )

    return GeneralizationMetricsReceipt(
        group_macro=group_macro,
        worst_group=worst_group,
        id_ood_gap=id_ood_gap,
        scenario_goal_completion=sgc,
    )


# ---------------------------------------------------------------------------
# Evolution and common-budget curves


class ParentChildPair(StrictModel):
    pair_id: str = Field(pattern=ID_PATTERN)
    parent_id: str = Field(pattern=ID_PATTERN)
    child_id: str = Field(pattern=ID_PATTERN)

    @model_validator(mode="after")
    def parent_and_child_differ(self) -> "ParentChildPair":
        if self.parent_id == self.child_id:
            raise ValueError("parent delta requires distinct parent and child ids")
        return self


class ParentDeltaPlan(StrictModel):
    pairs: tuple[ParentChildPair, ...]
    direction: ResearchMetricDirection = ResearchMetricDirection.maximize

    @model_validator(mode="after")
    def pair_population_is_unique(self) -> "ParentDeltaPlan":
        _ids_are_unique(
            [item.pair_id for item in self.pairs], field_name="parent pair ids"
        )
        child_ids = [item.child_id for item in self.pairs]
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("each child may appear in only one parent delta pair")
        return self


class ParentDeltaObservation(StrictModel):
    pair_id: str = Field(pattern=ID_PATTERN)
    disposition: ResearchMetricDisposition
    parent_score: float | None = None
    child_score: float | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def scores_match_disposition(self) -> "ParentDeltaObservation":
        included = self.disposition in _INCLUDED
        if included:
            if self.parent_score is None or self.child_score is None:
                raise ValueError("included parent deltas require both scores")
            if self.reason is not None:
                raise ValueError("included parent deltas forbid a reason")
            if self.disposition == ResearchMetricDisposition.zero_filled and (
                self.parent_score != 0 or self.child_score != 0
            ):
                raise ValueError("zero-filled parent delta scores must both equal zero")
        elif (
            self.parent_score is not None
            or self.child_score is not None
            or self.reason is None
        ):
            raise ValueError("unavailable parent deltas require only a reason")
        return self


def _indexed_observations(
    observations: Sequence[Any],
    *,
    key_name: str,
    expected_ids: Sequence[str],
    population_name: str,
) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    expected = set(expected_ids)
    for observation in observations:
        key = str(getattr(observation, key_name))
        if key not in expected:
            raise ValueError(f"{population_name} contains an unplanned id")
        if key in indexed:
            raise ValueError(f"{population_name} contains duplicate observations")
        indexed[key] = observation
    return indexed


def analyze_parent_deltas(
    plan: ParentDeltaPlan,
    observations: Sequence[ParentDeltaObservation],
) -> ResearchMetricReceipt:
    """Return the macro mean direction-normalized child-versus-parent delta."""

    pair_ids = tuple(item.pair_id for item in plan.pairs)
    indexed = _indexed_observations(
        observations,
        key_name="pair_id",
        expected_ids=pair_ids,
        population_name="parent delta population",
    )
    aligned: list[ParentDeltaObservation | None] = [
        indexed.get(pair_id) for pair_id in pair_ids
    ]
    dispositions = tuple(
        item.disposition if item is not None else ResearchMetricDisposition.missing
        for item in aligned
    )
    observation_identity = [
        (
            item.model_dump(mode="json")
            if item is not None
            else {
                "pair_id": pair_id,
                "disposition": ResearchMetricDisposition.missing.value,
                "reason": "planned parent pair observation is absent",
            }
        )
        for pair_id, item in zip(pair_ids, aligned, strict=True)
    ]
    population_identity = {
        "pairs": [item.model_dump(mode="json") for item in plan.pairs]
    }
    if any(disposition not in _INCLUDED for disposition in dispositions):
        return _invalid_receipt(
            metric=ResearchMetric.evolution_parent_delta_macro_v1,
            planned_ids=pair_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            reason="one or more planned parent delta pairs are unavailable",
        )
    deltas: list[float] = []
    components: list[ResearchMetricContribution] = []
    for pair, observation in zip(plan.pairs, aligned, strict=True):
        assert observation is not None
        assert observation.child_score is not None
        assert observation.parent_score is not None
        raw = observation.child_score - observation.parent_score
        delta = raw if plan.direction == ResearchMetricDirection.maximize else -raw
        deltas.append(delta)
        components.append(
            ResearchMetricContribution(
                contribution_id=pair.pair_id,
                source_ids=(pair.pair_id,),
                value=delta,
                weight=1.0 / len(plan.pairs),
            )
        )
    return _receipt(
        metric=ResearchMetric.evolution_parent_delta_macro_v1,
        planned_ids=pair_ids,
        dispositions=dispositions,
        population_identity=population_identity,
        observation_identity=observation_identity,
        parameters=plan,
        value=sum(deltas) / len(deltas),
        contributions=components,
    )


CandidateYieldDefinition = Literal[
    "valid", "eligible", "archive_admitted", "promoted"
]


class CandidateYieldPlan(StrictModel):
    candidate_ids: tuple[str, ...]
    yield_definition: CandidateYieldDefinition

    @model_validator(mode="after")
    def candidate_population_is_unique(self) -> "CandidateYieldPlan":
        _ids_are_unique(self.candidate_ids, field_name="candidate ids")
        return self


class CandidateYieldObservation(StrictModel):
    candidate_id: str = Field(pattern=ID_PATTERN)
    disposition: ResearchMetricDisposition
    yielded: bool | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def yield_matches_disposition(self) -> "CandidateYieldObservation":
        if self.disposition in _INCLUDED:
            if self.yielded is None or self.reason is not None:
                raise ValueError("included candidate yield cells require an outcome")
            if (
                self.disposition == ResearchMetricDisposition.zero_filled
                and self.yielded
            ):
                raise ValueError("zero-filled candidate yield cells must be false")
        elif self.yielded is not None or self.reason is None:
            raise ValueError("unavailable candidate yield cells require only a reason")
        return self


def analyze_candidate_yield(
    plan: CandidateYieldPlan,
    observations: Sequence[CandidateYieldObservation],
) -> ResearchMetricReceipt:
    """Compute the declared lifecycle-event yield over all planned candidates."""

    indexed = _indexed_observations(
        observations,
        key_name="candidate_id",
        expected_ids=plan.candidate_ids,
        population_name="candidate yield population",
    )
    aligned = [indexed.get(candidate_id) for candidate_id in plan.candidate_ids]
    dispositions = tuple(
        item.disposition if item is not None else ResearchMetricDisposition.missing
        for item in aligned
    )
    observation_identity = [
        (
            item.model_dump(mode="json")
            if item is not None
            else {
                "candidate_id": candidate_id,
                "disposition": ResearchMetricDisposition.missing.value,
                "reason": "planned candidate yield observation is absent",
            }
        )
        for candidate_id, item in zip(plan.candidate_ids, aligned, strict=True)
    ]
    population_identity = {"candidate_ids": list(plan.candidate_ids)}
    if any(disposition not in _INCLUDED for disposition in dispositions):
        return _invalid_receipt(
            metric=ResearchMetric.evolution_candidate_yield_v1,
            planned_ids=plan.candidate_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            reason="one or more planned candidate yield outcomes are unavailable",
        )
    outcomes = [bool(item.yielded) for item in aligned if item is not None]
    return _receipt(
        metric=ResearchMetric.evolution_candidate_yield_v1,
        planned_ids=plan.candidate_ids,
        dispositions=dispositions,
        population_identity=population_identity,
        observation_identity=observation_identity,
        parameters=plan,
        value=sum(outcomes) / len(outcomes),
        contributions=tuple(
            ResearchMetricContribution(
                contribution_id=candidate_id,
                source_ids=(candidate_id,),
                value=float(outcome),
                weight=1.0 / len(outcomes),
            )
            for candidate_id, outcome in zip(
                plan.candidate_ids, outcomes, strict=True
            )
        ),
    )


class EvolutionBudgetCheckpoint(StrictModel):
    checkpoint_id: str = Field(pattern=ID_PATTERN)
    budget: float = Field(gt=0.0, strict=True)


class EvolutionCurvePlan(StrictModel):
    """A common-horizon anytime curve fixed before observing candidate scores."""

    checkpoints: tuple[EvolutionBudgetCheckpoint, ...]
    common_budget_limit: float = Field(gt=0.0, strict=True)
    baseline_score: float
    direction: ResearchMetricDirection = ResearchMetricDirection.maximize

    @model_validator(mode="after")
    def checkpoints_close_the_common_budget(self) -> "EvolutionCurvePlan":
        _ids_are_unique(
            [item.checkpoint_id for item in self.checkpoints],
            field_name="evolution checkpoint ids",
        )
        budgets = [item.budget for item in self.checkpoints]
        if budgets != sorted(budgets) or len(set(budgets)) != len(budgets):
            raise ValueError("evolution checkpoint budgets must strictly increase")
        if budgets[-1] != self.common_budget_limit:
            raise ValueError(
                "the final evolution checkpoint must equal common_budget_limit"
            )
        return self


class EvolutionCurveObservation(StrictModel):
    checkpoint_id: str = Field(pattern=ID_PATTERN)
    disposition: ResearchMetricDisposition
    score: float | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def score_matches_disposition(self) -> "EvolutionCurveObservation":
        if self.disposition in _INCLUDED:
            if self.score is None or self.reason is not None:
                raise ValueError("included evolution checkpoints require a score")
            if (
                self.disposition == ResearchMetricDisposition.zero_filled
                and self.score != 0
            ):
                raise ValueError("zero-filled evolution checkpoint scores must equal zero")
        elif self.score is not None or self.reason is None:
            raise ValueError("unavailable evolution checkpoints require only a reason")
        return self


def analyze_evolution_curve(
    plan: EvolutionCurvePlan,
    observations: Sequence[EvolutionCurveObservation],
) -> EvolutionCurveMetricsReceipt:
    """Compute final best, gain, and normalized left-step common-budget AUBC."""

    checkpoint_ids = tuple(item.checkpoint_id for item in plan.checkpoints)
    indexed = _indexed_observations(
        observations,
        key_name="checkpoint_id",
        expected_ids=checkpoint_ids,
        population_name="evolution curve population",
    )
    aligned = [indexed.get(item_id) for item_id in checkpoint_ids]
    dispositions = tuple(
        item.disposition if item is not None else ResearchMetricDisposition.missing
        for item in aligned
    )
    observation_identity = [
        (
            item.model_dump(mode="json")
            if item is not None
            else {
                "checkpoint_id": item_id,
                "disposition": ResearchMetricDisposition.missing.value,
                "reason": "planned evolution checkpoint observation is absent",
            }
        )
        for item_id, item in zip(checkpoint_ids, aligned, strict=True)
    ]
    population_identity = {
        "checkpoints": [
            item.model_dump(mode="json") for item in plan.checkpoints
        ],
        "common_budget_limit": plan.common_budget_limit,
    }

    def invalid(metric: ResearchMetric) -> ResearchMetricReceipt:
        return _invalid_receipt(
            metric=metric,
            planned_ids=checkpoint_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            reason="one or more planned evolution curve checkpoints are unavailable",
        )

    if any(disposition not in _INCLUDED for disposition in dispositions):
        return EvolutionCurveMetricsReceipt(
            best_so_far_final=invalid(
                ResearchMetric.evolution_best_so_far_final_v1
            ),
            final_gain=invalid(ResearchMetric.evolution_final_gain_v1),
            aubc=invalid(
                ResearchMetric.evolution_aubc_common_budget_step_v1
            ),
        )

    best = plan.baseline_score
    previous_budget = 0.0
    area = 0.0
    best_components: list[ResearchMetricContribution] = []
    interval_components: list[ResearchMetricContribution] = []
    previous_source: tuple[str, ...] = ()
    for checkpoint, observation in zip(plan.checkpoints, aligned, strict=True):
        assert observation is not None and observation.score is not None
        interval_width = checkpoint.budget - previous_budget
        area += best * interval_width
        interval_components.append(
            ResearchMetricContribution(
                contribution_id=f"interval-{checkpoint.checkpoint_id}",
                source_ids=previous_source,
                value=best,
                weight=interval_width / plan.common_budget_limit,
            )
        )
        best = (
            max(best, observation.score)
            if plan.direction == ResearchMetricDirection.maximize
            else min(best, observation.score)
        )
        best_components.append(
            ResearchMetricContribution(
                contribution_id=checkpoint.checkpoint_id,
                source_ids=(checkpoint.checkpoint_id,),
                value=best,
            )
        )
        previous_budget = checkpoint.budget
        previous_source = (checkpoint.checkpoint_id,)
    gain = (
        best - plan.baseline_score
        if plan.direction == ResearchMetricDirection.maximize
        else plan.baseline_score - best
    )
    common = dict(
        planned_ids=checkpoint_ids,
        dispositions=dispositions,
        population_identity=population_identity,
        observation_identity=observation_identity,
        parameters=plan,
    )
    return EvolutionCurveMetricsReceipt(
        best_so_far_final=_receipt(
            metric=ResearchMetric.evolution_best_so_far_final_v1,
            value=best,
            contributions=best_components,
            **common,
        ),
        final_gain=_receipt(
            metric=ResearchMetric.evolution_final_gain_v1,
            value=gain,
            contributions=best_components,
            **common,
        ),
        aubc=_receipt(
            metric=ResearchMetric.evolution_aubc_common_budget_step_v1,
            value=area / plan.common_budget_limit,
            contributions=interval_components,
            **common,
        ),
    )


# ---------------------------------------------------------------------------
# Meta-evolution paired comparison


class MetaEvolutionPairPlan(StrictModel):
    pair_keys: tuple[str, ...]
    control_arm_id: str = Field(pattern=ID_PATTERN)
    treatment_arm_id: str = Field(pattern=ID_PATTERN)
    direction: ResearchMetricDirection = ResearchMetricDirection.maximize

    @model_validator(mode="after")
    def paired_population_is_closed(self) -> "MetaEvolutionPairPlan":
        _ids_are_unique(self.pair_keys, field_name="meta-evolution pair keys")
        if self.control_arm_id == self.treatment_arm_id:
            raise ValueError("paired meta-evolution arms must be distinct")
        return self


class MetaEvolutionArmObservation(StrictModel):
    pair_key: str = Field(pattern=ID_PATTERN)
    arm_id: str = Field(pattern=ID_PATTERN)
    disposition: ResearchMetricDisposition
    score: float | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def arm_score_matches_disposition(self) -> "MetaEvolutionArmObservation":
        if self.disposition in _INCLUDED:
            if self.score is None or self.reason is not None:
                raise ValueError("included paired arms require a score")
            if (
                self.disposition == ResearchMetricDisposition.zero_filled
                and self.score != 0
            ):
                raise ValueError("zero-filled paired arm scores must equal zero")
        elif self.score is not None or self.reason is None:
            raise ValueError("unavailable paired arms require only a reason")
        return self


def analyze_meta_evolution_pairs(
    plan: MetaEvolutionPairPlan,
    observations: Sequence[MetaEvolutionArmObservation],
) -> ResearchMetricReceipt:
    """Compute a macro paired delta only when both arms share every pair key."""

    allowed_arms = {plan.control_arm_id, plan.treatment_arm_id}
    indexed: dict[tuple[str, str], MetaEvolutionArmObservation] = {}
    for observation in observations:
        if observation.pair_key not in set(plan.pair_keys):
            raise ValueError("meta-evolution observations contain an unplanned pair key")
        if observation.arm_id not in allowed_arms:
            raise ValueError("meta-evolution observations contain an unplanned arm")
        key = (observation.pair_key, observation.arm_id)
        if key in indexed:
            raise ValueError("meta-evolution paired arm observations must be unique")
        indexed[key] = observation

    pair_dispositions: list[ResearchMetricDisposition] = []
    aligned_identity: list[dict[str, Any]] = []
    pair_values: list[tuple[str, float]] = []
    for pair_key in plan.pair_keys:
        control = indexed.get((pair_key, plan.control_arm_id))
        treatment = indexed.get((pair_key, plan.treatment_arm_id))
        control_disposition = (
            control.disposition
            if control is not None
            else ResearchMetricDisposition.missing
        )
        treatment_disposition = (
            treatment.disposition
            if treatment is not None
            else ResearchMetricDisposition.missing
        )
        pair_disposition = _combined_disposition(
            (control_disposition, treatment_disposition)
        )
        pair_dispositions.append(pair_disposition)
        aligned_identity.append(
            {
                "pair_key": pair_key,
                "control": (
                    control.model_dump(mode="json")
                    if control is not None
                    else {
                        "arm_id": plan.control_arm_id,
                        "disposition": ResearchMetricDisposition.missing.value,
                    }
                ),
                "treatment": (
                    treatment.model_dump(mode="json")
                    if treatment is not None
                    else {
                        "arm_id": plan.treatment_arm_id,
                        "disposition": ResearchMetricDisposition.missing.value,
                    }
                ),
            }
        )
        if pair_disposition in _INCLUDED:
            assert control is not None and control.score is not None
            assert treatment is not None and treatment.score is not None
            raw = treatment.score - control.score
            delta = (
                raw
                if plan.direction == ResearchMetricDirection.maximize
                else -raw
            )
            pair_values.append((pair_key, delta))
    population_identity = {
        "pair_keys": list(plan.pair_keys),
        "control_arm_id": plan.control_arm_id,
        "treatment_arm_id": plan.treatment_arm_id,
    }
    if any(disposition not in _INCLUDED for disposition in pair_dispositions):
        return _invalid_receipt(
            metric=ResearchMetric.meta_evolution_paired_delta_macro_v1,
            planned_ids=plan.pair_keys,
            dispositions=pair_dispositions,
            population_identity=population_identity,
            observation_identity=aligned_identity,
            parameters=plan,
            reason="control and treatment must both cover every declared pair key",
        )
    return _receipt(
        metric=ResearchMetric.meta_evolution_paired_delta_macro_v1,
        planned_ids=plan.pair_keys,
        dispositions=pair_dispositions,
        population_identity=population_identity,
        observation_identity=aligned_identity,
        parameters=plan,
        value=sum(value for _, value in pair_values) / len(pair_values),
        contributions=tuple(
            ResearchMetricContribution(
                contribution_id=pair_key,
                source_ids=(pair_key,),
                value=value,
                weight=1.0 / len(pair_values),
            )
            for pair_key, value in pair_values
        ),
    )


# ---------------------------------------------------------------------------
# Trajectory progress and right censoring


class ProgressCheckpoint(StrictModel):
    checkpoint_id: str = Field(pattern=ID_PATTERN)
    coordinate: float = Field(ge=0.0, strict=True)


class TrajectoryAnalysisPlan(StrictModel):
    trajectory_ids: tuple[str, ...]
    checkpoints: tuple[ProgressCheckpoint, ...]
    threshold: float
    direction: ResearchMetricDirection = ResearchMetricDirection.maximize

    @model_validator(mode="after")
    def trajectory_grid_is_complete(self) -> "TrajectoryAnalysisPlan":
        _ids_are_unique(self.trajectory_ids, field_name="trajectory ids")
        if len(self.checkpoints) < 2:
            raise ValueError("trajectory AUPC requires at least two checkpoints")
        _ids_are_unique(
            [item.checkpoint_id for item in self.checkpoints],
            field_name="trajectory checkpoint ids",
        )
        coordinates = [item.coordinate for item in self.checkpoints]
        if coordinates[0] != 0:
            raise ValueError("trajectory checkpoints must start at coordinate zero")
        if coordinates != sorted(coordinates) or len(set(coordinates)) != len(
            coordinates
        ):
            raise ValueError("trajectory checkpoint coordinates must strictly increase")
        return self

    @property
    def horizon(self) -> float:
        return self.checkpoints[-1].coordinate


class TrajectoryProgressObservation(StrictModel):
    trajectory_id: str = Field(pattern=ID_PATTERN)
    checkpoint_id: str = Field(pattern=ID_PATTERN)
    disposition: ResearchMetricDisposition
    progress: float | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def progress_matches_disposition(self) -> "TrajectoryProgressObservation":
        if self.disposition in _INCLUDED:
            if self.progress is None or self.reason is not None:
                raise ValueError("included trajectory checkpoints require progress")
            if (
                self.disposition == ResearchMetricDisposition.zero_filled
                and self.progress != 0
            ):
                raise ValueError("zero-filled trajectory progress must equal zero")
        elif self.progress is not None or self.reason is None:
            raise ValueError("unavailable trajectory checkpoints require only a reason")
        return self


def analyze_trajectory_progress(
    plan: TrajectoryAnalysisPlan,
    observations: Sequence[TrajectoryProgressObservation],
) -> TrajectoryMetricsReceipt:
    """Compute progress metrics over a complete trajectory/checkpoint grid.

    Time-to-threshold is the restricted mean of first observed checkpoint
    crossing times.  A trajectory that never crosses contributes the common
    horizon and is explicitly marked right-censored.
    """

    expected = {
        (trajectory_id, checkpoint.checkpoint_id)
        for trajectory_id in plan.trajectory_ids
        for checkpoint in plan.checkpoints
    }
    indexed: dict[tuple[str, str], TrajectoryProgressObservation] = {}
    for observation in observations:
        key = (observation.trajectory_id, observation.checkpoint_id)
        if key not in expected:
            raise ValueError("trajectory progress contains an unplanned grid cell")
        if key in indexed:
            raise ValueError("trajectory progress grid cells must be unique")
        indexed[key] = observation

    aligned_identity: list[dict[str, Any]] = []
    series: dict[str, list[TrajectoryProgressObservation | None]] = {}
    series_dispositions: list[ResearchMetricDisposition] = []
    for trajectory_id in plan.trajectory_ids:
        points: list[TrajectoryProgressObservation | None] = []
        point_dispositions: list[ResearchMetricDisposition] = []
        for checkpoint in plan.checkpoints:
            observation = indexed.get((trajectory_id, checkpoint.checkpoint_id))
            points.append(observation)
            point_disposition = (
                observation.disposition
                if observation is not None
                else ResearchMetricDisposition.missing
            )
            point_dispositions.append(point_disposition)
            aligned_identity.append(
                observation.model_dump(mode="json")
                if observation is not None
                else {
                    "trajectory_id": trajectory_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "disposition": ResearchMetricDisposition.missing.value,
                    "reason": "planned trajectory checkpoint is absent",
                }
            )
        series[trajectory_id] = points
        series_dispositions.append(_combined_disposition(point_dispositions))

    population_identity = {
        "trajectory_ids": list(plan.trajectory_ids),
        "checkpoints": [
            item.model_dump(mode="json") for item in plan.checkpoints
        ],
    }

    def invalid(metric: ResearchMetric) -> ResearchMetricReceipt:
        return _invalid_receipt(
            metric=metric,
            planned_ids=plan.trajectory_ids,
            dispositions=series_dispositions,
            population_identity=population_identity,
            observation_identity=aligned_identity,
            parameters=plan,
            reason="one or more planned trajectory series are incomplete",
        )

    if any(disposition not in _INCLUDED for disposition in series_dispositions):
        return TrajectoryMetricsReceipt(
            final_progress=invalid(
                ResearchMetric.trajectory_final_progress_macro_v1
            ),
            aupc=invalid(ResearchMetric.trajectory_aupc_trapezoid_macro_v1),
            regression_mass=invalid(
                ResearchMetric.trajectory_regression_mass_macro_v1
            ),
            time_to_threshold=invalid(
                ResearchMetric.trajectory_time_to_threshold_rmst_v1
            ),
        )

    final_values: list[tuple[str, float]] = []
    aupc_values: list[tuple[str, float]] = []
    regression_values: list[tuple[str, float]] = []
    time_values: list[tuple[str, float, bool]] = []
    for trajectory_id in plan.trajectory_ids:
        raw_points = series[trajectory_id]
        points = [
            float(item.progress)
            for item in raw_points
            if item is not None and item.progress is not None
        ]
        assert len(points) == len(plan.checkpoints)
        final_values.append((trajectory_id, points[-1]))
        area = sum(
            (points[index - 1] + points[index])
            * 0.5
            * (
                plan.checkpoints[index].coordinate
                - plan.checkpoints[index - 1].coordinate
            )
            for index in range(1, len(points))
        )
        aupc_values.append((trajectory_id, area / plan.horizon))
        if plan.direction == ResearchMetricDirection.maximize:
            regression = sum(
                max(points[index - 1] - points[index], 0.0)
                for index in range(1, len(points))
            )
            reached = [
                checkpoint.coordinate
                for checkpoint, value in zip(plan.checkpoints, points, strict=True)
                if value >= plan.threshold
            ]
        else:
            regression = sum(
                max(points[index] - points[index - 1], 0.0)
                for index in range(1, len(points))
            )
            reached = [
                checkpoint.coordinate
                for checkpoint, value in zip(plan.checkpoints, points, strict=True)
                if value <= plan.threshold
            ]
        regression_values.append((trajectory_id, regression))
        time_values.append(
            (
                trajectory_id,
                reached[0] if reached else plan.horizon,
                not bool(reached),
            )
        )

    def contributions(
        values: Sequence[tuple[str, float]],
    ) -> tuple[ResearchMetricContribution, ...]:
        return tuple(
            ResearchMetricContribution(
                contribution_id=trajectory_id,
                source_ids=(trajectory_id,),
                value=value,
                weight=1.0 / len(values),
            )
            for trajectory_id, value in values
        )

    common = dict(
        planned_ids=plan.trajectory_ids,
        dispositions=series_dispositions,
        population_identity=population_identity,
        observation_identity=aligned_identity,
        parameters=plan,
    )
    time_dispositions = [
        (
            ResearchMetricDisposition.censored
            if censored
            else series_disposition
        )
        for (_, _, censored), series_disposition in zip(
            time_values, series_dispositions, strict=True
        )
    ]
    return TrajectoryMetricsReceipt(
        final_progress=_receipt(
            metric=ResearchMetric.trajectory_final_progress_macro_v1,
            value=sum(value for _, value in final_values) / len(final_values),
            contributions=contributions(final_values),
            **common,
        ),
        aupc=_receipt(
            metric=ResearchMetric.trajectory_aupc_trapezoid_macro_v1,
            value=sum(value for _, value in aupc_values) / len(aupc_values),
            contributions=contributions(aupc_values),
            **common,
        ),
        regression_mass=_receipt(
            metric=ResearchMetric.trajectory_regression_mass_macro_v1,
            value=sum(value for _, value in regression_values)
            / len(regression_values),
            contributions=contributions(regression_values),
            **common,
        ),
        time_to_threshold=_receipt(
            metric=ResearchMetric.trajectory_time_to_threshold_rmst_v1,
            planned_ids=plan.trajectory_ids,
            dispositions=time_dispositions,
            population_identity=population_identity,
            observation_identity=aligned_identity,
            parameters=plan,
            value=sum(value for _, value, _ in time_values) / len(time_values),
            contributions=tuple(
                ResearchMetricContribution(
                    contribution_id=trajectory_id,
                    source_ids=(trajectory_id,),
                    value=value,
                    weight=1.0 / len(time_values),
                    censored=censored,
                )
                for trajectory_id, value, censored in time_values
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Calibration


class CalibrationAnalysisPlan(StrictModel):
    prediction_ids: tuple[str, ...]
    bin_edges: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    log_loss_epsilon: float = Field(default=1e-15, gt=0.0, lt=0.5, strict=True)

    @model_validator(mode="after")
    def calibration_population_and_bins_are_closed(self) -> "CalibrationAnalysisPlan":
        _ids_are_unique(self.prediction_ids, field_name="calibration prediction ids")
        if len(self.bin_edges) < 2:
            raise ValueError("ECE requires at least two bin edges")
        if self.bin_edges[0] != 0 or self.bin_edges[-1] != 1:
            raise ValueError("ECE bin edges must span exactly [0, 1]")
        if list(self.bin_edges) != sorted(self.bin_edges) or len(
            set(self.bin_edges)
        ) != len(self.bin_edges):
            raise ValueError("ECE bin edges must strictly increase")
        return self


class CalibrationObservation(StrictModel):
    prediction_id: str = Field(pattern=ID_PATTERN)
    outcome_disposition: ResearchMetricDisposition
    outcome: Literal[0, 1] | None = None
    outcome_reason: str | None = Field(default=None, min_length=1)
    confidence_disposition: ResearchMetricDisposition
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_reason: str | None = Field(default=None, min_length=1)

    @field_validator("outcome", mode="before")
    @classmethod
    def binary_outcome_is_an_integer(cls, value: Any) -> Any:
        if value is not None and type(value) is not int:
            raise ValueError("calibration outcome must be integer 0 or 1")
        return value

    @model_validator(mode="after")
    def calibration_channels_match_dispositions(self) -> "CalibrationObservation":
        channels = (
            (
                "outcome",
                self.outcome_disposition,
                self.outcome,
                self.outcome_reason,
            ),
            (
                "confidence",
                self.confidence_disposition,
                self.confidence,
                self.confidence_reason,
            ),
        )
        for name, disposition, value, reason in channels:
            if disposition in _INCLUDED:
                if value is None or reason is not None:
                    raise ValueError(f"included calibration {name} requires a value")
                if (
                    disposition == ResearchMetricDisposition.zero_filled
                    and value != 0
                ):
                    raise ValueError(
                        f"zero-filled calibration {name} must equal zero"
                    )
            elif value is not None or reason is None:
                raise ValueError(
                    f"unavailable calibration {name} requires only a reason"
                )
        return self


def analyze_calibration(
    plan: CalibrationAnalysisPlan,
    observations: Sequence[CalibrationObservation],
) -> CalibrationMetricsReceipt:
    """Compute Brier, clipped binary NLL, ECE, and confidence coverage.

    Brier/NLL/ECE fail closed when any planned label or confidence is missing.
    Confidence coverage remains defined over the planned prediction denominator
    and therefore exposes the missingness instead of becoming invalid itself.
    """

    indexed = _indexed_observations(
        observations,
        key_name="prediction_id",
        expected_ids=plan.prediction_ids,
        population_name="calibration population",
    )
    aligned = [indexed.get(prediction_id) for prediction_id in plan.prediction_ids]
    combined_dispositions: list[ResearchMetricDisposition] = []
    confidence_dispositions: list[ResearchMetricDisposition] = []
    observation_identity: list[dict[str, Any]] = []
    for prediction_id, observation in zip(
        plan.prediction_ids, aligned, strict=True
    ):
        if observation is None:
            combined_dispositions.append(ResearchMetricDisposition.missing)
            confidence_dispositions.append(ResearchMetricDisposition.missing)
            observation_identity.append(
                {
                    "prediction_id": prediction_id,
                    "outcome_disposition": ResearchMetricDisposition.missing.value,
                    "confidence_disposition": ResearchMetricDisposition.missing.value,
                    "reason": "planned calibration observation is absent",
                }
            )
        else:
            combined_dispositions.append(
                _combined_disposition(
                    (
                        observation.outcome_disposition,
                        observation.confidence_disposition,
                    )
                )
            )
            confidence_dispositions.append(observation.confidence_disposition)
            observation_identity.append(observation.model_dump(mode="json"))
    population_identity = {"prediction_ids": list(plan.prediction_ids)}
    coverage_count = sum(
        disposition in _INCLUDED for disposition in confidence_dispositions
    )
    confidence_coverage = _receipt(
        metric=ResearchMetric.calibration_confidence_coverage_v1,
        planned_ids=plan.prediction_ids,
        dispositions=confidence_dispositions,
        population_identity=population_identity,
        observation_identity=observation_identity,
        parameters=plan,
        value=coverage_count / len(plan.prediction_ids),
        contributions=tuple(
            ResearchMetricContribution(
                contribution_id=prediction_id,
                source_ids=(prediction_id,),
                value=1.0,
                weight=1.0 / len(plan.prediction_ids),
            )
            for prediction_id, disposition in zip(
                plan.prediction_ids, confidence_dispositions, strict=True
            )
            if disposition in _INCLUDED
        ),
    )

    def invalid(metric: ResearchMetric) -> ResearchMetricReceipt:
        return _invalid_receipt(
            metric=metric,
            planned_ids=plan.prediction_ids,
            dispositions=combined_dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            reason="every planned calibration label and confidence is required",
        )

    if any(disposition not in _INCLUDED for disposition in combined_dispositions):
        return CalibrationMetricsReceipt(
            brier=invalid(ResearchMetric.calibration_brier_v1),
            nll=invalid(ResearchMetric.calibration_binary_nll_clipped_v1),
            ece=invalid(ResearchMetric.calibration_ece_v1),
            confidence_coverage=confidence_coverage,
        )

    complete = [item for item in aligned if item is not None]
    outcomes = [int(item.outcome) for item in complete]
    confidences = [float(item.confidence) for item in complete]
    brier_values = [
        (confidence - outcome) ** 2
        for confidence, outcome in zip(confidences, outcomes, strict=True)
    ]
    clipped = [
        min(max(confidence, plan.log_loss_epsilon), 1 - plan.log_loss_epsilon)
        for confidence in confidences
    ]
    nll_values = [
        -(
            outcome * log(confidence)
            + (1 - outcome) * log(1 - confidence)
        )
        for confidence, outcome in zip(clipped, outcomes, strict=True)
    ]

    bins: list[list[int]] = [[] for _ in range(len(plan.bin_edges) - 1)]
    upper_edges = plan.bin_edges[1:]
    for index, confidence in enumerate(confidences):
        bin_index = bisect_left(upper_edges, confidence)
        bins[bin_index].append(index)
    ece = 0.0
    ece_components: list[ResearchMetricContribution] = []
    for bin_index, members in enumerate(bins):
        if not members:
            continue
        mean_confidence = sum(confidences[index] for index in members) / len(
            members
        )
        accuracy = sum(outcomes[index] for index in members) / len(members)
        gap = abs(accuracy - mean_confidence)
        weight = len(members) / len(complete)
        ece += weight * gap
        ece_components.append(
            ResearchMetricContribution(
                contribution_id=f"bin-{bin_index}",
                source_ids=tuple(plan.prediction_ids[index] for index in members),
                value=gap,
                weight=weight,
            )
        )

    common = dict(
        planned_ids=plan.prediction_ids,
        dispositions=combined_dispositions,
        population_identity=population_identity,
        observation_identity=observation_identity,
        parameters=plan,
    )
    return CalibrationMetricsReceipt(
        brier=_receipt(
            metric=ResearchMetric.calibration_brier_v1,
            value=sum(brier_values) / len(brier_values),
            contributions=tuple(
                ResearchMetricContribution(
                    contribution_id=prediction_id,
                    source_ids=(prediction_id,),
                    value=value,
                    weight=1.0 / len(brier_values),
                )
                for prediction_id, value in zip(
                    plan.prediction_ids, brier_values, strict=True
                )
            ),
            **common,
        ),
        nll=_receipt(
            metric=ResearchMetric.calibration_binary_nll_clipped_v1,
            value=sum(nll_values) / len(nll_values),
            contributions=tuple(
                ResearchMetricContribution(
                    contribution_id=prediction_id,
                    source_ids=(prediction_id,),
                    value=value,
                    weight=1.0 / len(nll_values),
                )
                for prediction_id, value in zip(
                    plan.prediction_ids, nll_values, strict=True
                )
            ),
            **common,
        ),
        ece=_receipt(
            metric=ResearchMetric.calibration_ece_v1,
            value=ece,
            contributions=ece_components,
            **common,
        ),
        confidence_coverage=confidence_coverage,
    )


# ---------------------------------------------------------------------------
# Selector diversity


class SelectorAnalysisPlan(StrictModel):
    candidate_ids: tuple[str, ...]
    normalization_tolerance: float = Field(
        default=1e-12, gt=0.0, le=1e-6, strict=True
    )

    @model_validator(mode="after")
    def selector_population_is_unique(self) -> "SelectorAnalysisPlan":
        _ids_are_unique(self.candidate_ids, field_name="selector candidate ids")
        return self


class SelectorProbabilityObservation(StrictModel):
    candidate_id: str = Field(pattern=ID_PATTERN)
    disposition: ResearchMetricDisposition
    probability: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def probability_matches_disposition(self) -> "SelectorProbabilityObservation":
        if self.disposition in _INCLUDED:
            if self.probability is None or self.reason is not None:
                raise ValueError("included selector candidates require probability")
            if (
                self.disposition == ResearchMetricDisposition.zero_filled
                and self.probability != 0
            ):
                raise ValueError("zero-filled selector probability must equal zero")
        elif self.probability is not None or self.reason is None:
            raise ValueError("unavailable selector candidates require only a reason")
        return self


def analyze_selector_distribution(
    plan: SelectorAnalysisPlan,
    observations: Sequence[SelectorProbabilityObservation],
) -> SelectorMetricsReceipt:
    """Compute Shannon entropy in nats and probability ESS over all candidates."""

    indexed = _indexed_observations(
        observations,
        key_name="candidate_id",
        expected_ids=plan.candidate_ids,
        population_name="selector population",
    )
    aligned = [indexed.get(candidate_id) for candidate_id in plan.candidate_ids]
    dispositions = tuple(
        item.disposition if item is not None else ResearchMetricDisposition.missing
        for item in aligned
    )
    observation_identity = [
        (
            item.model_dump(mode="json")
            if item is not None
            else {
                "candidate_id": candidate_id,
                "disposition": ResearchMetricDisposition.missing.value,
                "reason": "planned selector probability is absent",
            }
        )
        for candidate_id, item in zip(plan.candidate_ids, aligned, strict=True)
    ]
    population_identity = {"candidate_ids": list(plan.candidate_ids)}

    def invalid(reason: str) -> SelectorMetricsReceipt:
        common = dict(
            planned_ids=plan.candidate_ids,
            dispositions=dispositions,
            population_identity=population_identity,
            observation_identity=observation_identity,
            parameters=plan,
            reason=reason,
        )
        return SelectorMetricsReceipt(
            entropy=_invalid_receipt(
                metric=ResearchMetric.selector_entropy_nats_v1, **common
            ),
            effective_sample_size=_invalid_receipt(
                metric=ResearchMetric.selector_effective_sample_size_v1,
                **common,
            ),
        )

    if any(disposition not in _INCLUDED for disposition in dispositions):
        return invalid("every planned selector candidate requires a probability")
    probabilities = [
        float(item.probability) for item in aligned if item is not None
    ]
    total = sum(probabilities)
    if abs(total - 1.0) > plan.normalization_tolerance:
        return invalid("selector probabilities do not sum to one within tolerance")
    normalized = [probability / total for probability in probabilities]
    entropy_terms = [
        -probability * log(probability) if probability > 0 else 0.0
        for probability in normalized
    ]
    entropy = sum(entropy_terms)
    ess = 1.0 / sum(probability * probability for probability in normalized)
    common = dict(
        planned_ids=plan.candidate_ids,
        dispositions=dispositions,
        population_identity=population_identity,
        observation_identity=observation_identity,
        parameters=plan,
    )
    return SelectorMetricsReceipt(
        entropy=_receipt(
            metric=ResearchMetric.selector_entropy_nats_v1,
            value=entropy,
            contributions=tuple(
                ResearchMetricContribution(
                    contribution_id=candidate_id,
                    source_ids=(candidate_id,),
                    value=term,
                )
                for candidate_id, term in zip(
                    plan.candidate_ids, entropy_terms, strict=True
                )
            ),
            **common,
        ),
        effective_sample_size=_receipt(
            metric=ResearchMetric.selector_effective_sample_size_v1,
            value=ess,
            contributions=tuple(
                ResearchMetricContribution(
                    contribution_id=candidate_id,
                    source_ids=(candidate_id,),
                    value=probability * probability,
                )
                for candidate_id, probability in zip(
                    plan.candidate_ids, normalized, strict=True
                )
            ),
            **common,
        ),
    )


__all__ = [
    "CalibrationAnalysisPlan",
    "CalibrationMetricsReceipt",
    "CalibrationObservation",
    "CandidateYieldObservation",
    "CandidateYieldPlan",
    "EvolutionBudgetCheckpoint",
    "EvolutionCurveMetricsReceipt",
    "EvolutionCurveObservation",
    "EvolutionCurvePlan",
    "GeneralizationAnalysisPlan",
    "GeneralizationMetricsReceipt",
    "MetaEvolutionArmObservation",
    "MetaEvolutionPairPlan",
    "ParentChildPair",
    "ParentDeltaObservation",
    "ParentDeltaPlan",
    "ProgressCheckpoint",
    "ResearchMetric",
    "ResearchMetricContribution",
    "ResearchMetricDirection",
    "ResearchMetricDisposition",
    "ResearchMetricReceipt",
    "SelectorAnalysisPlan",
    "SelectorMetricsReceipt",
    "SelectorProbabilityObservation",
    "TrajectoryAnalysisPlan",
    "TrajectoryMetricsReceipt",
    "TrajectoryProgressObservation",
    "analyze_calibration",
    "analyze_candidate_yield",
    "analyze_evolution_curve",
    "analyze_generalization",
    "analyze_meta_evolution_pairs",
    "analyze_parent_deltas",
    "analyze_selector_distribution",
    "analyze_trajectory_progress",
]
