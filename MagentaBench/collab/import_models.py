"""Strict, content-addressed models for historical benchmark imports.

Historical imports describe source evidence.  They never acquire the BMP
standalone-verification semantics used by live MagentaBench reports.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

SOURCE_FORMAT = "magentabench-historical-source-v1"
RECORD_FORMAT = "magentabench-historical-record-v1"
ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"
SHA1_PATTERN = r"^[0-9a-f]{40}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

_ID_RE = re.compile(ID_PATTERN)
_SHA1_RE = re.compile(SHA1_PATTERN)
_SHA256_RE = re.compile(SHA256_PATTERN)
_GITHUB_NAME_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100})"
)
_MEDIA_TYPE_RE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{4,}"
    ),
)
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)


class HistoricalImportModel(BaseModel):
    """Immutable base model with no extension or coercion surface."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return the repository's deterministic JSON identity representation."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _safe_string(value: str, *, label: str, max_length: int = 256) -> str:
    if not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{label} must be a non-empty normalized string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError(f"{label} must not contain secret material")
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"{label} must not contain an authenticated URL")
        query_keys = {
            part.split("=", 1)[0].casefold().replace("-", "_")
            for part in parsed.query.split("&")
            if part
        }
        if query_keys & _CREDENTIAL_QUERY_KEYS:
            raise ValueError(f"{label} must not contain URL credentials")
        raise ValueError(f"{label} must not contain a URL")
    if (
        value.startswith(("/", "\\", "~/", "../", "./"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        raise ValueError(f"{label} must not contain an absolute or host path")
    return value


def _normalized_id(value: str, *, label: str) -> str:
    _safe_string(value, label=label, max_length=128)
    if _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a normalized identifier")
    return value


def repository_relative_path(value: str, *, label: str) -> str:
    """Validate a normalized, repository-relative POSIX path."""

    if (
        not value
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or value.startswith(("/", "~/"))
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise ValueError(f"{label} must be a normalized repository-relative path")
    parts = value.split("/")
    if value.endswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must be a normalized repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError(f"{label} must be a normalized repository-relative path")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError(f"{label} must not contain secret material")
    return value


def canonical_repository_name(value: str) -> str:
    """Return the case-insensitive ``owner/repository`` GitHub identity."""

    if value != value.strip() or not value:
        raise ValueError("repository must be normalized")
    if "://" not in value:
        match = _GITHUB_NAME_RE.fullmatch(value)
        if match is None:
            raise ValueError("repository must be a canonical GitHub owner/name")
        if match.group("repo").casefold().endswith(".git"):
            raise ValueError("repository must not include a .git suffix")
        return f"{match.group('owner')}/{match.group('repo')}".casefold()

    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.netloc != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("repository URL must be an unauthenticated canonical GitHub URL")
    name = parsed.path[1:]
    match = _GITHUB_NAME_RE.fullmatch(name)
    if match is None or match.group("repo").casefold().endswith(".git"):
        raise ValueError("repository URL must end with a canonical owner/name")
    if value != f"https://github.com/{name}":
        raise ValueError("repository URL must be normalized without suffixes")
    return f"{match.group('owner')}/{match.group('repo')}".casefold()


def _unique(values: tuple[Any, ...], *, label: str) -> tuple[Any, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    return values


Number: TypeAlias = StrictInt | StrictFloat


def _finite_number(value: Number, *, label: str) -> Number:
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number, not a boolean")
    return value


class GitObjectId(HistoricalImportModel):
    algorithm: Literal["sha1", "sha256"]
    digest: str

    @model_validator(mode="after")
    def digest_matches_algorithm(self) -> GitObjectId:
        pattern = _SHA1_RE if self.algorithm == "sha1" else _SHA256_RE
        if pattern.fullmatch(self.digest) is None:
            raise ValueError(f"{self.algorithm} Git object id has the wrong length or encoding")
        return self


class HistoricalSource(HistoricalImportModel):
    format: Literal["magentabench-historical-source-v1"] = SOURCE_FORMAT
    source_id: str
    repository: str
    commit_sha: str
    root_tree: GitObjectId
    normalizer_id: str
    normalizer_sha256: str
    visibility: Literal["public", "private", "unknown"]
    license_status: Literal["declared", "not-detected", "unknown"]
    license_id: str | None = None
    ref_hint: str | None = None

    @field_validator("source_id", "normalizer_id")
    @classmethod
    def identifiers_are_normalized(cls, value: str, info) -> str:
        return _normalized_id(value, label=info.field_name)

    @field_validator("repository")
    @classmethod
    def repository_is_safe(cls, value: str) -> str:
        canonical_repository_name(value)
        return value

    @field_validator("commit_sha")
    @classmethod
    def commit_is_full_sha1(cls, value: str) -> str:
        if _SHA1_RE.fullmatch(value) is None:
            raise ValueError("commit_sha must be a full lowercase 40-hex Git commit")
        return value

    @field_validator("normalizer_sha256")
    @classmethod
    def normalizer_is_content_addressed(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("normalizer_sha256 must be lowercase 64-hex")
        return value

    @field_validator("license_id")
    @classmethod
    def license_id_is_safe(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_string(value, label="license_id", max_length=128)
        return value

    @field_validator("ref_hint")
    @classmethod
    def ref_hint_is_non_authoritative(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, label="ref_hint", max_length=256)
            if value.startswith("-") or ".." in value or value.endswith((".", "/")):
                raise ValueError("ref_hint must be a normalized non-option Git ref hint")
        return value

    @model_validator(mode="after")
    def root_tree_is_sha1(self) -> HistoricalSource:
        if self.root_tree.algorithm != "sha1":
            raise ValueError("root_tree must use the Git SHA-1 object format")
        if (self.license_status == "declared") != (self.license_id is not None):
            raise ValueError(
                "license_id is required only when license_status is declared"
            )
        return self


def source_snapshot_identity(source: HistoricalSource) -> str:
    """Digest the immutable source boundary, excluding labels and hints."""

    payload = {
        "commit_sha": source.commit_sha,
        "normalizer_id": source.normalizer_id,
        "normalizer_sha256": source.normalizer_sha256,
        "repository": canonical_repository_name(source.repository),
        "root_tree": source.root_tree.model_dump(mode="json"),
    }
    return sha256(canonical_json_bytes(payload)).hexdigest()


def source_document_digest(source: HistoricalSource) -> str:
    return sha256(canonical_json_bytes(source)).hexdigest()


class _NamedIdentity(HistoricalImportModel):
    id: str
    name: str
    version: str | None = None

    @field_validator("id")
    @classmethod
    def id_is_normalized(cls, value: str) -> str:
        return _normalized_id(value, label="id")

    @field_validator("name", "version")
    @classmethod
    def labels_are_safe(cls, value: str | None, info) -> str | None:
        if value is not None:
            return _safe_string(value, label=info.field_name)
        return value


class BenchmarkIdentity(_NamedIdentity):
    pass


class DatasetIdentity(_NamedIdentity):
    split: str
    commit_sha: str | None = None
    content_sha256: str | None = None

    @field_validator("split")
    @classmethod
    def split_is_safe(cls, value: str) -> str:
        return _safe_string(value, label="split", max_length=128)

    @field_validator("commit_sha")
    @classmethod
    def dataset_commit_is_full(cls, value: str | None) -> str | None:
        if value is not None and _SHA1_RE.fullmatch(value) is None:
            raise ValueError("dataset commit_sha must be a full lowercase 40-hex Git commit")
        return value

    @field_validator("content_sha256")
    @classmethod
    def dataset_content_is_bound(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("dataset content_sha256 must be lowercase 64-hex")
        return value


class MethodIdentity(_NamedIdentity):
    subject_id: str

    @field_validator("subject_id")
    @classmethod
    def subject_is_normalized(cls, value: str) -> str:
        return _normalized_id(value, label="subject_id")


class ModelIdentity(_NamedIdentity):
    revision: str | None = None

    @field_validator("revision")
    @classmethod
    def revision_is_safe(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_string(value, label="revision")
        return value


class ProviderIdentity(_NamedIdentity):
    region: str | None = None

    @field_validator("region")
    @classmethod
    def region_is_safe(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_string(value, label="region", max_length=128)
        return value


class HarnessIdentity(_NamedIdentity):
    protocol_id: str
    configuration_sha256: str | None = None

    @field_validator("protocol_id")
    @classmethod
    def protocol_is_normalized(cls, value: str) -> str:
        return _normalized_id(value, label="protocol_id")

    @field_validator("configuration_sha256")
    @classmethod
    def configuration_is_bound(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("configuration_sha256 must be lowercase 64-hex")
        return value


class EvaluatorIdentity(_NamedIdentity):
    kind: Literal["deterministic", "human", "model", "hybrid", "unknown"]
    independent: StrictBool


class FactorValue(HistoricalImportModel):
    id: str
    value: StrictBool | Number | str
    unit: str | None = None

    @field_validator("id")
    @classmethod
    def factor_id_is_normalized(cls, value: str) -> str:
        return _normalized_id(value, label="factor id")

    @field_validator("value")
    @classmethod
    def factor_value_is_safe(cls, value: StrictBool | Number | str):
        if isinstance(value, str):
            return _safe_string(value, label="factor value", max_length=1000)
        if isinstance(value, bool):
            return value
        return _finite_number(value, label="factor value")

    @field_validator("unit")
    @classmethod
    def factor_unit_is_safe(cls, value: str | None) -> str | None:
        if value is not None:
            return _normalized_id(value, label="factor unit")
        return value


class HardwareConditions(HistoricalImportModel):
    architecture: Literal["x86_64", "aarch64", "other", "unknown"]
    cpu_count: StrictInt | None = Field(default=None, ge=1)
    memory_bytes: StrictInt | None = Field(default=None, ge=0)
    accelerator: str | None = None
    accelerator_count: StrictInt | None = Field(default=None, ge=0)

    @field_validator("accelerator")
    @classmethod
    def accelerator_is_safe(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_string(value, label="accelerator")
        return value

    @model_validator(mode="after")
    def accelerator_fields_are_coherent(self) -> HardwareConditions:
        if self.accelerator_count not in (None, 0) and self.accelerator is None:
            raise ValueError("accelerator is required when accelerator_count is positive")
        return self


class ExecutionBudget(HistoricalImportModel):
    max_cases: StrictInt | None = Field(default=None, ge=0)
    max_wall_seconds: Number | None = Field(default=None, ge=0)
    max_tokens: StrictInt | None = Field(default=None, ge=0)
    max_cost_usd: Number | None = Field(default=None, ge=0)

    @field_validator("max_wall_seconds", "max_cost_usd")
    @classmethod
    def budget_numbers_are_finite(cls, value: Number | None, info):
        if value is not None:
            return _finite_number(value, label=info.field_name)
        return value

    @model_validator(mode="after")
    def budget_is_not_empty(self) -> ExecutionBudget:
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("budget must declare at least one bound")
        return self


class ExecutionConditions(HistoricalImportModel):
    mode: Literal[
        "local-process",
        "docker",
        "apptainer",
        "appcontainer",
        "e2b",
        "remote-sandbox",
        "unknown",
    ]
    backend_id: str | None
    isolation: Literal["process", "container", "microvm", "host", "unknown"]
    network_policy: Literal[
        "disabled", "allowlist", "benchmark-defined", "unrestricted", "unknown"
    ]
    case_count: StrictInt = Field(ge=0)
    repetitions_per_case: StrictInt = Field(ge=1)
    seeds: tuple[StrictInt, ...]
    order_policy: Literal["fixed", "randomized", "source-defined", "unknown"]
    hardware: HardwareConditions
    image_sha256: str | None = None
    configuration_id: str | None = None
    configuration_sha256: str | None = None
    configuration_profiles: tuple[str, ...] = ()
    factors: tuple[FactorValue, ...] = ()
    budget: ExecutionBudget | None = None

    @field_validator("backend_id", "configuration_id")
    @classmethod
    def optional_ids_are_normalized(cls, value: str | None, info) -> str | None:
        if value is not None:
            return _normalized_id(value, label=info.field_name)
        return value

    @field_validator("image_sha256", "configuration_sha256")
    @classmethod
    def digests_are_sha256(cls, value: str | None, info) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} must be lowercase 64-hex")
        return value

    @field_validator("seeds")
    @classmethod
    def seeds_are_unique(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(isinstance(value, bool) for value in values):
            raise ValueError("seeds must contain integers, not booleans")
        return _unique(values, label="seeds")

    @field_validator("configuration_profiles")
    @classmethod
    def profiles_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _normalized_id(value, label="configuration profile")
        return _unique(values, label="configuration_profiles")

    @field_validator("factors")
    @classmethod
    def factor_ids_are_unique(cls, values: tuple[FactorValue, ...]) -> tuple[FactorValue, ...]:
        ids = tuple(value.id for value in values)
        _unique(ids, label="factor ids")
        return values


class Comparability(HistoricalImportModel):
    status: Literal["exact", "conditional", "not-comparable", "unknown"]
    comparison_group: str | None = None
    protocol_sha256: str | None = None
    case_set_sha256: str | None = None
    evaluator_sha256: str | None = None

    @field_validator("comparison_group")
    @classmethod
    def group_is_normalized(cls, value: str | None) -> str | None:
        if value is not None:
            return _normalized_id(value, label="comparison_group")
        return value

    @field_validator("protocol_sha256", "case_set_sha256", "evaluator_sha256")
    @classmethod
    def comparison_digests_are_bound(cls, value: str | None, info) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} must be lowercase 64-hex")
        return value

    @model_validator(mode="after")
    def exact_comparison_is_fully_bound(self) -> Comparability:
        if self.status == "exact" and (
            self.comparison_group is None
            or self.protocol_sha256 is None
            or self.case_set_sha256 is None
            or self.evaluator_sha256 is None
        ):
            raise ValueError("exact comparability requires group, protocol, case-set, and evaluator digests")
        return self


class ExperimentConditions(HistoricalImportModel):
    experiment_id: str
    benchmark: BenchmarkIdentity
    dataset: DatasetIdentity
    method: MethodIdentity
    model: ModelIdentity | None
    provider: ProviderIdentity | None
    harness: HarnessIdentity
    evaluator: EvaluatorIdentity
    execution: ExecutionConditions
    purpose: Literal["benchmark", "evaluation", "ablation", "training", "search", "exploratory", "unknown"]
    comparability: Comparability
    limitations: tuple[str, ...] = ()

    @field_validator("experiment_id")
    @classmethod
    def experiment_id_is_normalized(cls, value: str) -> str:
        return _normalized_id(value, label="experiment_id")

    @field_validator("limitations")
    @classmethod
    def limitations_are_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _normalized_id(value, label="limitation")
        return _unique(values, label="limitations")


def experiment_condition_digest(conditions: ExperimentConditions) -> str:
    return sha256(canonical_json_bytes(conditions)).hexdigest()


class ProvenanceRef(HistoricalImportModel):
    role: Literal[
        "declaration",
        "configuration",
        "dataset",
        "method",
        "model",
        "harness",
        "evaluator",
        "result",
        "metric",
        "asset",
    ]
    path: str
    git_blob_oid: GitObjectId
    content_sha256: str
    size_bytes: StrictInt = Field(ge=0)

    @field_validator("path")
    @classmethod
    def path_is_repository_relative(cls, value: str) -> str:
        return repository_relative_path(value, label="provenance path")

    @field_validator("content_sha256")
    @classmethod
    def content_is_bound(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("content_sha256 must be lowercase 64-hex")
        return value


class MetricDenominator(HistoricalImportModel):
    unit: Literal["cases", "rollouts", "samples", "items", "tokens", "other"]
    planned_count: StrictInt = Field(ge=0)
    observed_count: StrictInt = Field(ge=0)
    excluded_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def counts_fit_plan(self) -> MetricDenominator:
        if self.observed_count + self.excluded_count > self.planned_count:
            raise ValueError("observed and excluded denominator counts exceed planned_count")
        return self


class MetricUncertainty(HistoricalImportModel):
    method: Literal[
        "standard-deviation",
        "standard-error",
        "confidence-interval",
        "bootstrap",
        "range",
        "unknown",
    ]
    confidence_level: Number | None = None
    lower: Number | None = None
    upper: Number | None = None
    value: Number | None = None
    sample_size: StrictInt | None = Field(default=None, ge=1)

    @field_validator("confidence_level", "lower", "upper", "value")
    @classmethod
    def uncertainty_numbers_are_finite(cls, value: Number | None, info):
        if value is not None:
            return _finite_number(value, label=info.field_name)
        return value

    @model_validator(mode="after")
    def uncertainty_shape_is_coherent(self) -> MetricUncertainty:
        interval = self.method in {"confidence-interval", "bootstrap", "range"}
        if interval:
            if self.lower is None or self.upper is None or self.lower > self.upper:
                raise ValueError("interval uncertainty requires ordered lower and upper bounds")
        elif self.value is None:
            raise ValueError("point uncertainty requires value")
        if self.confidence_level is not None and not 0 < float(self.confidence_level) < 1:
            raise ValueError("confidence_level must be strictly between zero and one")
        if self.method == "confidence-interval" and self.confidence_level is None:
            raise ValueError("confidence-interval requires confidence_level")
        return self


class HistoricalMetric(HistoricalImportModel):
    metric_id: str
    definition_sha256: str
    state: Literal["observed", "missing", "invalid"]
    value: Number | None
    unit: str
    direction: Literal["higher-is-better", "lower-is-better", "neutral"]
    aggregation: Literal[
        "mean", "median", "sum", "minimum", "maximum", "rate", "micro", "macro", "none"
    ]
    denominator: MetricDenominator
    uncertainty: MetricUncertainty | None
    missing_count: StrictInt = Field(ge=0)
    invalid_count: StrictInt = Field(ge=0)
    zero_filled_count: StrictInt = Field(ge=0)

    @field_validator("metric_id", "unit")
    @classmethod
    def metric_tokens_are_normalized(cls, value: str, info) -> str:
        return _normalized_id(value, label=info.field_name)

    @field_validator("definition_sha256")
    @classmethod
    def definition_is_bound(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("definition_sha256 must be lowercase 64-hex")
        return value

    @field_validator("value")
    @classmethod
    def value_is_finite(cls, value: Number | None):
        if value is not None:
            return _finite_number(value, label="metric value")
        return value

    @model_validator(mode="after")
    def metric_state_and_counts_are_coherent(self) -> HistoricalMetric:
        if self.state == "observed" and self.value is None:
            raise ValueError("observed metric requires a value")
        if self.state != "observed" and self.value is not None:
            raise ValueError("missing or invalid metric must not have a value")
        if self.state == "missing" and self.missing_count == 0:
            raise ValueError("missing metric requires missing_count")
        if self.state == "invalid" and self.invalid_count == 0:
            raise ValueError("invalid metric requires invalid_count")
        if self.zero_filled_count > self.missing_count + self.invalid_count:
            raise ValueError("zero_filled_count exceeds missing plus invalid counts")
        accounted = (
            self.denominator.observed_count
            + self.denominator.excluded_count
            + self.missing_count
            + self.invalid_count
        )
        if accounted > self.denominator.planned_count:
            raise ValueError("metric counts exceed the declared denominator")
        return self


class HistoricalAsset(HistoricalImportModel):
    asset_id: str
    role: Literal[
        "declaration",
        "configuration",
        "dataset",
        "report",
        "manifest",
        "result",
        "log",
        "trace",
        "other",
    ]
    status: Literal["available", "unavailable", "partial", "unknown"]
    materialization_state: Literal["materialized", "metadata-only", "external-unavailable"]
    media_type: str
    content_sha256: str
    size_bytes: StrictInt = Field(ge=0)

    @field_validator("asset_id")
    @classmethod
    def asset_id_is_normalized(cls, value: str) -> str:
        return _normalized_id(value, label="asset_id")

    @field_validator("media_type")
    @classmethod
    def media_type_is_normalized(cls, value: str) -> str:
        if _MEDIA_TYPE_RE.fullmatch(value) is None:
            raise ValueError("media_type must be a normalized lowercase MIME type")
        return value

    @field_validator("content_sha256")
    @classmethod
    def asset_content_is_bound(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("content_sha256 must be lowercase 64-hex")
        return value


EvidenceTier: TypeAlias = Literal["legacy-evaluated", "declaration-only", "candidate"]


class HistoricalRecordBase(HistoricalImportModel):
    format: Literal["magentabench-historical-record-v1"] = RECORD_FORMAT
    record_id: str
    kind: str
    source_id: str
    source_snapshot_sha256: str
    logical_key: str
    supersedes: tuple[str, ...] = ()
    evidence_tier: EvidenceTier
    claim_eligible: Literal[False] = False
    provenance: tuple[ProvenanceRef, ...] = Field(min_length=1)

    @field_validator("record_id")
    @classmethod
    def record_id_is_sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("record_id must be lowercase 64-hex")
        return value

    @field_validator("source_snapshot_sha256")
    @classmethod
    def source_snapshot_is_bound(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("source_snapshot_sha256 must be lowercase 64-hex")
        return value

    @field_validator("source_id", "logical_key")
    @classmethod
    def record_keys_are_normalized(cls, value: str, info) -> str:
        return _normalized_id(value, label=info.field_name)

    @field_validator("supersedes")
    @classmethod
    def supersedes_are_unique_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_SHA256_RE.fullmatch(value) is None for value in values):
            raise ValueError("supersedes must contain lowercase SHA-256 record ids")
        return _unique(values, label="supersedes")

    @field_validator("provenance")
    @classmethod
    def provenance_is_unique(cls, values: tuple[ProvenanceRef, ...]) -> tuple[ProvenanceRef, ...]:
        identities = tuple(
            (value.role, value.path, value.content_sha256, value.size_bytes)
            for value in values
        )
        _unique(identities, label="provenance refs")
        return values

    @model_validator(mode="after")
    def identity_is_canonical(self) -> HistoricalRecordBase:
        expected = compute_record_id(self)
        if self.record_id != expected:
            raise ValueError(
                "record_id differs from canonical JSON SHA-256 excluding record_id"
            )
        return self

    @property
    def logical_key_sha256(self) -> str:
        return logical_key_digest(self.kind, self.logical_key)


class HistoricalDeclaration(HistoricalRecordBase):
    kind: Literal["declaration"]
    evidence_tier: Literal["declaration-only", "candidate"]
    experiment: ExperimentConditions
    metric_ids: tuple[str, ...]

    @field_validator("metric_ids")
    @classmethod
    def metric_ids_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _normalized_id(value, label="metric id")
        return _unique(values, label="metric_ids")


class HistoricalRun(HistoricalRecordBase):
    kind: Literal["run"]
    evidence_tier: Literal["legacy-evaluated", "candidate"]
    experiment: ExperimentConditions
    run_id: str
    parent_run_id: str | None = None
    terminal_state: Literal["completed", "partial", "failed", "cancelled", "timed-out"]
    metrics: tuple[HistoricalMetric, ...]

    @field_validator("run_id", "parent_run_id")
    @classmethod
    def run_ids_are_normalized(cls, value: str | None, info) -> str | None:
        if value is not None:
            return _normalized_id(value, label=info.field_name)
        return value

    @field_validator("metrics")
    @classmethod
    def metric_ids_are_unique(cls, values: tuple[HistoricalMetric, ...]) -> tuple[HistoricalMetric, ...]:
        _unique(tuple(value.metric_id for value in values), label="run metric ids")
        return values

    @model_validator(mode="after")
    def metrics_match_evidence_and_terminal_state(self) -> HistoricalRun:
        if self.evidence_tier != "legacy-evaluated" and self.metrics:
            raise ValueError("candidate runs cannot emit metrics")
        if self.metrics and self.terminal_state not in {"completed", "partial"}:
            raise ValueError("non-result terminal states cannot emit metrics")
        if (
            self.evidence_tier == "legacy-evaluated"
            and self.terminal_state == "completed"
            and not self.metrics
        ):
            raise ValueError("completed legacy-evaluated run requires metrics")
        return self


class HistoricalAssetRecord(HistoricalRecordBase):
    kind: Literal["asset"]
    evidence_tier: EvidenceTier
    experiment_id: str | None = None
    run_id: str | None = None
    asset: HistoricalAsset

    @field_validator("experiment_id", "run_id")
    @classmethod
    def related_ids_are_normalized(cls, value: str | None, info) -> str | None:
        if value is not None:
            return _normalized_id(value, label=info.field_name)
        return value

    @model_validator(mode="after")
    def asset_is_bound_by_provenance(self) -> HistoricalAssetRecord:
        if not any(
            ref.role == "asset"
            and ref.content_sha256 == self.asset.content_sha256
            and ref.size_bytes == self.asset.size_bytes
            for ref in self.provenance
        ):
            raise ValueError("asset requires a matching content-addressed asset provenance ref")
        return self


HistoricalRecord: TypeAlias = Annotated[
    HistoricalDeclaration | HistoricalRun | HistoricalAssetRecord,
    Field(discriminator="kind"),
]


def compute_record_id(record: HistoricalRecordBase | Mapping[str, Any]) -> str:
    """Compute the content identity, always excluding the self-referential id."""

    if isinstance(record, BaseModel):
        payload: Any = record.model_dump(mode="json", exclude={"record_id"})
    else:
        payload = {key: value for key, value in record.items() if key != "record_id"}
    return sha256(canonical_json_bytes(payload)).hexdigest()


def logical_key_digest(kind: str, logical_key: str) -> str:
    """Digest the explicit logical identity used for conflict detection."""

    return sha256(
        canonical_json_bytes({"kind": kind, "logical_key": logical_key})
    ).hexdigest()


__all__ = [
    "RECORD_FORMAT",
    "SOURCE_FORMAT",
    "BenchmarkIdentity",
    "Comparability",
    "DatasetIdentity",
    "EvaluatorIdentity",
    "ExecutionBudget",
    "ExecutionConditions",
    "ExperimentConditions",
    "FactorValue",
    "GitObjectId",
    "HardwareConditions",
    "HarnessIdentity",
    "HistoricalAsset",
    "HistoricalAssetRecord",
    "HistoricalDeclaration",
    "HistoricalMetric",
    "HistoricalRecord",
    "HistoricalRecordBase",
    "HistoricalRun",
    "HistoricalSource",
    "MethodIdentity",
    "MetricDenominator",
    "MetricUncertainty",
    "ModelIdentity",
    "ProvenanceRef",
    "ProviderIdentity",
    "canonical_json_bytes",
    "canonical_repository_name",
    "compute_record_id",
    "experiment_condition_digest",
    "logical_key_digest",
    "repository_relative_path",
    "source_document_digest",
    "source_snapshot_identity",
]
