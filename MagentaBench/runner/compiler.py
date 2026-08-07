"""Compile BMP TOML declarations into deterministic resolved manifests.

The compiler is deliberately side-effect free except for the rejected-run audit
record written when a one-factor experiment violates its declared isolation
boundary. Execution consumes :class:`CompiledRun` objects produced here.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pydantic

if int(pydantic.VERSION.split(".", 1)[0]) < 2:  # pragma: no cover - import guard
    raise RuntimeError("MagentaBench requires Pydantic v2")

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from MagentaBench.schemas import (
    BackendSpec,
    BenchmarkSpecAdapter,
    ClaimDesign,
    ClaimReport,
    ClaimScope,
    ExecutionSpec,
    ExperimentContrast,
    GateName,
    GateResult,
    ObservationReport,
    ProtocolSpec,
    ResolvedBmpManifest,
    ResolvedManifestMetadata,
    RunPurpose,
    SUBJECT_KIND_SCOPE_MATRIX,
    SubjectSpecAdapter,
    TestOverrideReceipt,
)
from MagentaBench.schemas.compiler import (
    _compile_benchmark_artifact,
    _compile_subject_artifact,
    _resolve_execution_spec,
)


class CompilationError(ValueError):
    """A declaration cannot be resolved into a valid run manifest."""


class RegistryLookupError(CompilationError):
    """A referenced registry entry is missing or ambiguous."""


class IsolationViolation(CompilationError):
    """Resolved control/treatment manifests differ outside ``allowed_diff``."""

    def __init__(self, forbidden_paths: Iterable[str], all_paths: Iterable[str] = ()) -> None:
        self.forbidden_paths = tuple(sorted(set(forbidden_paths)))
        self.all_paths = tuple(sorted(set(all_paths)))
        super().__init__(
            "resolved manifest diff exceeds allowed intervention: "
            + ", ".join(self.forbidden_paths)
        )


@dataclass(frozen=True)
class CompiledRun:
    """Verified run value; every derived identity comes from the manifest."""

    manifest: ResolvedBmpManifest

    @property
    def canonical_json(self) -> bytes:
        return canonical_manifest_json(self.manifest)

    @property
    def wire_json(self) -> bytes:
        return canonical_json_bytes(self.manifest)

    @property
    def manifest_digest(self) -> str:
        return sha256_bytes(self.canonical_json)

    @property
    def factor_values(self) -> Mapping[str, Any]:
        return self.manifest.metadata.factors


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value using the BMP canonical encoding."""

    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_identity_dict(manifest: ResolvedBmpManifest) -> dict[str, Any]:
    """Return the schema-defined identity projection for ``manifest``."""

    return manifest.identity_data()


def canonical_manifest_json(manifest: ResolvedBmpManifest) -> bytes:
    return canonical_json_bytes(manifest_identity_dict(manifest))


def manifest_sha256(manifest: ResolvedBmpManifest) -> str:
    return sha256_bytes(canonical_manifest_json(manifest))


def _deep_set(target: dict[str, Any], dotted_path: str, value: Any) -> None:
    parts = dotted_path.split(".")
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            raise CompilationError(
                f"factor path {dotted_path!r} traverses non-table field {part!r}"
            )
        current = child
    current[parts[-1]] = copy.deepcopy(value)


