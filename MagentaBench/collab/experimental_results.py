"""Strict reader for the approved H20 historical-results catalog snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, TypeAlias, TypeVar, cast

from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

SnapshotFormat: TypeAlias = Literal["magentabench-h20-experimental-results-snapshot-v1"]
MetricDirection: TypeAlias = Literal["maximize", "minimize", "descriptive"]
MetricAggregation: TypeAlias = Literal[
    "mean", "rate", "sum", "minimum", "maximum", "median"
]
MetricDenominator: TypeAlias = Literal[
    "planned_units", "observed_units", "numeric_units", "none"
]
ObservationStatus: TypeAlias = Literal[
    "success", "verified_fail", "invalid_output", "no_output"
]
ResultReason: TypeAlias = Literal[
    "official-evaluator-success",
    "official-evaluator-verified-failure",
    "source-invalid-output",
    "agent-no-output",
]
EvidenceClass: TypeAlias = Literal[
    "derived-non-claim-view", "historical-official-harness-report"
]
ReconciliationStatus: TypeAlias = Literal["matched", "not-compared"]
_T = TypeVar("_T", bound=str)

SNAPSHOT_FORMAT: Final[SnapshotFormat] = (
    "magentabench-h20-experimental-results-snapshot-v1"
)
EXPECTED_CATALOG_SHA256 = (
    "6eaacb5c6b9437dfd00a7fdae1b64da888e4ff512ec8712739568a4cfa153b90"
)
EXPECTED_CATALOG_FILE_COUNT: Final[Literal[197]] = 197
EXPECTED_CATALOG_SIZE_BYTES: Final[Literal[4_355_698]] = 4_355_698
EXPECTED_FACT_MANIFEST_SHA256 = (
    "5f7e6b242c145e5c229ce1b0e1fe2283a5bdd9e3150f8f55dc96f3f74a11d7f3"
)
EXPECTED_FACT_FILE_COUNT: Final[Literal[145]] = 145
EXPECTED_FACT_SIZE_BYTES: Final[Literal[4_344_353]] = 4_344_353
EXPECTED_SCHEMA_MANIFEST_SHA256 = (
    "a60acac73359b4efa2f4879a4b4d933b03c1660f8f9a5573253f1d37744a0fe2"
)
EXPECTED_SCHEMA_FILE_COUNT: Final[Literal[4]] = 4
EXPECTED_SCHEMA_SIZE_BYTES: Final[Literal[22_430]] = 22_430
EXPECTED_COMBINED_MANIFEST_SHA256 = (
    "bfda24aaab90834c0418a1b56d9797fc94a4c6e4cee9201286c5dc01df6235b1"
)
EXPECTED_OWNER_COUNT: Final[Literal[30]] = 30
EXPECTED_UNIT_COUNT: Final[Literal[2_360]] = 2_360
EXPECTED_OWNER_IDENTITY_SHA256 = (
    "20b5afce513239de2dd97f1eeb8707069a8dac79f625860ca508e6d49de62ed0"
)
EXPECTED_UNIT_IDENTITY_SHA256 = (
    "45d3a4f96b599fc0d4da8551f82316a7df2cbeee39557ea6affc963d4e223b1c"
)

_BENCHMARKS = ("biomnibench-da", "cmtbench", "naturebench", "swebench-verified")
_SCHEMAS = {
    "benchmark.json": "schema/benchmark.schema.json",
    "observations.jsonl": "schema/observation.schema.json",
    "run.json": "schema/run.schema.json",
    "source.json": "schema/source.schema.json",
}
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,254}[A-Za-z0-9])?$")
_MAX_FILE_BYTES = 4 * 1024 * 1024
_METRIC_DIRECTIONS: tuple[MetricDirection, ...] = (
    "maximize",
    "minimize",
    "descriptive",
)
_METRIC_AGGREGATIONS: tuple[MetricAggregation, ...] = (
    "mean",
    "rate",
    "sum",
    "minimum",
    "maximum",
    "median",
)
_METRIC_DENOMINATORS: tuple[MetricDenominator, ...] = (
    "planned_units",
    "observed_units",
    "numeric_units",
    "none",
)
_OBSERVATION_STATUSES: tuple[ObservationStatus, ...] = (
    "success",
    "verified_fail",
    "invalid_output",
    "no_output",
)
_PUBLICATION_DECISION_SHA256 = (
    "9696c1b1a8da6a34b9288dd8b129e60fef70a8f143b54b125c210657a38fd145"
)
_EXPECTED_BENCHMARK_COUNTS = {
    "biomnibench-da": (10, 1_500),
    "cmtbench": (8, 800),
    "swebench-verified": (12, 60),
}
_EXPECTED_STATUS_COUNTS = {
    "invalid_output": 12,
    "no_output": 2,
    "success": 1_380,
    "verified_fail": 966,
}
_SWE_CODE_COMMIT = "174590db9b51b61ace9270dbf1f24d4364c6c640"


class ExperimentalResultsError(ValueError):
    """The H20 catalog or sanitized snapshot violated its fixed contract."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _safe_relative_path(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or value.startswith(("/", "~/"))
        or "\x00" in value
    ):
        raise ValueError("path must be normalized and relative")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("path must be normalized and relative")
    return value


