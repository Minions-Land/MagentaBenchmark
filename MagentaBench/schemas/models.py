"""Pydantic contracts for the benchmark-side BMP 0.1 protocol.

Hand-written TOML declarations are parsed into ``*Spec`` models.  Compilation
normalizes and pins them into ``*Artifact`` and ``Resolved*`` models suitable
for canonical hashing and execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal, Mapping, Union
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    computed_field,
    field_validator,
    model_validator,
)

BMP_VERSION = "0.1"
ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
ADAPTER_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
OCI_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
SECRET_KEY_PATTERN = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL",
    re.IGNORECASE,
)
NON_SECRET_TOKEN_KEY_PATTERN = re.compile(
    r"^(?:cache|completion|context|generation|input|max_context|max_generation|output|prompt|total)_?tokens$",
    re.IGNORECASE,
)
IDENTITY_EXCLUDE: frozenset[str] = frozenset(
    {
        "created_at",
        "wall_clock_start",
        "wall_clock_end",
        "record_root",
        "resume_count",
        "runner_invocation_id",
    }
)


def _reject_secret_like_keys(value: Any, *, field_name: str) -> Any:
    """Reject secret-bearing keys recursively inside a generic metadata value."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if SECRET_KEY_PATTERN.search(str(key)) and not (
                "TOKEN" in str(key).upper()
                and NON_SECRET_TOKEN_KEY_PATTERN.fullmatch(str(key))
            ):
                raise ValueError(
                    f"{field_name} must not contain secret-like key {key!r}"
                )
            _reject_secret_like_keys(item, field_name=field_name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_like_keys(item, field_name=field_name)
    return value


class StrictModel(BaseModel):
    """Base class shared by all BMP wire contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class RegistryEntry(StrictModel):
    """Fields required on every registry declaration."""

    id: str = Field(pattern=ID_PATTERN)
    kind: str = Field(min_length=1)
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION


class SourceRegistryEntry(RegistryEntry):
    """Registry declaration backed by a code or content source."""

    source: str = Field(min_length=1)
    commit: str | None = Field(default=None, min_length=1)


def _validate_json_configuration(value: Any, *, field_name: str) -> Any:
    """Validate an extensible configuration tree without admitting secrets."""

    _reject_secret_like_keys(value, field_name=field_name)

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(f"{field_name} keys must be non-empty strings")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    try:
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-compatible") from exc
    return value


class ConfigurationSpec(RegistryEntry):
    """Open, identity-bearing TOML configuration profile.

    The profile deliberately carries a generic JSON-compatible tree.  Adapter
    code owns the meaning of its paths; BMP owns merge order, secret rejection,
    source bytes, and the resulting digest.
    """

    kind: Literal["configuration"]
    extends: tuple[str, ...] = ()
    values: Mapping[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(populate_by_name=True)
    json_schema: Mapping[str, Any] = Field(default_factory=dict, alias="schema")

    @field_validator("extends")
    @classmethod
    def parent_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("configuration extends ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("configuration extends ids must be valid BMP ids")
        return values

    @field_validator("values", "json_schema", mode="before")
    @classmethod
    def values_are_json_compatible(cls, value: Any, info: Any) -> Any:
        return _validate_json_configuration(value or {}, field_name=f"ConfigurationSpec.{info.field_name}")


class ConfigurationSelection(StrictModel):
    """Experiment-local composition of registry profiles and external TOML."""

    profiles: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    values: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def profile_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("configuration profiles must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("configuration profile ids must be valid BMP ids")
        return values

    @field_validator("files")
    @classmethod
    def files_are_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            candidate = value.replace("\\", "/")
            parts = candidate.split("/")
            if (
                not candidate
                or candidate.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ValueError("configuration files must be normalized relative paths")
            normalized.append(candidate)
        if len(set(normalized)) != len(normalized):
            raise ValueError("configuration files must be unique")
        return tuple(normalized)

    @field_validator("values", mode="before")
    @classmethod
    def inline_values_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(value or {}, field_name="ConfigurationSelection.values")


class ArtifactRef(StrictModel):
    """Content-addressed reference to an artifact stored outside the manifest."""

    path: str = Field(min_length=1, pattern=r"^/")
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("artifact path must be absolute")
        return value

    def identity_data(self) -> dict[str, int | str]:
        return {"sha256": self.sha256, "size_bytes": self.size_bytes}


class ConfigurationArtifact(StrictModel):
    """Resolved configuration tree bound to every profile/source byte."""

    id: str = Field(pattern=ID_PATTERN)
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    profiles: tuple[str, ...] = ()
    source_refs: tuple[ArtifactRef, ...] = ()
    schema_digest: str = Field(pattern=SHA256_PATTERN)
    values: Mapping[str, Any] = Field(default_factory=dict)
    artifact_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("values", mode="before")
    @classmethod
    def artifact_values_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value or {}, field_name="ConfigurationArtifact.values"
        )

    @model_validator(mode="after")
    def source_refs_are_unique(self) -> "ConfigurationArtifact":
        identities = [(ref.sha256, ref.size_bytes) for ref in self.source_refs]
        if len(set(identities)) != len(identities):
            raise ValueError("configuration source refs must be content-unique")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "adapter": self.adapter,
            "profiles": list(self.profiles),
            "source_refs": [ref.identity_data() for ref in self.source_refs],
            "schema_digest": self.schema_digest,
            "values": self.values,
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AdapterCapability(RegistryEntry):
    """Declarative capability contract for a pluggable BMP adapter.

    The entrypoint is metadata only until a host explicitly registers the
    implementation object.  This keeps a TOML declaration from silently
    selecting executable code while still making supported kinds and config
    paths inspectable and hashable.
    """

    kind: Literal["adapter"]
    adapter_kind: Literal[
        "benchmark_loader", "subject", "backend_factory", "execution"
    ]
    entrypoint: str = Field(min_length=1)
    digest: str = Field(pattern=SHA256_PATTERN)
    config_paths: tuple[str, ...] = ()
    supported_benchmark_kinds: tuple[str, ...] = ()
    supported_subject_kinds: tuple[str, ...] = ()
    supported_backend_kinds: tuple[str, ...] = ()

    @field_validator(
        "config_paths",
        "supported_benchmark_kinds",
        "supported_subject_kinds",
        "supported_backend_kinds",
    )
    @classmethod
    def capability_values_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("adapter capability values must be unique")
        if any(not value.strip() for value in values):
            raise ValueError("adapter capability values must be non-empty")
        return values


class EnvironmentBindingRef(StrictModel):
    """Environment value identity without serializing the value itself."""

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_digest: str = Field(pattern=SHA256_PATTERN)
    secret: bool
    source_name: str = Field(pattern=ID_PATTERN)


class ResourceSpec(StrictModel):
    """Task resources, separate from Python environment requirements.

    REQUIRED-IN-STEP-2: claim runs must reject a missing docker_image_digest.
    """

    build_timeout_sec: float = Field(gt=0, strict=True)
    docker_image: str = Field(min_length=1)
    docker_image_digest: str | None = Field(default=None, pattern=OCI_SHA256_PATTERN)
    cpus: int = Field(gt=0, strict=True)
    memory_mb: int = Field(gt=0, strict=True)
    storage_mb: int = Field(gt=0, strict=True)
    gpus: int = Field(ge=0, strict=True)
    allow_internet: bool
    mcp_servers: tuple[str, ...] = ()
    env: tuple[EnvironmentBindingRef, ...] = ()
    agent_timeout_sec: float = Field(gt=0, strict=True)
    verifier_timeout_sec: float = Field(gt=0, strict=True)

    @property
    def claim_image_identity_valid(self) -> bool:
        """Whether this image is immutable enough for a claim run."""

        return self.docker_image_digest is not None

    @model_validator(mode="after")
    def names_are_unique(self) -> "ResourceSpec":
        if any(not server.strip() for server in self.mcp_servers):
            raise ValueError("mcp_servers must contain non-empty names")
        if len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError("mcp_servers must be unique")
        env_names = [binding.name for binding in self.env]
        if len(set(env_names)) != len(env_names):
            raise ValueError("environment binding names must be unique")
        return self


class CredentialRef(StrictModel):
    """Identity-bearing credential digest; secret values are never serialized."""

    name: str = Field(pattern=ID_PATTERN)
    value_sha256: str = Field(pattern=SHA256_PATTERN)
    secret: Literal[True]
    source_file: str = Field(min_length=1)

    def identity_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value_sha256": self.value_sha256,
            "secret": self.secret,
        }


class ProviderBinding(StrictModel):
    """Resolved provider, transport, model, and credential identity."""

    provider_id: str = Field(pattern=ID_PATTERN)
    base_url: str = Field(min_length=1)
    wire_api: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    credential_ref: CredentialRef

    @field_validator("base_url")
    @classmethod
    def base_url_is_secret_free_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        return value


class NetworkObservationMode(str, Enum):
    active_probe = "active_probe"
    connection_log = "connection_log"
    unobservable = "unobservable"


class NetworkPolicySource(str, Enum):
    backend_artifact = "backend_artifact"
    case_set_artifact = "case_set_artifact"


class NetworkBoundary(str, Enum):
    process = "process"
    task_container = "task_container"


class ResolvedNetworkPolicy(StrictModel):
    """Concrete adapter-resolved network policy bound to one executed case."""

    resolver_adapter: str = Field(pattern=ADAPTER_PATTERN)
    execution_adapter: str = Field(pattern=ADAPTER_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    boundary: NetworkBoundary
    allow_internet: bool
    required_observation: NetworkObservationMode
    source: NetworkPolicySource
    source_artifact_digest: str = Field(pattern=SHA256_PATTERN)


class NetworkEndpointRecord(StrictModel):
    protocol: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9+.-]*$")
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535, strict=True)
    outcome: str = Field(min_length=1)

    @field_validator("host")
    @classmethod
    def host_contains_no_url_or_credentials(cls, value: str) -> str:
        if (
            any(character.isspace() for character in value)
            or any(marker in value for marker in ("://", "/", "?", "#", "@", "="))
        ):
            raise ValueError("network endpoint host must not contain URL data or credentials")
        return value

    @field_validator("outcome")
    @classmethod
    def outcome_contains_no_url_or_credentials(cls, value: str) -> str:
        if any(marker in value for marker in ("://", "?", "@", "=")):
            raise ValueError("network endpoint outcome must not contain URL or credential data")
        return value


class NetworkObservation(StrictModel):
    """Observed network behavior bound to a resolved, content-addressed policy.

    ``declared_allow_internet`` is a redundant observation-side cross-check;
    the bound ``ResolvedNetworkPolicy`` remains authoritative. A deny policy
    requires an active failed-egress probe; unobservable fails isolation.
    """

    policy_digest: str = Field(pattern=SHA256_PATTERN)
    declared_allow_internet: bool
    mode: NetworkObservationMode
    egress_attempted: bool
    egress_succeeded: bool
    reached_endpoints: tuple[NetworkEndpointRecord, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()

    @property
    def claim_isolation_valid(self) -> bool:
        """Whether this observation can substantiate claim-run isolation."""

        if self.mode == NetworkObservationMode.unobservable:
            return False
        if not self.declared_allow_internet:
            return (
                self.mode == NetworkObservationMode.active_probe
                and self.egress_attempted
                and not self.egress_succeeded
            )
        return True

    @model_validator(mode="after")
    def observation_is_coherent(self) -> "NetworkObservation":
        if self.egress_succeeded and not self.egress_attempted:
            raise ValueError("successful egress requires an egress attempt")
        if self.mode == NetworkObservationMode.active_probe and not self.egress_attempted:
            raise ValueError("active_probe requires egress_attempted=true")
        if self.mode == NetworkObservationMode.unobservable and (
            self.egress_attempted or self.egress_succeeded or self.reached_endpoints
        ):
            raise ValueError("unobservable network mode cannot claim observed activity")
        return self


class SubjectKind(str, Enum):
    """Resolved kind of the subject that actually entered an execution path."""

    hcp_harness = "hcp_harness"
    opaque_agent = "opaque_agent"
    evolver = "evolver"
    meta_evolver = "meta_evolver"
    fake = "fake"


class JournalRecord(StrictModel):
    format: Literal["harnessx-journal-v2"]
    session_id: str = Field(pattern=ID_PATTERN)
    run_ids: tuple[str, ...]
    segment_refs: tuple[ArtifactRef, ...]
    trace_refs: tuple[ArtifactRef, ...]
    state_refs: tuple[ArtifactRef, ...]
    session_index_ref: ArtifactRef

    @field_validator("run_ids")
    @classmethod
    def run_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("journal run_ids must be valid BMP ids")
        if len(set(values)) != len(values):
            raise ValueError("journal run_ids must be unique")
        return values


class SystemPromptRecord(StrictModel):
    step_id: str = Field(pattern=ID_PATTERN)
    prompt_ref: ArtifactRef


class WorkspaceRecord(StrictModel):
    namespace: str = Field(min_length=1)
    setup_refs: tuple[ArtifactRef, ...]
    state_refs: tuple[ArtifactRef, ...]
    journal: JournalRecord


class ScoringKind(str, Enum):
    """Benchmark-owned verdict semantics.

    Continuous scoring supports metric/effect claims but not pass-rate claims;
    downstream compilation must reject pass-rate estimands without a threshold.
    """

    binary = "binary"
    continuous = "continuous"


def _validate_scoring_semantics(
    scoring_kind: ScoringKind,
    authoritative_reward_metric: str | None,
    reward_pass_value: float | None,
) -> None:
    if authoritative_reward_metric is None:
        raise ValueError("authoritative_reward_metric is required")
    if scoring_kind == ScoringKind.binary and reward_pass_value is None:
        raise ValueError("binary scoring requires reward_pass_value")
    if scoring_kind == ScoringKind.continuous and reward_pass_value is not None:
        raise ValueError("continuous scoring forbids reward_pass_value")


class TaskSuiteBenchmarkSpec(SourceRegistryEntry):
    kind: Literal["task_suite"]
    task_manifest: str = Field(min_length=1)
    verifier: str = Field(min_length=1)
    # Benchmark-owned scoring semantics; never configure these on a backend.
    scoring_kind: ScoringKind
    authoritative_reward_metric: str = Field(min_length=1)
    reward_pass_value: float | None = None

    @field_validator("task_manifest")
    @classmethod
    def task_manifest_is_relative(cls, value: str) -> str:
        return _validate_logical_relative_path(value, field_name="task_manifest")

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "TaskSuiteBenchmarkSpec":
        _validate_scoring_semantics(
            self.scoring_kind,
            self.authoritative_reward_metric,
            self.reward_pass_value,
        )
        return self


class ToolAgentSuiteBenchmarkSpec(SourceRegistryEntry):
    kind: Literal["tool_agent_suite"]
    task_root: str = Field(min_length=1)
    input_contract: str = Field(min_length=1)
    output_contract: tuple[str, ...] = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    # Benchmark-owned scoring semantics; never configure these on a backend.
    scoring_kind: ScoringKind
    authoritative_reward_metric: str = Field(min_length=1)
    reward_pass_value: float | None = None

    @field_validator("task_root")
    @classmethod
    def task_root_is_relative(cls, value: str) -> str:
        return _validate_logical_relative_path(value, field_name="task_root")

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "ToolAgentSuiteBenchmarkSpec":
        _validate_scoring_semantics(
            self.scoring_kind,
            self.authoritative_reward_metric,
            self.reward_pass_value,
        )
        return self


class CustomBenchmarkSpec(SourceRegistryEntry):
    """Generic benchmark declaration owned by an external BMP adapter."""

    kind: Literal["custom"]
    content_globs: tuple[str, ...] = Field(min_length=1)
    verifier: str = Field(min_length=1)
    scoring_kind: ScoringKind
    authoritative_reward_metric: str = Field(min_length=1)
    reward_pass_value: float | None = None
    config: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("content_globs")
    @classmethod
    def content_globs_are_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("custom benchmark content_globs must be unique")
        for value in values:
            _validate_logical_relative_path(value, field_name="content_globs")
        return values

    @field_validator("config", mode="before")
    @classmethod
    def config_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(value or {}, field_name="CustomBenchmarkSpec.config")

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "CustomBenchmarkSpec":
        _validate_scoring_semantics(
            self.scoring_kind,
            self.authoritative_reward_metric,
            self.reward_pass_value,
        )
        return self


BenchmarkSpec = Annotated[
    Union[TaskSuiteBenchmarkSpec, ToolAgentSuiteBenchmarkSpec, CustomBenchmarkSpec],
    Field(discriminator="kind"),
]
BenchmarkSpecAdapter = TypeAdapter(BenchmarkSpec)


class ArtifactIdentity(StrictModel):
    """Compiler-generated identity shared by resolved registry artifacts."""

    artifact_digest: str = Field(pattern=SHA256_PATTERN)


class AbsoluteSourceArtifact(ArtifactIdentity):
    source: str = Field(min_length=1)
    source_content_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("source")
    @classmethod
    def source_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("artifact source must be an absolute path")
        return value


class TaskSuiteBenchmarkArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["task_suite"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    task_manifest: str = Field(min_length=1)
    verifier: str = Field(min_length=1)
    scoring_kind: ScoringKind
    authoritative_reward_metric: str = Field(min_length=1)
    reward_pass_value: float | None = None

    @field_validator("task_manifest")
    @classmethod
    def task_manifest_is_relative(cls, value: str) -> str:
        return _validate_logical_relative_path(value, field_name="task_manifest")

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "TaskSuiteBenchmarkArtifact":
        _validate_scoring_semantics(
            self.scoring_kind,
            self.authoritative_reward_metric,
            self.reward_pass_value,
        )
        return self


class ToolAgentSuiteBenchmarkArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["tool_agent_suite"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    task_root: str = Field(min_length=1)
    input_contract: str = Field(min_length=1)
    output_contract: tuple[str, ...] = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    scoring_kind: ScoringKind
    authoritative_reward_metric: str = Field(min_length=1)
    reward_pass_value: float | None = None

    @field_validator("task_root")
    @classmethod
    def task_root_is_relative(cls, value: str) -> str:
        return _validate_logical_relative_path(value, field_name="task_root")

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "ToolAgentSuiteBenchmarkArtifact":
        _validate_scoring_semantics(
            self.scoring_kind,
            self.authoritative_reward_metric,
            self.reward_pass_value,
        )
        return self


class CustomBenchmarkArtifact(AbsoluteSourceArtifact):
    """Resolved form of a benchmark implemented by an external adapter."""

    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["custom"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    content_globs: tuple[str, ...] = Field(min_length=1)
    verifier: str = Field(min_length=1)
    scoring_kind: ScoringKind
    authoritative_reward_metric: str = Field(min_length=1)
    reward_pass_value: float | None = None
    config: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("content_globs")
    @classmethod
    def content_globs_are_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("custom benchmark content_globs must be unique")
        for value in values:
            _validate_logical_relative_path(value, field_name="content_globs")
        return values

    @field_validator("config", mode="before")
    @classmethod
    def config_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(value or {}, field_name="CustomBenchmarkArtifact.config")

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "CustomBenchmarkArtifact":
        _validate_scoring_semantics(
            self.scoring_kind,
            self.authoritative_reward_metric,
            self.reward_pass_value,
        )
        return self


BenchmarkArtifact = Annotated[
    Union[TaskSuiteBenchmarkArtifact, ToolAgentSuiteBenchmarkArtifact, CustomBenchmarkArtifact],
    Field(discriminator="kind"),
]
BenchmarkArtifactAdapter = TypeAdapter(BenchmarkArtifact)


class AssemblySidecarRef(StrictModel):
    """Opaque reference to the assembly sidecar produced by ``magenta_hcp``.

    BMP records path and digest for provenance but does not interpret contents.
    """

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    sidecar_schema_version: str = Field(default="0.1", min_length=1)

    @field_validator("path")
    @classmethod
    def sidecar_path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("sidecar path must be absolute")
        return value


class AssemblySubjectSpec(SourceRegistryEntry):
    kind: Literal["hcp_harness"]
    assembly_profile: str = Field(default="default", min_length=1)
    emits_trace: bool = False


class OpaqueAgentSubjectSpec(SourceRegistryEntry):
    kind: Literal["opaque_agent"]
    entrypoint: str = Field(min_length=1)
    launch_argv: tuple[str, ...] | None = None
    interface: str = Field(min_length=1)
    emits_trace: bool = False

    @model_validator(mode="after")
    def launch_argv_matches_entrypoint(self) -> "OpaqueAgentSubjectSpec":
        if self.launch_argv is not None:
            if not self.launch_argv or any(not item for item in self.launch_argv):
                raise ValueError("launch_argv must contain non-empty arguments")
            if self.launch_argv[0] != self.entrypoint:
                raise ValueError("launch_argv[0] must equal entrypoint")
        return self


class EvolverSubjectSpec(SourceRegistryEntry):
    kind: Literal["evolver"]
    target: Literal["harness"]
    emits_trace: bool = False


class MetaEvolverSubjectSpec(SourceRegistryEntry):
    kind: Literal["meta_evolver"]
    target: Literal["evolver"]
    emits_trace: bool = False


class FakeSubjectSpec(StrictModel):
    """Deterministic subject reserved for protocol conformance tests."""

    kind: Literal["fake"]
    id: str = Field(pattern=ID_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    adapter: Literal["fake"] = "fake"
    fixed_answer: str = "BMP_OK"
    fault_mode: Literal[
        "none",
        "no_output",
        "invalid_output",
        "timeout",
        "agent_error",
        "harness_fault",
        "verifier_error",
        "infra_error",
        "unsupported",
    ] = "none"


SubjectSpec = Annotated[
    Union[
        AssemblySubjectSpec,
        OpaqueAgentSubjectSpec,
        EvolverSubjectSpec,
        MetaEvolverSubjectSpec,
        FakeSubjectSpec,
    ],
    Field(discriminator="kind"),
]
SubjectSpecAdapter = TypeAdapter(SubjectSpec)


class AssemblySubjectArtifact(AbsoluteSourceArtifact):
    """Pinned harness whose assembly evidence remains opaque to BMP core."""

    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["hcp_harness"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    assembly_profile: str = Field(default="default", min_length=1)
    sidecar_ref: AssemblySidecarRef | None = None
    emits_trace: bool = False


class OpaqueAgentSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["opaque_agent"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    entrypoint: str = Field(min_length=1)
    launch_argv: tuple[str, ...] | None = None
    interface: str = Field(min_length=1)
    emits_trace: bool = False

    @model_validator(mode="after")
    def launch_argv_matches_entrypoint(self) -> "OpaqueAgentSubjectArtifact":
        if self.launch_argv is not None:
            if not self.launch_argv or any(not item for item in self.launch_argv):
                raise ValueError("launch_argv must contain non-empty arguments")
            if self.launch_argv[0] != self.entrypoint:
                raise ValueError("launch_argv[0] must equal entrypoint")
        return self


class EvolverSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["evolver"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    target: Literal["harness"]
    emits_trace: bool = False


class MetaEvolverSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["meta_evolver"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    target: Literal["evolver"]
    emits_trace: bool = False


class FakeSubjectArtifact(ArtifactIdentity):
    kind: Literal["fake"]
    id: str = Field(pattern=ID_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    adapter: Literal["fake"] = "fake"
    fixed_answer: str = "BMP_OK"
    fault_mode: Literal[
        "none",
        "no_output",
        "invalid_output",
        "timeout",
        "agent_error",
        "harness_fault",
        "verifier_error",
        "infra_error",
        "unsupported",
    ] = "none"


SubjectArtifact = Annotated[
    Union[
        AssemblySubjectArtifact,
        OpaqueAgentSubjectArtifact,
        EvolverSubjectArtifact,
        MetaEvolverSubjectArtifact,
        FakeSubjectArtifact,
    ],
    Field(discriminator="kind"),
]
SubjectArtifactAdapter = TypeAdapter(SubjectArtifact)


class Budget(StrictModel):
    """Hard limits for a run; absent fields mean that limit is unspecified."""

    max_tokens: int | None = Field(default=None, ge=0)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_cost: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def at_least_one_limit(self) -> "Budget":
        if all(value is None for value in (self.max_tokens, self.max_wall_seconds, self.max_cost)):
            raise ValueError("budget must declare at least one limit")
        return self


class MountSpec(StrictModel):
    """A content-addressed host-to-runtime mount declaration."""

    host_path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    container_path: str = Field(min_length=1)
    read_only: bool = True

    @field_validator("host_path", "container_path")
    @classmethod
    def mount_paths_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("mount paths must be absolute")
        return value

    def identity_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "container_path": self.container_path,
            "read_only": self.read_only,
            "content_sha256": self.content_sha256,
        }


class EnvironmentSpec(StrictModel):
    """Reproducible environment requirements; interpreter pin is mandatory."""

    id: str = Field(pattern=ID_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    python_version: str = Field(pattern=r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
    packages: tuple[str, ...] = ()
    env_var_names: tuple[str, ...] = ()
    mounts: tuple[MountSpec, ...] = ()
    build_timeout_seconds: float = Field(default=600.0, gt=0, strict=True)

    @field_validator("packages")
    @classmethod
    def package_requirements_are_nonempty(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("package requirements must be non-empty")
        if len(set(values)) != len(values):
            raise ValueError("package requirements must be unique")
        return values

    @field_validator("env_var_names")
    @classmethod
    def env_vars_are_names_only(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        name_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        invalid = [value for value in values if not name_pattern.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid environment variable names: {invalid}")
        if len(set(values)) != len(values):
            raise ValueError("environment variable names must be unique")
        return values

    def identity_data(self) -> dict[str, Any]:
        """Return path-independent environment identity.

        Host mount paths are provenance.  The declared content digest and the
        runtime-visible mount shape identify the environment across checkouts.
        """

        return {
            "id": self.id,
            "bmp_version": self.bmp_version,
            "python_version": self.python_version,
            "packages": list(self.packages),
            "env_var_names": list(self.env_var_names),
            "mounts": [mount.identity_data() for mount in self.mounts],
            "build_timeout_seconds": self.build_timeout_seconds,
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PackageRecord(StrictModel):
    """Installed package name/version observed by an environment builder.

    A package wheel hash is not yet verified by the environment builder; add it
    when the builder provides content-addressed install receipts.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class EnvironmentReceipt(StrictModel):
    """Observed environment identity recorded in execution provenance."""

    spec_id: str = Field(pattern=ID_PATTERN)
    spec_digest: str = Field(pattern=SHA256_PATTERN)
    python_executable: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    installed_packages: tuple[PackageRecord, ...]
    build_duration_seconds: float = Field(ge=0, strict=True)
    built_at: str = Field(min_length=1)

    @field_validator("python_executable")
    @classmethod
    def executable_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("python_executable must be an absolute path")
        return value

    @field_validator("built_at")
    @classmethod
    def built_at_must_be_timezone_aware_iso8601(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("built_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("built_at must include a timezone")
        return value

    @model_validator(mode="after")
    def package_names_are_unique(self) -> "EnvironmentReceipt":
        normalized = [package.name.casefold() for package in self.installed_packages]
        if len(set(normalized)) != len(normalized):
            raise ValueError("installed package names must be unique")
        return self


class BackendSpec(RegistryEntry):
    """Pinned execution backend registry entry."""

    image: str | None = Field(default=None, min_length=1)
    executable: str | None = Field(default=None, min_length=1)
    version: str | None = Field(default=None, min_length=1)
    digest: str | None = Field(default=None, min_length=1)
    # Warning: never use for API keys or secrets; names only.
    defaults: Mapping[str, Any] = Field(default_factory=dict)
    environment: EnvironmentSpec | None = None

    @field_validator("defaults", mode="before")
    @classmethod
    def defaults_reject_secret_keys(cls, value: Any) -> Any:
        return _reject_secret_like_keys(value, field_name="BackendSpec.defaults")

    @model_validator(mode="after")
    def adapter_fields_match_read_set(self) -> "BackendSpec":
        expected_kind = {
            "harbor": "local",
            "harbor-shim": "local",
            "subprocess": "local",
            "fake": "local",
            "aose-docker": "container",
        }.get(self.adapter)
        if expected_kind is not None and self.kind != expected_kind:
            raise ValueError(
                f"backend adapter {self.adapter!r} requires kind={expected_kind!r}"
            )
        if self.adapter in {"harbor", "harbor-shim"}:
            if self.executable is None or self.version is None or self.digest is None:
                raise ValueError("harbor requires executable, version, and digest")
            if self.image is not None:
                raise ValueError("harbor forbids backend image; task identity owns images")
            if self.adapter == "harbor" and re.fullmatch(SHA256_PATTERN, self.digest) is None:
                raise ValueError("harbor digest must be lowercase SHA-256")
        elif self.adapter == "subprocess":
            if self.executable is None or self.digest is None:
                raise ValueError("subprocess requires executable and digest")
            if re.fullmatch(SHA256_PATTERN, self.digest) is None:
                raise ValueError("subprocess digest must be lowercase SHA-256")
            if self.image is not None or self.version is not None:
                raise ValueError("subprocess forbids image and version")
        elif self.adapter == "aose-docker":
            if self.image is None or self.digest is None:
                raise ValueError("aose-docker requires image and digest")
            if re.fullmatch(OCI_SHA256_PATTERN, self.image) is None or re.fullmatch(SHA256_PATTERN, self.digest) is None:
                raise ValueError("aose-docker image/digest must be lowercase SHA-256")
            if self.executable is not None or self.version is not None:
                raise ValueError("aose-docker forbids executable and version")
            if self.image.removeprefix("sha256:") != self.digest:
                raise ValueError("aose-docker image and digest must identify the same image")
        elif self.adapter == "fake":
            if any(
                value is not None
                for value in (self.image, self.executable, self.version, self.digest)
            ):
                raise ValueError("fake forbids image, executable, version, and digest")
        return self


def _validate_logical_relative_path(value: str, *, field_name: str) -> str:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"{field_name} must be a normalized relative path")
    return normalized


def _validate_execution_model_name(value: str) -> str:
    if value.startswith("none/") and value not in {
        "none/deterministic",
        "none/echo",
    }:
        raise ValueError(
            "model none/* suffix must be one of none/deterministic or none/echo"
        )
    return value


ProtocolKind = Literal[
    "test_time_scaling",
    "mechanism_validation",
    "benchmark_evaluation",
]


class ProtocolSpec(RegistryEntry):
    """Execution schedule defaults resolved before local execution overrides."""

    kind: ProtocolKind
    rollouts_per_case: int = Field(default=1, ge=1)
    parallelism: int = Field(default=1, ge=1)
    case_order: Literal[
        "fixed", "seeded_random", "random", "custom", "explicit"
    ] = "fixed"
    explicit_case_ids: tuple[str, ...] = ()
    adaptive_budget: bool = False
    candidate_selection: Literal["single", "exact", "best_of_n"]
    state_reset: Literal["per_case", "per_rollout", "never"] = "per_case"
    budget: Budget | None = None
    checkpoint_policy: Literal["disabled", "save", "resume", "save_and_resume"] = "disabled"
    deterministic_conformance: bool = False

    @field_validator("explicit_case_ids")
    @classmethod
    def explicit_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("explicit case ids must be valid BMP ids")
        if len(set(values)) != len(values):
            raise ValueError("explicit case ids must be unique")
        return values

    @model_validator(mode="after")
    def explicit_ids_match_order_policy(self) -> "ProtocolSpec":
        if self.case_order in {"custom", "explicit"} and not self.explicit_case_ids:
            raise ValueError(
                "explicit_case_ids must be non-empty when case_order is custom or explicit"
            )
        if self.case_order not in {"custom", "explicit"} and self.explicit_case_ids:
            raise ValueError(
                "explicit_case_ids are forbidden unless case_order is custom or explicit"
            )
        return self


class ExecutionSpec(StrictModel):
    """TOML execution declaration: registry references plus local overrides."""

    backend: str = Field(pattern=ID_PATTERN)
    model: str = Field(min_length=1)
    seed: int | None = None
    budget: Budget | None = None

    @field_validator("model")
    @classmethod
    def model_name_is_closed(cls, value: str) -> str:
        return _validate_execution_model_name(value)

    # Warning: never use for API keys or secrets; names only.
    backend_overrides: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("backend_overrides", mode="before")
    @classmethod
    def overrides_reject_secret_keys(cls, value: Any) -> Any:
        return _reject_secret_like_keys(
            value,
            field_name="ExecutionSpec.backend_overrides",
        )


class ResolvedExecutionSpec(StrictModel):
    """Execution contract with registry references inlined.

    provider_binding is optional only until the CLI-agent adapter resolves it;
    a model-scope claim MUST compile-reject when provider_binding is None.
    """

    backend: BackendSpec
    model: str = Field(min_length=1)
    provider_binding: ProviderBinding | None = None
    seed: int | None = None
    budget: Budget
    protocol: ProtocolSpec | None = None

    @field_validator("model")
    @classmethod
    def model_name_is_closed(cls, value: str) -> str:
        return _validate_execution_model_name(value)

    @model_validator(mode="after")
    def seed_matches_case_order(self) -> "ResolvedExecutionSpec":
        case_order = None if self.protocol is None else self.protocol.case_order
        if case_order == "seeded_random" and self.seed is None:
            raise ValueError("seed is required when case_order=seeded_random")
        if case_order != "seeded_random" and self.seed is not None:
            raise ValueError("seed is forbidden unless case_order=seeded_random")
        return self


class CaseArtifact(StrictModel):
    case_id: str = Field(pattern=ID_PATTERN)
    public_input_ref: ArtifactRef
    task_contract_refs: tuple[ArtifactRef, ...] = ()
    verifier_contract_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def refs_are_unique(self) -> "CaseArtifact":
        refs = (
            self.public_input_ref,
            *self.task_contract_refs,
            *self.verifier_contract_refs,
        )
        identities = [(ref.sha256, ref.size_bytes) for ref in refs]
        if len(set(identities)) != len(identities):
            raise ValueError("case artifact refs must be content-unique")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "public_input_ref": self.public_input_ref.identity_data(),
            "task_contract_refs": [
                ref.identity_data() for ref in self.task_contract_refs
            ],
            "verifier_contract_refs": [
                ref.identity_data() for ref in self.verifier_contract_refs
            ],
        }


class CaseSetArtifact(StrictModel):
    benchmark_id: str = Field(pattern=ID_PATTERN)
    benchmark_digest: str = Field(pattern=SHA256_PATTERN)
    loader_adapter: str = Field(pattern=ADAPTER_PATTERN)
    loader_digest: str = Field(pattern=SHA256_PATTERN)
    selection_method: Literal["all_cases", "explicit_case_ids"] = "all_cases"
    case_order: Literal[
        "fixed", "seeded_random", "random", "custom", "explicit"
    ] = "fixed"
    order_seed: int | None = None
    source_content_digest: str = Field(pattern=SHA256_PATTERN)
    source_content_refs: tuple[ArtifactRef, ...]
    ordered_case_ids: tuple[str, ...]
    cases: tuple[CaseArtifact, ...]

    @model_validator(mode="after")
    def order_exactly_matches_cases(self) -> "CaseSetArtifact":
        case_ids = tuple(case.case_id for case in self.cases)
        if not case_ids:
            raise ValueError("case set must contain at least one case")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case set ids must be unique")
        if self.ordered_case_ids != case_ids:
            raise ValueError("ordered_case_ids must exactly match case order")
        if self.case_order == "seeded_random" and self.order_seed is None:
            raise ValueError("order_seed is required for seeded_random case order")
        if self.case_order != "seeded_random" and self.order_seed is not None:
            raise ValueError("order_seed is forbidden for non-seeded case order")
        if (
            self.case_order in {"custom", "explicit"}
            and self.selection_method != "explicit_case_ids"
        ):
            raise ValueError(
                "explicit case order requires selection_method=explicit_case_ids"
            )
        if (
            self.case_order not in {"custom", "explicit"}
            and self.selection_method == "explicit_case_ids"
        ):
            raise ValueError(
                "selection_method=explicit_case_ids requires explicit case order"
            )
        source_identities = [
            (ref.sha256, ref.size_bytes) for ref in self.source_content_refs
        ]
        if (
            not source_identities
            or len(set(source_identities)) != len(source_identities)
        ):
            raise ValueError(
                "source content refs must be non-empty and content-unique"
            )
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "benchmark_digest": self.benchmark_digest,
            "loader_adapter": self.loader_adapter,
            "loader_digest": self.loader_digest,
            "selection_method": self.selection_method,
            "case_order": self.case_order,
            "order_seed": self.order_seed,
            "source_content_digest": self.source_content_digest,
            "source_content_refs": [
                ref.identity_data() for ref in self.source_content_refs
            ],
            "ordered_case_ids": list(self.ordered_case_ids),
            "cases": [case.identity_data() for case in self.cases],
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CaseSetActivationReceipt(StrictModel):
    case_set_ref: ArtifactRef
    case_set_digest: str = Field(pattern=SHA256_PATTERN)
    loader_adapter: str = Field(pattern=ADAPTER_PATTERN)
    loader_digest: str = Field(pattern=SHA256_PATTERN)
    ordered_case_ids: tuple[str, ...]

    @field_validator("ordered_case_ids")
    @classmethod
    def ordered_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("activated case ids must be non-empty and unique")
        return value


class ClaimScope(str, Enum):
    component = "component"
    whole_harness = "whole_harness"
    model = "model"
    checkpoint = "checkpoint"
    evolver = "evolver"
    meta_evolver = "meta_evolver"
    schedule = "schedule"
    ablation = "ablation"
    hyperparameter = "hyperparameter"
    conformance = "conformance"


class RunPurpose(str, Enum):
    exploratory = "exploratory"
    claim = "claim"


SUBJECT_KIND_SCOPE_MATRIX: Mapping[str, frozenset[ClaimScope]] = MappingProxyType({
    "hcp_harness": frozenset(
        {
            ClaimScope.component,
            ClaimScope.whole_harness,
            ClaimScope.model,
            ClaimScope.checkpoint,
            ClaimScope.schedule,
            ClaimScope.ablation,
            ClaimScope.hyperparameter,
        }
    ),
    "opaque_agent": frozenset(
        {
            ClaimScope.whole_harness,
            ClaimScope.model,
            ClaimScope.checkpoint,
            ClaimScope.schedule,
            ClaimScope.hyperparameter,
        }
    ),
    "evolver": frozenset(
        {
            ClaimScope.model,
            ClaimScope.checkpoint,
            ClaimScope.evolver,
            ClaimScope.schedule,
            ClaimScope.hyperparameter,
        }
    ),
    "meta_evolver": frozenset(
        {
            ClaimScope.model,
            ClaimScope.checkpoint,
            ClaimScope.meta_evolver,
            ClaimScope.schedule,
            ClaimScope.hyperparameter,
        }
    ),
    "fake": frozenset({ClaimScope.conformance}),
})


class ExperimentContrast(StrictModel):
    mode: Literal["one_factor", "all_arms"]
    control_id: str | None = Field(default=None, pattern=ID_PATTERN)
    treatment_id: str | None = Field(default=None, pattern=ID_PATTERN)
    counterbalanced: bool

    @model_validator(mode="after")
    def contrast_shape_matches_mode(self) -> "ExperimentContrast":
        if self.mode == "one_factor":
            if self.control_id is None or self.treatment_id is None:
                raise ValueError("one_factor contrast requires control_id and treatment_id")
            if self.control_id == self.treatment_id:
                raise ValueError("control_id and treatment_id must be distinct")
        elif (
            self.control_id is not None
            or self.treatment_id is not None
            or self.counterbalanced
        ):
            raise ValueError("all_arms contrast forbids arm filtering and counterbalancing")
        return self


class ClaimDesign(StrictModel):
    """Identity-bearing declaration of a run's attribution and purpose."""

    scope: ClaimScope
    purpose: RunPurpose
    vary: tuple[str, ...]

    @field_validator("vary")
    @classmethod
    def vary_paths_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("vary paths must be unique")
        return value


class TestOverrideReceipt(StrictModel):
    reason: str = Field(min_length=1)
    forced_purpose: Literal["exploratory"] = "exploratory"
    forced_scope: Literal["conformance"] = "conformance"


class ResolvedManifestMetadata(StrictModel):
    experiment_id: str = Field(pattern=ID_PATTERN)
    run_id: str = Field(pattern=ID_PATTERN)
    allowed_diff: tuple[str, ...] = ()
    factors: Mapping[str, Any] = Field(default_factory=dict)
    configuration: ConfigurationArtifact | None = None
    test_override: TestOverrideReceipt | None = None

    @field_validator("allowed_diff")
    @classmethod
    def validate_dotted_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        dotted_path = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*$")
        if len(set(value)) != len(value):
            raise ValueError("allowed_diff paths must be unique")
        invalid = [path for path in value if not dotted_path.fullmatch(path)]
        if invalid:
            raise ValueError(f"invalid dotted allowed_diff paths: {invalid}")
        return value


class ResolvedBmpManifest(StrictModel):
    IDENTITY_EXCLUDE: ClassVar[frozenset[str]] = IDENTITY_EXCLUDE

    bmp_version: Literal["0.1"] = BMP_VERSION
    benchmark: BenchmarkArtifact
    subject: SubjectArtifact
    execution: ResolvedExecutionSpec
    claim_design: ClaimDesign
    contrast: ExperimentContrast
    metadata: ResolvedManifestMetadata
    created_at: str | None = None
    wall_clock_start: str | None = None
    wall_clock_end: str | None = None
    record_root: str | None = None
    resume_count: int = Field(default=0, ge=0)
    runner_invocation_id: str | None = None

    def identity_data(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude=self.IDENTITY_EXCLUDE)
        data["benchmark"].pop("source", None)
        data["subject"].pop("source", None)
        environment = data["execution"]["backend"].get("environment")
        source_environment = self.execution.backend.environment
        if environment is not None and source_environment is not None:
            environment["mounts"] = [
                mount.identity_data() for mount in source_environment.mounts
            ]
        binding = data["execution"].get("provider_binding")
        source_binding = self.execution.provider_binding
        if binding is not None and source_binding is not None:
            binding["credential_ref"] = source_binding.credential_ref.identity_data()
        source_script_ref = getattr(self.subject, "script_ref", None)
        if source_script_ref is not None:
            data["subject"]["script_ref"] = source_script_ref.identity_data()
        return data

    def canonical_digest(self) -> str:
        canonical = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class RunStatus(str, Enum):
    pass_ = "pass"
    verified_fail = "verified_fail"
    scored = "scored"
    no_output = "no_output"
    invalid_output = "invalid_output"
    timeout = "timeout"
    agent_error = "agent_error"
    harness_fault = "harness_fault"
    verifier_error = "verifier_error"
    infra_error = "infra_error"
    unsupported = "unsupported"


class VerifierEvidence(StrictModel):
    verifier: str = Field(min_length=1)
    passed: bool | None = None
    score: float | None = None
    metrics: Mapping[str, float] = Field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    # Warning: never use for API keys or secrets; names only.
    details: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("details", mode="before")
    @classmethod
    def details_reject_secret_keys(cls, value: Any) -> Any:
        return _reject_secret_like_keys(value, field_name="VerifierEvidence.details")


class UsageRecord(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    wall_clock_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def token_total_is_consistent(self) -> "UsageRecord":
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class ProvenanceRecord(StrictModel):
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    runner_digest: str = Field(pattern=SHA256_PATTERN)
    benchmark_digest: str = Field(pattern=SHA256_PATTERN)
    subject_digest: str = Field(pattern=SHA256_PATTERN)
    backend_digest: str = Field(min_length=1)
    trace_emission_claimed: bool = False
    executable: str | None = None
    executable_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    distribution: str | None = None
    version: str | None = None
    commit: str | None = None
    image_digest: str | None = None
    backend_kind: str | None = Field(default=None, min_length=1)
    network_mode: str | None = Field(default=None, min_length=1)
    workspace_namespace: str | None = Field(default=None, min_length=1)
    environment_receipt: EnvironmentReceipt | None = None
    container_receipt_ref: ArtifactRef | None = None
    test_override: TestOverrideReceipt | None = None

    @model_validator(mode="after")
    def no_equals_in_direct_strings(self) -> "ProvenanceRecord":
        for field_name, value in self.__dict__.items():
            if isinstance(value, str) and "=" in value:
                raise ValueError(
                    f"ProvenanceRecord.{field_name} must not contain '=' "
                    "(possible key=value secret)"
                )
        return self


class EvidenceBundle(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "status": {
                                "enum": ["pass", "verified_fail", "scored"]
                            }
                        },
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "output_refs": {"minItems": 1},
                            "verifier_evidence": {"type": "object"},
                        },
                        "required": ["output_refs", "verifier_evidence"],
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "pass"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "verifier_evidence": {
                                "properties": {"passed": {"const": True}},
                                "required": ["passed"],
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "verified_fail"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "verifier_evidence": {
                                "properties": {"passed": {"const": False}},
                                "required": ["passed"],
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "scored"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "verifier_evidence": {
                                "properties": {
                                    "passed": {"type": "null"},
                                    "score": {"type": "number"},
                                },
                                "required": ["passed", "score"],
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {
                            "provenance": {
                                "properties": {
                                    "trace_emission_claimed": {"const": True}
                                },
                                "required": ["trace_emission_claimed"],
                            }
                        },
                        "required": ["provenance"],
                    },
                    "then": {
                        "properties": {"trace_ref": {"type": "object"}},
                        "required": ["trace_ref"],
                    },
                },
            ]
        }
    )

    run_id: str = Field(pattern=ID_PATTERN)
    status: RunStatus
    output_refs: tuple[ArtifactRef, ...] = ()
    trace_ref: ArtifactRef | None = None
    checkpoint_ref: ArtifactRef | None = None
    log_refs: tuple[ArtifactRef, ...] = ()
    verifier_evidence: VerifierEvidence | None = None
    usage: UsageRecord | None = None
    network_policy: ResolvedNetworkPolicy | None = None
    network_observation: NetworkObservation | None = None
    provenance: ProvenanceRecord

    @model_validator(mode="after")
    def validate_status_evidence(self) -> "EvidenceBundle":
        scored_statuses = {
            RunStatus.pass_,
            RunStatus.verified_fail,
            RunStatus.scored,
        }
        if self.status in scored_statuses:
            if not self.output_refs:
                raise ValueError(f"status {self.status.value!r} requires output_refs")
            if self.verifier_evidence is None:
                raise ValueError(f"status {self.status.value!r} requires verifier_evidence")
        if self.status == RunStatus.pass_ and (
            self.verifier_evidence is None or self.verifier_evidence.passed is not True
        ):
            raise ValueError("status 'pass' requires verifier_evidence.passed=true")
        if self.status == RunStatus.verified_fail and (
            self.verifier_evidence is None or self.verifier_evidence.passed is not False
        ):
            raise ValueError("status 'verified_fail' requires verifier_evidence.passed=false")
        if self.status == RunStatus.scored and (
            self.verifier_evidence is None
            or self.verifier_evidence.passed is not None
            or self.verifier_evidence.score is None
        ):
            raise ValueError(
                "status 'scored' requires a score and no binary passed verdict"
            )
        if self.provenance.trace_emission_claimed and self.trace_ref is None:
            raise ValueError("trace_ref is required when the subject claims trace emission")
        return self


