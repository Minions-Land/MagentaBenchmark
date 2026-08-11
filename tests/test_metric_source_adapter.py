from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.runner.adapter_registry import AdapterRegistry
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.evidence import artifact_ref, atomic_write_json
from MagentaBench.schemas import (
    AdapterCapabilityArtifact,
    ArtifactRef,
    ExperimentCellCoordinates,
    ExperimentCellDisposition,
    ExperimentCellObservation,
    ExternalMetricAdapterInput,
    ExternalMetricComputationReceipt,
    MetricArtifact,
    MetricMatrixCell,
    MetricSpec,
    PlannedMetricCell,
    build_experiment_cell_ledger,
    build_experiment_cell_plan,
    build_metric_cell_matrix,
)


ROOT = Path(__file__).parents[1]
MANIFEST_DIGEST = "e" * 64


def _write_ref(tmp_path: Path, name: str, value: object) -> ArtifactRef:
    path = tmp_path / name
    atomic_write_json(path, value)
    return artifact_ref(path)


@lru_cache(maxsize=1)
def _capability() -> AdapterCapabilityArtifact:
    artifact = Compiler(ROOT)._adapter_capability_artifact(
        "magentabench.cell-metrics", "metric_source"
    )
    assert artifact is not None
    return artifact


def _metric(
    tmp_path: Path,
    *,
    within_group: str = "mean",
    declaration_name: str = "metric-base.json",
) -> MetricArtifact:
    spec = MetricSpec(
        id="domain-macro.accuracy.v1",
        kind="metric",
        adapter="magentabench.cell-metrics",
        bmp_version="0.1",
        value_kind="rate",
        level="experiment",
        direction="maximize",
        unit="fraction",
        source="regime",
        source_field="cell_ledger",
        formula="external_adapter_v1",
        population="domains",
        group_by=("domain",),
        across_groups="macro_mean",
        missing_observation="invalidate",
        config={
            "group_axis": "domain",
            "member_axis": "task",
            "stage_id": "evaluate",
            "checkpoint_id": "final",
            "members_by_group": {
                "domain-1": ["task-1", "task-2"],
                "domain-2": ["task-3"],
            },
            "within_group": within_group,
            "across_groups": "macro_mean",
        },
    )
    declaration_ref = _write_ref(
        tmp_path, declaration_name, {"metric": spec.model_dump(mode="json")}
    )
    provisional = MetricArtifact(
        metric=spec,
        declaration_ref=declaration_ref,
        artifact_digest="0" * 64,
    )
    return provisional.model_copy(
        update={"artifact_digest": provisional.canonical_digest()}
    )


def _input(
    tmp_path: Path,
    *,
    name: str = "base",
    missing: bool = False,
    weight: float = 1.0,
    planned_metric_digest: str | None = None,
    matrix_plan_digest: str | None = None,
    matrix_value_delta: float = 0.0,
) -> ExternalMetricAdapterInput:
    metric = _metric(tmp_path)
    membership = _write_ref(
        tmp_path,
        f"membership-{name}.json",
        {
            "members_by_group": {
                "domain-1": ["task-1", "task-2"],
                "domain-2": ["task-3"],
            }
        },
    )
    definitions = (
        ("cell-1", "domain-1", "task-1", 0.8),
        ("cell-2", "domain-1", "task-2", 0.6),
        ("cell-3", "domain-2", "task-3", 0.4),
    )
    cells = tuple(
        PlannedMetricCell(
            cell_id=cell_id,
            coordinates=ExperimentCellCoordinates(
                stage_id="evaluate",
                checkpoint_id="final",
                task_id=task_id,
                domain_id=domain_id,
            ),
            metric_id=metric.metric.id,
            metric_digest=planned_metric_digest or metric.artifact_digest,
            membership_digest=membership.sha256,
            weight=weight,
        )
        for cell_id, domain_id, task_id, _ in definitions
    )
    plan = build_experiment_cell_plan(
        regime_id="domain-eval",
        regime_digest="c" * 64,
        stage_manifest_digests={"evaluate": MANIFEST_DIGEST},
        membership_refs=(membership,),
        cells=cells,
    )
    observations = tuple(
        ExperimentCellObservation(
            cell_id=cell_id,
            disposition=(
                ExperimentCellDisposition.missing
                if missing and cell_id == "cell-2"
                else ExperimentCellDisposition.observed
            ),
            value=None if missing and cell_id == "cell-2" else value,
            terminal_status=None if missing and cell_id == "cell-2" else "scored",
            reason=(
                "planned evaluator cell missing"
                if missing and cell_id == "cell-2"
                else None
            ),
            metric_result_ref=(
                None
                if missing and cell_id == "cell-2"
                else _write_ref(
                    tmp_path,
                    f"{name}-{cell_id}.json",
                    {"cell_id": cell_id, "value": value},
                )
            ),
        )
        for cell_id, _, _, value in definitions
    )
    ledger = build_experiment_cell_ledger(plan, observations)
    ledger_ref = _write_ref(tmp_path, f"cell-ledger-{name}.json", ledger)

    matrix = None
    matrix_ref = None
    if matrix_plan_digest is not None or matrix_value_delta:
        matrix_cells = tuple(
            MetricMatrixCell(
                row_id="final",
                column_id=task_id,
                source_cell_id=cell_id,
                disposition=observation.disposition,
                value=(
                    None
                    if observation.value is None
                    else observation.value + matrix_value_delta
                ),
                weight=weight,
            )
            for (cell_id, _, task_id, _), observation in zip(
                definitions, observations, strict=True
            )
        )
        matrix = build_metric_cell_matrix(
            source_plan_digest=matrix_plan_digest or plan.plan_digest,
            source_observation_digest=ledger.observation_digest,
            metric_id=metric.metric.id,
            metric_digest=metric.artifact_digest,
            row_axis="checkpoint",
            column_axis="task",
            row_ids=("final",),
            column_ids=tuple(item[2] for item in definitions),
            cells=matrix_cells,
        )
        matrix_ref = _write_ref(tmp_path, f"cell-matrix-{name}.json", matrix)

    capability = _capability()
    return ExternalMetricAdapterInput(
        manifest_digest=MANIFEST_DIGEST,
        capability_digest=capability.artifact_digest,
        capability=capability,
        metric=metric,
        cell_ledger=ledger,
        cell_ledger_ref=ledger_ref,
        cell_matrix=matrix,
        cell_matrix_ref=matrix_ref,
        source_refs=(membership,),
    )


