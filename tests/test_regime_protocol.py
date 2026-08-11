from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas import (
    ArtifactRef,
    ContinualAnalysisPlan,
    ExperimentCellCoordinates,
    ExperimentCellDisposition,
    ExperimentCellObservation,
    ExperimentRegimeSpec,
    MetricMatrixCell,
    PlannedMetricCell,
    ResolvedBmpManifest,
    analyze_continual_matrix,
    build_experiment_cell_ledger,
    build_experiment_cell_plan,
    build_metric_cell_matrix,
    load_experiment_regime_spec,
)


ROOT = Path(__file__).parents[1]
REPEATED_EXPERIMENT = (
    ROOT / "MagentaBench/conformance/experiments/repeated-sampling-smoke.toml"
)


def test_registered_regime_is_a_topologically_closed_stage_dag() -> None:
    regime = load_experiment_regime_spec(
        ROOT / "registries/regimes/repeated-sampling-fake-v1.toml"
    )
    assert regime.regime_kind.value == "repeated_sampling"
    assert regime.stages[0].protocol_id == "fake.repeated-sampling.v1"

    payload = regime.model_dump(mode="json")
    payload["stages"][0]["predecessors"] = ["unknown-stage"]
    with pytest.raises(ValidationError, match="unknown predecessors"):
        ExperimentRegimeSpec.model_validate(payload)


def test_active_stage_binds_the_exact_registered_protocol() -> None:
    manifest = Compiler(ROOT).compile(REPEATED_EXPERIMENT)[0].manifest
    payload = manifest.model_dump(mode="json")
    payload["execution"]["protocol"]["parallelism"] += 1
    with pytest.raises(
        ValidationError,
        match="active manifest artifacts differ from regime dependencies",
    ):
        ResolvedBmpManifest.model_validate(payload)


def test_regime_identity_excludes_dependency_declaration_locations() -> None:
    artifact = Compiler(ROOT).compile(REPEATED_EXPERIMENT)[0].manifest.regime
    assert artifact is not None
    relocated = artifact.model_copy(
        update={
            "declaration_ref": artifact.declaration_ref.model_copy(
                update={"path": "/relocated/regime.toml"}
            ),
            "dependencies": tuple(
                dependency.model_copy(
                    update={
                        "declaration_ref": dependency.declaration_ref.model_copy(
                            update={
                                "path": (
                                    "/relocated/registries/"
                                    f"{dependency.registry_kind}/{dependency.id}.toml"
                                )
                            }
                        )
                    }
                )
                for dependency in artifact.dependencies
            ),
        }
    )
    assert relocated.canonical_digest() == artifact.canonical_digest()


def test_cell_ledger_cannot_drop_a_planned_matrix_cell(tmp_path: Path) -> None:
    membership = tmp_path / "membership.json"
    membership.write_text("{}", encoding="utf-8")
    membership_ref = ArtifactRef(
        path=str(membership.resolve()), sha256="a" * 64, size_bytes=2
    )
    cells = tuple(
        PlannedMetricCell(
            cell_id=f"cell-{task}",
            coordinates=ExperimentCellCoordinates(
                stage_id="evaluate",
                checkpoint_id="final",
                task_id=task,
            ),
            metric_id="accuracy",
            metric_digest="b" * 64,
            membership_digest="a" * 64,
        )
        for task in ("task-1", "task-2")
    )
    plan = build_experiment_cell_plan(
        regime_id="continual-test",
        regime_digest="c" * 64,
        stage_manifest_digests={"evaluate": "d" * 64},
        membership_refs=(membership_ref,),
        cells=cells,
    )
    observations = tuple(
        ExperimentCellObservation(
            cell_id=cell.cell_id,
            disposition=ExperimentCellDisposition.missing,
            reason="planned slot was not launched",
        )
        for cell in cells
    )
    ledger = build_experiment_cell_ledger(plan, observations)
    assert ledger.missing_count == 2
    assert ledger.observation_digest == ledger.canonical_observation_digest()

    with pytest.raises(ValidationError, match="exactly follow planned cell order"):
        build_experiment_cell_ledger(plan, observations[:-1])


def _continual_matrix(*, invalidate_terminal_task: str | None = None):
    rows = ("baseline", "learn-1", "learn-2", "learn-3")
    columns = ("task-1", "task-2", "task-3")
    values = {
        "baseline": (0.10, 0.20, 0.30),
        "learn-1": (0.80, 0.25, 0.35),
        "learn-2": (0.70, 0.90, 0.40),
        "learn-3": (0.60, 0.80, 0.95),
    }
    cells = []
    for row in rows:
        for column, value in zip(columns, values[row], strict=True):
            invalid = row == "learn-3" and column == invalidate_terminal_task
            cells.append(
                MetricMatrixCell(
                    row_id=row,
                    column_id=column,
                    source_cell_id=f"cell-{row}-{column}",
                    disposition=(
                        ExperimentCellDisposition.invalid
                        if invalid
                        else ExperimentCellDisposition.observed
                    ),
                    value=None if invalid else value,
                )
            )
    return build_metric_cell_matrix(
        source_plan_digest="a" * 64,
        source_observation_digest="b" * 64,
        metric_id="accuracy",
        metric_digest="c" * 64,
        row_axis="checkpoint",
        column_axis="task",
        row_ids=rows,
        column_ids=columns,
        cells=tuple(cells),
    )


def test_continual_metrics_require_the_complete_checkpoint_task_matrix() -> None:
    plan = ContinualAnalysisPlan(
        task_ids=("task-1", "task-2", "task-3"),
        baseline_checkpoint_id="baseline",
        terminal_checkpoint_id="learn-3",
        learned_checkpoint_by_task={
            "task-1": "learn-1",
            "task-2": "learn-2",
            "task-3": "learn-3",
        },
    )
    average, backward, forgetting, forward = analyze_continual_matrix(
        _continual_matrix(), plan
    )
    assert average.value == pytest.approx((0.60 + 0.80 + 0.95) / 3)
    assert backward.value == pytest.approx(-0.15)
    assert forgetting.value == pytest.approx(0.15)
    assert forward.value == pytest.approx(0.075)

    invalid_average, invalid_backward, _, _ = analyze_continual_matrix(
        _continual_matrix(invalidate_terminal_task="task-1"), plan
    )
    assert invalid_average.state.value == "invalid"
    assert invalid_backward.state.value == "invalid"
