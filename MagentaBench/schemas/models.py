"""Pydantic contracts for Benchmark Measurement Protocol (BMP) 0.1.

Hand-written TOML declarations are parsed into ``*Spec`` models.  Compilation
normalizes and pins them into ``*Artifact`` and ``Resolved*`` models suitable
for canonical hashing and execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Mapping, Union

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
SECRET_KEY_PATTERN = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL",
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
            if SECRET_KEY_PATTERN.search(str(key)):
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
    """Registry declaration backed by a pinned code or content source."""

    source: str = Field(min_length=1)
    commit: str = Field(min_length=1)


class ArtifactRef(StrictModel):
    """Content-addressed reference to an artifact stored outside the manifest."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("artifact path must be absolute")
        return value


class TaskSuiteBenchmarkSpec(SourceRegistryEntry):
    kind: Literal["task_suite"]
    task_manifest: str = Field(min_length=1)
    verifier: str = Field(min_length=1)
    # Benchmark-owned scoring semantics; never configure these on a backend.
    authoritative_reward_metric: str | None = Field(default=None, min_length=1)
    reward_pass_value: float | None = None

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "TaskSuiteBenchmarkSpec":
        if (self.authoritative_reward_metric is None) != (self.reward_pass_value is None):
            raise ValueError(
                "authoritative_reward_metric and reward_pass_value must be provided together"
            )
        return self


class ToolAgentSuiteBenchmarkSpec(SourceRegistryEntry):
    kind: Literal["tool_agent_suite"]
    task_root: str = Field(min_length=1)
    input_contract: str = Field(min_length=1)
    output_contract: tuple[str, ...] = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    # Benchmark-owned scoring semantics; never configure these on a backend.
    authoritative_reward_metric: str | None = Field(default=None, min_length=1)
    reward_pass_value: float | None = None

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "ToolAgentSuiteBenchmarkSpec":
        if (self.authoritative_reward_metric is None) != (self.reward_pass_value is None):
            raise ValueError(
                "authoritative_reward_metric and reward_pass_value must be provided together"
            )
        return self


BenchmarkSpec = Annotated[
    Union[TaskSuiteBenchmarkSpec, ToolAgentSuiteBenchmarkSpec],
    Field(discriminator="kind"),
]
BenchmarkSpecAdapter = TypeAdapter(BenchmarkSpec)


class ArtifactIdentity(StrictModel):
    """Compiler-generated identity shared by resolved registry artifacts."""

    artifact_digest: str = Field(pattern=SHA256_PATTERN)


class AbsoluteSourceArtifact(ArtifactIdentity):
    source: str = Field(min_length=1)

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
    commit: str = Field(min_length=1)
    task_manifest: str = Field(min_length=1)
    verifier: str = Field(min_length=1)
    authoritative_reward_metric: str | None = Field(default=None, min_length=1)
    reward_pass_value: float | None = None

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "TaskSuiteBenchmarkArtifact":
        if (self.authoritative_reward_metric is None) != (self.reward_pass_value is None):
            raise ValueError(
                "authoritative_reward_metric and reward_pass_value must be provided together"
            )
        return self


class ToolAgentSuiteBenchmarkArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["tool_agent_suite"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str = Field(min_length=1)
    task_root: str = Field(min_length=1)
    input_contract: str = Field(min_length=1)
    output_contract: tuple[str, ...] = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    authoritative_reward_metric: str | None = Field(default=None, min_length=1)
    reward_pass_value: float | None = None

    @model_validator(mode="after")
    def scoring_semantics_are_complete(self) -> "ToolAgentSuiteBenchmarkArtifact":
        if (self.authoritative_reward_metric is None) != (self.reward_pass_value is None):
            raise ValueError(
                "authoritative_reward_metric and reward_pass_value must be provided together"
            )
        return self


BenchmarkArtifact = Annotated[
    Union[TaskSuiteBenchmarkArtifact, ToolAgentSuiteBenchmarkArtifact],
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
    interface: str = Field(min_length=1)
    emits_trace: bool = False


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
    commit: str = Field(min_length=1)
    assembly_profile: str = Field(default="default", min_length=1)
    sidecar_ref: AssemblySidecarRef | None = None
    emits_trace: bool = False


class OpaqueAgentSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["opaque_agent"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str = Field(min_length=1)
    entrypoint: str = Field(min_length=1)
    interface: str = Field(min_length=1)
    emits_trace: bool = False


class EvolverSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["evolver"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str = Field(min_length=1)
    target: Literal["harness"]
    emits_trace: bool = False


class MetaEvolverSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["meta_evolver"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str = Field(min_length=1)
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
    """A host-to-runtime mount declaration for an execution environment."""

    host_path: str = Field(min_length=1)
    container_path: str = Field(min_length=1)
    read_only: bool = True

    @field_validator("host_path", "container_path")
    @classmethod
    def mount_paths_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("mount paths must be absolute")
        return value


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


class PackageRecord(StrictModel):
    """Installed package provenance captured by an environment builder."""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


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
    version: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    # Warning: never use for API keys or secrets; names only.
    defaults: Mapping[str, Any] = Field(default_factory=dict)
    environment: EnvironmentSpec | None = None

    @field_validator("defaults", mode="before")
    @classmethod
    def defaults_reject_secret_keys(cls, value: Any) -> Any:
        return _reject_secret_like_keys(value, field_name="BackendSpec.defaults")

    @model_validator(mode="after")
    def has_launch_target(self) -> "BackendSpec":
        if self.image is None and self.executable is None:
            raise ValueError("backend requires image or executable")
        return self


class ProtocolSpec(RegistryEntry):
    """Execution schedule defaults resolved before local execution overrides."""

    rollouts_per_case: int = Field(default=1, ge=1)
    parallelism: int = Field(default=1, ge=1)
    case_order: Literal["fixed", "seeded_random", "random"] = "fixed"
    adaptive_budget: bool = False
    candidate_selection: str | None = None
    state_reset: Literal["per_case", "per_rollout", "never"] = "per_case"
    budget: Budget | None = None
    checkpoint_policy: Literal["disabled", "save", "resume", "save_and_resume"] = "disabled"
    deterministic_conformance: bool = False


class ExecutionSpec(StrictModel):
    """TOML execution declaration: registry references plus local overrides."""

    backend: str = Field(pattern=ID_PATTERN)
    model: str = Field(min_length=1)
    seed: int
    budget: Budget
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
    """Execution contract with registry references inlined."""

    backend: BackendSpec
    model: str = Field(min_length=1)
    seed: int
    budget: Budget
    protocol: ProtocolSpec | None = None


class ResolvedManifestMetadata(StrictModel):
    experiment_id: str = Field(pattern=ID_PATTERN)
    run_id: str = Field(pattern=ID_PATTERN)
    allowed_diff: tuple[str, ...] = ()
    factors: Mapping[str, Any] = Field(default_factory=dict)

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
    metadata: ResolvedManifestMetadata
    created_at: str | None = None
    wall_clock_start: str | None = None
    wall_clock_end: str | None = None
    record_root: str | None = None
    resume_count: int = Field(default=0, ge=0)
    runner_invocation_id: str | None = None

    def canonical_digest(self) -> str:
        data = self.model_dump(mode="json", exclude=self.IDENTITY_EXCLUDE)
        canonical = json.dumps(
            data,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class RunStatus(str, Enum):
    pass_ = "pass"
    verified_fail = "verified_fail"
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
    passed: bool
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
    distribution: str | None = None
    version: str | None = None
    commit: str | None = None
    image_digest: str | None = None
    backend_kind: str | None = Field(default=None, min_length=1)
    network_mode: str | None = Field(default=None, min_length=1)
    workspace_namespace: str | None = Field(default=None, min_length=1)
    environment_receipt: EnvironmentReceipt | None = None

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
    run_id: str = Field(pattern=ID_PATTERN)
    status: RunStatus
    output_refs: tuple[ArtifactRef, ...] = ()
    trace_ref: ArtifactRef | None = None
    checkpoint_ref: ArtifactRef | None = None
    log_refs: tuple[ArtifactRef, ...] = ()
    verifier_evidence: VerifierEvidence | None = None
    usage: UsageRecord | None = None
    provenance: ProvenanceRecord

    @model_validator(mode="after")
    def validate_status_evidence(self) -> "EvidenceBundle":
        if self.status in (RunStatus.pass_, RunStatus.verified_fail):
            if not self.output_refs:
                raise ValueError(f"status {self.status.value!r} requires output_refs")
            if self.verifier_evidence is None:
                raise ValueError(f"status {self.status.value!r} requires verifier_evidence")
        if (
            self.status == RunStatus.pass_
            and self.verifier_evidence is not None
            and not self.verifier_evidence.passed
        ):
            raise ValueError("status 'pass' requires verifier_evidence.passed=true")
        if (
            self.status == RunStatus.verified_fail
            and self.verifier_evidence is not None
            and self.verifier_evidence.passed
        ):
            raise ValueError("status 'verified_fail' requires verifier_evidence.passed=false")
        if self.provenance.trace_emission_claimed and self.trace_ref is None:
            raise ValueError("trace_ref is required when the subject claims trace emission")
        return self


class GateName(str, Enum):
    execution_valid = "execution_valid"
    protocol_valid = "protocol_valid"
    isolation_valid = "isolation_valid"
    scoring_valid = "scoring_valid"
    statistics_valid = "statistics_valid"
    claim_eligible = "claim_eligible"


REQUIRED_GATE_NAMES = frozenset(
    {
        GateName.execution_valid,
        GateName.protocol_valid,
        GateName.isolation_valid,
        GateName.scoring_valid,
        GateName.statistics_valid,
    }
)


class GateResult(StrictModel):
    valid: bool
    reason: str | None = None
    evidence_refs: tuple[str, ...] = ()

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
    run_id: str = Field(pattern=ID_PATTERN)
    case_id: str | None = None
    evidence_bundle_sha256: str = Field(pattern=SHA256_PATTERN)


class ClaimReport(StrictModel):
    experiment_id: str = Field(pattern=ID_PATTERN)
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    gates: Mapping[GateName, GateResult]
    effect: EffectEstimate | None = None
    failure_breakdown: Mapping[RunStatus, int] = Field(default_factory=dict)
    lineage: tuple[LineageRef, ...] = ()

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


__all__ = [
    "BMP_VERSION",
    "IDENTITY_EXCLUDE",
    "ArtifactRef",
    "AssemblySubjectArtifact",
    "AssemblySubjectSpec",
    "BackendSpec",
    "BenchmarkArtifact",
    "BenchmarkArtifactAdapter",
    "BenchmarkSpec",
    "BenchmarkSpecAdapter",
    "Budget",
    "ClaimReport",
    "EffectEstimate",
    "EnvironmentReceipt",
    "EnvironmentSpec",
    "EvidenceBundle",
    "ExecutionSpec",
    "FakeSubjectArtifact",
    "FakeSubjectSpec",
    "GateName",
    "GateResult",
    "AssemblySidecarRef",
    "LineageRef",
    "MountSpec",
    "PackageRecord",
    "ProtocolSpec",
    "ProvenanceRecord",
    "ResolvedBmpManifest",
    "ResolvedExecutionSpec",
    "ResolvedManifestMetadata",
    "RunStatus",
    "SubjectArtifact",
    "SubjectArtifactAdapter",
    "SubjectSpec",
    "SubjectSpecAdapter",
    "UsageRecord",
    "VerifierEvidence",
]
