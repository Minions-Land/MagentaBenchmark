from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from MagentaBench.collab.import_models import (
    HistoricalRecord,
    HistoricalRun,
    HistoricalUnitResult,
    canonical_json_bytes,
    compute_record_id,
    logical_key_digest,
)
from MagentaBench.collab.imports import (
    HistoricalImportFinding,
    LoadedHistoricalRecord,
    _validate_record_references,
    load_historical_imports,
)

ROOT = Path(__file__).parents[1]
_RECORD_ADAPTER = TypeAdapter(HistoricalRecord)


def _records_and_run() -> tuple[list[LoadedHistoricalRecord], HistoricalRun]:
    snapshot = load_historical_imports(ROOT)
    run = next(
        item.record
        for item in snapshot.records
        if isinstance(item.record, HistoricalRun)
        and item.record.evidence_tier == "legacy-evaluated"
        and item.record.metrics
        and item.record.experiment.model is not None
    )
    return list(snapshot.records), run


def _loaded_run(
    run: HistoricalRun,
    *,
    run_id: str | None = None,
    experiment_update: dict[str, object] | None = None,
    metric_definition_sha256: str | None = None,
    supersedes: tuple[str, ...] = (),
    path_suffix: str = "aggregate",
) -> LoadedHistoricalRecord:
    experiment = run.experiment.model_copy(update=experiment_update or {})
    metrics = tuple(
        metric.model_copy(
            update=(
                {"definition_sha256": metric_definition_sha256}
                if metric_definition_sha256 is not None and index == 0
                else {}
            )
        )
        for index, metric in enumerate(run.metrics)
    )
    payload = run.model_dump(mode="json")
    payload.update(
        {
            "experiment": experiment.model_dump(mode="json"),
            "metrics": [metric.model_dump(mode="json") for metric in metrics],
            "record_id": "0" * 64,
            "run_id": run.run_id if run_id is None else run_id,
            "supersedes": list(supersedes),
        }
    )
    payload["record_id"] = compute_record_id(payload)
    record = _RECORD_ADAPTER.validate_json(canonical_json_bytes(payload), strict=True)
    assert isinstance(record, HistoricalRun)
    return LoadedHistoricalRecord(
        record=record,
        path=f"imports/test/records/{path_suffix}.json",
        logical_key_sha256=logical_key_digest(record.kind, record.logical_key),
    )


def _unit(
    run: HistoricalRun,
    *,
    source_run_id: str | None = None,
    source_run_record_id: str | None = None,
    aggregate_run_record_id: str | None = None,
    aggregate_reconciliation_status: str = "matched",
    experiment_update: dict[str, object] | None = None,
    metric_definition_sha256: str | None = None,
    unit_id: str = "case-001",
) -> LoadedHistoricalRecord:
    experiment = run.experiment.model_copy(update=experiment_update or {})
    source_metric = run.metrics[0]
    metric = source_metric.model_copy(
        update={
            "aggregation": "none",
            "denominator": source_metric.denominator.model_copy(
                update={
                    "excluded_count": 0,
                    "observed_count": 1,
                    "planned_count": 1,
                }
            ),
            "invalid_count": 0,
            "missing_count": 0,
            "state": "observed",
            "uncertainty": None,
            "value": 1.0,
            "zero_filled_count": 0,
            **(
                {"definition_sha256": metric_definition_sha256}
                if metric_definition_sha256 is not None
                else {}
            ),
        }
    )
    aggregate_id = (
        run.record_id if aggregate_run_record_id is None else aggregate_run_record_id
    )
    payload = {
        "aggregate_reconciliation_status": aggregate_reconciliation_status,
        "aggregate_run_record_id": aggregate_id,
        "attempt_id": "attempt-001",
        "claim_eligible": False,
        "code_commit": None,
        "evidence_tier": "legacy-evaluated",
        "experiment": experiment.model_dump(mode="json"),
        "format": "magentabench-historical-record-v1",
        "kind": "unit-result",
        "logical_key": f"unit-reference-test-{unit_id}",
        "metric": metric.model_dump(mode="json"),
        "provenance": [item.model_dump(mode="json") for item in run.provenance],
        "record_id": "0" * 64,
        "result_reason": "official-evaluator-success",
        "result_status": "success",
        "source_evidence_class": "legacy-evaluated",
        "source_id": run.source_id,
        "source_run_id": source_run_id or run.run_id,
        "source_run_record_id": source_run_record_id or run.record_id,
        "source_snapshot_sha256": run.source_snapshot_sha256,
        "supersedes": [],
        "terminal_state": "completed",
        "unit_id": unit_id,
        "unit_kind": "case",
        "verification_status": "unverified",
    }
    payload["record_id"] = compute_record_id(payload)
    record = _RECORD_ADAPTER.validate_json(canonical_json_bytes(payload), strict=True)
    assert isinstance(record, HistoricalUnitResult)
    return LoadedHistoricalRecord(
        record=record,
        path="imports/test/records/unit.json",
        logical_key_sha256=logical_key_digest(record.kind, record.logical_key),
    )


