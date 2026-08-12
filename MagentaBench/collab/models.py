"""Strict experiment-bundle models for merge-friendly lab collaboration.

Bundles pin experiment intent and commands without becoming BMP declarations.
Live ownership and status remain in the separate ``lab/`` event ledger.
"""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
import re
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BUNDLE_FORMAT = "magentabench-experiment-bundle-v1"
ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
ENV_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{0,126}$"
_ALLOWED_ARG_PLACEHOLDERS = frozenset({"{record_root}", "{report}"})


class BundleModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )


def portable_path(value: str, *, label: str) -> str:
    if not value or "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a non-empty, single-line POSIX path")
    parts = value.split("/")
    if (
        value.startswith("/")
        or value.endswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"{label} must be a normalized repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError(f"{label} must be a normalized repository-relative path")
    return value


def _unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    return values


def _single_line(value: str, *, label: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be one non-null line")
    return value


def _safe_argv(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if not values or any(not value or "\x00" in value or "\n" in value or "\r" in value for value in values):
        raise ValueError(f"{label} must contain non-empty, single-line arguments")
    interpreters = {"bash", "sh", "dash", "zsh", "python", "python3"}

    def is_code_flag(value: str) -> bool:
        return value == "-c" or (
            value.startswith("-")
            and not value.startswith("--")
            and "c" in value[1:]
        )

    for index, value in enumerate(values[:-1]):
        executable = PurePosixPath(value).name.casefold()
        if executable in interpreters and any(is_code_flag(argument) for argument in values[index + 1 :]):
            raise ValueError(f"{label} must not embed an opaque shell or Python program")
    credential_option = re.compile(
        r"(?i)(?:^|[-_])(?:api[-_]?key|access[-_]?token|auth[-_]?token|"
        r"bearer[-_]?token|token|password|secret|credential)s?$"
    )
    authorization_header = re.compile(
        r"(?i)\b(?:proxy-)?authorization\s*:\s*(?:basic|bearer)\s+\S+"
    )
    for value in values:
        if "{" in value or "}" in value:
            if value not in _ALLOWED_ARG_PLACEHOLDERS:
                raise ValueError(f"{label} contains an unsupported placeholder: {value!r}")
        option = value.split("=", 1)[0].lstrip("-")
        assignment = value.split("=", 1)[0]
        if credential_option.search(option) or credential_option.search(assignment):
            raise ValueError(f"{label} must not contain credential-bearing arguments")
        if authorization_header.search(value):
            raise ValueError(f"{label} must not contain authorization header values")
        parsed = urlsplit(value)
        if parsed.scheme:
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(f"{label} URI arguments must not contain userinfo")
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
                normalized = key.casefold().replace("-", "_")
                if normalized in {"apikey", "api_key"} or any(
                    part in {"credential", "key", "password", "secret", "sig", "signature", "token"}
                    for part in normalized.split("_")
                ):
                    raise ValueError(f"{label} URI arguments must not contain credential fields")
    return values


class BundlePurpose(str, Enum):
    exploratory = "exploratory"
    claim = "claim"


class ExecutionMode(str, Enum):
    local_process = "local-process"
    docker = "docker"
    apptainer = "apptainer"
    appcontainer = "appcontainer"
    e2b = "e2b"
    remote_sandbox = "remote-sandbox"


class BundleDesign(BundleModel):
    question: str = Field(min_length=1, max_length=4000)
    hypothesis: str = Field(min_length=1, max_length=4000)
    primary_metrics: tuple[str, ...] = Field(min_length=1)
    planned_case_ids: tuple[str, ...] = ()
    repetitions_per_case: int = Field(ge=1, strict=True)
    seeds: tuple[int, ...] = ()
    stop_conditions: tuple[str, ...] = Field(min_length=1)

    @field_validator("primary_metrics", "planned_case_ids")
    @classmethod
    def identifiers_are_normalized(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        for value in values:
            _single_line(value, label=info.field_name)
            if re.fullmatch(ID_PATTERN, value) is None:
                raise ValueError(f"{info.field_name} must contain normalized identifiers")
        return _unique(values, label=info.field_name)

    @field_validator("seeds")
    @classmethod
    def seeds_are_unique(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(isinstance(value, bool) for value in values):
            raise ValueError("seeds must contain integers, not booleans")
        if len(set(values)) != len(values):
            raise ValueError("seeds must be unique")
        return values

    @field_validator("stop_conditions")
    @classmethod
    def stop_conditions_are_lines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _single_line(value, label="stop condition")
        return _unique(values, label="stop conditions")


class BundleExecution(BundleModel):
    mode: ExecutionMode
    backend_id: str = Field(pattern=ID_PATTERN)
    isolation_boundary: Literal["process", "task-container", "microvm"]
    workspace_lifecycle: Literal["ephemeral", "persist-on-failure"]
    network_policy: Literal["disabled", "benchmark-defined", "allowlist"]
    artifact_export_required: Literal[True] = True
    preflight_argv: tuple[str, ...]
    run_argv: tuple[str, ...]
    required_env: tuple[str, ...] = ()
    record_root_template: str = Field(min_length=1, max_length=1000)

    @field_validator("preflight_argv", "run_argv")
    @classmethod
    def argv_is_safe(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        return _safe_argv(values, label=info.field_name)

    @field_validator("required_env")
    @classmethod
    def env_names_are_safe(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(ENV_NAME_PATTERN, value) is None for value in values):
            raise ValueError("required_env must contain uppercase variable names only")
        return _unique(values, label="required_env")

    @field_validator("record_root_template")
    @classmethod
    def record_root_is_durable_template(cls, value: str) -> str:
        _single_line(value, label="record_root_template")
        allowed = {"{artifact_root}", "{run_id}"}
        placeholders = set(re.findall(r"\{[^{}]+\}", value))
        if placeholders != allowed or value.count("{artifact_root}") != 1 or value.count("{run_id}") != 1:
            raise ValueError(
                "record_root_template must contain {artifact_root} and {run_id} exactly once"
            )
        remainder = value.replace("{artifact_root}", "artifact-root").replace("{run_id}", "run-id")
        normalized = portable_path(remainder, label="record_root_template")
        if normalized == ".runs" or normalized.startswith(".runs/"):
            raise ValueError("record_root_template must not use scratch-only .runs")
        return value


class BundleEvidence(BundleModel):
    classification: Literal["exploratory", "claim-candidate"]
    required_files: tuple[str, ...] = Field(min_length=1)
    verifier_argv: tuple[str, ...]
    retention_policy: str = Field(min_length=1, max_length=2000)

    @field_validator("required_files")
    @classmethod
    def required_files_are_portable(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(portable_path(value, label="required evidence file") for value in values)
        return _unique(normalized, label="required_files")

    @field_validator("verifier_argv")
    @classmethod
    def verifier_argv_is_safe(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _safe_argv(values, label="verifier_argv")


class ExperimentBundle(BundleModel):
    format: Literal["magentabench-experiment-bundle-v1"] = BUNDLE_FORMAT
    id: str = Field(pattern=ID_PATTERN)
    bmp_spec: str
    bmp_spec_sha256: str = Field(pattern=SHA256_PATTERN)
    protocol_id: str = Field(pattern=ID_PATTERN)
    purpose: BundlePurpose
    lab_issue: str = Field(pattern=ID_PATTERN)
    related_issues: tuple[str, ...] = ()
    summary: str = Field(min_length=1, max_length=4000)
    design: BundleDesign
    execution: BundleExecution
    evidence: BundleEvidence

    @field_validator("bmp_spec")
    @classmethod
    def bmp_spec_is_portable(cls, value: str) -> str:
        return portable_path(value, label="bmp_spec")

    @field_validator("related_issues")
    @classmethod
    def related_issue_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("related_issues must contain normalized identifiers")
        return _unique(values, label="related_issues")

    @model_validator(mode="after")
    def evidence_matches_purpose(self) -> "ExperimentBundle":
        if self.lab_issue in self.related_issues:
            raise ValueError("lab_issue must not be repeated in related_issues")
        if self.purpose == BundlePurpose.exploratory and self.evidence.classification != "exploratory":
            raise ValueError("exploratory bundles require exploratory evidence classification")
        if self.purpose == BundlePurpose.claim and self.evidence.classification != "claim-candidate":
            raise ValueError("claim bundles require claim-candidate evidence classification")
        return self
