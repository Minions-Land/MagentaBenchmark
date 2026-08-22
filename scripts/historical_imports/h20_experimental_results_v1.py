#!/usr/bin/env python3
"""Create the approved H20 sanitized snapshot and historical import records."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter

from MagentaBench.collab.experimental_results import (
    ExperimentalResultsError,
    H20ExperimentalResultsSnapshot,
    SnapshotMetricContract,
    SnapshotObservation,
    SnapshotRun,
    build_snapshot,
    canonical_json_bytes,
    load_snapshot,
)
from MagentaBench.collab.import_models import (
    HistoricalRecord,
    HistoricalRun,
    HistoricalSource,
    HistoricalUnitResult,
    compute_record_id,
    source_snapshot_identity,
)

SOURCE_ID = "h20-experimental-results-20260822"
SOURCE_PATH = "imports/h20-experimental-results-20260822/source_snapshot.json"
NORMALIZER_ID = "h20-experimental-results-20260822.v1"
PUBLICATION_DECISION_SHA256 = (
    "9696c1b1a8da6a34b9288dd8b129e60fef70a8f143b54b125c210657a38fd145"
)
_RECORD_ADAPTER: TypeAdapter[HistoricalRecord] = TypeAdapter(HistoricalRecord)


class ProjectionError(ValueError):
    """The sanitized snapshot cannot be projected without guessing."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_blob_sha1(value: bytes) -> str:
    header = f"blob {len(value)}\0".encode("ascii")
    return hashlib.sha1(header + value).hexdigest()  # noqa: S324 - Git object ID


def _pretty_json(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)


def _prepare_output_root(path: Path) -> Path:
    if path.exists():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise ProjectionError("output root must be a new or empty directory")
    else:
        path.mkdir(parents=True)
    return path


def _normalizer_sha256() -> str:
    return _sha256(Path(__file__).read_bytes())


def _load_aggregate(project_root: Path, record_id: str) -> HistoricalRun:
    matches = sorted((project_root / "imports").glob(f"*/records/{record_id}.json"))
    if len(matches) != 1:
        raise ProjectionError(f"aggregate record {record_id} is not unique")
    record = _RECORD_ADAPTER.validate_json(matches[0].read_bytes(), strict=True)
    if not isinstance(record, HistoricalRun) or record.record_id != record_id:
        raise ProjectionError(f"aggregate record {record_id} is not an evaluated run")
    return record


def _direction(value: str) -> Literal["higher-is-better", "lower-is-better", "neutral"]:
    mapping = {
        "maximize": "higher-is-better",
        "minimize": "lower-is-better",
        "descriptive": "neutral",
    }
    try:
        return mapping[value]  # type: ignore[return-value]
    except KeyError as error:
        raise ProjectionError(f"unsupported direction {value}") from error


def _derived_metric_definition(
    benchmark_id: str, contract: SnapshotMetricContract
) -> str:
    payload = {
        "benchmark_id": benchmark_id,
        "format": "magentabench-h20-derived-metric-definition-v1",
        "metric": {
            "aggregation": contract.aggregation,
            "denominator": contract.denominator,
            "direction": contract.direction,
            "metric_id": contract.metric_id,
            "unit": contract.unit,
            "value_statuses": list(contract.value_statuses),
        },
    }
    return _sha256(canonical_json_bytes(payload))


def _metric_identity(
    run: SnapshotRun,
    contract: SnapshotMetricContract,
    aggregate: HistoricalRun | None,
) -> tuple[str, str, str]:
    if aggregate is not None:
        match = next(
            (
                metric
                for metric in aggregate.metrics
                if metric.metric_id == contract.metric_id
            ),
            None,
        )
        if match is not None:
            return match.definition_sha256, match.unit, match.direction
    return (
        _derived_metric_definition(run.benchmark_id, contract),
        contract.unit,
        _direction(contract.direction),
    )


def _execution_from_aggregate(
    run: SnapshotRun, aggregate: HistoricalRun
) -> dict[str, Any]:
    execution = deepcopy(aggregate.experiment.execution.model_dump(mode="json"))
    execution.update(
        {
            "case_count": run.dataset.planned_count,
            "configuration_id": "h20-task-matrix-projector-v1",
            "configuration_profiles": list(run.configuration.profiles),
            "configuration_sha256": run.configuration.digest,
        }
    )
    return execution


