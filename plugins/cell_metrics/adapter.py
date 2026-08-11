"""Generic complete-membership group reducer for experiment cell ledgers."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from MagentaBench.schemas import (
    ExperimentCellDisposition,
    ExternalMetricAdapterInput,
    ExternalMetricComputationReceipt,
    ExternalMetricGroupResult,
    MetricArtifact,
    MetricComputationState,
)


_MODULE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class CellLedgerMetricAdapter:
    """Reduce exactly registered group membership with no observed-only fallback."""

    adapter = "magentabench.cell-metrics"
    digest = _MODULE_DIGEST

    @staticmethod
    def _axis(coordinates: Any, axis: str) -> str:
        return coordinates.axis_value(axis)

    def compute(
        self,
        metric: Any,
        inputs: ExternalMetricAdapterInput,
    ) -> ExternalMetricComputationReceipt:
        if not isinstance(metric, MetricArtifact):
            raise TypeError("cell metric adapter requires a resolved MetricArtifact")
        if metric.canonical_digest() != metric.artifact_digest:
            raise ValueError("metric adapter argument artifact digest drift")
        if metric != inputs.metric:
            raise ValueError("metric adapter argument/input complete identity drift")
        capability = inputs.capability.capability
        if (
            capability.adapter != self.adapter
            or capability.adapter_kind != "metric_source"
            or capability.digest != self.digest
        ):
            raise ValueError("metric adapter runtime/capability identity drift")
        if inputs.capability_digest != inputs.capability.artifact_digest:
            raise ValueError("metric adapter capability digest drift")
        if inputs.cell_matrix is not None:
            raise ValueError("cell ledger metric adapter does not consume a cell matrix")
        metric_spec = metric.metric
        config = dict(metric_spec.config)
        ledger = inputs.cell_ledger
        group_axis = str(config["group_axis"])
        member_axis = str(config["member_axis"])
        stage_id = str(config["stage_id"])
        checkpoint_id = str(config["checkpoint_id"])
        expected_groups = {
            str(group_id): tuple(str(member) for member in members)
            for group_id, members in dict(config["members_by_group"]).items()
        }
        if tuple(key.value for key in metric_spec.group_by) != (group_axis,):
            raise ValueError("metric group_by differs from adapter group axis")
        if (
            metric_spec.across_groups is None
            or metric_spec.across_groups.value != config["across_groups"]
        ):
            raise ValueError(
                "metric across_groups differs from adapter aggregation config"
            )
        observations_by_id = {
            observation.cell_id: observation
            for observation in ledger.observations
        }
        selected = [
            cell
            for cell in ledger.plan.cells
            if cell.metric_id == metric_spec.id
            and cell.coordinates.stage_id == stage_id
            and cell.coordinates.checkpoint_id == checkpoint_id
        ]
        if not selected:
            raise ValueError("metric adapter selection matches no planned cells")
        if ledger.plan.stage_manifest_digests[stage_id] != inputs.manifest_digest:
            raise ValueError("selected cell stage differs from the input manifest")
        if any(cell.metric_digest != metric.artifact_digest for cell in selected):
            raise ValueError("selected planned cell metric digest drift")
        if any(cell.weight != 1.0 for cell in selected):
            raise ValueError("unweighted cell reducer requires planned cell weight=1")
        observed_membership: dict[str, list[str]] = defaultdict(list)
        selected_by_group_member: dict[tuple[str, str], str] = {}
        for cell in selected:
            group_id = self._axis(cell.coordinates, group_axis)
            member_id = self._axis(cell.coordinates, member_axis)
            key = (group_id, member_id)
            if key in selected_by_group_member:
                raise ValueError(
                    "cell metric adapter requires one planned cell per group member"
                )
            selected_by_group_member[key] = cell.cell_id
            observed_membership[group_id].append(member_id)
        canonical_observed = {
            group_id: tuple(members)
            for group_id, members in observed_membership.items()
        }
        if canonical_observed != expected_groups:
            raise ValueError(
                "planned cell membership differs from metric adapter configuration"
            )

        dispositions = Counter(
            observations_by_id[cell.cell_id].disposition for cell in selected
        )
        invalid = [
            observations_by_id[cell.cell_id]
            for cell in selected
            if observations_by_id[cell.cell_id].disposition
            not in {
                ExperimentCellDisposition.observed,
                ExperimentCellDisposition.zero_filled,
            }
        ]
        config_digest = _digest(metric_spec.config)
        source_population_digest = _digest(
            {
                "plan": ledger.plan.plan_digest,
                "observations": ledger.observation_digest,
                "selected": [cell.cell_id for cell in selected],
            }
        )
        base = {
            "manifest_digest": inputs.manifest_digest,
            "metric_id": metric_spec.id,
            "metric_digest": metric.artifact_digest,
            "adapter_capability_digest": inputs.capability.artifact_digest,
            "adapter_config_digest": config_digest,
            "source_refs": inputs.effective_source_refs,
            "source_population_digest": source_population_digest,
            "planned_cell_count": len(selected),
            "observed_count": dispositions[ExperimentCellDisposition.observed],
            "zero_filled_count": dispositions[
                ExperimentCellDisposition.zero_filled
            ],
            "excluded_count": dispositions[ExperimentCellDisposition.excluded],
            "missing_count": dispositions[ExperimentCellDisposition.missing],
            "invalid_count": dispositions[ExperimentCellDisposition.invalid],
        }
        if invalid:
            contribution_ids: tuple[str, ...] = ()
            return ExternalMetricComputationReceipt(
                **base,
                state=MetricComputationState.invalid,
                reason="one or more required planned group cells are unavailable",
                contributing_cell_ids=contribution_ids,
                contribution_digest=_digest(list(contribution_ids)),
            )

        group_results: list[ExternalMetricGroupResult] = []
        for group_id, member_ids in expected_groups.items():
            cell_ids = tuple(
                selected_by_group_member[(group_id, member_id)]
                for member_id in member_ids
            )
            values = [
                float(observations_by_id[cell_id].value) for cell_id in cell_ids
            ]
            if config["within_group"] == "minimum":
                group_value = min(values)
            else:
                group_value = sum(values) / len(values)
            group_results.append(
                ExternalMetricGroupResult(
                    group_id=group_id,
                    value=group_value,
                    contributing_cell_ids=cell_ids,
                )
            )
        group_values = [group.value for group in group_results]
        if config["across_groups"] == "minimum":
            aggregate = min(group_values)
        else:
            aggregate = sum(group_values) / len(group_values)
        contribution_ids = tuple(
            cell_id
            for group in group_results
            for cell_id in group.contributing_cell_ids
        )
        # Keep a direct assertion that all selected cells participate.  This
        # prevents a plugin config from validating membership but silently
        # dropping a group during aggregation.
        if set(contribution_ids) != {cell.cell_id for cell in selected}:
            raise ValueError("external metric did not consume its selected population")
        return ExternalMetricComputationReceipt(
            **base,
            state=MetricComputationState.complete,
            value=aggregate,
            groups=tuple(group_results),
            contributing_cell_ids=contribution_ids,
            contribution_digest=_digest(list(contribution_ids)),
        )


__all__ = ["CellLedgerMetricAdapter"]
