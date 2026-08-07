"""Standalone report byte and lineage verification tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from MagentaBench.schemas import (
    ArtifactRef,
    ObservationReport,
    RecordIndex,
    ReportVerificationError,
    RunPurpose,
    VerifiedObservationReport,
    verify_observation_report,
)


def compact_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def ref(path: Path) -> ArtifactRef:
    content = path.read_bytes()
    return ArtifactRef(
        path=str(path.resolve()),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def write_verified_observation(root: Path) -> Path:
    root.mkdir()
    aggregate_path = root / "aggregate.json"
    index_path = root / "record_index.json"
    report_path = root / "observation_report.json"
    index = RecordIndex(
        format="bmp-record-index-v1",
        experiment_id="verified-observation",
        manifest_refs=(),
        aggregate_path=str(aggregate_path.resolve()),
    )
    index_path.write_bytes(compact_bytes(index.model_dump(mode="json")))
    empty_experiment_digest = hashlib.sha256(compact_bytes([])).hexdigest()
    report = ObservationReport(
        purpose=RunPurpose.exploratory,
        experiment_id="verified-observation",
        manifest_digest=empty_experiment_digest,
        record_index_ref=ref(index_path),
    )
    report_bytes = compact_bytes(report.model_dump(mode="json"))
    report_path.write_bytes(report_bytes)
    aggregate_path.write_bytes(
        compact_bytes(
            {
                "experiment_id": report.experiment_id,
                "experiment_digest": report.manifest_digest,
                "run_count": 0,
                "run_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            }
        )
    )
    return report_path


def test_verified_observation_report_is_a_distinct_verified_type(tmp_path: Path) -> None:
    report_path = write_verified_observation(tmp_path / "record")
    verified = verify_observation_report(report_path)
    assert isinstance(verified, VerifiedObservationReport)
    assert verified.report.experiment_id == "verified-observation"
    assert verified.record_index.manifest_refs == ()


def test_report_verifier_aggregates_integrity_mismatches(tmp_path: Path) -> None:
    report_path = write_verified_observation(tmp_path / "record")
    aggregate_path = report_path.parent / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["run_report_sha256"] = "0" * 64
    aggregate["experiment_digest"] = "1" * 64
    aggregate_path.write_bytes(compact_bytes(aggregate))
    with pytest.raises(ReportVerificationError) as captured:
        verify_observation_report(report_path)
    assert len(captured.value.mismatches) == 2
    assert any("run_report_sha256 mismatch" in item for item in captured.value.mismatches)
    assert any("experiment_digest" in item for item in captured.value.mismatches)


def test_report_verifier_rejects_missing_record_index(tmp_path: Path) -> None:
    report = ObservationReport(
        purpose=RunPurpose.exploratory,
        experiment_id="unindexed",
        manifest_digest="a" * 64,
    )
    path = tmp_path / "observation_report.json"
    path.write_bytes(compact_bytes(report.model_dump(mode="json")))
    with pytest.raises(ReportVerificationError, match="record_index_ref is missing"):
        verify_observation_report(path)