def _swe_experiment(run: SnapshotRun) -> dict[str, Any]:
    protocol_digest = run.protocol.digest
    if protocol_digest is None:
        raise ProjectionError(f"{run.run_id}: SWE protocol digest is missing")
    model_id = run.method.model
    if model_id is None:
        raise ProjectionError(f"{run.run_id}: SWE model identity is missing")
    return {
        "benchmark": {
            "id": "swebench-verified",
            "name": "SWE-bench Verified",
            "version": "historical-v5-five-case",
        },
        "comparability": {
            "case_set_sha256": run.dataset.digest,
            "comparison_group": "swebench-verified-v5-five-case",
            "evaluator_sha256": run.evaluator.digest,
            "protocol_sha256": protocol_digest,
            "status": "exact",
        },
        "dataset": {
            "commit_sha": (
                run.dataset.revision if len(run.dataset.revision) == 40 else None
            ),
            "content_sha256": run.dataset.digest,
            "id": run.dataset.id,
            "name": "SWE-bench Verified fixed five-case subset",
            "split": run.dataset.split,
            "version": run.dataset.revision,
        },
        "evaluator": {
            "id": run.evaluator.id,
            "independent": True,
            "kind": "deterministic",
            "name": "SWE-bench official harness",
            "version": run.evaluator.version,
        },
        "execution": {
            "backend_id": None,
            "budget": {"max_cases": run.dataset.planned_count},
            "case_count": run.dataset.planned_count,
            "configuration_id": "swebench-verified-v5-historical",
            "configuration_profiles": list(run.configuration.profiles),
            "configuration_sha256": run.configuration.digest,
            "factors": [],
            "hardware": {
                "accelerator": None,
                "accelerator_count": None,
                "architecture": "unknown",
                "cpu_count": None,
                "memory_bytes": None,
            },
            "image_sha256": None,
            "isolation": "host",
            "mode": "local-process",
            "network_policy": "unknown",
            "order_policy": "fixed",
            "repetitions_per_case": 1,
            "seeds": [],
        },
        "experiment_id": "swebench-verified-v5-five-case-20260716",
        "harness": {
            "configuration_sha256": run.configuration.digest,
            "id": "swebench-official-harness",
            "name": "SWE-bench official harness",
            "protocol_id": run.protocol.id,
            "version": run.evaluator.version,
        },
        "limitations": [
            "five-case-subset",
            "historical-not-bmp-verified",
            "runtime-identity-unbound",
        ],
        "method": {
            "id": run.method.id,
            "name": run.method.id,
            "subject_id": run.method.id,
            "version": run.method.version,
        },
        "model": {
            "id": model_id,
            "name": model_id,
            "revision": None,
            "version": None,
        },
        "provider": None,
        "purpose": "evaluation",
    }


def _experiment(run: SnapshotRun, aggregate: HistoricalRun | None) -> dict[str, Any]:
    if aggregate is None:
        return _swe_experiment(run)
    experiment = deepcopy(aggregate.experiment.model_dump(mode="json"))
    experiment["execution"] = _execution_from_aggregate(run, aggregate)
    limitations = set(experiment.get("limitations", []))
    limitations.discard("aggregate-only")
    limitations.add("derived-task-level-owner")
    experiment["limitations"] = sorted(limitations)
    return experiment


def _metric_value(
    contract: SnapshotMetricContract, rows: list[SnapshotObservation]
) -> float:
    values = [
        float(row.value)
        for row in rows
        if row.status in contract.value_statuses and row.value is not None
    ]
    if not values:
        raise ProjectionError(
            f"{contract.metric_id}: owner metric has no numeric values"
        )
    if contract.aggregation in {"mean", "rate"}:
        denominator = {
            "planned_units": len(rows),
            "observed_units": len(rows),
            "numeric_units": len(values),
        }.get(contract.denominator)
        if denominator is None or denominator <= 0:
            raise ProjectionError(
                f"{contract.metric_id}: aggregate denominator is invalid"
            )
        return sum(values) / denominator
    if contract.aggregation == "sum":
        return sum(values)
    if contract.aggregation == "minimum":
        return min(values)
    if contract.aggregation == "maximum":
        return max(values)
    if contract.aggregation == "median":
        return float(statistics.median(values))
    raise ProjectionError(f"{contract.metric_id}: unsupported aggregation")