def _adapter():
    registry = AdapterRegistry.from_project(
        ROOT,
        required_capabilities={("magentabench.cell-metrics", "metric_source")},
    )
    return registry.metric_source_adapter("magentabench.cell-metrics")


def test_metric_source_adapter_enforces_registered_group_membership(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    complete_input = _input(tmp_path, name="complete")
    receipt = adapter.compute(complete_input.metric, complete_input)
    assert receipt.value == pytest.approx(0.55)
    assert [group.value for group in receipt.groups] == pytest.approx([0.7, 0.4])
    assert receipt.planned_cell_count == 3
    assert receipt.contribution_digest == receipt.canonical_contribution_digest()

    missing_input = _input(tmp_path, name="missing", missing=True)
    invalid = adapter.compute(missing_input.metric, missing_input)
    assert invalid.state.value == "invalid"
    assert invalid.missing_count == 1
    assert invalid.value is None


def test_metric_source_adapter_rejects_same_id_with_different_configuration(
    tmp_path: Path,
) -> None:
    inputs = _input(tmp_path)
    replacement = _metric(
        tmp_path,
        within_group="minimum",
        declaration_name="metric-replacement.json",
    )
    assert replacement.metric.id == inputs.metric.metric.id
    assert replacement.artifact_digest != inputs.metric.artifact_digest

    with pytest.raises(ValueError, match="complete identity drift"):
        _adapter().compute(replacement, inputs)


def test_external_metric_input_rejects_capability_digest_substitution(
    tmp_path: Path,
) -> None:
    payload = _input(tmp_path).model_dump(mode="json")
    payload["capability_digest"] = "1" * 64

    with pytest.raises(ValidationError, match="differs from its artifact"):
        ExternalMetricAdapterInput.model_validate(payload)


def test_external_metric_input_rejects_planned_metric_digest_substitution(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="planned cell metric digest differs"):
        _input(tmp_path, planned_metric_digest="1" * 64)


def test_external_metric_input_rejects_unbound_ledger_ref(tmp_path: Path) -> None:
    payload = _input(tmp_path).model_dump(mode="json")
    payload["cell_ledger_ref"]["sha256"] = "1" * 64

    with pytest.raises(ValidationError, match="does not bind its canonical bytes"):
        ExternalMetricAdapterInput.model_validate(payload)


def test_external_metric_input_rejects_membership_authority_substitution(
    tmp_path: Path,
) -> None:
    payload = _input(tmp_path).model_dump(mode="json")
    alternate = _write_ref(tmp_path, "alternate-membership.json", {"tasks": []})
    payload["source_refs"] = [alternate.model_dump(mode="json")]

    with pytest.raises(ValidationError, match="membership authority"):
        ExternalMetricAdapterInput.model_validate(payload)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"matrix_plan_digest": "1" * 64}, "source digests differ"),
        ({"matrix_value_delta": 0.1}, "projection differs"),
    ],
)
def test_external_metric_input_rejects_matrix_substitution(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _input(tmp_path, **kwargs)


def test_complete_external_metric_receipt_cannot_hide_contributions(
    tmp_path: Path,
) -> None:
    inputs = _input(tmp_path)
    receipt = _adapter().compute(inputs.metric, inputs)
    payload = receipt.model_dump(mode="json")
    payload["groups"] = []
    payload["contributing_cell_ids"] = []
    payload["contribution_digest"] = (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )

    with pytest.raises(ValidationError, match="requires contributions"):
        ExternalMetricComputationReceipt.model_validate(payload)


def test_metric_source_adapter_rejects_weights_it_does_not_reduce(
    tmp_path: Path,
) -> None:
    inputs = _input(tmp_path, weight=2.0)

    with pytest.raises(ValueError, match="weight=1"):
        _adapter().compute(inputs.metric, inputs)