class BudgetAllocation(StrictModel):
    """Pre-launch token/cost allocation; wall time remains global."""

    max_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_cost: float | None = Field(default=None, ge=0, strict=True)

def _allocation_sums(total: BudgetAllocation, parts: tuple[BudgetAllocation, ...]) -> bool:
    for field_name in ("max_tokens", "max_cost"):
        value = getattr(total, field_name)
        values = [getattr(part, field_name) for part in parts]
        if value is None:
            if any(item is not None for item in values):
                return False
        elif any(item is None for item in values) or value != sum(values):
            return False
    return True


class CaseAllocation(StrictModel):
    """Per-case cap allocated before dividing it among attempts."""

    case_id: str = Field(pattern=ID_PATTERN)
    allocation_id: str = Field(pattern=ID_PATTERN)
    allocated: BudgetAllocation
    attempt_count: int = Field(ge=1, strict=True)


class AttemptContext(StrictModel):
    """Scheduler-derived context passed atomically to one backend attempt."""

    case_id: str = Field(pattern=ID_PATTERN)
    execution_run_id: str = Field(pattern=ID_PATTERN)
    attempt_index: int = Field(ge=0, strict=True)
    attempt_budget: BudgetAllocation
    remaining_global_budget: BudgetAllocation
    remaining_wall_seconds: float | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def attempt_fits_remaining_budget(self) -> "AttemptContext":
        for field_name in ("max_tokens", "max_cost"):
            attempt = getattr(self.attempt_budget, field_name)
            remaining = getattr(self.remaining_global_budget, field_name)
            if remaining is not None and (attempt is None or attempt > remaining):
                raise ValueError(
                    f"attempt {field_name} must not exceed remaining global budget"
                )
        return self