def _owner_metric(
    run: SnapshotRun,
    contract: SnapshotMetricContract,
    aggregate: HistoricalRun | None,
) -> dict[str, Any]:
    rows = [row for row in run.observations if row.metric_id == contract.metric_id]
    if len(rows) != run.dataset.planned_count:
        raise ProjectionError(f"{run.run_id}/{contract.metric_id}: denominator drift")
    observed = sum(
        row.status in {"success", "verified_fail"} and row.value is not None
        for row in rows
    )
    missing = sum(row.status in {"missing", "no_output"} for row in rows)
    invalid = len(rows) - observed - missing
    definition, unit, direction = _metric_identity(run, contract, aggregate)
    return {
        "aggregation": contract.aggregation,
        "definition_sha256": definition,
        "denominator": {
            "excluded_count": 0,
            "observed_count": observed,
            "planned_count": run.dataset.planned_count,
            "unit": "cases",
        },
        "direction": direction,
        "invalid_count": invalid,
        "metric_id": contract.metric_id,
        "missing_count": missing,
        "state": "observed",
        "uncertainty": None,
        "unit": unit,
        "value": _metric_value(contract, rows),
        "zero_filled_count": 0,
    }


def _provenance(
    *, snapshot_sha256: str, snapshot_size: int, source_blob: str
) -> list[dict[str, Any]]:
    return [
        {
            "content_sha256": snapshot_sha256,
            "git_blob_oid": {"algorithm": "sha1", "digest": source_blob},
            "path": SOURCE_PATH,
            "role": "result",
            "size_bytes": snapshot_size,
        }
    ]


def _record(payload: dict[str, Any]) -> HistoricalRecord:
    payload["record_id"] = compute_record_id(payload)
    return _RECORD_ADAPTER.validate_json(canonical_json_bytes(payload), strict=True)


def _owner_record(
    run: SnapshotRun,
    *,
    aggregate: HistoricalRun | None,
    source_snapshot_sha256: str,
    provenance: list[dict[str, Any]],
) -> HistoricalRun:
    identity = {
        "experiment": _experiment(run, aggregate)["experiment_id"],
        "run": run.run_id,
        "source": SOURCE_ID,
    }
    payload = {
        "claim_eligible": False,
        "evidence_tier": "legacy-evaluated",
        "experiment": _experiment(run, aggregate),
        "format": "magentabench-historical-record-v1",
        "kind": "run",
        "logical_key": f"h20-run-{_sha256(canonical_json_bytes(identity))}",
        "metrics": [
            _owner_metric(run, contract, aggregate) for contract in run.metrics
        ],
        "parent_run_id": None,
        "provenance": provenance,
        "run_id": run.run_id,
        "source_id": SOURCE_ID,
        "source_snapshot_sha256": source_snapshot_sha256,
        "supersedes": [],
        "terminal_state": "completed",
    }
    record = _record(payload)
    if not isinstance(record, HistoricalRun):
        raise ProjectionError("owner projection did not produce a historical run")
    return record


def _unit_metric(
    run: SnapshotRun,
    observation: SnapshotObservation,
    contract: SnapshotMetricContract,
    aggregate: HistoricalRun | None,
) -> dict[str, Any]:
    definition, unit, direction = _metric_identity(run, contract, aggregate)
    if observation.status in {"success", "verified_fail"}:
        state = "observed"
        value = observation.value
        observed, missing, invalid = 1, 0, 0
        if value is None:
            raise ProjectionError("observed unit result has no numeric value")
    elif observation.status in {"missing", "no_output"}:
        state = "missing"
        value = None
        observed, missing, invalid = 0, 1, 0
    else:
        state = "invalid"
        value = None
        observed, missing, invalid = 0, 0, 1
    return {
        "aggregation": "none",
        "definition_sha256": definition,
        "denominator": {
            "excluded_count": 0,
            "observed_count": observed,
            "planned_count": 1,
            "unit": "cases",
        },
        "direction": direction,
        "invalid_count": invalid,
        "metric_id": observation.metric_id,
        "missing_count": missing,
        "state": state,
        "uncertainty": None,
        "unit": unit,
        "value": value,
        "zero_filled_count": 0,
    }