def _safe_id(value: str) -> str:
    if _SAFE_ID_RE.fullmatch(value) is None:
        raise ValueError("value must be a normalized identifier")
    return value


def _sha256(value: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError("value must be lowercase SHA-256")
    return value


class SnapshotFile(_FrozenModel):
    path: str
    sha256: str
    size_bytes: StrictInt = Field(ge=0)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        return _sha256(value)


class SnapshotDataset(_FrozenModel):
    id: str
    revision: str
    split: str
    unit_kind: str
    planned_count: StrictInt = Field(ge=1)
    digest: str

    @field_validator("id", "unit_kind")
    @classmethod
    def identifiers_are_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("revision", "split")
    @classmethod
    def labels_are_safe(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > 128:
            raise ValueError("label must be normalized")
        return value

    @field_validator("digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        return _sha256(value)


class SnapshotMethod(_FrozenModel):
    id: str
    model: str | None = None
    version: str | None = None
    code_commit: str | None = None

    @field_validator("id")
    @classmethod
    def id_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("model", "version")
    @classmethod
    def labels_are_safe(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or value != value.strip() or len(value) > 256
        ):
            raise ValueError("label must be normalized")
        return value

    @field_validator("code_commit")
    @classmethod
    def commit_is_sha1(cls, value: str | None) -> str | None:
        if value is not None and _SHA1_RE.fullmatch(value) is None:
            raise ValueError("code_commit must be a full SHA-1")
        return value


class SnapshotEvaluator(_FrozenModel):
    id: str
    version: str
    digest: str

    @field_validator("id")
    @classmethod
    def id_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("version")
    @classmethod
    def version_is_safe(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > 128:
            raise ValueError("version must be normalized")
        return value

    @field_validator("digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        return _sha256(value)


class SnapshotProtocol(_FrozenModel):
    id: str
    version: str
    digest: str | None = None

    @field_validator("id")
    @classmethod
    def id_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("version")
    @classmethod
    def version_is_safe(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > 128:
            raise ValueError("version must be normalized")
        return value

    @field_validator("digest")
    @classmethod
    def digest_is_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value)


class SnapshotConfiguration(_FrozenModel):
    digest: str
    profiles: tuple[str, ...]

    @field_validator("digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("profiles")
    @classmethod
    def profiles_are_sorted_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or tuple(sorted(set(values))) != values:
            raise ValueError("profiles must be non-empty, sorted, and unique")
        for value in values:
            _safe_id(value)
        return values


class SnapshotMetricContract(_FrozenModel):
    metric_id: str
    unit: str
    direction: MetricDirection
    aggregation: MetricAggregation
    denominator: MetricDenominator
    value_statuses: tuple[str, ...]
    aggregate_reconciliation_status: ReconciliationStatus | None

    @field_validator("metric_id", "unit")
    @classmethod
    def identifiers_are_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("value_statuses")
    @classmethod
    def statuses_are_sorted_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or tuple(sorted(set(values))) != values:
            raise ValueError("value_statuses must be non-empty, sorted, and unique")
        for value in values:
            _safe_id(value)
        return values


class SnapshotObservation(_FrozenModel):
    unit_id: str
    unit_kind: str
    attempt_id: str
    metric_id: str
    status: ObservationStatus
    result_reason: ResultReason
    value: StrictInt | float | None
    source_digest: str

    @field_validator("unit_id")
    @classmethod
    def unit_is_safe(cls, value: str) -> str:
        if _UNIT_ID_RE.fullmatch(value) is None:
            raise ValueError("unit_id must be normalized")
        return value

    @field_validator("unit_kind", "attempt_id", "metric_id")
    @classmethod
    def identifiers_are_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("value")
    @classmethod
    def value_is_finite(cls, value: int | float | None) -> int | float | None:
        if value is not None and (
            isinstance(value, bool) or not math.isfinite(float(value))
        ):
            raise ValueError("value must be a finite number")
        return value

    @field_validator("source_digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        return _sha256(value)


class SnapshotRun(_FrozenModel):
    benchmark_id: str
    run_id: str
    status: Literal["completed"]
    claim_eligible: Literal[False]
    source_evidence_class: EvidenceClass
    dataset: SnapshotDataset
    method: SnapshotMethod
    evaluator: SnapshotEvaluator
    protocol: SnapshotProtocol
    configuration: SnapshotConfiguration
    metrics: tuple[SnapshotMetricContract, ...]
    aggregate_record_id: str | None
    run_file: SnapshotFile
    observations_file: SnapshotFile
    observations: tuple[SnapshotObservation, ...]

    @field_validator("benchmark_id", "run_id")
    @classmethod
    def identifiers_are_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("aggregate_record_id")
    @classmethod
    def aggregate_id_is_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value)


class SnapshotInventory(_FrozenModel):
    catalog_sha256: str
    catalog_file_count: Literal[197]
    catalog_size_bytes: Literal[4_355_698]
    fact_manifest_sha256: str
    fact_file_count: Literal[145]
    fact_size_bytes: Literal[4_344_353]
    schema_manifest_sha256: str
    schema_file_count: Literal[4]
    schema_size_bytes: Literal[22_430]
    combined_manifest_sha256: str
    facts: tuple[SnapshotFile, ...]
    schemas: tuple[SnapshotFile, ...]

    @field_validator(
        "catalog_sha256",
        "fact_manifest_sha256",
        "schema_manifest_sha256",
        "combined_manifest_sha256",
    )
    @classmethod
    def digests_are_sha256(cls, value: str) -> str:
        return _sha256(value)


class H20ExperimentalResultsSnapshot(_FrozenModel):
    format: Literal["magentabench-h20-experimental-results-snapshot-v1"]
    publication_decision: Literal["Minions-Land/MagentaBenchmark#159"]
    publication_decision_sha256: str
    inventory: SnapshotInventory
    owner_count: Literal[30]
    unit_count: Literal[2360]
    owner_identity_sha256: str
    unit_identity_sha256: str
    runs: tuple[SnapshotRun, ...]

    @field_validator(
        "publication_decision_sha256",
        "owner_identity_sha256",
        "unit_identity_sha256",
    )
    @classmethod
    def digests_are_sha256(cls, value: str) -> str:
        return _sha256(value)


def canonical_json_bytes(value: Any, *, newline: bool = False) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manifest_digest(files: Iterable[SnapshotFile]) -> str:
    rows = [item.model_dump(mode="json") for item in files]
    return _digest_bytes(canonical_json_bytes(rows, newline=True))


def _read_regular_file(path: Path, *, relative: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ExperimentalResultsError(
            f"{relative}: cannot open accepted file"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ExperimentalResultsError(
                f"{relative}: accepted input is not a unique regular file"
            )
        if before.st_size > _MAX_FILE_BYTES:
            raise ExperimentalResultsError(
                f"{relative}: accepted input exceeds size limit"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ExperimentalResultsError(
                    f"{relative}: input changed while reading"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ExperimentalResultsError(f"{relative}: input grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ExperimentalResultsError(f"{relative}: input changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExperimentalResultsError("JSON object contains a duplicate key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ExperimentalResultsError(f"JSON contains invalid numeric constant {value}")


def _load_json_bytes(data: bytes, *, relative: str) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ExperimentalResultsError(f"{relative}: invalid JSON") from error


def _load_jsonl_bytes(data: bytes, *, relative: str) -> list[Any]:
    rows: list[Any] = []
    for line_number, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            raise ExperimentalResultsError(f"{relative}: blank JSONL row {line_number}")
        rows.append(_load_json_bytes(line, relative=f"{relative}:{line_number}"))
    return rows


def _file_identity(root: Path, path: Path) -> tuple[SnapshotFile, bytes]:
    relative = path.relative_to(root).as_posix()
    data = _read_regular_file(path, relative=relative)
    return (
        SnapshotFile(path=relative, sha256=_digest_bytes(data), size_bytes=len(data)),
        data,
    )


def _catalog_files(root: Path) -> tuple[SnapshotFile, ...]:
    files: list[SnapshotFile] = []
    for benchmark_id in _BENCHMARKS:
        benchmark_dir = root / benchmark_id
        if not benchmark_dir.is_dir() or benchmark_dir.is_symlink():
            raise ExperimentalResultsError(
                f"{benchmark_id}: benchmark directory is unavailable"
            )
        for path in sorted(item for item in benchmark_dir.rglob("*") if item.is_file()):
            if path.name == "seal.json" or "generated" in path.parts:
                continue
            identity, _ = _file_identity(root, path)
            files.append(identity)
    return tuple(files)


def _typed_fact_files(root: Path) -> tuple[SnapshotFile, ...]:
    accepted_names = frozenset(_SCHEMAS)
    files: list[SnapshotFile] = []
    for benchmark_id in _BENCHMARKS:
        for path in sorted((root / benchmark_id).rglob("*")):
            if path.is_file() and path.name in accepted_names:
                identity, _ = _file_identity(root, path)
                files.append(identity)
    return tuple(sorted(files, key=lambda item: item.path))


def _schema_files(root: Path) -> tuple[SnapshotFile, ...]:
    files = [
        _file_identity(root, root / path)[0] for path in sorted(set(_SCHEMAS.values()))
    ]
    return tuple(sorted(files, key=lambda item: item.path))


def _validate_manifest(
    files: tuple[SnapshotFile, ...],
    *,
    expected_count: int,
    expected_size: int,
    expected_digest: str,
    label: str,
) -> None:
    size = sum(item.size_bytes for item in files)
    digest = _manifest_digest(files)
    if (len(files), size, digest) != (expected_count, expected_size, expected_digest):
        raise ExperimentalResultsError(
            f"{label}: accepted inventory differs from the approved snapshot"
        )


def _schema_validators(root: Path) -> dict[str, Any]:
    validators: dict[str, Any] = {}
    for basename, relative in _SCHEMAS.items():
        data = _read_regular_file(root / relative, relative=relative)
        # The approved observation schema has a legacy duplicate annotation key.
        # Its exact bytes are pinned above; source facts themselves remain strict.
        try:
            schema = json.loads(data.decode("utf-8"), parse_constant=_invalid_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ExperimentalResultsError(
                f"{relative}: invalid schema JSON"
            ) from error
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        validators[basename] = validator_class(schema)
    return validators


def _validate_document(validator: Any, document: Any, *, relative: str) -> None:
    error = next(iter(validator.iter_errors(document)), None)
    if error is not None:
        raise ExperimentalResultsError(
            f"{relative}: document does not match its schema"
        )


def _source_digest(value: Any, *, relative: str) -> str:
    if not isinstance(value, str):
        raise ExperimentalResultsError(f"{relative}: source_digest is missing")
    digest = value.removeprefix("sha256:")
    if _SHA256_RE.fullmatch(digest) is None:
        raise ExperimentalResultsError(f"{relative}: source_digest is invalid")
    return digest


def _required_mapping(value: Any, *, relative: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentalResultsError(f"{relative}: required object is missing")
    return value


def _required_string(value: Any, *, relative: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExperimentalResultsError(f"{relative}: required string is missing")
    return value


def _required_literal(value: Any, *, relative: str, allowed: tuple[_T, ...]) -> _T:
    result = _required_string(value, relative=relative)
    if result not in allowed:
        raise ExperimentalResultsError(f"{relative}: value is not allowed")
    return cast(_T, result)


def _reason_code(status: ObservationStatus) -> ResultReason:
    reasons: dict[ObservationStatus, ResultReason] = {
        "success": "official-evaluator-success",
        "verified_fail": "official-evaluator-verified-failure",
        "invalid_output": "source-invalid-output",
        "no_output": "agent-no-output",
    }
    return reasons[status]


def _selected_run(benchmark_id: str, run_id: str) -> bool:
    if benchmark_id in {"biomnibench-da", "cmtbench"}:
        return run_id.endswith("-task-matrix")
    return benchmark_id == "swebench-verified"


def _aggregate_records(project_root: Path) -> dict[str, str]:
    directories = (
        project_root / "imports" / "aosebench-biomnibench-da-def4dae7" / "records",
        project_root / "imports" / "minionsos2-cmtbench-150fa10" / "records",
    )
    records: dict[str, str] = {}
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            document = _load_json_bytes(path.read_bytes(), relative=path.name)
            if isinstance(document, dict) and document.get("kind") == "run":
                records[str(document["logical_key"])] = str(document["record_id"])
    return records


def _metric_reconciliation(
    benchmark_id: str, run_id: str, metric_id: str
) -> Literal["matched", "not-compared"] | None:
    if benchmark_id == "swebench-verified":
        return None
    if benchmark_id == "cmtbench":
        return "matched"
    if metric_id == "judge-success-rate" or (
        run_id == "biomnibench-da-xhigh-cellvoyager-task-matrix"
        and metric_id in {"score-mean", "score-median"}
    ):
        return "not-compared"
    return "matched"


def _run_fact(
    root: Path,
    benchmark_id: str,
    run_path: Path,
    validators: dict[str, Any],
    aggregate_records: dict[str, str],
) -> SnapshotRun | None:
    run_identity, run_bytes = _file_identity(root, run_path)
    run = _load_json_bytes(run_bytes, relative=run_identity.path)
    _validate_document(validators["run.json"], run, relative=run_identity.path)
    run_map = _required_mapping(run, relative=run_identity.path)
    run_id = _required_string(run_map.get("run_id"), relative=run_identity.path)
    if not _selected_run(benchmark_id, run_id):
        return None
    if run_map.get("benchmark_id") != benchmark_id:
        raise ExperimentalResultsError(
            f"{run_identity.path}: benchmark ownership mismatch"
        )
    verification = _required_mapping(
        run_map.get("verification"), relative=run_identity.path
    )
    if verification.get("claim_eligible") is not False:
        raise ExperimentalResultsError(
            f"{run_identity.path}: historical run is claim eligible"
        )
    if run_map.get("status") != "completed":
        raise ExperimentalResultsError(
            f"{run_identity.path}: selected run is not completed"
        )

    dataset = _required_mapping(run_map.get("dataset"), relative=run_identity.path)
    method = _required_mapping(run_map.get("method"), relative=run_identity.path)
    evaluator = _required_mapping(run_map.get("evaluator"), relative=run_identity.path)
    protocol = _required_mapping(run_map.get("protocol"), relative=run_identity.path)
    configuration = _required_mapping(
        run_map.get("configuration"), relative=run_identity.path
    )
    planned = dataset.get("case_count", dataset.get("task_count"))
    if not isinstance(planned, int) or isinstance(planned, bool) or planned <= 0:
        raise ExperimentalResultsError(
            f"{run_identity.path}: dataset denominator is invalid"
        )

    source = protocol.get("source")
    protocol_digest = source.get("digest") if isinstance(source, dict) else None
    if protocol_digest is not None:
        protocol_digest = _source_digest(protocol_digest, relative=run_identity.path)
    profiles = configuration.get("profiles")
    if not isinstance(profiles, list) or not all(
        isinstance(item, str) for item in profiles
    ):
        raise ExperimentalResultsError(
            f"{run_identity.path}: configuration profiles are invalid"
        )

    contracts: list[SnapshotMetricContract] = []
    metrics = run_map.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ExperimentalResultsError(
            f"{run_identity.path}: metric contracts are missing"
        )
    for metric in metrics:
        metric_map = _required_mapping(metric, relative=run_identity.path)
        metric_id = _required_string(
            metric_map.get("metric_id"), relative=run_identity.path
        )
        value_statuses = metric_map.get("value_statuses")
        if not isinstance(value_statuses, list) or not all(
            isinstance(item, str) for item in value_statuses
        ):
            raise ExperimentalResultsError(
                f"{run_identity.path}: value statuses are invalid"
            )
        contracts.append(
            SnapshotMetricContract(
                metric_id=metric_id,
                unit=_required_string(
                    metric_map.get("unit"), relative=run_identity.path
                ),
                direction=_required_literal(
                    metric_map.get("direction"),
                    relative=run_identity.path,
                    allowed=_METRIC_DIRECTIONS,
                ),
                aggregation=_required_literal(
                    metric_map.get("aggregation"),
                    relative=run_identity.path,
                    allowed=_METRIC_AGGREGATIONS,
                ),
                denominator=_required_literal(
                    metric_map.get("denominator", "numeric_units"),
                    relative=run_identity.path,
                    allowed=_METRIC_DENOMINATORS,
                ),
                value_statuses=tuple(sorted(value_statuses)),
                aggregate_reconciliation_status=_metric_reconciliation(
                    benchmark_id, run_id, metric_id
                ),
            )
        )

    observation_path = run_path.with_name("observations.jsonl")
    observation_identity, observation_bytes = _file_identity(root, observation_path)
    rows = _load_jsonl_bytes(observation_bytes, relative=observation_identity.path)
    observations: list[SnapshotObservation] = []
    contract_ids = {item.metric_id for item in contracts}
    for index, row in enumerate(rows, start=1):
        relative = f"{observation_identity.path}:{index}"
        _validate_document(validators["observations.jsonl"], row, relative=relative)
        row_map = _required_mapping(row, relative=relative)
        if (
            row_map.get("benchmark_id", benchmark_id) != benchmark_id
            or row_map.get("run_id", run_id) != run_id
        ):
            raise ExperimentalResultsError(
                f"{relative}: observation ownership mismatch"
            )
        if row_map.get("method_id") != method.get("method_id"):
            raise ExperimentalResultsError(f"{relative}: observation method mismatch")
        metric_id = _required_string(row_map.get("metric_id"), relative=relative)
        if metric_id not in contract_ids:
            raise ExperimentalResultsError(f"{relative}: undeclared observation metric")
        status = _required_literal(
            row_map.get("status"),
            relative=relative,
            allowed=_OBSERVATION_STATUSES,
        )
        value = row_map.get("value")
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ExperimentalResultsError(f"{relative}: observation value is invalid")
        observations.append(
            SnapshotObservation(
                unit_id=_required_string(row_map.get("unit_id"), relative=relative),
                unit_kind=_required_string(row_map.get("unit_kind"), relative=relative),
                attempt_id=_required_string(
                    row_map.get("attempt_id"), relative=relative
                ),
                metric_id=metric_id,
                status=status,
                result_reason=_reason_code(status),
                value=value,
                source_digest=_source_digest(
                    row_map.get("source_digest"), relative=relative
                ),
            )
        )

    base_logical_key = run_id.removesuffix("-task-matrix")
    aggregate_record_id = None
    if benchmark_id != "swebench-verified":
        aggregate_record_id = aggregate_records.get(base_logical_key)
        if aggregate_record_id is None:
            raise ExperimentalResultsError(
                f"{run_identity.path}: aggregate crosswalk is missing"
            )

    evidence_class: EvidenceClass = (
        "historical-official-harness-report"
        if benchmark_id == "swebench-verified"
        else "derived-non-claim-view"
    )
    code_commit = method.get("code_commit")
    if code_commit is not None and not isinstance(code_commit, str):
        raise ExperimentalResultsError(
            f"{run_identity.path}: method code commit is invalid"
        )
    return SnapshotRun(
        benchmark_id=benchmark_id,
        run_id=run_id,
        status="completed",
        claim_eligible=False,
        source_evidence_class=evidence_class,
        dataset=SnapshotDataset(
            id=_required_string(dataset.get("id"), relative=run_identity.path),
            revision=_required_string(
                dataset.get("revision"), relative=run_identity.path
            ),
            split=_required_string(dataset.get("split"), relative=run_identity.path),
            unit_kind=_required_string(
                dataset.get("unit_kind"), relative=run_identity.path
            ),
            planned_count=planned,
            digest=_source_digest(dataset.get("digest"), relative=run_identity.path),
        ),
        method=SnapshotMethod(
            id=_required_string(method.get("method_id"), relative=run_identity.path),
            model=method.get("model") if isinstance(method.get("model"), str) else None,
            version=method.get("version")
            if isinstance(method.get("version"), str)
            else None,
            code_commit=code_commit,
        ),
        evaluator=SnapshotEvaluator(
            id=_required_string(evaluator.get("id"), relative=run_identity.path),
            version=_required_string(
                evaluator.get("version"), relative=run_identity.path
            ),
            digest=_source_digest(evaluator.get("digest"), relative=run_identity.path),
        ),
        protocol=SnapshotProtocol(
            id=_required_string(protocol.get("id"), relative=run_identity.path),
            version=_required_string(
                protocol.get("version"), relative=run_identity.path
            ),
            digest=protocol_digest,
        ),
        configuration=SnapshotConfiguration(
            digest=_source_digest(
                configuration.get("digest"), relative=run_identity.path
            ),
            profiles=tuple(sorted(profiles)),
        ),
        metrics=tuple(sorted(contracts, key=lambda item: item.metric_id)),
        aggregate_record_id=aggregate_record_id,
        run_file=run_identity,
        observations_file=observation_identity,
        observations=tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.unit_id,
                    item.attempt_id,
                    item.metric_id,
                ),
            )
        ),
    )


def build_snapshot(root: Path, *, project_root: Path) -> H20ExperimentalResultsSnapshot:
    """Validate the fixed H20 catalog and return its sanitized typed facts."""

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ExperimentalResultsError("source root is not a directory")
    catalog = _catalog_files(root)
    _validate_manifest(
        catalog,
        expected_count=EXPECTED_CATALOG_FILE_COUNT,
        expected_size=EXPECTED_CATALOG_SIZE_BYTES,
        expected_digest=EXPECTED_CATALOG_SHA256,
        label="catalog",
    )
    facts = _typed_fact_files(root)
    _validate_manifest(
        facts,
        expected_count=EXPECTED_FACT_FILE_COUNT,
        expected_size=EXPECTED_FACT_SIZE_BYTES,
        expected_digest=EXPECTED_FACT_MANIFEST_SHA256,
        label="structured facts",
    )
    schemas = _schema_files(root)
    _validate_manifest(
        schemas,
        expected_count=EXPECTED_SCHEMA_FILE_COUNT,
        expected_size=EXPECTED_SCHEMA_SIZE_BYTES,
        expected_digest=EXPECTED_SCHEMA_MANIFEST_SHA256,
        label="schemas",
    )
    combined = tuple(sorted((*facts, *schemas), key=lambda item: item.path))
    _validate_manifest(
        combined,
        expected_count=149,
        expected_size=4_366_783,
        expected_digest=EXPECTED_COMBINED_MANIFEST_SHA256,
        label="facts and schemas",
    )

    validators = _schema_validators(root)
    for fact in facts:
        path = root / fact.path
        data = _read_regular_file(path, relative=fact.path)
        basename = path.name
        if basename == "observations.jsonl":
            for index, row in enumerate(
                _load_jsonl_bytes(data, relative=fact.path), start=1
            ):
                _validate_document(
                    validators[basename], row, relative=f"{fact.path}:{index}"
                )
        else:
            _validate_document(
                validators[basename],
                _load_json_bytes(data, relative=fact.path),
                relative=fact.path,
            )

    aggregate_records = _aggregate_records(project_root)
    runs: list[SnapshotRun] = []
    for benchmark_id in _BENCHMARKS:
        run_paths = sorted((root / benchmark_id / "runs").glob("*/run.json"))
        for run_path in run_paths:
            run_fact = _run_fact(
                root, benchmark_id, run_path, validators, aggregate_records
            )
            if run_fact is not None:
                runs.append(run_fact)
    runs.sort(key=lambda item: (item.benchmark_id, item.run_id))

    owner_identities = [[item.benchmark_id, item.run_id] for item in runs]
    unit_identities = [
        [
            run.benchmark_id,
            run.run_id,
            observation.unit_id,
            observation.attempt_id,
            observation.metric_id,
        ]
        for run in runs
        for observation in run.observations
    ]
    owner_digest = _digest_bytes(canonical_json_bytes(owner_identities))
    unit_digest = _digest_bytes(canonical_json_bytes(unit_identities))
    unit_count = sum(len(item.observations) for item in runs)
    if (
        len(runs),
        unit_count,
        owner_digest,
        unit_digest,
    ) != (
        EXPECTED_OWNER_COUNT,
        EXPECTED_UNIT_COUNT,
        EXPECTED_OWNER_IDENTITY_SHA256,
        EXPECTED_UNIT_IDENTITY_SHA256,
    ):
        raise ExperimentalResultsError("selected owner or unit identity set drifted")

    snapshot = H20ExperimentalResultsSnapshot(
        format=SNAPSHOT_FORMAT,
        publication_decision="Minions-Land/MagentaBenchmark#159",
        publication_decision_sha256=(
            "9696c1b1a8da6a34b9288dd8b129e60fef70a8f143b54b125c210657a38fd145"
        ),
        inventory=SnapshotInventory(
            catalog_sha256=EXPECTED_CATALOG_SHA256,
            catalog_file_count=EXPECTED_CATALOG_FILE_COUNT,
            catalog_size_bytes=EXPECTED_CATALOG_SIZE_BYTES,
            fact_manifest_sha256=EXPECTED_FACT_MANIFEST_SHA256,
            fact_file_count=EXPECTED_FACT_FILE_COUNT,
            fact_size_bytes=EXPECTED_FACT_SIZE_BYTES,
            schema_manifest_sha256=EXPECTED_SCHEMA_MANIFEST_SHA256,
            schema_file_count=EXPECTED_SCHEMA_FILE_COUNT,
            schema_size_bytes=EXPECTED_SCHEMA_SIZE_BYTES,
            combined_manifest_sha256=EXPECTED_COMBINED_MANIFEST_SHA256,
            facts=facts,
            schemas=schemas,
        ),
        owner_count=EXPECTED_OWNER_COUNT,
        unit_count=EXPECTED_UNIT_COUNT,
        owner_identity_sha256=owner_digest,
        unit_identity_sha256=unit_digest,
        runs=tuple(runs),
    )
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: H20ExperimentalResultsSnapshot) -> None:
    """Apply exact count, identity, and claim-boundary checks to a snapshot."""

    inventory = snapshot.inventory
    if (
        snapshot.publication_decision_sha256 != _PUBLICATION_DECISION_SHA256
        or snapshot.owner_identity_sha256 != EXPECTED_OWNER_IDENTITY_SHA256
        or snapshot.unit_identity_sha256 != EXPECTED_UNIT_IDENTITY_SHA256
        or inventory.catalog_sha256 != EXPECTED_CATALOG_SHA256
        or inventory.fact_manifest_sha256 != EXPECTED_FACT_MANIFEST_SHA256
        or inventory.schema_manifest_sha256 != EXPECTED_SCHEMA_MANIFEST_SHA256
        or inventory.combined_manifest_sha256 != EXPECTED_COMBINED_MANIFEST_SHA256
        or _manifest_digest(inventory.facts) != EXPECTED_FACT_MANIFEST_SHA256
        or _manifest_digest(inventory.schemas) != EXPECTED_SCHEMA_MANIFEST_SHA256
    ):
        raise ExperimentalResultsError("snapshot inventory identity is invalid")
    if (
        len(snapshot.runs) != EXPECTED_OWNER_COUNT
        or sum(len(run.observations) for run in snapshot.runs) != EXPECTED_UNIT_COUNT
    ):
        raise ExperimentalResultsError("snapshot result counts are invalid")
    if any(run.claim_eligible for run in snapshot.runs):
        raise ExperimentalResultsError("snapshot cannot contain claim-eligible runs")
    benchmark_counts: dict[str, tuple[int, int]] = {}
    status_counts: dict[str, int] = {}
    inventory_files = {
        (item.path, item.sha256, item.size_bytes) for item in inventory.facts
    }
    for benchmark_id in _EXPECTED_BENCHMARK_COUNTS:
        selected = [run for run in snapshot.runs if run.benchmark_id == benchmark_id]
        benchmark_counts[benchmark_id] = (
            len(selected),
            sum(len(run.observations) for run in selected),
        )
    if benchmark_counts != _EXPECTED_BENCHMARK_COUNTS:
        raise ExperimentalResultsError("snapshot benchmark counts are invalid")
    for run in snapshot.runs:
        if tuple(metric.metric_id for metric in run.metrics) != tuple(
            sorted(metric.metric_id for metric in run.metrics)
        ):
            raise ExperimentalResultsError("snapshot metric contracts are not ordered")
        if (
            run.run_file.path,
            run.run_file.sha256,
            run.run_file.size_bytes,
        ) not in inventory_files or (
            run.observations_file.path,
            run.observations_file.sha256,
            run.observations_file.size_bytes,
        ) not in inventory_files:
            raise ExperimentalResultsError(
                "snapshot run evidence is outside the inventory"
            )
        if run.benchmark_id == "swebench-verified":
            if (
                run.method.code_commit != _SWE_CODE_COMMIT
                or run.aggregate_record_id is not None
            ):
                raise ExperimentalResultsError("SWE run identity is invalid")
        elif run.method.code_commit is not None or run.aggregate_record_id is None:
            raise ExperimentalResultsError("task-matrix run identity is invalid")
        if len(run.observations) != len(run.metrics) * run.dataset.planned_count:
            raise ExperimentalResultsError("snapshot run denominator is invalid")
        for observation in run.observations:
            status_counts[observation.status] = (
                status_counts.get(observation.status, 0) + 1
            )
    if status_counts != _EXPECTED_STATUS_COUNTS:
        raise ExperimentalResultsError("snapshot status counts are invalid")
    owner_identities = [[item.benchmark_id, item.run_id] for item in snapshot.runs]
    unit_identities = [
        [
            run.benchmark_id,
            run.run_id,
            observation.unit_id,
            observation.attempt_id,
            observation.metric_id,
        ]
        for run in snapshot.runs
        for observation in run.observations
    ]
    if owner_identities != sorted(owner_identities) or unit_identities != sorted(
        unit_identities
    ):
        raise ExperimentalResultsError("snapshot facts are not canonically ordered")
    if (
        _digest_bytes(canonical_json_bytes(owner_identities))
        != EXPECTED_OWNER_IDENTITY_SHA256
        or _digest_bytes(canonical_json_bytes(unit_identities))
        != EXPECTED_UNIT_IDENTITY_SHA256
    ):
        raise ExperimentalResultsError("snapshot fact identities are invalid")


def load_snapshot(path: Path) -> H20ExperimentalResultsSnapshot:
    relative = path.name
    data = _read_regular_file(path, relative=relative)
    try:
        snapshot = H20ExperimentalResultsSnapshot.model_validate_json(data, strict=True)
    except Exception as error:
        raise ExperimentalResultsError("snapshot fields are invalid") from error
    validate_snapshot(snapshot)
    return snapshot


__all__ = [
    "EXPECTED_CATALOG_SHA256",
    "EXPECTED_OWNER_COUNT",
    "EXPECTED_UNIT_COUNT",
    "ExperimentalResultsError",
    "H20ExperimentalResultsSnapshot",
    "SnapshotMetricContract",
    "SnapshotObservation",
    "SnapshotRun",
    "build_snapshot",
    "canonical_json_bytes",
    "load_snapshot",
    "validate_snapshot",
]