class AttemptAllocation(StrictModel):
    """Per-rollout cap reserved from a case allocation before launch."""

    attempt_id: str = Field(pattern=ID_PATTERN)
    case_allocation_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    allocated: BudgetAllocation
    reservation_sequence: int = Field(ge=0, strict=True)
    launched: bool
    launch_sequence: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def reservation_precedes_launch(self) -> "AttemptAllocation":
        if self.launched and self.launch_sequence is None:
            raise ValueError("launched attempt allocation requires launch_sequence")
        if not self.launched and self.launch_sequence is not None:
            raise ValueError("unlaunched attempt allocation must not have launch_sequence")
        if self.launched and self.reservation_sequence >= self.launch_sequence:
            raise ValueError("attempt allocation must be reserved before launch")
        return self


class BudgetDebit(StrictModel):
    """Measured leaf usage and returned unused cap at completion."""

    attempt_id: str = Field(pattern=ID_PATTERN)
    child_run_id: str = Field(pattern=ID_PATTERN)
    completion_sequence: int = Field(ge=1, strict=True)
    spent: UsageRecord
    released: BudgetAllocation
    budget_exceeded: bool = False


class AttemptExecution(StrictModel):
    """One launched scheduler attempt; unlaunched slots have no execution record."""

    attempt_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    attempt_index: int = Field(ge=0, strict=True)
    status: RunStatus
    evidence_bundle_ref: ArtifactRef | None
    debit: BudgetDebit | None
    selected: bool
    selection_reason: str | None = None
    reward_value: float | None = None
    reward_metric: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def launched_attempt_has_evidence(self) -> "AttemptExecution":
        if self.evidence_bundle_ref is None:
            raise ValueError("launched attempts require evidence_bundle_ref")
        if self.debit is None:
            raise ValueError("launched attempts require a budget debit")
        if self.debit.attempt_id != self.attempt_id:
            raise ValueError("attempt debit must match attempt_id")
        if self.debit.budget_exceeded and self.status != RunStatus.agent_error:
            raise ValueError("budget-exceeded attempts require agent_error status")
        if (self.reward_value is None) != (self.reward_metric is None):
            raise ValueError("reward_value and reward_metric must be provided together")
        return self


