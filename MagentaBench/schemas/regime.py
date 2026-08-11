"""Replayable stage/cell ledgers for longitudinal Agent experiments.

The stage DAG in :mod:`MagentaBench.schemas.models` declares what should run.
This module records the complete checkpoint x task/domain/scenario population
that actually entered analysis.  Derived continual/generalization metrics must
consume this ledger rather than discover cells from successful result files.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from .models import (
    AdapterCapabilityArtifact,
    ArtifactRef,
    ExperimentStageRole,
    ID_PATTERN,
    MetricComputationState,
    MetricArtifact,
    RunStatus,
    SHA256_PATTERN,
    StageFeedbackVisibility,
    StageStatePolicy,
    StrictModel,
)


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_artifact_bytes(value: StrictModel) -> bytes:
    """Return the one wire encoding accepted for an in-memory artifact ref."""

    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _ref_matches_model(ref: ArtifactRef, value: StrictModel) -> bool:
    encoded = _canonical_artifact_bytes(value)
    return ref.sha256 == hashlib.sha256(encoded).hexdigest() and ref.size_bytes == len(
        encoded
    )


def _utc(value: str, *, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC")
    return value


class ExperimentCellDisposition(str, Enum):
    observed = "observed"
    zero_filled = "zero_filled"
    excluded = "excluded"
    missing = "missing"
    invalid = "invalid"


class ExperimentCellCoordinates(StrictModel):
    """Axes shared by IID, transfer, continual, evolution, and stress cells."""

    stage_id: str = Field(pattern=ID_PATTERN)
    checkpoint_id: str = Field(pattern=ID_PATTERN)
    task_id: str = Field(pattern=ID_PATTERN)
    domain_id: str | None = Field(default=None, pattern=ID_PATTERN)
    scenario_id: str | None = Field(default=None, pattern=ID_PATTERN)
    variant_id: str | None = Field(default=None, pattern=ID_PATTERN)
    generation: int | None = Field(default=None, ge=0, strict=True)
    repetition: int = Field(default=0, ge=0, strict=True)

    def axis_value(self, axis: str) -> str:
        if axis == "generation":
            if self.generation is None:
                raise ValueError("cell has no generation coordinate")
            return str(self.generation)
        value = getattr(self, f"{axis}_id", None)
        if value is None:
            raise ValueError(f"cell has no {axis} coordinate")
        return str(value)


class PlannedMetricCell(StrictModel):
    cell_id: str = Field(pattern=ID_PATTERN)
    coordinates: ExperimentCellCoordinates
    metric_id: str = Field(pattern=ID_PATTERN)
    metric_digest: str = Field(pattern=SHA256_PATTERN)
    membership_digest: str = Field(pattern=SHA256_PATTERN)
    weight: float = Field(default=1.0, gt=0, strict=True)


class ExperimentCellPlan(StrictModel):
    format: Literal["bmp-experiment-cell-plan-v1"] = "bmp-experiment-cell-plan-v1"
    regime_id: str = Field(pattern=ID_PATTERN)
    regime_digest: str = Field(pattern=SHA256_PATTERN)
    stage_manifest_digests: Mapping[str, str]
    membership_refs: tuple[ArtifactRef, ...]
    cells: tuple[PlannedMetricCell, ...]
    plan_digest: str = Field(pattern=SHA256_PATTERN)

    def identity_data(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "regime_id": self.regime_id,
            "regime_digest": self.regime_digest,
            "stage_manifest_digests": dict(self.stage_manifest_digests),
            "membership_refs": [ref.identity_data() for ref in self.membership_refs],
            "cells": [cell.model_dump(mode="json") for cell in self.cells],
        }

    def canonical_digest(self) -> str:
        return _digest_json(self.identity_data())

    @field_validator("stage_manifest_digests")
    @classmethod
    def manifest_map_is_closed(cls, values: Mapping[str, str]) -> Mapping[str, str]:
        if not values:
            raise ValueError("cell plan requires stage manifest digests")
        for stage_id, digest in values.items():
            if not stage_id or re.fullmatch(ID_PATTERN, stage_id) is None:
                raise ValueError("cell plan stage ids must be valid BMP ids")
            if re.fullmatch(SHA256_PATTERN, digest) is None:
                raise ValueError("cell plan manifest digests must be SHA-256")
        return dict(values)

    @model_validator(mode="after")
    def plan_is_complete_and_content_addressed(self) -> "ExperimentCellPlan":
        if not self.membership_refs:
            raise ValueError("cell plan requires dataset membership authority refs")
        membership_identities = [
            (ref.sha256, ref.size_bytes) for ref in self.membership_refs
        ]
        if len(set(membership_identities)) != len(membership_identities):
            raise ValueError("cell plan membership authority refs must be content-unique")
        if not self.cells:
            raise ValueError("cell plan requires at least one planned cell")
        cell_ids = [cell.cell_id for cell in self.cells]
        if len(set(cell_ids)) != len(cell_ids):
            raise ValueError("planned cell ids must be unique")
        coordinate_keys = [
            (
                cell.metric_id,
                json.dumps(
                    cell.coordinates.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            for cell in self.cells
        ]
        if len(set(coordinate_keys)) != len(coordinate_keys):
            raise ValueError("planned metric coordinates must be unique")
        unknown_stages = sorted(
            {
                cell.coordinates.stage_id
                for cell in self.cells
                if cell.coordinates.stage_id not in self.stage_manifest_digests
            }
        )
        if unknown_stages:
            raise ValueError(f"planned cells use unknown stages: {unknown_stages}")
        membership_digests = {ref.sha256 for ref in self.membership_refs}
        unknown_memberships = sorted(
            {
                cell.membership_digest
                for cell in self.cells
                if cell.membership_digest not in membership_digests
            }
        )
        if unknown_memberships:
            raise ValueError(
                f"planned cells use unbound membership digests: {unknown_memberships}"
            )
        if self.plan_digest != self.canonical_digest():
            raise ValueError("experiment cell plan digest drift")
        return self


class ExperimentCellObservation(StrictModel):
    cell_id: str = Field(pattern=ID_PATTERN)
    disposition: ExperimentCellDisposition
    value: float | None = None
    terminal_status: RunStatus | None = None
    reason: str | None = Field(default=None, min_length=1)
    metric_result_ref: ArtifactRef | None = None
    evidence_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def observation_matches_disposition(self) -> "ExperimentCellObservation":
        if self.disposition in {
            ExperimentCellDisposition.observed,
            ExperimentCellDisposition.zero_filled,
        }:
            if self.value is None or self.terminal_status is None:
                raise ValueError("included cells require value and terminal status")
            if self.disposition == ExperimentCellDisposition.zero_filled and self.value != 0:
                raise ValueError("zero-filled experiment cells must equal zero")
            if self.reason is not None:
                raise ValueError("included experiment cells forbid a failure reason")
        else:
            if self.value is not None:
                raise ValueError("non-included experiment cells forbid values")
            if self.reason is None:
                raise ValueError("non-included experiment cells require a reason")
        if self.disposition == ExperimentCellDisposition.observed:
            if self.metric_result_ref is None:
                raise ValueError("observed experiment cells require metric result evidence")
        elif self.metric_result_ref is not None:
            raise ValueError("only observed experiment cells bind metric result evidence")
        refs = (
            *((self.metric_result_ref,) if self.metric_result_ref is not None else ()),
            *self.evidence_refs,
        )
        identities = [(ref.path, ref.sha256, ref.size_bytes) for ref in refs]
        if len(set(identities)) != len(identities):
            raise ValueError("experiment cell evidence refs must be unique")
        return self


class ExperimentCellLedger(StrictModel):
    format: Literal["bmp-experiment-cell-ledger-v1"] = (
        "bmp-experiment-cell-ledger-v1"
    )
    plan: ExperimentCellPlan
    observations: tuple[ExperimentCellObservation, ...]
    observed_count: int = Field(ge=0, strict=True)
    zero_filled_count: int = Field(ge=0, strict=True)
    excluded_count: int = Field(ge=0, strict=True)
    missing_count: int = Field(ge=0, strict=True)
    invalid_count: int = Field(ge=0, strict=True)
    observation_digest: str = Field(pattern=SHA256_PATTERN)

    def observation_identity_data(self) -> list[dict[str, Any]]:
        return [observation.model_dump(mode="json") for observation in self.observations]

    def canonical_observation_digest(self) -> str:
        return _digest_json(self.observation_identity_data())

    @model_validator(mode="after")
    def ledger_covers_the_planned_population(self) -> "ExperimentCellLedger":
        planned_ids = [cell.cell_id for cell in self.plan.cells]
        observed_ids = [observation.cell_id for observation in self.observations]
        if observed_ids != planned_ids:
            raise ValueError(
                "experiment cell observations must exactly follow planned cell order"
            )
        expected_counts = {
            ExperimentCellDisposition.observed: self.observed_count,
            ExperimentCellDisposition.zero_filled: self.zero_filled_count,
            ExperimentCellDisposition.excluded: self.excluded_count,
            ExperimentCellDisposition.missing: self.missing_count,
            ExperimentCellDisposition.invalid: self.invalid_count,
        }
        for disposition, expected in expected_counts.items():
            if sum(item.disposition == disposition for item in self.observations) != expected:
                raise ValueError("experiment cell disposition counts do not reconcile")
        if sum(expected_counts.values()) != len(self.plan.cells):
            raise ValueError("experiment cell counts must cover every planned cell")
        if self.observation_digest != self.canonical_observation_digest():
            raise ValueError("experiment cell observation digest drift")
        return self


MatrixRowAxis = Literal["checkpoint", "stage", "generation"]
MatrixColumnAxis = Literal["task", "domain", "scenario", "variant"]


class MetricMatrixCell(StrictModel):
    row_id: str = Field(pattern=ID_PATTERN)
    column_id: str = Field(pattern=ID_PATTERN)
    source_cell_id: str = Field(pattern=ID_PATTERN)
    disposition: ExperimentCellDisposition
    value: float | None = None
    weight: float = Field(default=1.0, gt=0, strict=True)

    @model_validator(mode="after")
    def matrix_value_matches_disposition(self) -> "MetricMatrixCell":
        included = self.disposition in {
            ExperimentCellDisposition.observed,
            ExperimentCellDisposition.zero_filled,
        }
        if included != (self.value is not None):
            raise ValueError("metric matrix values must match cell dispositions")
        if self.disposition == ExperimentCellDisposition.zero_filled and self.value != 0:
            raise ValueError("zero-filled metric matrix cells must equal zero")
        return self


class MetricCellMatrix(StrictModel):
    format: Literal["bmp-metric-cell-matrix-v1"] = "bmp-metric-cell-matrix-v1"
    source_plan_digest: str = Field(pattern=SHA256_PATTERN)
    source_observation_digest: str = Field(pattern=SHA256_PATTERN)
    metric_id: str = Field(pattern=ID_PATTERN)
    metric_digest: str = Field(pattern=SHA256_PATTERN)
    row_axis: MatrixRowAxis
    column_axis: MatrixColumnAxis
    row_ids: tuple[str, ...]
    column_ids: tuple[str, ...]
    cells: tuple[MetricMatrixCell, ...]
    matrix_digest: str = Field(pattern=SHA256_PATTERN)

    def identity_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"matrix_digest"})

    def canonical_digest(self) -> str:
        return _digest_json(self.identity_data())

    @field_validator("row_ids", "column_ids")
    @classmethod
    def matrix_axes_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("metric matrix axes must be non-empty and unique")
        return values

    @model_validator(mode="after")
    def matrix_is_a_complete_cartesian_population(self) -> "MetricCellMatrix":
        expected = [
            (row_id, column_id)
            for row_id in self.row_ids
            for column_id in self.column_ids
        ]
        observed = [(cell.row_id, cell.column_id) for cell in self.cells]
        if observed != expected:
            raise ValueError("metric matrix must contain the ordered Cartesian product")
        source_ids = [cell.source_cell_id for cell in self.cells]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("metric matrix source cells must be unique")
        if self.matrix_digest != self.canonical_digest():
            raise ValueError("metric cell matrix digest drift")
        return self


class ExternalMetricAdapterInput(StrictModel):
    """Typed input envelope presented to a source-closed metric plugin."""

    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    capability_digest: str = Field(pattern=SHA256_PATTERN)
    capability: AdapterCapabilityArtifact
    metric: MetricArtifact
    cell_ledger: ExperimentCellLedger
    cell_ledger_ref: ArtifactRef
    cell_matrix: MetricCellMatrix | None = None
    cell_matrix_ref: ArtifactRef | None = None
    source_refs: tuple[ArtifactRef, ...]

    @property
    def effective_source_refs(self) -> tuple[ArtifactRef, ...]:
        if self.cell_matrix_ref is None:
            return (self.cell_ledger_ref, *self.source_refs)
        return (self.cell_matrix_ref, self.cell_ledger_ref, *self.source_refs)

    @model_validator(mode="after")
    def input_evidence_matches_metric_source(self) -> "ExternalMetricAdapterInput":
        if not self.source_refs:
            raise ValueError("external metric adapter requires source artifact refs")
        if (self.cell_matrix is None) != (self.cell_matrix_ref is None):
            raise ValueError("cell matrix and its artifact ref must be provided together")
        if self.metric.canonical_digest() != self.metric.artifact_digest:
            raise ValueError("external metric artifact digest drift")
        if self.capability.canonical_digest() != self.capability.artifact_digest:
            raise ValueError("external metric capability artifact digest drift")
        capability = self.capability.capability
        metric = self.metric.metric
        if self.capability_digest != self.capability.artifact_digest:
            raise ValueError("external metric capability digest differs from its artifact")
        if capability.adapter_kind != "metric_source":
            raise ValueError("external metric input requires a metric_source capability")
        if capability.adapter != metric.adapter:
            raise ValueError("external metric and capability adapters differ")
        if metric.source.value not in capability.supported_metric_sources:
            raise ValueError("external metric source is outside its capability")
        if metric.formula.value not in capability.supported_metric_formulas:
            raise ValueError("external metric formula is outside its capability")
        if not _ref_matches_model(self.cell_ledger_ref, self.cell_ledger):
            raise ValueError("cell ledger artifact ref does not bind its canonical bytes")
        if self.cell_matrix is not None:
            assert self.cell_matrix_ref is not None
            if not _ref_matches_model(self.cell_matrix_ref, self.cell_matrix):
                raise ValueError("cell matrix artifact ref does not bind its canonical bytes")
        identities = [
            (ref.path, ref.sha256, ref.size_bytes)
            for ref in self.effective_source_refs
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("external metric source refs must be unique")
        if self.source_refs != self.cell_ledger.plan.membership_refs:
            raise ValueError(
                "external metric source refs differ from cell-plan membership authority"
            )
        if self.manifest_digest not in set(
            self.cell_ledger.plan.stage_manifest_digests.values()
        ):
            raise ValueError("external metric manifest is absent from the cell plan")
        metric_cells = [
            cell
            for cell in self.cell_ledger.plan.cells
            if cell.metric_id == metric.id
        ]
        if not metric_cells:
            raise ValueError("external metric has no planned cells")
        if any(cell.metric_digest != self.metric.artifact_digest for cell in metric_cells):
            raise ValueError("planned cell metric digest differs from external metric")
        if self.cell_matrix is not None:
            matrix = self.cell_matrix
            if (
                matrix.source_plan_digest != self.cell_ledger.plan.plan_digest
                or matrix.source_observation_digest
                != self.cell_ledger.observation_digest
            ):
                raise ValueError("cell matrix source digests differ from its ledger")
            if (
                matrix.metric_id != metric.id
                or matrix.metric_digest != self.metric.artifact_digest
            ):
                raise ValueError("cell matrix metric identity differs from external metric")
            planned_by_id = {
                cell.cell_id: cell for cell in self.cell_ledger.plan.cells
            }
            observed_by_id = {
                item.cell_id: item for item in self.cell_ledger.observations
            }
            matrix_ids = [cell.source_cell_id for cell in matrix.cells]
            expected_ids = [cell.cell_id for cell in metric_cells]
            if set(matrix_ids) != set(expected_ids):
                raise ValueError("cell matrix does not cover the metric's planned cells")
            for cell in matrix.cells:
                planned = planned_by_id[cell.source_cell_id]
                observed = observed_by_id[cell.source_cell_id]
                if (
                    cell.disposition != observed.disposition
                    or cell.value != observed.value
                    or cell.weight != planned.weight
                ):
                    raise ValueError("cell matrix projection differs from its source ledger")
        return self


class ExternalMetricGroupResult(StrictModel):
    group_id: str = Field(pattern=ID_PATTERN)
    value: float
    contributing_cell_ids: tuple[str, ...]

    @field_validator("contributing_cell_ids")
    @classmethod
    def group_cell_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("external metric groups require unique contributing cells")
        return values


class ExternalMetricComputationReceipt(StrictModel):
    format: Literal["bmp-external-metric-computation-v1"] = (
        "bmp-external-metric-computation-v1"
    )
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    metric_id: str = Field(pattern=ID_PATTERN)
    metric_digest: str = Field(pattern=SHA256_PATTERN)
    adapter_capability_digest: str = Field(pattern=SHA256_PATTERN)
    adapter_config_digest: str = Field(pattern=SHA256_PATTERN)
    source_refs: tuple[ArtifactRef, ...]
    source_population_digest: str = Field(pattern=SHA256_PATTERN)
    state: MetricComputationState
    value: float | None = None
    reason: str | None = Field(default=None, min_length=1)
    planned_cell_count: int = Field(ge=1, strict=True)
    observed_count: int = Field(ge=0, strict=True)
    zero_filled_count: int = Field(ge=0, strict=True)
    excluded_count: int = Field(ge=0, strict=True)
    missing_count: int = Field(ge=0, strict=True)
    invalid_count: int = Field(ge=0, strict=True)
    groups: tuple[ExternalMetricGroupResult, ...] = ()
    contributing_cell_ids: tuple[str, ...]
    contribution_digest: str = Field(pattern=SHA256_PATTERN)

    def canonical_contribution_digest(self) -> str:
        return _digest_json(list(self.contributing_cell_ids))

    @model_validator(mode="after")
    def external_metric_receipt_reconciles(self) -> "ExternalMetricComputationReceipt":
        if self.state == MetricComputationState.complete:
            if self.value is None or self.reason is not None:
                raise ValueError("complete external metric requires a value")
            if not self.contributing_cell_ids:
                raise ValueError("complete external metric requires contributions")
        elif self.value is not None or self.reason is None:
            raise ValueError("failed external metric requires a reason and no value")
        if (
            self.observed_count
            + self.zero_filled_count
            + self.excluded_count
            + self.missing_count
            + self.invalid_count
            != self.planned_cell_count
        ):
            raise ValueError("external metric disposition counts must cover the plan")
        if len(set(self.contributing_cell_ids)) != len(self.contributing_cell_ids):
            raise ValueError("external metric contributing cells must be unique")
        if any(
            re.fullmatch(ID_PATTERN, cell_id) is None
            for cell_id in self.contributing_cell_ids
        ):
            raise ValueError("external metric contributing cell ids must be BMP ids")
        if self.state == MetricComputationState.complete and len(
            self.contributing_cell_ids
        ) != self.observed_count + self.zero_filled_count:
            raise ValueError(
                "complete external metric contributions must cover included cells"
            )
        if self.contribution_digest != self.canonical_contribution_digest():
            raise ValueError("external metric contribution digest drift")
        grouped_ids = [
            cell_id for group in self.groups for cell_id in group.contributing_cell_ids
        ]
        if self.groups and (
            len(set(grouped_ids)) != len(grouped_ids)
            or set(grouped_ids) != set(self.contributing_cell_ids)
        ):
            raise ValueError("external metric groups must partition contributions")
        return self


class SealedHoldoutReleaseReceipt(StrictModel):
    format: Literal["bmp-sealed-holdout-release-v1"] = (
        "bmp-sealed-holdout-release-v1"
    )
    regime_digest: str = Field(pattern=SHA256_PATTERN)
    holdout_stage_id: str = Field(pattern=ID_PATTERN)
    selection_stage_id: str = Field(pattern=ID_PATTERN)
    selection_receipt_ref: ArtifactRef
    holdout_membership_refs: tuple[ArtifactRef, ...]
    selection_closed_at: str
    released_at: str

    @field_validator("selection_closed_at", "released_at")
    @classmethod
    def timestamps_are_utc(cls, value: str, info: Any) -> str:
        return _utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def holdout_releases_only_after_selection(self) -> "SealedHoldoutReleaseReceipt":
        closed = datetime.fromisoformat(self.selection_closed_at.replace("Z", "+00:00"))
        released = datetime.fromisoformat(self.released_at.replace("Z", "+00:00"))
        if released < closed:
            raise ValueError("sealed holdout cannot release before selection closes")
        if not self.holdout_membership_refs:
            raise ValueError("sealed holdout release requires membership authority")
        return self


class ExperimentStageActivationReceipt(StrictModel):
    format: Literal["bmp-experiment-stage-activation-v1"] = (
        "bmp-experiment-stage-activation-v1"
    )
    regime_id: str = Field(pattern=ID_PATTERN)
    regime_digest: str = Field(pattern=SHA256_PATTERN)
    stage_id: str = Field(pattern=ID_PATTERN)
    stage_role: ExperimentStageRole
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    state_policy: StageStatePolicy
    feedback_visibility: StageFeedbackVisibility
    sealed: bool
    predecessor_receipt_refs: tuple[ArtifactRef, ...] = ()
    input_state_refs: tuple[ArtifactRef, ...] = ()
    output_state_refs: tuple[ArtifactRef, ...] = ()
    holdout_release_ref: ArtifactRef | None = None
    cell_plan_ref: ArtifactRef
    cell_plan_digest: str = Field(pattern=SHA256_PATTERN)
    cell_ledger_ref: ArtifactRef
    cell_observation_digest: str = Field(pattern=SHA256_PATTERN)
    started_at: str
    finished_at: str
    terminal_status: RunStatus

    @field_validator("started_at", "finished_at")
    @classmethod
    def stage_timestamps_are_utc(cls, value: str, info: Any) -> str:
        return _utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def stage_activation_matches_state_and_sealing(self) -> "ExperimentStageActivationReceipt":
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
        if finished < started:
            raise ValueError("experiment stage cannot finish before it starts")
        if self.state_policy == StageStatePolicy.reset:
            if self.input_state_refs:
                raise ValueError("reset stage forbids inherited input state")
        elif not self.predecessor_receipt_refs or not self.input_state_refs:
            raise ValueError("non-reset stage requires predecessor and input state refs")
        if self.state_policy == StageStatePolicy.read_only and self.output_state_refs:
            raise ValueError("read-only stage forbids output state")
        if self.state_policy in {StageStatePolicy.carry, StageStatePolicy.fork} and (
            not self.output_state_refs
        ):
            raise ValueError("carry/fork stage requires output state refs")
        if self.sealed:
            if (
                self.stage_role != ExperimentStageRole.holdout
                or self.feedback_visibility != StageFeedbackVisibility.none
                or self.holdout_release_ref is None
            ):
                raise ValueError(
                    "sealed activation requires holdout role, no feedback, and release proof"
                )
        elif self.holdout_release_ref is not None:
            raise ValueError("unsealed stage forbids holdout release evidence")
        return self


def build_experiment_cell_plan(
    *,
    regime_id: str,
    regime_digest: str,
    stage_manifest_digests: Mapping[str, str],
    membership_refs: tuple[ArtifactRef, ...],
    cells: tuple[PlannedMetricCell, ...],
) -> ExperimentCellPlan:
    """Create a cell plan with its canonical digest populated."""

    identity = {
        "format": "bmp-experiment-cell-plan-v1",
        "regime_id": regime_id,
        "regime_digest": regime_digest,
        "stage_manifest_digests": dict(stage_manifest_digests),
        "membership_refs": [ref.identity_data() for ref in membership_refs],
        "cells": [cell.model_dump(mode="json") for cell in cells],
    }
    return ExperimentCellPlan(
        regime_id=regime_id,
        regime_digest=regime_digest,
        stage_manifest_digests=stage_manifest_digests,
        membership_refs=membership_refs,
        cells=cells,
        plan_digest=_digest_json(identity),
    )


def build_experiment_cell_ledger(
    plan: ExperimentCellPlan,
    observations: tuple[ExperimentCellObservation, ...],
) -> ExperimentCellLedger:
    """Create a reconciled observation ledger over every planned cell."""

    counts = {
        disposition: sum(item.disposition == disposition for item in observations)
        for disposition in ExperimentCellDisposition
    }
    payload = [observation.model_dump(mode="json") for observation in observations]
    return ExperimentCellLedger(
        plan=plan,
        observations=observations,
        observed_count=counts[ExperimentCellDisposition.observed],
        zero_filled_count=counts[ExperimentCellDisposition.zero_filled],
        excluded_count=counts[ExperimentCellDisposition.excluded],
        missing_count=counts[ExperimentCellDisposition.missing],
        invalid_count=counts[ExperimentCellDisposition.invalid],
        observation_digest=_digest_json(payload),
    )


def build_metric_cell_matrix(
    *,
    source_plan_digest: str,
    source_observation_digest: str,
    metric_id: str,
    metric_digest: str,
    row_axis: MatrixRowAxis,
    column_axis: MatrixColumnAxis,
    row_ids: tuple[str, ...],
    column_ids: tuple[str, ...],
    cells: tuple[MetricMatrixCell, ...],
) -> MetricCellMatrix:
    """Create a complete content-addressed matrix projection."""

    identity = {
        "format": "bmp-metric-cell-matrix-v1",
        "source_plan_digest": source_plan_digest,
        "source_observation_digest": source_observation_digest,
        "metric_id": metric_id,
        "metric_digest": metric_digest,
        "row_axis": row_axis,
        "column_axis": column_axis,
        "row_ids": list(row_ids),
        "column_ids": list(column_ids),
        "cells": [cell.model_dump(mode="json") for cell in cells],
    }
    return MetricCellMatrix(
        source_plan_digest=source_plan_digest,
        source_observation_digest=source_observation_digest,
        metric_id=metric_id,
        metric_digest=metric_digest,
        row_axis=row_axis,
        column_axis=column_axis,
        row_ids=row_ids,
        column_ids=column_ids,
        cells=cells,
        matrix_digest=_digest_json(identity),
    )


class ContinualAnalysisPlan(StrictModel):
    """Registered checkpoint semantics for matrix-derived continual metrics."""

    task_ids: tuple[str, ...]
    baseline_checkpoint_id: str = Field(pattern=ID_PATTERN)
    terminal_checkpoint_id: str = Field(pattern=ID_PATTERN)
    learned_checkpoint_by_task: Mapping[str, str]

    @model_validator(mode="after")
    def continual_boundaries_are_complete(self) -> "ContinualAnalysisPlan":
        if len(self.task_ids) < 2 or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("continual analysis requires at least two unique tasks")
        if set(self.learned_checkpoint_by_task) != set(self.task_ids):
            raise ValueError("continual learned checkpoints must cover every task")
        return self


class DerivedMatrixMetricResult(StrictModel):
    metric: Literal[
        "average_accuracy_macro.v1",
        "backward_transfer_macro.v1",
        "forgetting_max_history_macro.v1",
        "forward_transfer_macro.v1",
    ]
    state: MetricComputationState
    value: float | None = None
    reason: str | None = Field(default=None, min_length=1)
    source_matrix_digest: str = Field(pattern=SHA256_PATTERN)
    contributing_cell_ids: tuple[str, ...]

    @model_validator(mode="after")
    def derived_result_is_coherent(self) -> "DerivedMatrixMetricResult":
        if self.state == MetricComputationState.complete:
            if self.value is None or self.reason is not None:
                raise ValueError("complete derived matrix metric requires a value")
        elif self.value is not None or self.reason is None:
            raise ValueError("failed derived matrix metric requires a reason")
        if len(set(self.contributing_cell_ids)) != len(self.contributing_cell_ids):
            raise ValueError("derived matrix contributing cells must be unique")
        return self


def analyze_continual_matrix(
    matrix: MetricCellMatrix,
    plan: ContinualAnalysisPlan,
) -> tuple[DerivedMatrixMetricResult, ...]:
    """Compute four canonical continual metrics from a complete planned matrix."""

    if matrix.row_axis != "checkpoint" or matrix.column_axis != "task":
        raise ValueError("continual analysis requires checkpoint x task matrix")
    if tuple(matrix.column_ids) != tuple(plan.task_ids):
        raise ValueError("continual task order differs from the matrix")
    required_rows = {
        plan.baseline_checkpoint_id,
        plan.terminal_checkpoint_id,
        *plan.learned_checkpoint_by_task.values(),
    }
    missing_rows = sorted(required_rows - set(matrix.row_ids))
    if missing_rows:
        raise ValueError(f"continual matrix lacks required checkpoints: {missing_rows}")
    by_key = {(cell.row_id, cell.column_id): cell for cell in matrix.cells}

    def result(
        metric: str,
        selected: list[MetricMatrixCell],
        reducer: Any,
    ) -> DerivedMatrixMetricResult:
        unavailable = [
            cell
            for cell in selected
            if cell.disposition
            not in {
                ExperimentCellDisposition.observed,
                ExperimentCellDisposition.zero_filled,
            }
        ]
        if unavailable:
            return DerivedMatrixMetricResult(
                metric=metric,
                state=MetricComputationState.invalid,
                reason="one or more required planned matrix cells are unavailable",
                source_matrix_digest=matrix.matrix_digest,
                contributing_cell_ids=tuple(cell.source_cell_id for cell in selected),
            )
        return DerivedMatrixMetricResult(
            metric=metric,
            state=MetricComputationState.complete,
            value=float(reducer([float(cell.value) for cell in selected])),
            source_matrix_digest=matrix.matrix_digest,
            contributing_cell_ids=tuple(cell.source_cell_id for cell in selected),
        )

    terminal_cells = [
        by_key[(plan.terminal_checkpoint_id, task_id)] for task_id in plan.task_ids
    ]
    average = result(
        "average_accuracy_macro.v1",
        terminal_cells,
        lambda values: sum(values) / len(values),
    )

    prior_tasks = plan.task_ids[:-1]
    bwt_pairs = [
        (
            by_key[(plan.terminal_checkpoint_id, task_id)],
            by_key[(plan.learned_checkpoint_by_task[task_id], task_id)],
        )
        for task_id in prior_tasks
    ]
    bwt_cells = [cell for pair in bwt_pairs for cell in pair]
    bwt = result(
        "backward_transfer_macro.v1",
        bwt_cells,
        lambda values: sum(
            values[index] - values[index + 1]
            for index in range(0, len(values), 2)
        )
        / len(bwt_pairs),
    )

    row_position = {row_id: index for index, row_id in enumerate(matrix.row_ids)}
    forgetting_groups: list[list[MetricMatrixCell]] = []
    for task_id in prior_tasks:
        learned_row = plan.learned_checkpoint_by_task[task_id]
        start = row_position[learned_row]
        end = row_position[plan.terminal_checkpoint_id]
        if start > end:
            raise ValueError("learned checkpoint occurs after terminal checkpoint")
        forgetting_groups.append(
            [by_key[(row_id, task_id)] for row_id in matrix.row_ids[start : end + 1]]
        )
    forgetting_cells = [cell for group in forgetting_groups for cell in group]

    def forgetting_reducer(values: list[float]) -> float:
        cursor = 0
        differences: list[float] = []
        for group in forgetting_groups:
            group_values = values[cursor : cursor + len(group)]
            cursor += len(group)
            differences.append(max(group_values[:-1] or group_values) - group_values[-1])
        return sum(differences) / len(differences)

    forgetting = result(
        "forgetting_max_history_macro.v1",
        forgetting_cells,
        forgetting_reducer,
    )

    fwt_pairs: list[tuple[MetricMatrixCell, MetricMatrixCell]] = []
    for task_index, task_id in enumerate(plan.task_ids[1:], start=1):
        previous_task = plan.task_ids[task_index - 1]
        previous_checkpoint = plan.learned_checkpoint_by_task[previous_task]
        fwt_pairs.append(
            (
                by_key[(previous_checkpoint, task_id)],
                by_key[(plan.baseline_checkpoint_id, task_id)],
            )
        )
    fwt_cells = [cell for pair in fwt_pairs for cell in pair]
    fwt = result(
        "forward_transfer_macro.v1",
        fwt_cells,
        lambda values: sum(
            values[index] - values[index + 1]
            for index in range(0, len(values), 2)
        )
        / len(fwt_pairs),
    )
    return average, bwt, forgetting, fwt


__all__ = [
    "ContinualAnalysisPlan",
    "DerivedMatrixMetricResult",
    "ExperimentCellCoordinates",
    "ExperimentCellDisposition",
    "ExperimentCellLedger",
    "ExperimentCellObservation",
    "ExperimentCellPlan",
    "ExternalMetricAdapterInput",
    "ExternalMetricComputationReceipt",
    "ExternalMetricGroupResult",
    "ExperimentStageActivationReceipt",
    "MetricCellMatrix",
    "MetricMatrixCell",
    "PlannedMetricCell",
    "SealedHoldoutReleaseReceipt",
    "analyze_continual_matrix",
    "build_experiment_cell_ledger",
    "build_experiment_cell_plan",
    "build_metric_cell_matrix",
]