def _unit_record(
    run: SnapshotRun,
    observation: SnapshotObservation,
    *,
    owner: HistoricalRun,
    aggregate: HistoricalRun | None,
    source_snapshot_sha256: str,
    provenance: list[dict[str, Any]],
) -> HistoricalUnitResult:
    contract = next(
        (item for item in run.metrics if item.metric_id == observation.metric_id),
        None,
    )
    if contract is None:
        raise ProjectionError("unit result has no metric contract")
    natural_identity = {
        "attempt": observation.attempt_id,
        "experiment": owner.experiment.experiment_id,
        "metric": observation.metric_id,
        "run": run.run_id,
        "source": SOURCE_ID,
        "unit": observation.unit_id,
    }
    payload = {
        "aggregate_reconciliation_status": (
            contract.aggregate_reconciliation_status if aggregate is not None else None
        ),
        "aggregate_run_record_id": (
            aggregate.record_id if aggregate is not None else None
        ),
        "attempt_id": observation.attempt_id,
        "claim_eligible": False,
        "code_commit": run.method.code_commit,
        "evidence_tier": "legacy-evaluated",
        "experiment": owner.experiment.model_dump(mode="json"),
        "format": "magentabench-historical-record-v1",
        "kind": "unit-result",
        "logical_key": f"h20-unit-{_sha256(canonical_json_bytes(natural_identity))}",
        "metric": _unit_metric(run, observation, contract, aggregate),
        "provenance": provenance,
        "result_reason": observation.result_reason,
        "result_status": observation.status,
        "source_evidence_class": run.source_evidence_class,
        "source_id": SOURCE_ID,
        "source_run_id": run.run_id,
        "source_run_record_id": owner.record_id,
        "source_snapshot_sha256": source_snapshot_sha256,
        "supersedes": [],
        "terminal_state": "completed",
        "unit_id": observation.unit_id,
        "unit_kind": observation.unit_kind,
        "verification_status": "unverified",
    }
    record = _record(payload)
    if not isinstance(record, HistoricalUnitResult):
        raise ProjectionError("unit projection did not produce a unit result")
    return record