_USAGE_LEDGER_FIELDS = ("total_tokens", "cost")


def _usage_reconciles(total: UsageRecord, parts: tuple[UsageRecord, ...]) -> bool:
    for field_name in _USAGE_LEDGER_FIELDS:
        total_value = getattr(total, field_name)
        part_values = [getattr(part, field_name) for part in parts]
        if total_value is None or any(value is None for value in part_values):
            return False
        if total_value != sum(part_values):
            return False
    return True


class BudgetLedger(StrictModel):
    """Planned allocation hierarchy plus derived aggregate usage."""

    case_allocations: tuple[CaseAllocation, ...]
    attempt_allocations: tuple[AttemptAllocation, ...]
    aborted_at_exhaustion: bool
    aborted_children: tuple[str, ...]
    total_usage: UsageRecord
    parent_overhead: UsageRecord
    global_elapsed_wall_seconds: float = Field(ge=0, strict=True)
    reconciles_exactly: bool

    @model_validator(mode="after")
    def validate_ledger(self) -> "BudgetLedger":
        if self.total_usage.wall_clock_seconds is not None:
            raise ValueError(
                "BudgetLedger.total_usage must not sum attempt wall-clock seconds"
            )
        allocation_ids = [item.allocation_id for item in self.case_allocations]
        case_ids = [item.case_id for item in self.case_allocations]
        if len(set(allocation_ids)) != len(allocation_ids):
            raise ValueError("case allocation ids must be unique")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case allocations must be unique per case")
        attempt_ids = [item.attempt_id for item in self.attempt_allocations]
        reservation_sequences = [
            item.reservation_sequence for item in self.attempt_allocations
        ]
        launch_sequences = [
            item.launch_sequence for item in self.attempt_allocations if item.launched
        ]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("attempt allocation ids must be unique")
        if (
            len(set(reservation_sequences)) != len(reservation_sequences)
            or reservation_sequences != sorted(reservation_sequences)
        ):
            raise ValueError("attempt reservation sequences must increase monotonically")
        if (
            len(set(launch_sequences)) != len(launch_sequences)
            or launch_sequences != sorted(launch_sequences)
        ):
            raise ValueError("attempt launch sequences must increase monotonically")
        case_by_allocation_id = {
            item.allocation_id: item for item in self.case_allocations
        }
        attempts_by_case_allocation: dict[str, list[AttemptAllocation]] = {}
        for item in self.attempt_allocations:
            parent = case_by_allocation_id.get(item.case_allocation_id)
            if parent is None or parent.case_id != item.case_id:
                raise ValueError("attempt allocations must reference their case allocation")
            attempts_by_case_allocation.setdefault(item.case_allocation_id, []).append(item)
        for parent in self.case_allocations:
            children = attempts_by_case_allocation.get(parent.allocation_id, [])
            if len(children) != parent.attempt_count or not _allocation_sums(
                parent.allocated,
                tuple(child.allocated for child in children),
            ):
                raise ValueError("attempt allocations must exactly divide the case allocation")
        unlaunched_attempt_ids = {
            item.attempt_id for item in self.attempt_allocations if not item.launched
        }
        if len(set(self.aborted_children)) != len(self.aborted_children):
            raise ValueError("aborted child ids must be unique")
        if set(self.aborted_children) != unlaunched_attempt_ids:
            raise ValueError("aborted children must equal unlaunched attempt allocations")
        if bool(self.aborted_children) != self.aborted_at_exhaustion:
            raise ValueError("aborted_at_exhaustion must exactly reflect aborted children")
        return self