def expand_factor_sweep(
    base: Mapping[str, Any], factors: Mapping[str, Any] | None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Expand lexically sorted axes and values into deterministic combinations.

    Returns pairs of ``(expanded_declaration, selected_factor_values)``. Factor
    paths that start with ``experiment.`` or ``execution.`` modify those tables;
    bare ``benchmark``, ``subject`` and ``protocol`` modify experiment refs.
    Other bare factors are metadata-only (for example ``repetition``).
    """

    if not factors:
        return [(copy.deepcopy(dict(base)), {})]

    axes: list[tuple[str, list[Any]]] = []
    for path in sorted(factors):
        raw_values = factors[path]
        values = list(raw_values) if isinstance(raw_values, list) else [raw_values]
        if not values:
            raise CompilationError(f"factor {path!r} has no values")
        axes.append((path, sorted(values, key=lambda value: str(value))))

    expanded: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for combination in itertools.product(*(values for _, values in axes)):
        declaration = copy.deepcopy(dict(base))
        selected: dict[str, Any] = {}
        for (path, _), value in zip(axes, combination):
            selected[path] = copy.deepcopy(value)
            if path in {"benchmark", "subject", "protocol"}:
                declaration.setdefault("experiment", {})[path] = copy.deepcopy(value)
            elif path.startswith("experiment.") or path.startswith("execution."):
                _deep_set(declaration, path, value)
            else:
                # Metadata-only factors still participate in run identity.
                continue
        expanded.append((declaration, selected))
    return expanded


def resolved_diff_paths(left: Any, right: Any, prefix: str = "") -> tuple[str, ...]:
    """Return leaf-level dotted paths whose resolved values differ."""

    left = _jsonable(left)
    right = _jsonable(right)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                paths.append(path)
            else:
                paths.extend(resolved_diff_paths(left[key], right[key], path))
        return tuple(paths)
    if isinstance(left, list) and isinstance(right, list):
        paths = []
        for index in range(max(len(left), len(right))):
            path = f"{prefix}.{index}" if prefix else str(index)
            if index >= len(left) or index >= len(right):
                paths.append(path)
            else:
                paths.extend(resolved_diff_paths(left[index], right[index], path))
        return tuple(paths)
    return () if left == right else (prefix or "$",)


def enforce_allowed_diff(
    control: ResolvedBmpManifest,
    treatment: ResolvedBmpManifest,
    allowed_diff: Iterable[str],
    *,
    resolved_paths: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Validate a control/treatment pair and return its complete resolved diff."""

    if resolved_paths is None:
        # Metadata contains pair labels and run identity, not causal configuration.
        left = {
            "benchmark": control.benchmark.model_dump(mode="json"),
            "subject": control.subject.model_dump(mode="json"),
            "execution": control.execution.model_dump(mode="json"),
        }
        right = {
            "benchmark": treatment.benchmark.model_dump(mode="json"),
            "subject": treatment.subject.model_dump(mode="json"),
            "execution": treatment.execution.model_dump(mode="json"),
        }
        paths = resolved_diff_paths(left, right)
    else:
        paths = tuple(resolved_paths)
    allowed = tuple(allowed_diff)
    forbidden = [path for path in paths if path not in allowed]
    if forbidden:
        raise IsolationViolation(forbidden, paths)
    return paths


class Compiler:
    """Load registries and compile an experiment TOML into resolved run plans."""

    _EXPERIMENT_KEYS = frozenset(
        {
            "id",
            "benchmark",
            "subject",
            "protocol",
            "contrast",
            "allowed_diff",
            "design",
        }
    )
    _REGISTRY_SECTIONS = {
        "benchmark": ("benchmarks", BenchmarkSpecAdapter),
        "subject": ("subjects", SubjectSpecAdapter),
        "protocol": ("protocols", ProtocolSpec),
        "backend": ("backends", BackendSpec),
    }

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        allow_test_override: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.allow_test_override = allow_test_override
        self.registry_root = self.project_root / "registries"
        self._registry_cache: dict[tuple[str, str], tuple[Any, Path]] = {}

    @staticmethod
    def _parse_contrast(experiment: Mapping[str, Any]) -> ExperimentContrast:
        raw = experiment.get("contrast")
        if raw is None:
            raw = {"mode": "all_arms", "counterbalanced": False}
        if not isinstance(raw, Mapping):
            raise CompilationError("[experiment.contrast] must be a table")
        try:
            return ExperimentContrast.model_validate(raw)
        except pydantic.ValidationError as exc:
            raise CompilationError(f"invalid [experiment.contrast]: {exc}") from exc

    @staticmethod
    def _load_toml(path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                value = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CompilationError(f"cannot load TOML {path}: {exc}") from exc
        if not isinstance(value, dict):  # defensive: TOML roots are tables
            raise CompilationError(f"TOML root must be a table: {path}")
        return value

    def _lookup(self, kind: str, entry_id: str) -> tuple[Any, Path]:
        key = (kind, entry_id)
        if key in self._registry_cache:
            return self._registry_cache[key]
        try:
            directory_name, validator = self._REGISTRY_SECTIONS[kind]
        except KeyError as exc:  # pragma: no cover - internal misuse
            raise RegistryLookupError(f"unknown registry kind {kind!r}") from exc

        matches: list[tuple[Any, Path]] = []
        directory = self.registry_root / directory_name
        for path in sorted(directory.glob("*.toml")):
            raw = self._load_toml(path)
            unexpected_sections = sorted(set(raw) - {kind})
            if unexpected_sections:
                raise CompilationError(
                    f"registry {path} contains unexpected sections: "
                    f"{unexpected_sections}"
                )
            section = raw.get(kind)
            if not isinstance(section, dict) or section.get("id") != entry_id:
                continue
            try:
                if hasattr(validator, "validate_python"):
                    parsed = validator.validate_python(section)
                else:
                    parsed = validator.model_validate(section)
            except pydantic.ValidationError as exc:
                raise CompilationError(
                    f"invalid {kind} registry entry {entry_id!r} in {path}: {exc}"
                ) from exc
            matches.append((parsed, path))

        if not matches:
            raise RegistryLookupError(
                f"{kind} registry id {entry_id!r} not found under {directory}"
            )
        if len(matches) > 1:
            paths = ", ".join(str(path) for _, path in matches)
            raise RegistryLookupError(
                f"duplicate {kind} registry id {entry_id!r}: {paths}"
            )
        self._registry_cache[key] = matches[0]
        return matches[0]

    @staticmethod
    def _artifact_digest(data: Mapping[str, Any]) -> str:
        return sha256_bytes(canonical_json_bytes(data))

    def _benchmark_artifact(self, entry_id: str):
        spec, registry_path = self._lookup("benchmark", entry_id)
        return _compile_benchmark_artifact(spec, base_dir=registry_path.parent)

    def _subject_artifact(self, entry_id: str):
        spec, registry_path = self._lookup("subject", entry_id)
        return _compile_subject_artifact(spec, base_dir=registry_path.parent)

    _SCOPE_PROOF_TYPES = {
        ClaimScope.component: "AssemblySidecarRef",
        ClaimScope.model: "ModelActivationReceipt",
        ClaimScope.checkpoint: "CheckpointLoadReceipt",
        ClaimScope.evolver: "EvolutionRunEvidence",
        ClaimScope.meta_evolver: "NestedIsolationReceipt and RecursiveBudgetReceipt",
        ClaimScope.schedule: "ScheduleActivationReceipt",
        ClaimScope.ablation: "AssemblySidecarRef",
        ClaimScope.hyperparameter: "HyperparameterActivationReceipt",
        ClaimScope.conformance: "FakeConformanceEvidence",
        ClaimScope.whole_harness: "WholeHarnessArtifactEvidence",
    }
    # Conformance is the only intended-reachable scope and remains subject to
    # an end-to-end Pipeline proof; every research claim scope is inactive.
    _ACTIVE_SCOPES = frozenset({ClaimScope.conformance})
    _SCHEDULER_ADAPTER = "magentabench.scheduler"
    _BACKEND_DEFAULT_KEYS = {
        "fake": frozenset(),
        "subprocess": frozenset(),
        "aose-docker": frozenset(),
        "harbor": frozenset(
            {
                "agent_kwargs",
                "agent_timeout_multiplier",
                "environment_type",
            }
        ),
        "harbor-shim": frozenset(
            {
                "agent_kwargs",
                "agent_override",
                "agent_timeout_multiplier",
                "environment_type",
            }
        ),
    }
    _NONE_MODELS = frozenset({"none", "none/deterministic", "none/echo"})
    _SCHEDULE_VARY_PATHS = frozenset(
        {
            "execution.protocol.rollouts_per_case",
            "execution.protocol.parallelism",
            "execution.protocol.case_order",
            "execution.protocol.candidate_selection",
            "execution.protocol.state_reset",
            "execution.protocol.checkpoint_policy",
            "execution.budget.max_tokens",
            "execution.budget.max_wall_seconds",
            "execution.budget.max_cost",
        }
    )

    def _validate_subject_evidence_for_scope(
        self, manifest: ResolvedBmpManifest
    ) -> None:
        """Reject attribution scopes unsupported by frozen subject evidence."""

        scope = manifest.claim_design.scope
        subject = manifest.subject
        if scope == ClaimScope.schedule:
            raise CompilationError(
                "schedule scope requires missing native subprocess schedule tuple "
                "and CaseSetActivationReceipt with Pipeline multi-case loading"
            )
        if (
            scope == ClaimScope.conformance
            and not self.allow_test_override
            and not (
                manifest.benchmark.kind == "task_suite"
                and manifest.benchmark.adapter == "fake"
                and manifest.subject.kind == "fake"
                and manifest.subject.adapter == "fake"
                and manifest.execution.backend.adapter == "fake"
                and manifest.execution.protocol is not None
                and manifest.execution.protocol.kind == "mechanism_validation"
                and manifest.benchmark.verifier == "fake.exact.v1"
            )
        ):
            raise CompilationError(
                "conformance tuple requires missing PipelineAdapterActivationReceipt"
            )
        legal_scopes = SUBJECT_KIND_SCOPE_MATRIX.get(subject.kind, frozenset())
        proof_type = self._SCOPE_PROOF_TYPES[scope]
        if scope not in legal_scopes:
            raise CompilationError(
                f"claim scope {scope.value!r} for subject kind {subject.kind!r} "
                f"requires missing evidence class {proof_type}"
            )
        if scope not in self._ACTIVE_SCOPES:
            raise CompilationError(
                f"claim scope {scope.value!r} requires missing evidence class "
                f"{proof_type}; runtime support is not active"
            )
        if scope == ClaimScope.component and getattr(subject, "sidecar_ref", None) is None:
            raise CompilationError(
                "claim scope 'component' requires missing evidence class AssemblySidecarRef"
            )
        if (
            scope == ClaimScope.conformance
            and manifest.claim_design.purpose != RunPurpose.exploratory
        ):
            raise CompilationError(
                "conformance scope requires run purpose 'exploratory'"
            )

    def _compile_expanded(
        self,
        declaration: Mapping[str, Any],
        factor_values: Mapping[str, Any],
        run_index: int,
    ) -> CompiledRun:
        unexpected_sections = sorted(
            set(declaration) - {"experiment", "execution", "factors"}
        )
        if unexpected_sections:
            raise CompilationError(
                f"unknown top-level TOML sections: {unexpected_sections}"
            )
        experiment = declaration.get("experiment")
        execution_raw = declaration.get("execution")
        if not isinstance(experiment, dict) or not isinstance(execution_raw, dict):
            raise CompilationError("experiment TOML requires [experiment] and [execution]")
        if "claim_mode" in experiment:
            raise CompilationError(
                "claim_mode is forbidden; use [experiment.contrast] (ExperimentContrast)"
            )
        unknown_experiment_keys = sorted(set(experiment) - self._EXPERIMENT_KEYS)
        if unknown_experiment_keys:
            raise CompilationError(
                f"unknown [experiment] fields: {unknown_experiment_keys}"
            )
        required = ("id", "benchmark", "subject", "protocol")
        missing = [name for name in required if not experiment.get(name)]
        if missing:
            raise CompilationError(f"[experiment] missing fields: {', '.join(missing)}")
        design_raw = experiment.get("design")
        if not isinstance(design_raw, dict):
            raise CompilationError(
                "[experiment.design] is required with scope, purpose, and vary"
            )
        try:
            claim_design = ClaimDesign.model_validate(design_raw)
        except pydantic.ValidationError as exc:
            raise CompilationError(f"invalid [experiment.design]: {exc}") from exc
        if self.allow_test_override:
            claim_design = ClaimDesign(
                scope=ClaimScope.conformance,
                purpose=RunPurpose.exploratory,
                vary=(),
            )
        else:
            self._validate_scope_vary_declaration(claim_design)
        contrast = self._parse_contrast(experiment)

        try:
            execution = ExecutionSpec.model_validate(execution_raw)
        except pydantic.ValidationError as exc:
            raise CompilationError(f"invalid [execution]: {exc}") from exc
        if execution.model not in self._NONE_MODELS:
            raise CompilationError(
                "execution model requires missing evidence class ModelActivationReceipt"
            )
        unsupported_override_fields = sorted(
            set(execution.backend_overrides) - {"defaults"}
        )
        if unsupported_override_fields:
            raise CompilationError(
                "backend_overrides contains unbound backend identity fields: "
                f"{unsupported_override_fields}"
            )
        benchmark = self._benchmark_artifact(str(experiment["benchmark"]))
        subject = self._subject_artifact(str(experiment["subject"]))
        backend, _ = self._lookup("backend", execution.backend)
        protocol, _ = self._lookup("protocol", str(experiment["protocol"]))
        adapter_model = {
            "fake": "none/deterministic",
            "subprocess": "none/echo",
            "aose-docker": "none",
            "harbor": "none/echo",
            "harbor-shim": "none/echo",
        }.get(backend.adapter)
        if execution.model != adapter_model:
            raise CompilationError(
                f"model sentinel {execution.model!r} is not activated by "
                f"backend adapter {backend.adapter!r}; ModelActivationReceipt missing"
            )
        expected_reset_policy = {
            "fake": "never",
            "subprocess": "per_rollout",
            "aose-docker": "never",
            "harbor": "never",
            "harbor-shim": "never",
        }.get(backend.adapter)
        if protocol.state_reset != expected_reset_policy:
            raise CompilationError(
                f"state_reset {protocol.state_reset!r} is not activated by "
                f"backend adapter {backend.adapter!r}; StateResetReceipt missing"
            )

        benchmark_pair = (benchmark.kind, benchmark.adapter)
        if benchmark_pair not in {
            ("task_suite", "fake"),
            ("tool_agent_suite", "aosebench"),
        }:
            raise CompilationError(
                f"unknown benchmark adapter combination: {benchmark_pair!r}"
            )
        if benchmark.adapter == "aosebench" and (
            benchmark.task_root != "benchmark/tasks"
            or benchmark.input_contract != "/app/instruction.md; /app/data:ro"
            or tuple(benchmark.output_contract)
            != ("/app/trace.md", "/app/answer.txt")
            or benchmark.evaluator != "aosebench.rubric-judge"
        ):
            raise CompilationError("AOSE benchmark native task contract mismatch")
        if subject.kind == "fake":
            subject_combo = (subject.kind, subject.adapter, None)
        else:
            subject_combo = (
                subject.kind,
                subject.adapter,
                getattr(subject, "interface", None),
            )
        if subject_combo not in {
            ("fake", "fake", None),
            ("opaque_agent", "fake", "task_to_output"),
            ("opaque_agent", "cli-agent", "aosebench-container-v1"),
        }:
            raise CompilationError(
                f"unknown subject adapter/interface combination: {subject_combo!r}"
            )

        deterministic = bool(getattr(protocol, "deterministic_conformance", False))
        deterministic_allowed = (
            protocol.kind == "mechanism_validation"
            and claim_design.purpose == RunPurpose.exploratory
            and claim_design.scope == ClaimScope.conformance
            and benchmark.adapter == "fake"
            and subject.kind == "fake"
            and subject.adapter == "fake"
            and backend.adapter == "fake"
            and benchmark.verifier == "fake.exact.v1"
        )
        if deterministic and not deterministic_allowed:
            raise CompilationError(
                "deterministic_conformance requires the all-fake exploratory "
                "mechanism-validation conformance path"
            )
        if backend.adapter == "fake" and subject.kind == "fake" and not deterministic:
            raise CompilationError(
                "all-fake conformance requires deterministic_conformance=true"
            )

        scope = claim_design.scope
        if scope == ClaimScope.schedule:
            raise CompilationError(
                "schedule scope requires missing native subprocess schedule tuple "
                "and CaseSetActivationReceipt with Pipeline multi-case loading"
            )
        proof_type = self._SCOPE_PROOF_TYPES[scope]
        legal_scopes = SUBJECT_KIND_SCOPE_MATRIX.get(subject.kind, frozenset())
        if scope not in legal_scopes:
            raise CompilationError(
                f"claim scope {scope.value!r} for subject kind {subject.kind!r} "
                f"requires missing evidence class {proof_type}"
            )
        if scope not in self._ACTIVE_SCOPES:
            raise CompilationError(
                f"claim scope {scope.value!r} requires missing evidence class "
                f"{proof_type}; runtime support is not active"
            )

        kind_scope_matrix = {
            "mechanism_validation": {
                RunPurpose.exploratory: {
                    ClaimScope.conformance,
                    ClaimScope.whole_harness,
                },
            },
            "test_time_scaling": {
                RunPurpose.exploratory: {
                    ClaimScope.schedule,
                    ClaimScope.conformance,
                },
                RunPurpose.claim: {ClaimScope.schedule},
            },
            "benchmark_evaluation": {
                RunPurpose.exploratory: {
                    ClaimScope.whole_harness,
                    ClaimScope.model,
                    ClaimScope.conformance,
                },
                RunPurpose.claim: {
                    ClaimScope.whole_harness,
                    ClaimScope.model,
                },
            },
        }
        permitted_scopes = kind_scope_matrix.get(protocol.kind, {}).get(
            claim_design.purpose, set()
        )
        if claim_design.scope not in permitted_scopes:
            raise CompilationError(
                f"protocol kind {protocol.kind!r} does not permit purpose "
                f"{claim_design.purpose.value!r} with scope "
                f"{claim_design.scope.value!r}"
            )
        if protocol.adapter != self._SCHEDULER_ADAPTER:
            raise CompilationError(
                "protocol adapter does not match active scheduler: "
                f"declared {protocol.adapter!r}, active {self._SCHEDULER_ADAPTER!r}; "
                "ProtocolActivationReceipt missing"
            )
        if backend.environment is not None:
            raise CompilationError(
                f"backend adapter {backend.adapter!r} requires missing "
                "EnvironmentActivationReceipt"
            )
        allowed_default_keys = self._BACKEND_DEFAULT_KEYS.get(backend.adapter)
        if allowed_default_keys is None:
            raise CompilationError(
                f"backend adapter {backend.adapter!r} has no declared defaults read-set"
            )
        unknown_default_keys = sorted(
            set(backend.defaults) - allowed_default_keys
        )
        override_defaults = execution.backend_overrides.get("defaults", {})
        if not isinstance(override_defaults, Mapping):
            raise CompilationError("backend_overrides.defaults must be a table/object")
        unknown_default_keys.extend(
            sorted(set(override_defaults) - allowed_default_keys)
        )
        if unknown_default_keys:
            raise CompilationError(
                "backend defaults contain keys not read by the active adapter: "
                f"{sorted(set(unknown_default_keys))}"
            )
        if protocol.case_order != "seeded_random" and execution.seed is not None:
            raise CompilationError(
                "execution.seed is forbidden unless case_order='seeded_random'"
            )
        if protocol.case_order == "seeded_random" and execution.seed is None:
            raise CompilationError(
                "execution.seed is required for case_order='seeded_random'"
            )
        if protocol.checkpoint_policy == "resume":
            raise CompilationError(
                "checkpoint_policy='resume' requires CheckpointLoadReceipt; "
                "receipt type not yet defined"
            )
        if bool(getattr(protocol, "adaptive_budget", False)):
            raise CompilationError(
                "protocol adaptive_budget=true requires missing evidence class "
                "AdaptiveBudgetReceipt"
            )
        selection = getattr(protocol, "candidate_selection", None)
        if selection not in {None, "single", "exact", "best_of_n"}:
            raise CompilationError(
                f"candidate_selection {selection!r} requires missing evidence class "
                "CandidateSelectionReceipt"
            )
        if selection in {"single", "exact"} and protocol.rollouts_per_case != 1:
            raise CompilationError(
                f"candidate_selection {selection!r} requires rollouts_per_case=1"
            )
        if selection == "exact" and benchmark.scoring_kind.value != "binary":
            raise CompilationError(
                "candidate_selection='exact' requires binary benchmark scoring "
                "and ExactSelectionReceipt"
            )
        allowed_raw = experiment.get("allowed_diff", ())
        if isinstance(allowed_raw, str):
            allowed_diff = (allowed_raw,)
        else:
            allowed_diff = tuple(allowed_raw or ())

        # The stable ordinal is defined over the canonical lexical sweep order.
        metadata = ResolvedManifestMetadata(
            experiment_id=experiment["id"],
            run_id=f"{experiment['id']}__run{run_index:04d}",
            allowed_diff=allowed_diff,
            factors=dict(factor_values),
            test_override=(
                TestOverrideReceipt(reason="explicit allow_test_override=true")
                if self.allow_test_override
                else None
            ),
        )
        resolved_execution = _resolve_execution_spec(
            execution,
            backend=backend,
            protocol=protocol,
        )
        manifest = ResolvedBmpManifest(
            benchmark=benchmark,
            subject=subject,
            execution=resolved_execution,
            claim_design=claim_design,
            contrast=contrast,
            metadata=metadata,
        )
        self._validate_subject_evidence_for_scope(manifest)
        return CompiledRun(manifest=manifest)

    @staticmethod
    def _pair_key(run: CompiledRun) -> bytes:
        factors = {
            key: value
            for key, value in run.factor_values.items()
            if key not in {"subject", "experiment.subject"}
        }
        return canonical_json_bytes(factors)

    @staticmethod
    def _validate_scope_vary_declaration(design: ClaimDesign) -> None:
        dotted_path = re.compile(
            r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)+$"
        )
        invalid = [path for path in design.vary if not dotted_path.fullmatch(path)]
        if invalid:
            raise CompilationError(
                f"claim scope {design.scope.value!r} has invalid canonical vary paths: {invalid}"
            )
        if design.scope == ClaimScope.conformance:
            if design.vary:
                raise CompilationError("conformance scope requires vary=[]")
            return
        if design.scope == ClaimScope.whole_harness:
            forbidden = [path for path in design.vary if not path.startswith("subject.")]
            if forbidden:
                raise CompilationError(
                    "whole_harness scope permits only subject.* vary paths; "
                    f"forbidden: {forbidden}"
                )
        if design.scope == ClaimScope.schedule:
            forbidden = [
                path for path in design.vary
                if path not in Compiler._SCHEDULE_VARY_PATHS
            ]
            if forbidden:
                raise CompilationError(
                    "schedule scope contains non-schedule vary paths: "
                    f"{forbidden}"
                )

    @classmethod
    def _enforce_scope_diff(
        cls,
        control: ResolvedBmpManifest,
        treatment: ResolvedBmpManifest,
        resolved_paths: tuple[str, ...],
    ) -> None:
        if control.claim_design != treatment.claim_design:
            raise CompilationError("claim design must be invariant across comparison arms")
        design = control.claim_design
        cls._validate_scope_vary_declaration(design)
        if design.scope == ClaimScope.conformance:
            return
        enforce_allowed_diff(
            control,
            treatment,
            design.vary,
            resolved_paths=resolved_paths,
        )
        unused = sorted(set(design.vary) - set(resolved_paths))
        if unused:
            raise CompilationError(
                f"declared vary paths are not activated by any arm: {unused}"
            )

    def _enforce_one_factor(
        self,
        declaration: Mapping[str, Any],
        runs: list[CompiledRun],
    ) -> None:
        contrast = runs[0].manifest.contrast
        if contrast.mode != "one_factor":
            for run in runs:
                self._validate_scope_vary_declaration(run.manifest.claim_design)
                if run.manifest.claim_design.vary:
                    raise CompilationError(
                        "declared vary paths require explicit comparison arms"
                    )
            return
        control_id = contrast.control_id
        treatment_id = contrast.treatment_id
        if not control_id or not treatment_id:
            raise CompilationError(
                "one_factor experiment requires control and treatment subject ids"
            )
        by_pair: dict[bytes, dict[str, CompiledRun]] = {}
        for run in runs:
            subject_id = run.manifest.subject.id
            if subject_id not in {control_id, treatment_id}:
                raise CompilationError(
                    f"one_factor sweep contains undeclared subject {subject_id!r}"
                )
            by_pair.setdefault(self._pair_key(run), {})[subject_id] = run
        if not by_pair:
            raise CompilationError("one_factor sweep contains no control/treatment runs")
        for pair in by_pair.values():
            if set(pair) != {control_id, treatment_id}:
                raise CompilationError("one_factor sweep has an unpaired control/treatment")
            control = pair[control_id].manifest
            treatment = pair[treatment_id].manifest
            paths = enforce_allowed_diff(
                control, treatment, control.metadata.allowed_diff
            )
            self._enforce_scope_diff(control, treatment, paths)

    def compile(
        self,
        experiment_path: str | os.PathLike[str],
        *,
        record_root: str | os.PathLike[str] | None = None,
    ) -> list[CompiledRun]:
        """Compile and isolation-check every run in an experiment TOML."""

        path = Path(experiment_path).resolve()
        # Re-read registry files on every compilation so drift cannot be hidden
        # by a long-lived compiler instance.
        self._registry_cache.clear()
        declaration = self._load_toml(path)
        experiment = declaration.get("experiment")
        if not isinstance(experiment, dict) or not experiment.get("id"):
            raise CompilationError("experiment TOML requires [experiment].id")
        if "claim_mode" in experiment:
            raise CompilationError(
                "claim_mode is forbidden; use [experiment.contrast] (ExperimentContrast)"
            )
        unknown_experiment_keys = sorted(set(experiment) - self._EXPERIMENT_KEYS)
        if unknown_experiment_keys:
            raise CompilationError(
                f"unknown [experiment] fields: {unknown_experiment_keys}"
            )
        contrast = self._parse_contrast(experiment)

        factors = declaration.get("factors")
        if factors is not None and not isinstance(factors, dict):
            raise CompilationError("[factors] must be a table")
        if isinstance(factors, dict) and any(
            key.startswith("experiment.design.")
            and isinstance(values, list)
            and len(values) > 1
            for key, values in factors.items()
        ):
            raise CompilationError("claim design must be invariant across comparison arms")
        base = {key: value for key, value in declaration.items() if key != "factors"}

        # A one-factor contrast may omit a redundant subject axis.
        if contrast.mode == "one_factor":
            if not contrast.control_id or not contrast.treatment_id:
                raise CompilationError(
                    "one_factor contrast requires control_id and treatment_id"
                )
            has_subject_axis = isinstance(factors, dict) and any(
                key in {"subject", "experiment.subject"} for key in factors
            )
            if not has_subject_axis:
                factors = dict(factors or {})
                factors = {
                    "subject": [contrast.control_id, contrast.treatment_id],
                    **factors,
                }

        runs = [
            self._compile_expanded(expanded, selected, index)
            for index, (expanded, selected) in enumerate(
                expand_factor_sweep(base, factors)
            )
        ]
        designs = {canonical_json_bytes(run.manifest.claim_design) for run in runs}
        if len(designs) != 1:
            raise CompilationError(
                "claim design must be invariant across every expanded run"
            )
        try:
            self._enforce_one_factor(declaration, runs)
        except IsolationViolation as exc:
            if record_root is not None:
                self._write_isolation_rejection(
                    Path(record_root), str(experiment["id"]), runs, exc
                )
            raise
        return runs

    @staticmethod
    def _write_isolation_rejection(
        record_root: Path,
        experiment_id: str,
        runs: list[CompiledRun],
        violation: IsolationViolation,
    ) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        directory = record_root / experiment_id / f"REJECTED_{timestamp}"
        directory.mkdir(parents=True, exist_ok=False)
        digest_basis = canonical_json_bytes([run.manifest_digest for run in runs])
        reason = "forbidden resolved diff paths: " + ", ".join(
            violation.forbidden_paths
        )
        purpose = runs[0].manifest.claim_design.purpose
        if purpose == RunPurpose.claim:
            not_executed = GateResult(
                valid=False, reason="not executed: isolation violation"
            )
            report: Any = ClaimReport(
                purpose=RunPurpose.claim,
                experiment_id=experiment_id,
                manifest_digest=sha256_bytes(digest_basis),
                gates={
                    GateName.execution_valid: not_executed,
                    GateName.protocol_valid: not_executed,
                    GateName.isolation_valid: GateResult(valid=False, reason=reason),
                    GateName.scoring_valid: not_executed,
                    GateName.statistics_valid: not_executed,
                },
                failure_breakdown={},
                lineage=(),
            )
        else:
            report = ObservationReport(
                purpose=RunPurpose.exploratory,
                experiment_id=experiment_id,
                manifest_digest=sha256_bytes(digest_basis),
                observations=(),
                failure_breakdown={},
                lineage=(),
            )
        target = directory / "isolation_violation.json"
        temporary = target.with_suffix(".json.tmp")
        with temporary.open("wb") as handle:
            handle.write(canonical_json_bytes(report) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if purpose == RunPurpose.exploratory:
            from .evidence import atomic_write_json

            atomic_write_json(
                directory / "isolation_violation_receipt.json",
                {
                    "rejection_type": "isolation_violation",
                    "reason": reason,
                    "forbidden_paths": list(violation.forbidden_paths),
                    "resolved_diff_paths": list(violation.all_paths),
                },
            )
        return target


__all__ = [
    "CompilationError",
    "CompiledRun",
    "Compiler",
    "IsolationViolation",
    "RegistryLookupError",
    "canonical_json_bytes",
    "canonical_manifest_json",
    "enforce_allowed_diff",
    "expand_factor_sweep",
    "manifest_identity_dict",
    "manifest_sha256",
    "resolved_diff_paths",
    "sha256_bytes",
]