def project_snapshot(
    snapshot: H20ExperimentalResultsSnapshot,
    *,
    snapshot_bytes: bytes,
    output_root: Path,
    project_root: Path,
    source_commit: str,
    source_tree: str,
    source_blob: str,
) -> dict[str, Any]:
    """Project one immutable snapshot into canonical historical records."""

    if any(
        len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
        for value in (source_commit, source_tree, source_blob)
    ):
        raise ProjectionError("source commit, tree, and blob must be full SHA-1 values")
    if _git_blob_sha1(snapshot_bytes) != source_blob:
        raise ProjectionError(
            "source blob does not identify the supplied snapshot bytes"
        )
    output_root = _prepare_output_root(output_root)
    normalizer_sha256 = _normalizer_sha256()
    source = HistoricalSource.model_validate(
        {
            "commit_sha": source_commit,
            "format": "magentabench-historical-source-v1",
            "license_id": None,
            "license_status": "not-detected",
            "normalizer_id": NORMALIZER_ID,
            "normalizer_sha256": normalizer_sha256,
            "publication_approval": {
                "approval_id": "github-issue-159",
                "approved_at": date(2026, 8, 22),
                "approved_by": "PoorOtterBob",
                "decision_ref": "Minions-Land/MagentaBenchmark#159",
                "decision_sha256": PUBLICATION_DECISION_SHA256,
                "destination_repository": "Minions-Land/MagentaBenchmark",
                "scope": "typed-results-only",
            },
            "ref_hint": "results/159-h20-experimental-results-import-v1",
            "repository": "Minions-Land/MagentaBenchmark",
            "root_tree": {"algorithm": "sha1", "digest": source_tree},
            "source_id": SOURCE_ID,
            "visibility": "private",
        },
        strict=True,
    )
    snapshot_identity = source_snapshot_identity(source)
    snapshot_sha256 = _sha256(snapshot_bytes)
    provenance = _provenance(
        snapshot_sha256=snapshot_sha256,
        snapshot_size=len(snapshot_bytes),
        source_blob=source_blob,
    )

    aggregates: dict[str, HistoricalRun | None] = {}
    owners: dict[str, HistoricalRun] = {}
    records: list[HistoricalRecord] = []
    for run in snapshot.runs:
        aggregate = (
            _load_aggregate(project_root, run.aggregate_record_id)
            if run.aggregate_record_id is not None
            else None
        )
        aggregates[run.run_id] = aggregate
        owner = _owner_record(
            run,
            aggregate=aggregate,
            source_snapshot_sha256=snapshot_identity,
            provenance=provenance,
        )
        owners[run.run_id] = owner
        records.append(owner)
    for run in snapshot.runs:
        for observation in run.observations:
            records.append(
                _unit_record(
                    run,
                    observation,
                    owner=owners[run.run_id],
                    aggregate=aggregates[run.run_id],
                    source_snapshot_sha256=snapshot_identity,
                    provenance=provenance,
                )
            )
    records.sort(key=lambda item: item.record_id)
    if len(records) != 2_390 or len({item.record_id for item in records}) != 2_390:
        raise ProjectionError("projected record count or identity set is invalid")
    if any(item.claim_eligible for item in records):
        raise ProjectionError("historical projection cannot produce claim eligibility")

    _write_new(output_root / "source.json", _pretty_json(source))
    for record in records:
        _write_new(
            output_root / "records" / f"{record.record_id}.json", _pretty_json(record)
        )

    status_counts: dict[str, int] = {}
    benchmark_counts: dict[str, dict[str, int]] = {}
    for run in snapshot.runs:
        current = benchmark_counts.setdefault(
            run.benchmark_id, {"owners": 0, "units": 0}
        )
        current["owners"] += 1
        current["units"] += len(run.observations)
        for observation in run.observations:
            status_counts[observation.status] = (
                status_counts.get(observation.status, 0) + 1
            )
    return {
        "benchmark_counts": dict(sorted(benchmark_counts.items())),
        "claim_eligible": False,
        "format": "magentabench-h20-experimental-results-projection-summary-v1",
        "normalizer_sha256": normalizer_sha256,
        "owner_count": len(snapshot.runs),
        "record_count": len(records),
        "source_blob": source_blob,
        "source_commit": source_commit,
        "source_snapshot_sha256": snapshot_identity,
        "source_tree": source_tree,
        "status_counts": dict(sorted(status_counts.items())),
        "unit_count": sum(len(run.observations) for run in snapshot.runs),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="create the sanitized snapshot")
    snapshot.add_argument("--source-root", required=True, type=Path)
    snapshot.add_argument("--output", required=True, type=Path)
    project = subparsers.add_parser("project", help="create canonical import records")
    project.add_argument("--snapshot", required=True, type=Path)
    project.add_argument("--output-root", required=True, type=Path)
    project.add_argument("--source-commit", required=True)
    project.add_argument("--source-tree", required=True)
    project.add_argument("--source-blob", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    try:
        if args.command == "snapshot":
            if args.output.exists():
                raise ProjectionError("snapshot output must not already exist")
            snapshot = build_snapshot(args.source_root, project_root=project_root)
            data = canonical_json_bytes(snapshot, newline=True)
            _write_new(args.output, data)
            summary = {
                "format": snapshot.format,
                "owner_count": snapshot.owner_count,
                "output_sha256": _sha256(data),
                "output_size_bytes": len(data),
                "unit_count": snapshot.unit_count,
            }
        else:
            snapshot_bytes = args.snapshot.read_bytes()
            snapshot = load_snapshot(args.snapshot)
            summary = project_snapshot(
                snapshot,
                snapshot_bytes=snapshot_bytes,
                output_root=args.output_root,
                project_root=project_root,
                source_commit=args.source_commit,
                source_tree=args.source_tree,
                source_blob=args.source_blob,
            )
    except (ExperimentalResultsError, ProjectionError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "ok": False}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