class CheckpointSaveReceipt(StrictModel):
    written_digest: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0, strict=True)
    write_completion_sequence: int = Field(ge=1, strict=True)
    path: str = Field(min_length=1, pattern=r"^/")

    @field_validator("path")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("checkpoint save path must be absolute")
        return value


class CheckpointLoadReceipt(StrictModel):
    loaded_checkpoint_digest: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_digest: str = Field(pattern=SHA256_PATTERN)
    schedule_receipt_digest: str = Field(pattern=SHA256_PATTERN)
    selected_bundle_digests: tuple[str, ...]

    @field_validator("selected_bundle_digests")
    @classmethod
    def selected_digests_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(SHA256_PATTERN, value) is None for value in values):
            raise ValueError("selected bundle digests must be SHA-256 values")
        if len(set(values)) != len(values):
            raise ValueError("selected bundle digests must be unique")
        return values


class ScheduleActivationReceipt(StrictModel):
    """Declared schedule compared with measured scheduler activation."""

    run_id: str = Field(pattern=ID_PATTERN)
    protocol_digest: str = Field(pattern=SHA256_PATTERN)
    scheduler_digest: str = Field(pattern=SHA256_PATTERN)
    pipeline_digest: str = Field(pattern=SHA256_PATTERN)
    reservation_policy: Literal["equal_division_per_case"]
    global_deadline_at: str | None = Field(default=None, min_length=1)
    declared_rollouts_per_case: int = Field(ge=1, strict=True)
    observed_attempt_count: int = Field(ge=0, strict=True)
    declared_parallelism: int = Field(ge=1, strict=True)
    observed_max_concurrency: int = Field(ge=0, strict=True)
    declared_case_order: Literal[
        "fixed", "seeded_random", "random", "custom", "explicit"
    ]
    observed_case_order: tuple[str, ...]
    declared_state_reset: Literal["per_case", "per_rollout", "never"]
    observed_state_reset_count: int = Field(ge=0, strict=True)
    declared_candidate_selection: str = Field(min_length=1)
    observed_selection_policy: str = Field(min_length=1)
    declared_checkpoint_policy: Literal[
        "disabled", "save", "resume", "save_and_resume"
    ]
    checkpoint_save_ref: CheckpointSaveReceipt | None = None
    checkpoint_load_ref: CheckpointLoadReceipt | None = None
    ancestor_schedule_receipt_ref: ArtifactRef | None = None
    order_seed: int | None = None
    attempts: tuple[AttemptExecution, ...]
    budget_ledger: BudgetLedger
    schedule_valid: bool
    mismatch_reasons: tuple[str, ...]

    @field_validator("global_deadline_at")
    @classmethod
    def deadline_is_timezone_aware_iso8601(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("global_deadline_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("global_deadline_at must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def validate_schedule_receipt(self) -> "ScheduleActivationReceipt":
        policy = self.declared_checkpoint_policy
        if self.schedule_valid:
            if policy == "resume":
                raise ValueError("checkpoint_policy=resume requires CheckpointLoadReceipt")
            if policy == "disabled" and (
                self.checkpoint_save_ref is not None or self.checkpoint_load_ref is not None
            ):
                raise ValueError("disabled checkpoint policy forbids save/load receipts")
            if policy == "save" and (
                self.checkpoint_save_ref is None or self.checkpoint_load_ref is not None
            ):
                raise ValueError(
                    "save checkpoint policy requires save ref and forbids load ref"
                )
            if policy == "save_and_resume" and (
                self.checkpoint_save_ref is None
                or self.checkpoint_load_ref is None
                or self.ancestor_schedule_receipt_ref is None
            ):
                raise ValueError(
                    "save_and_resume requires save/load refs and ancestor schedule lineage"
                )
        if (
            self.checkpoint_load_ref is not None
            and self.ancestor_schedule_receipt_ref is not None
            and self.checkpoint_load_ref.schedule_receipt_digest
            != self.ancestor_schedule_receipt_ref.sha256
        ):
            raise ValueError("checkpoint load schedule digest must match ancestor receipt")
        if self.declared_case_order == "seeded_random" and self.order_seed is None:
            raise ValueError("order_seed is required for seeded_random case order")
        if self.declared_case_order != "seeded_random" and self.order_seed is not None:
            raise ValueError("order_seed is forbidden for non-seeded case order")
        if self.observed_attempt_count != len(self.attempts):
            raise ValueError("observed_attempt_count must equal launched attempts")
        attempt_ids = [item.attempt_id for item in self.attempts]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("attempt execution ids must be unique")
        slots = [(item.case_id, item.attempt_index) for item in self.attempts]
        if len(set(slots)) != len(slots):
            raise ValueError("attempt execution slots must be unique")
        if any(index >= self.declared_rollouts_per_case for _, index in slots):
            raise ValueError("attempt_index exceeds declared rollouts per case")
        allocation_by_id = {
            item.attempt_id: item
            for item in self.budget_ledger.attempt_allocations
        }
        launched_ids = {
            item.attempt_id
            for item in self.budget_ledger.attempt_allocations
            if item.launched
        }
        unlaunched_ids = {
            item.attempt_id
            for item in self.budget_ledger.attempt_allocations
            if not item.launched
        }
        if set(attempt_ids) != launched_ids:
            raise ValueError("launched allocations and attempt executions must match exactly")
        if set(self.budget_ledger.aborted_children) != unlaunched_ids:
            raise ValueError("aborted children must equal unlaunched attempt allocations")
        child_run_ids: list[str] = []
        completion_sequences: list[int] = []
        spent_records: list[UsageRecord] = []
        for attempt in self.attempts:
            allocation = allocation_by_id[attempt.attempt_id]
            if allocation.case_id != attempt.case_id:
                raise ValueError("attempt execution case must match its allocation")
            if attempt.debit is None:
                raise ValueError("launched attempt execution requires a debit")
            child_run_ids.append(attempt.debit.child_run_id)
            completion_sequences.append(attempt.debit.completion_sequence)
            spent_records.append(attempt.debit.spent)
            if attempt.debit.completion_sequence <= (allocation.launch_sequence or 0):
                raise ValueError("attempt debit completion must follow launch")
            if not attempt.debit.budget_exceeded:
                cap = allocation.allocated
                released = attempt.debit.released
                if cap.max_tokens is not None and (
                    attempt.debit.spent.total_tokens is None
                    or released.max_tokens is None
                    or attempt.debit.spent.total_tokens + released.max_tokens
                    != cap.max_tokens
                ):
                    raise ValueError("spent plus released tokens must equal allocated cap")
                if cap.max_cost is not None and (
                    attempt.debit.spent.cost is None
                    or released.max_cost is None
                    or attempt.debit.spent.cost + released.max_cost != cap.max_cost
                ):
                    raise ValueError("spent plus released cost must equal allocated cap")
        if len(set(child_run_ids)) != len(child_run_ids):
            raise ValueError("attempt debit child run ids must be unique")
        if len(set(completion_sequences)) != len(completion_sequences):
            raise ValueError("attempt debit completion sequences must be unique")
        all_sequences = [
            item.reservation_sequence for item in self.budget_ledger.attempt_allocations
        ] + [
            item.launch_sequence
            for item in self.budget_ledger.attempt_allocations
            if item.launch_sequence is not None
        ] + completion_sequences
        if len(set(all_sequences)) != len(all_sequences):
            raise ValueError("scheduler event sequences must be globally unique")
        spent_ok = _usage_reconciles(
            self.budget_ledger.total_usage,
            (*tuple(spent_records), self.budget_ledger.parent_overhead),
        )
        if self.budget_ledger.reconciles_exactly != spent_ok:
            raise ValueError("reconciles_exactly disagrees with spend arithmetic")

        planned_by_case: dict[str, list[AttemptAllocation]] = {}
        for allocation in self.budget_ledger.attempt_allocations:
            planned_by_case.setdefault(allocation.case_id, []).append(allocation)
        for case_id, allocations in planned_by_case.items():
            if len(allocations) != self.declared_rollouts_per_case:
                raise ValueError(f"case {case_id!r} does not retain every planned rollout")
        launched_by_case = {
            case_id: [item for item in allocations if item.launched]
            for case_id, allocations in planned_by_case.items()
        }
        selected_counts = {
            case_id: sum(
                item.selected for item in self.attempts if item.case_id == case_id
            )
            for case_id in launched_by_case
        }
        cases_with_launches = {
            case_id for case_id, allocations in launched_by_case.items() if allocations
        }
        if self.schedule_valid and any(
            selected_counts[case_id] != 1 for case_id in cases_with_launches
        ):
            raise ValueError("every launched case requires exactly one selected attempt")

        expected_reset_count = {
            "per_case": len(cases_with_launches),
            "per_rollout": len(launched_ids),
            "never": 0,
        }[self.declared_state_reset]
        measured_mismatches: list[str] = []
        if self.observed_max_concurrency > len(self.attempts):
            raise ValueError("observed concurrency cannot exceed launched attempts")
        if self.observed_max_concurrency > self.declared_parallelism:
            measured_mismatches.append("observed concurrency exceeds declared parallelism")
        if self.observed_state_reset_count != expected_reset_count:
            measured_mismatches.append("observed state reset count differs from declaration")
        if self.observed_selection_policy != self.declared_candidate_selection:
            measured_mismatches.append("observed selection policy differs from declaration")
        if self.budget_ledger.reconciles_exactly is False:
            measured_mismatches.append("budget ledger does not reconcile exactly")
        if any(
            attempt.debit is not None and attempt.debit.budget_exceeded
            for attempt in self.attempts
        ):
            measured_mismatches.append("attempt exceeded its budget allocation")
        if unlaunched_ids:
            measured_mismatches.append("budget exhausted before all attempts launched")
        if self.schedule_valid and (self.mismatch_reasons or measured_mismatches):
            raise ValueError("schedule_valid=true contradicts observed schedule mismatches")
        if not self.schedule_valid and not self.mismatch_reasons:
            raise ValueError("schedule_valid=false requires mismatch_reasons")
        return self


class GateName(str, Enum):
    execution_valid = "execution_valid"
    protocol_valid = "protocol_valid"
    isolation_valid = "isolation_valid"
    scoring_valid = "scoring_valid"
    statistics_valid = "statistics_valid"
    claim_eligible = "claim_eligible"


REQUIRED_GATE_ORDER = (
    GateName.execution_valid,
    GateName.protocol_valid,
    GateName.isolation_valid,
    GateName.scoring_valid,
    GateName.statistics_valid,
)
REQUIRED_GATE_NAMES = frozenset(REQUIRED_GATE_ORDER)


class GateResult(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"valid": {"const": False}},
                        "required": ["valid"],
                    },
                    "then": {
                        "properties": {
                            "reason": {"type": "string", "minLength": 1}
                        },
                        "required": ["reason"],
                    },
                },
                {
                    "if": {
                        "properties": {"valid": {"const": True}},
                        "required": ["valid"],
                    },
                    "then": {
                        "properties": {"evidence_refs": {"minItems": 1}},
                        "required": ["evidence_refs"],
                    },
                },
            ]
        }
    )

    valid: bool
    reason: str | None = None
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_gate_has_positive_evidence(self) -> "GateResult":
        if self.valid and not self.evidence_refs:
            raise ValueError("valid gate requires positive evidence_refs")
        if any(not ref.strip() for ref in self.evidence_refs):
            raise ValueError("gate evidence_refs must be non-empty")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("gate evidence_refs must be unique")
        return self

    @model_validator(mode="after")
    def invalid_gate_has_reason(self) -> "GateResult":
        if not self.valid and not self.reason:
            raise ValueError("an invalid gate requires a reason")
        return self


