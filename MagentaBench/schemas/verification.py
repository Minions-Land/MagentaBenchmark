"""Standalone byte and lineage verification for BMP run reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TypeVar

from pydantic import ValidationError

from .models import (
    ArtifactRef,
    ClaimReport,
    EvidenceBundle,
    ObservationReport,
    RecordIndex,
    ResolvedBmpManifest,
    RunReport,
    RunReportAdapter,
    ScheduleActivationReceipt,
)


class ReportVerificationError(ValueError):
    """All integrity mismatches found while verifying one report."""

    def __init__(self, mismatches: list[str]) -> None:
        self.mismatches = tuple(mismatches)
        super().__init__("report verification failed:\n- " + "\n- ".join(mismatches))


@dataclass(frozen=True)
class VerifiedClaimReport:
    report: ClaimReport
    report_path: Path
    record_index: RecordIndex
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedObservationReport:
    report: ObservationReport
    report_path: Path
    record_index: RecordIndex
    warnings: tuple[str, ...] = ()


VerifiedRunReport = VerifiedClaimReport | VerifiedObservationReport
ReportT = TypeVar("ReportT", ClaimReport, ObservationReport)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compact_json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _resolve_path(path: str, path_map: Mapping[str, str]) -> Path:
    original = Path(path)
    matches = [prefix for prefix in path_map if path.startswith(prefix)]
    if not matches:
        return original
    prefix = max(matches, key=len)
    suffix = path[len(prefix) :].lstrip("/")
    return Path(path_map[prefix]) / suffix


def _verify_ref(
    ref: ArtifactRef,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> tuple[Path, bytes | None]:
    path = _resolve_path(ref.path, path_map)
    try:
        content = path.read_bytes()
    except OSError as exc:
        mismatches.append(f"{label}: cannot read {path}: {exc}")
        return path, None
    observed_digest = _sha256(content)
    if observed_digest != ref.sha256:
        mismatches.append(
            f"{label}: sha256 mismatch at {path}: expected {ref.sha256}, observed {observed_digest}"
        )
    if len(content) != ref.size_bytes:
        mismatches.append(
            f"{label}: size mismatch at {path}: expected {ref.size_bytes}, observed {len(content)}"
        )
    return path, content


def _parse_json_model(model: type[ReportT], content: bytes, *, label: str, mismatches: list[str]) -> ReportT | None:
    try:
        return model.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        mismatches.append(f"{label}: invalid schema: {exc}")
        return None


def _verify_report(
    report_path: str | Path,
    *,
    expected_type: type[ReportT],
    path_map: Mapping[str, str] | None = None,
) -> tuple[ReportT, Path, RecordIndex]:
    relocation = {} if path_map is None else dict(path_map)
    path = Path(report_path).expanduser().resolve()
    mismatches: list[str] = []
    try:
        report_bytes = path.read_bytes()
    except OSError as exc:
        raise ReportVerificationError([f"report: cannot read {path}: {exc}"]) from exc
    report = _parse_json_model(expected_type, report_bytes, label="report", mismatches=mismatches)
    if report is None:
        raise ReportVerificationError(mismatches)
    if report.record_index_ref is None:
        mismatches.append("report: record_index_ref is missing; standalone provenance is incomplete")
        raise ReportVerificationError(mismatches)

    _, index_bytes = _verify_ref(
        report.record_index_ref,
        label="record index",
        path_map=relocation,
        mismatches=mismatches,
    )
    index = (
        None
        if index_bytes is None
        else _parse_json_model(RecordIndex, index_bytes, label="record index", mismatches=mismatches)
    )
    if index is None:
        raise ReportVerificationError(mismatches)
    if index.experiment_id != report.experiment_id:
        mismatches.append(
            f"record index: experiment_id mismatch: report={report.experiment_id}, index={index.experiment_id}"
        )

    manifest_digests: list[str] = []
    for position, ref in enumerate(index.manifest_refs):
        _, manifest_bytes = _verify_ref(
            ref,
            label=f"manifest[{position}]",
            path_map=relocation,
            mismatches=mismatches,
        )
        if manifest_bytes is None:
            continue
        manifest = _parse_json_model(
            ResolvedBmpManifest,
            manifest_bytes,
            label=f"manifest[{position}]",
            mismatches=mismatches,
        )
        if manifest is not None:
            manifest_digests.append(manifest.canonical_digest())
    observed_experiment_digest = _compact_json_digest(manifest_digests)
    if observed_experiment_digest != report.manifest_digest:
        mismatches.append(
            "report manifest_digest mismatch: "
            f"expected {report.manifest_digest}, observed {observed_experiment_digest}"
        )

    for position, lineage in enumerate(report.lineage):
        _, bundle_bytes = _verify_ref(
            lineage.evidence_bundle_ref,
            label=f"lineage[{position}].evidence_bundle",
            path_map=relocation,
            mismatches=mismatches,
        )
        if bundle_bytes is not None:
            bundle = _parse_json_model(
                EvidenceBundle,
                bundle_bytes,
                label=f"lineage[{position}].evidence_bundle",
                mismatches=mismatches,
            )
            if bundle is not None and bundle.run_id != lineage.run_id:
                mismatches.append(
                    f"lineage[{position}]: bundle run_id {bundle.run_id!r} != {lineage.run_id!r}"
                )
        _, schedule_bytes = _verify_ref(
            lineage.schedule_receipt_ref,
            label=f"lineage[{position}].schedule_receipt",
            path_map=relocation,
            mismatches=mismatches,
        )
        if schedule_bytes is not None:
            schedule = _parse_json_model(
                ScheduleActivationReceipt,
                schedule_bytes,
                label=f"lineage[{position}].schedule_receipt",
                mismatches=mismatches,
            )
            if schedule is not None and schedule.run_id != lineage.run_id:
                mismatches.append(
                    f"lineage[{position}]: schedule run_id {schedule.run_id!r} != {lineage.run_id!r}"
                )

    aggregate_path = _resolve_path(index.aggregate_path, relocation)
    try:
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        mismatches.append(f"aggregate: cannot load {aggregate_path}: {exc}")
    else:
        if not isinstance(aggregate, dict):
            mismatches.append("aggregate: root must be an object")
        else:
            expected_report_digest = aggregate.get("run_report_sha256")
            observed_report_digest = _sha256(report_bytes)
            if expected_report_digest != observed_report_digest:
                mismatches.append(
                    "aggregate run_report_sha256 mismatch: "
                    f"expected {expected_report_digest}, observed {observed_report_digest}"
                )
            if aggregate.get("experiment_digest") != report.manifest_digest:
                mismatches.append("aggregate experiment_digest does not match report manifest_digest")

    if mismatches:
        raise ReportVerificationError(mismatches)
    return report, path, index


def verify_claim_report(
    report_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedClaimReport:
    report, path, index = _verify_report(
        report_path,
        expected_type=ClaimReport,
        path_map=path_map,
    )
    return VerifiedClaimReport(
        report=report,
        report_path=path,
        record_index=index,
        warnings=(
            "provenance incomplete: claim has no PreregistrationReceipt or SelectionLineage",
        ),
    )


def verify_observation_report(
    report_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedObservationReport:
    report, path, index = _verify_report(
        report_path,
        expected_type=ObservationReport,
        path_map=path_map,
    )
    return VerifiedObservationReport(report=report, report_path=path, record_index=index)


def verify_run_report(
    report_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedRunReport:
    path = Path(report_path).expanduser().resolve()
    try:
        parsed: RunReport = RunReportAdapter.validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ReportVerificationError([f"report syntax: {exc}"]) from exc
    if isinstance(parsed, ClaimReport):
        return verify_claim_report(path, path_map=path_map)
    return verify_observation_report(path, path_map=path_map)


def _parse_path_maps(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("--map values must be OLD=NEW")
        old, new = value.split("=", 1)
        if not old or not new:
            raise argparse.ArgumentTypeError("--map values must be OLD=NEW")
        result[old] = new
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a BMP report and all indexed lineage")
    parser.add_argument("report", help="Path to claim_report.json or observation_report.json")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Relocate an absolute recorded path prefix",
    )
    args = parser.parse_args(argv)
    try:
        verified = verify_run_report(args.report, path_map=_parse_path_maps(args.map))
    except (ReportVerificationError, argparse.ArgumentTypeError) as exc:
        parser.exit(1, f"{exc}\n")
    for warning in verified.warnings:
        print(f"warning: {warning}")
    print(f"verified: {verified.report_path}")
    return 0


__all__ = [
    "ReportVerificationError",
    "VerifiedClaimReport",
    "VerifiedObservationReport",
    "VerifiedRunReport",
    "verify_claim_report",
    "verify_observation_report",
    "verify_run_report",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