def _codes(records: list[LoadedHistoricalRecord]) -> set[str]:
    findings: list[HistoricalImportFinding] = []
    _validate_record_references(records, findings)
    return {item.code for item in findings}


def test_unit_and_same_run_aggregate_references_are_valid() -> None:
    records, run = _records_and_run()

    assert _codes([*records, _unit(run)]) == set()


def test_unit_source_run_record_must_exist() -> None:
    records, run = _records_and_run()

    assert "unit-source-run-missing" in _codes(
        [*records, _unit(run, source_run_record_id="0" * 64)]
    )


def test_unit_source_run_identity_must_match() -> None:
    records, run = _records_and_run()

    assert "unit-source-run-mismatch" in _codes(
        [*records, _unit(run, source_run_id="different-run")]
    )


def test_unit_source_run_id_must_resolve_unambiguously() -> None:
    records, run = _records_and_run()
    replacement = _loaded_run(
        run,
        supersedes=(run.record_id,),
        path_suffix="replacement-run",
    )
    replacement_run = replacement.record
    assert isinstance(replacement_run, HistoricalRun)

    assert "unit-source-run-ambiguous" in _codes(
        [*records, replacement, _unit(replacement_run)]
    )


def test_unit_source_run_conditions_must_match() -> None:
    records, run = _records_and_run()
    dataset = run.experiment.dataset.model_copy(update={"split": "train"})

    assert "unit-source-run-condition-mismatch" in _codes(
        [*records, _unit(run, experiment_update={"dataset": dataset})]
    )


def test_unit_source_metric_definition_must_match() -> None:
    records, run = _records_and_run()

    assert "unit-source-run-metric-mismatch" in _codes(
        [*records, _unit(run, metric_definition_sha256="0" * 64)]
    )


def test_aggregate_reference_must_exist_and_match_the_cohort() -> None:
    records, run = _records_and_run()
    other = next(
        item.record
        for item in records
        if isinstance(item.record, HistoricalRun)
        and item.record.experiment.benchmark.id != run.experiment.benchmark.id
    )

    assert "unit-aggregate-run-missing" in _codes(
        [*records, _unit(run, aggregate_run_record_id="0" * 64)]
    )
    assert "unit-aggregate-run-incompatible" in _codes(
        [*records, _unit(run, aggregate_run_record_id=other.record_id)]
    )


def test_aggregate_cohort_binds_model_and_protocol_digest() -> None:
    records, run = _records_and_run()
    model = run.experiment.model
    assert model is not None
    wrong_model = model.model_copy(update={"id": "wrong-model"})
    model_aggregate = _loaded_run(
        run,
        run_id="wrong-model-aggregate",
        experiment_update={
            "experiment_id": "wrong-model-aggregate",
            "model": wrong_model,
        },
        path_suffix="wrong-model-aggregate",
    )
    protocol_aggregate = _loaded_run(
        run,
        run_id="wrong-protocol-aggregate",
        experiment_update={
            "experiment_id": "wrong-protocol-aggregate",
            "comparability": run.experiment.comparability.model_copy(
                update={"protocol_sha256": "0" * 64}
            ),
        },
        path_suffix="wrong-protocol-aggregate",
    )

    assert "unit-aggregate-run-incompatible" in _codes(
        [
            *records,
            model_aggregate,
            _unit(run, aggregate_run_record_id=model_aggregate.record.record_id),
        ]
    )
    assert "unit-aggregate-run-incompatible" in _codes(
        [
            *records,
            protocol_aggregate,
            _unit(run, aggregate_run_record_id=protocol_aggregate.record.record_id),
        ]
    )


def test_compared_aggregate_metric_definition_must_match() -> None:
    records, run = _records_and_run()
    aggregate = _loaded_run(
        run,
        run_id="wrong-metric-aggregate",
        experiment_update={"experiment_id": "wrong-metric-aggregate"},
        metric_definition_sha256="0" * 64,
        path_suffix="wrong-metric-aggregate",
    )

    assert "unit-aggregate-metric-mismatch" in _codes(
        [
            *records,
            aggregate,
            _unit(run, aggregate_run_record_id=aggregate.record.record_id),
        ]
    )


def test_aggregate_reconciliation_status_is_population_consistent() -> None:
    records, run = _records_and_run()

    assert "unit-aggregate-reconciliation-conflict" in _codes(
        [
            *records,
            _unit(run, unit_id="case-001"),
            _unit(
                run,
                unit_id="case-002",
                aggregate_reconciliation_status="mismatch",
            ),
        ]
    )