class EffectEstimate(StrictModel):
    metric: str = Field(min_length=1)
    point_estimate: float
    confidence_interval: tuple[float, float]
    n_runs: int = Field(ge=1)
    n_pairs: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_interval_and_counts(self) -> "EffectEstimate":
        lower, upper = self.confidence_interval
        if lower > upper:
            raise ValueError("confidence interval lower bound exceeds upper bound")
        if self.n_pairs is not None and self.n_pairs * 2 > self.n_runs:
            raise ValueError("n_pairs cannot account for more runs than n_runs")
        return self


class LineageRef(StrictModel):
    """Bindings from one parent plan run to its selected child attempt."""

    run_id: str = Field(pattern=ID_PATTERN)
    attempt_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    evidence_bundle_ref: ArtifactRef
    schedule_receipt_ref: ArtifactRef
    case_set_receipt_ref: ArtifactRef


class RecordIndex(StrictModel):
    """Content-addressed source index used for standalone report verification."""

    format: Literal["bmp-record-index-v1"]
    experiment_id: str = Field(pattern=ID_PATTERN)
    manifest_refs: tuple[ArtifactRef, ...]
    aggregate_path: str = Field(min_length=1, pattern=r"^/")

    @field_validator("aggregate_path")
    @classmethod
    def aggregate_path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("record index aggregate_path must be absolute")
        return value

    @model_validator(mode="after")
    def manifest_paths_are_unique(self) -> "RecordIndex":
        paths = [ref.path for ref in self.manifest_refs]
        if len(set(paths)) != len(paths):
            raise ValueError("record index manifest refs must be unique")
        return self


class Observation(StrictModel):
    metric: str = Field(min_length=1)
    value: float
    n_runs: int = Field(ge=1)


class ObservationReport(StrictModel):
    """Exploratory observations with no claim eligibility or causal fields."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"isolation_valid": {"const": True}},
                        "required": ["isolation_valid"],
                    },
                    "then": {
                        "properties": {"isolation_reasons": {"maxItems": 0}}
                    },
                    "else": {
                        "properties": {"isolation_reasons": {"minItems": 1}}
                    },
                }
            ]
        }
    )

    purpose: Literal[RunPurpose.exploratory]
    subject_kind: SubjectKind
    experiment_id: str = Field(pattern=ID_PATTERN)
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    isolation_valid: bool
    isolation_reasons: tuple[str, ...]
    observations: tuple[Observation, ...] = ()
    failure_breakdown: Mapping[RunStatus, int] = Field(default_factory=dict)
    lineage: tuple[LineageRef, ...] = ()
    record_index_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def isolation_result_is_explicit(self) -> "ObservationReport":
        reasons = self.isolation_reasons
        if self.isolation_valid and reasons:
            raise ValueError("valid exploratory isolation cannot have failure reasons")
        if not self.isolation_valid and not reasons:
            raise ValueError("invalid exploratory isolation requires failure reasons")
        if any(not reason.strip() for reason in reasons):
            raise ValueError("exploratory isolation reasons must be non-empty")
        if len(set(reasons)) != len(reasons):
            raise ValueError("exploratory isolation reasons must be unique")
        return self

    @field_validator("failure_breakdown")
    @classmethod
    def failure_counts_are_nonnegative(
        cls, value: Mapping[RunStatus, int]
    ) -> Mapping[RunStatus, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("failure breakdown counts must be non-negative")
        return value


class ClaimReport(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "gates": {
                                "properties": {
                                    name.value: {
                                        "properties": {"valid": {"const": True}},
                                        "required": ["valid"],
                                    }
                                    for name in REQUIRED_GATE_ORDER
                                },
                                "required": [
                                    name.value for name in REQUIRED_GATE_ORDER
                                ],
                            }
                        },
                        "required": ["gates"],
                    },
                    "then": {
                        "properties": {"claim_eligible": {"const": True}}
                    },
                    "else": {
                        "properties": {"claim_eligible": {"const": False}}
                    },
                },
                {
                    "if": {
                        "properties": {
                            "claim_eligible": {"const": True},
                            "effect": {"type": "object"},
                        },
                        "required": ["claim_eligible", "effect"],
                    },
                    "then": {
                        "properties": {
                            "effect_is_causal_claim": {"const": True}
                        }
                    },
                    "else": {
                        "properties": {
                            "effect_is_causal_claim": {"const": False}
                        }
                    },
                },
            ]
        }
    )

    purpose: Literal[RunPurpose.claim]
    subject_kind: SubjectKind
    experiment_id: str = Field(pattern=ID_PATTERN)
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    gates: Mapping[GateName, GateResult] = Field(
        json_schema_extra={
            "propertyNames": {
                "enum": [name.value for name in REQUIRED_GATE_ORDER]
            },
            "minProperties": len(REQUIRED_GATE_NAMES),
            "maxProperties": len(REQUIRED_GATE_NAMES),
        }
    )
    effect: EffectEstimate | None = None
    failure_breakdown: Mapping[RunStatus, int] = Field(default_factory=dict)
    lineage: tuple[LineageRef, ...] = ()
    record_index_ref: ArtifactRef | None = None

    @model_validator(mode="wrap")
    @classmethod
    def validate_derived_wire_fields(cls, value: Any, handler: Any) -> "ClaimReport":
        """Accept serialized computed fields only when they equal derivation.

        Production persistence includes computed fields.  They are redundant
        wire evidence, never caller-controlled overrides.
        """

        supplied: dict[str, Any] = {}
        if isinstance(value, Mapping):
            value = dict(value)
            for name in ("claim_eligible", "effect_is_causal_claim"):
                if name in value:
                    supplied[name] = value.pop(name)
        result = handler(value)
        for name, expected in (
            ("claim_eligible", result.claim_eligible),
            ("effect_is_causal_claim", result.effect_is_causal_claim),
        ):
            if name in supplied and (
                not isinstance(supplied[name], bool) or supplied[name] != expected
            ):
                raise ValueError(f"serialized {name} contradicts derived value")
        return result

    @field_validator("gates")
    @classmethod
    def validate_gate_set(
        cls, value: Mapping[GateName, GateResult]
    ) -> Mapping[GateName, GateResult]:
        names = set(value)
        if GateName.claim_eligible in names:
            raise ValueError("claim_eligible is derived and must not appear in gates")
        missing = REQUIRED_GATE_NAMES - names
        unexpected = names - REQUIRED_GATE_NAMES
        if missing or unexpected:
            raise ValueError(
                f"gates must contain exactly the five validity gates; "
                f"missing={sorted(item.value for item in missing)}, "
                f"unexpected={sorted(item.value for item in unexpected)}"
            )
        return value

    @computed_field(return_type=bool)
    @property
    def claim_eligible(self) -> bool:
        """Whether every independently recorded validity gate passes."""

        return all(self.gates[name].valid for name in REQUIRED_GATE_NAMES)

    @computed_field(return_type=bool)
    @property
    def effect_is_causal_claim(self) -> bool:
        """Whether an estimate may be described using causal claim language."""

        return self.claim_eligible and self.effect is not None

    @model_validator(mode="after")
    def validate_failure_counts(self) -> "ClaimReport":
        invalid_counts = [count for count in self.failure_breakdown.values() if count < 0]
        if invalid_counts:
            raise ValueError("failure breakdown counts must be non-negative")
        return self


RunReport = Annotated[
    Union[ObservationReport, ClaimReport],
    Field(discriminator="purpose"),
]
RunReportAdapter = TypeAdapter(RunReport)


__all__ = [
    "BMP_VERSION",
    "AdapterCapability",
    "IDENTITY_EXCLUDE",
    "ArtifactRef",
    "AttemptAllocation",
    "AttemptContext",
    "AttemptExecution",
    "AssemblySubjectArtifact",
    "AssemblySubjectSpec",
    "BackendSpec",
    "BenchmarkArtifact",
    "BenchmarkArtifactAdapter",
    "BenchmarkSpec",
    "BenchmarkSpecAdapter",
    "ConfigurationArtifact",
    "ConfigurationSelection",
    "ConfigurationSpec",
    "Budget",
    "BudgetAllocation",
    "BudgetDebit",
    "BudgetLedger",
    "CaseAllocation",
    "CaseArtifact",
    "CaseSetActivationReceipt",
    "CaseSetArtifact",
    "CheckpointLoadReceipt",
    "CheckpointSaveReceipt",
    "ClaimDesign",
    "ClaimReport",
    "ClaimScope",
    "CustomBenchmarkArtifact",
    "CustomBenchmarkSpec",
    "CredentialRef",
    "EffectEstimate",
    "EnvironmentBindingRef",
    "EnvironmentReceipt",
    "EnvironmentSpec",
    "EvidenceBundle",
    "ExecutionSpec",
    "ExperimentContrast",
    "FakeSubjectArtifact",
    "FakeSubjectSpec",
    "GateName",
    "GateResult",
    "AssemblySidecarRef",
    "JournalRecord",
    "LineageRef",
    "MountSpec",
    "NetworkBoundary",
    "NetworkEndpointRecord",
    "NetworkObservation",
    "NetworkObservationMode",
    "NetworkPolicySource",
    "ResolvedNetworkPolicy",
    "Observation",
    "ObservationReport",
    "PackageRecord",
    "ProtocolKind",
    "ProtocolSpec",
    "ProviderBinding",
    "ProvenanceRecord",
    "RecordIndex",
    "ResolvedBmpManifest",
    "ResolvedExecutionSpec",
    "ResolvedManifestMetadata",
    "ResourceSpec",
    "RunPurpose",
    "RunReport",
    "RunReportAdapter",
    "RunStatus",
    "ScheduleActivationReceipt",
    "ScoringKind",
    "SubjectKind",
    "SUBJECT_KIND_SCOPE_MATRIX",
    "SubjectArtifact",
    "SubjectArtifactAdapter",
    "SubjectSpec",
    "SubjectSpecAdapter",
    "SystemPromptRecord",
    "TestOverrideReceipt",
    "UsageRecord",
    "VerifierEvidence",
    "WorkspaceRecord",
]
