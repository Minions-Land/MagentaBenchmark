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
    ArtifactRef,
    AdapterCapability,
    AdapterCapabilityArtifact,
    BackendSpec,
    BenchmarkSpecAdapter,
    ClaimDesign,
    ClaimReport,
    ComparisonKind,
    ConfigurationArtifact,
    ConfigurationCompositionStep,
    ConfigurationSelection,
    ConfigurationSpec,
    DatasetArtifact,
    DatasetSpec,
    EvaluatorArtifact,
    EvaluatorSpec,
    ExecutionSpec,
    EvolutionMethodArtifact,
    EvolutionMethodSpec,
    ExperimentContrast,
    ExperimentRegimeArtifact,
    ExperimentRegimeSpec,
    FactorArtifact,
    FactorCategory,
    FactorSpec,
    GateName,
    GateResult,
    ObservationReport,
    MetricArtifact,
    MetricFormula,
    MetricSpec,
    MetricUncertaintyMethod,
    MetaEvolutionMethodArtifact,
    MetaEvolutionMethodSpec,
    ProtocolSpec,
    RegimeDependencyArtifact,
    ResolvedBmpManifest,
    ResolvedManifestMetadata,
    RunPurpose,
    SUBJECT_KIND_COMPARISON_MATRIX,
    SubjectSpecAdapter,
    TestOverrideReceipt,
)
from MagentaBench.schemas.compiler import (
    _compile_benchmark_artifact,
    _compile_dataset_artifact,
    _compile_subject_artifact,
    _resolve_execution_spec,
)
from MagentaBench.schemas.models import SubjectKind

from .configuration import (
    ConfigurationRegistryError,
    apply_dotted_overrides,
    validate_json_schema_configuration,
    validate_json_schema_document,
)
from .case_order import CaseOrderError, load_custom_case_order
from .adapter_source import (
    AdapterSourceError,
    closure_digest,
    import_closure,
    resolve_entrypoint,
    resolve_source_root,
)


class CompilationError(ValueError):
    """A declaration cannot be resolved into a valid run manifest."""


class RegistryLookupError(CompilationError):
    """A referenced registry entry is missing or ambiguous."""


_BUILTIN_BENCHMARK_LOADER_ADAPTERS = frozenset({"fake"})
_BUILTIN_BACKEND_FACTORY_ADAPTERS = frozenset({"fake", "subprocess"})
_BUILTIN_EXECUTION_COMPATIBILITY = frozenset(
    {
        ("fake", "fake", None),
        ("fake", "subprocess", None),
    }
)


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
    bare ``benchmark``, ``dataset``, ``subject``, ``protocol``, ``evolver``, and
    ``meta_evolver`` modify experiment refs.
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
            if path in {
                "benchmark",
                "dataset",
                "subject",
                "protocol",
                "evolver",
                "meta_evolver",
            }:
                declaration.setdefault("experiment", {})[path] = copy.deepcopy(value)
            elif path.startswith("experiment.") or path.startswith("execution."):
                _deep_set(declaration, path, value)
            elif path.startswith("configuration."):
                _deep_set(
                    declaration.setdefault("experiment", {}),
                    path,
                    value,
                )
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
        def configuration_projection(
            configuration: ConfigurationArtifact | None,
        ) -> Any:
            """Return the causal configuration surface, excluding derivations.

            ``artifact_digest`` and ``schema_digest`` are identities derived
            from the configuration recipe.  Treating those hashes as causal
            paths makes a harmless value intervention impossible to declare,
            while omitting source/profile/schema identity would allow a source
            replacement to masquerade as the same intervention.  The
            projection therefore keeps the resolved values plus the immutable
            source and ownership/schema contract, but deliberately excludes
            fields that are pure digest or replay metadata.  Composition is a
            replay/provenance record; its semantic outputs are represented by
            the fields above and it is not an independent intervention path.
            """

            if configuration is None:
                return None
            return {
                "values": configuration.values,
                "schema": configuration.json_schema,
                "ownership": configuration.ownership,
                "adapter": configuration.adapter,
                "profiles": list(configuration.profiles),
                "source_refs": [
                    ref.identity_data() for ref in configuration.source_refs
                ],
            }

        left = {
            "benchmark": control.benchmark.model_dump(mode="json"),
            "dataset": (
                None
                if control.dataset is None
                else control.dataset.model_dump(mode="json", exclude={"source"})
            ),
            "subject": control.subject.model_dump(mode="json"),
            "execution": control.execution.model_dump(mode="json"),
            "configuration": configuration_projection(control.metadata.configuration),
            "evolver": (
                None
                if control.metadata.evolver is None
                else control.metadata.evolver.model_dump(mode="json")
            ),
            "meta_evolver": (
                None
                if control.metadata.meta_evolver is None
                else control.metadata.meta_evolver.model_dump(mode="json")
            ),
        }
        right = {
            "benchmark": treatment.benchmark.model_dump(mode="json"),
            "dataset": (
                None
                if treatment.dataset is None
                else treatment.dataset.model_dump(mode="json", exclude={"source"})
            ),
            "subject": treatment.subject.model_dump(mode="json"),
            "execution": treatment.execution.model_dump(mode="json"),
            "configuration": configuration_projection(treatment.metadata.configuration),
            "evolver": (
                None
                if treatment.metadata.evolver is None
                else treatment.metadata.evolver.model_dump(mode="json")
            ),
            "meta_evolver": (
                None
                if treatment.metadata.meta_evolver is None
                else treatment.metadata.meta_evolver.model_dump(mode="json")
            ),
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
            "dataset",
            "evaluator",
            "metrics",
            "subject",
            "protocol",
            "regime",
            "stage",
            "factors",
            "contrast",
            "design",
            "configuration",
            "evolver",
            "meta_evolver",
        }
    )
    _REGISTRY_SECTIONS = {
        "benchmark": ("benchmarks", BenchmarkSpecAdapter),
        "dataset": ("datasets", DatasetSpec),
        "evaluator": ("evaluators", EvaluatorSpec),
        "metric": ("metrics", MetricSpec),
        "subject": ("subjects", SubjectSpecAdapter),
        "protocol": ("protocols", ProtocolSpec),
        "regime": ("regimes", ExperimentRegimeSpec),
        "backend": ("backends", BackendSpec),
        "configuration": ("configurations", ConfigurationSpec),
        "evolver": ("evolvers", EvolutionMethodSpec),
        "meta_evolver": ("meta_evolvers", MetaEvolutionMethodSpec),
        "factor": ("factors", FactorSpec),
        "adapter": ("adapters", AdapterCapability),
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

    def verify_registry_lock(self, lock_path: str | os.PathLike[str] | None = None):
        """Verify the repository-level TOML registry lock when requested.

        This is deliberately explicit: conformance tests and temporary
        projects commonly add fixture declarations after copying the base
        registry, while release/CI entry points can require the lock before
        compiling a claim.
        """

        from MagentaBench.schemas.registry_lock import verify_registry_lock

        return verify_registry_lock(self.registry_root, lock_path)

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
        return _compile_benchmark_artifact(
            spec,
            declaration_path=registry_path,
        )

    def _dataset_artifact(self, entry_id: str) -> DatasetArtifact:
        spec, registry_path = self._lookup("dataset", entry_id)
        return _compile_dataset_artifact(spec, declaration_path=registry_path)

    def _subject_artifact(self, entry_id: str):
        spec, registry_path = self._lookup("subject", entry_id)
        return _compile_subject_artifact(spec, base_dir=registry_path.parent)

    def _factor_artifact(self, entry_id: str) -> FactorArtifact:
        factor, declaration_path = self._lookup("factor", entry_id)
        declaration_ref = self._configuration_source_ref(declaration_path)
        provisional = FactorArtifact(
            factor=factor,
            declaration_ref=declaration_ref,
            artifact_digest="0" * 64,
        )
        return provisional.model_copy(
            update={"artifact_digest": provisional.canonical_digest()}
        )

    def _evaluator_artifact(self, entry_id: str) -> EvaluatorArtifact:
        evaluator, declaration_path = self._lookup("evaluator", entry_id)
        declaration_ref = self._configuration_source_ref(declaration_path)
        provisional = EvaluatorArtifact(
            evaluator=evaluator,
            declaration_ref=declaration_ref,
            artifact_digest="0" * 64,
        )
        return provisional.model_copy(
            update={"artifact_digest": provisional.canonical_digest()}
        )

    def _metric_artifact(self, entry_id: str) -> MetricArtifact:
        metric, declaration_path = self._lookup("metric", entry_id)
        declaration_ref = self._configuration_source_ref(declaration_path)
        provisional = MetricArtifact(
            metric=metric,
            declaration_ref=declaration_ref,
            artifact_digest="0" * 64,
        )
        return provisional.model_copy(
            update={"artifact_digest": provisional.canonical_digest()}
        )

    def _regime_artifact(self, entry_id: str) -> ExperimentRegimeArtifact:
        """Resolve a stage DAG and bind every referenced registry declaration."""

        regime, declaration_path = self._lookup("regime", entry_id)
        dependencies: dict[tuple[str, str], RegimeDependencyArtifact] = {}

        def bind(
            registry_kind: str,
            dependency_id: str,
            *,
            artifact_digest: str,
            dependency_path: Path,
        ) -> None:
            key = (registry_kind, dependency_id)
            candidate = RegimeDependencyArtifact(
                registry_kind=registry_kind,
                id=dependency_id,
                declaration_ref=self._configuration_source_ref(dependency_path),
                artifact_digest=artifact_digest,
            )
            previous = dependencies.get(key)
            if previous is not None and previous != candidate:
                raise CompilationError(
                    f"regime dependency {registry_kind}:{dependency_id} drift"
                )
            dependencies[key] = candidate

        for stage in regime.stages:
            benchmark = self._benchmark_artifact(stage.benchmark_id)
            benchmark_spec, benchmark_path = self._lookup(
                "benchmark", stage.benchmark_id
            )
            del benchmark_spec
            bind(
                "benchmark",
                stage.benchmark_id,
                artifact_digest=benchmark.artifact_digest,
                dependency_path=benchmark_path,
            )
            dataset = self._dataset_artifact(stage.dataset_id)
            _, dataset_path = self._lookup("dataset", stage.dataset_id)
            bind(
                "dataset",
                stage.dataset_id,
                artifact_digest=dataset.artifact_digest,
                dependency_path=dataset_path,
            )
            evaluator = self._evaluator_artifact(stage.evaluator_id)
            _, evaluator_path = self._lookup("evaluator", stage.evaluator_id)
            bind(
                "evaluator",
                stage.evaluator_id,
                artifact_digest=evaluator.artifact_digest,
                dependency_path=evaluator_path,
            )
            protocol_spec, protocol_path = self._lookup(
                "protocol", stage.protocol_id
            )
            protocol_identity = {
                "protocol": protocol_spec.identity_data(),
                "declaration_ref": self._configuration_source_ref(
                    protocol_path
                ).identity_data(),
            }
            bind(
                "protocol",
                stage.protocol_id,
                artifact_digest=sha256_bytes(
                    canonical_json_bytes(protocol_identity)
                ),
                dependency_path=protocol_path,
            )
            metric_artifacts = tuple(
                self._metric_artifact(metric_id) for metric_id in stage.metric_ids
            )
            metric_ids = {artifact.metric.id for artifact in metric_artifacts}
            emitted = {
                binding.metric_id for binding in evaluator.evaluator.metrics
            }
            missing_emitted = sorted(emitted - metric_ids)
            missing_inputs = sorted(
                {
                    dependency
                    for artifact in metric_artifacts
                    for dependency in artifact.metric.inputs
                    if dependency not in metric_ids
                }
            )
            if missing_emitted or missing_inputs:
                raise CompilationError(
                    f"regime stage {stage.id!r} metric closure is incomplete: "
                    f"evaluator={missing_emitted}, dependencies={missing_inputs}"
                )
            if evaluator.evaluator.scoring_kind.value != "binary" and any(
                artifact.metric.formula
                in {
                    MetricFormula.pass_at_1_v1,
                    MetricFormula.pass_at_k_unbiased_v1,
                    MetricFormula.pass_power_k_v1,
                    MetricFormula.empirical_any_at_k_v1,
                    MetricFormula.empirical_all_at_k_v1,
                }
                for artifact in metric_artifacts
            ):
                raise CompilationError(
                    f"regime stage {stage.id!r} selects binary pass metrics "
                    "without a binary evaluator"
                )
            if dataset.adapter != benchmark.adapter:
                raise CompilationError(
                    f"regime stage {stage.id!r} dataset/benchmark adapter mismatch"
                )
            if evaluator.evaluator.adapter != benchmark.adapter:
                raise CompilationError(
                    f"regime stage {stage.id!r} evaluator/benchmark adapter mismatch"
                )
            for artifact in metric_artifacts:
                parameters = artifact.metric.parameters
                if (
                    parameters is not None
                    and parameters.k is not None
                    and parameters.k > protocol_spec.rollouts_per_case
                ):
                    raise CompilationError(
                        f"regime stage {stage.id!r} metric {artifact.metric.id!r} "
                        f"requires k={parameters.k}, but protocol plans only "
                        f"{protocol_spec.rollouts_per_case} rollouts per task"
                    )
                _, metric_path = self._lookup("metric", artifact.metric.id)
                bind(
                    "metric",
                    artifact.metric.id,
                    artifact_digest=artifact.artifact_digest,
                    dependency_path=metric_path,
                )

        provisional = ExperimentRegimeArtifact(
            regime=regime,
            declaration_ref=self._configuration_source_ref(declaration_path),
            dependencies=tuple(
                dependencies[key] for key in sorted(dependencies)
            ),
            artifact_digest="0" * 64,
        )
        return provisional.model_copy(
            update={"artifact_digest": provisional.canonical_digest()}
        )

    def _evolver_artifact(
        self,
        entry_id: str,
        configuration: ConfigurationArtifact,
    ) -> EvolutionMethodArtifact:
        method, declaration_path = self._lookup("evolver", entry_id)
        payload = method.model_dump(mode="json")
        payload.update(
            {
                "declaration_ref": self._configuration_source_ref(declaration_path),
                "configuration_digest": configuration.artifact_digest,
                "artifact_digest": "0" * 64,
            }
        )
        provisional = EvolutionMethodArtifact.model_validate(payload)
        return provisional.model_copy(
            update={"artifact_digest": provisional.canonical_digest()}
        )

    def _meta_evolver_artifact(
        self,
        entry_id: str,
        configuration: ConfigurationArtifact,
        parent: EvolutionMethodArtifact,
    ) -> MetaEvolutionMethodArtifact:
        method, declaration_path = self._lookup("meta_evolver", entry_id)
        if method.parent_evolver_id != parent.id:
            raise CompilationError(
                f"meta-evolver {entry_id!r} parent does not match {parent.id!r}"
            )
        payload = method.model_dump(mode="json")
        payload.update(
            {
                "declaration_ref": self._configuration_source_ref(declaration_path),
                "configuration_digest": configuration.artifact_digest,
                "parent_evolver_digest": parent.artifact_digest,
                "artifact_digest": "0" * 64,
            }
        )
        provisional = MetaEvolutionMethodArtifact.model_validate(payload)
        return provisional.model_copy(
            update={"artifact_digest": provisional.canonical_digest()}
        )

    @staticmethod
    def _validate_evolution_method_binding(
        method: EvolutionMethodArtifact | MetaEvolutionMethodArtifact,
        *,
        configuration: ConfigurationArtifact,
        metrics: tuple[MetricArtifact, ...],
        subject_adapter: str,
    ) -> None:
        if method.subject_adapter != subject_adapter:
            raise CompilationError(
                f"evolution method {method.id!r} requires subject adapter "
                f"{method.subject_adapter!r}, got {subject_adapter!r}"
            )
        if method.configuration_profile_id not in configuration.profiles:
            raise CompilationError(
                f"evolution method {method.id!r} requires configuration profile "
                f"{method.configuration_profile_id!r}"
            )
        metric_by_id = {artifact.metric.id: artifact.metric for artifact in metrics}
        metric = metric_by_id.get(method.selection.metric_id)
        if metric is None:
            raise CompilationError(
                f"evolution method {method.id!r} requires selected metric "
                f"{method.selection.metric_id!r}"
            )
        if metric.direction.value != method.selection.direction:
            raise CompilationError(
                f"evolution method {method.id!r} selection direction differs from "
                f"metric {metric.id!r}"
            )
        observed: Any = configuration.values
        try:
            for part in method.selection_configuration_path.split("."):
                if not isinstance(observed, Mapping):
                    raise KeyError(part)
                observed = observed[part]
        except KeyError as exc:
            raise CompilationError(
                f"evolution method {method.id!r} selection configuration is absent"
            ) from exc
        if observed != method.selection.configuration_data():
            raise CompilationError(
                f"evolution method {method.id!r} selection configuration drift"
            )
        wrong_owners = sorted(
            path
            for path in (
                f"{method.selection_configuration_path}.{key}"
                for key in method.selection.configuration_data()
            )
            if configuration.ownership.get(path) != method.adapter
        )
        if wrong_owners:
            raise CompilationError(
                f"evolution method {method.id!r} selection configuration has "
                f"wrong ownership: {wrong_owners}"
            )

    def _resolved_protocol(self, entry_id: str) -> ProtocolSpec:
        protocol, _ = self._lookup("protocol", entry_id)
        if protocol.case_order != "custom":
            return protocol
        declaration = protocol.custom_order
        if declaration is None:  # ProtocolSpec already rejects this.
            raise CompilationError("custom protocol is missing custom_order")
        relative = Path(declaration.source)
        if (
            relative.is_absolute()
            or relative.as_posix() != declaration.source
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise CompilationError(
                "custom order source must be a normalized project-relative path"
            )
        source = self._resolve_configuration_source(self.project_root / relative)
        try:
            source.relative_to(self.project_root)
        except ValueError as exc:
            raise CompilationError("custom order source escapes project root") from exc
        resolved_declaration = declaration.model_copy(
            update={"source": str(source)}
        )
        try:
            load_custom_case_order(resolved_declaration)
        except CaseOrderError as exc:
            raise CompilationError(str(exc)) from exc
        return protocol.model_copy(update={"custom_order": resolved_declaration})

    def _adapter_capability_artifact(
        self, adapter: str, adapter_kind: str
    ) -> AdapterCapabilityArtifact | None:
        directory = self.registry_root / "adapters"
        if not directory.exists():
            return None
        matches: list[tuple[AdapterCapability, Path]] = []
        for path in sorted(directory.glob("*.toml")):
            raw = self._load_toml(path)
            declaration = raw.get("adapter")
            if (
                not isinstance(declaration, Mapping)
                or declaration.get("adapter") != adapter
                or declaration.get("adapter_kind") != adapter_kind
            ):
                continue
            if set(raw) != {"adapter"}:
                raise CompilationError(
                    f"adapter registry {path} requires only [adapter]"
                )
            try:
                capability = AdapterCapability.model_validate(declaration)
            except pydantic.ValidationError as exc:
                if (
                    adapter_kind == "execution"
                    and "none_model_sentinels" in str(exc)
                ):
                    raise CompilationError(
                        "execution capability declares a real model as a none "
                        "sentinel; ModelActivationReceipt missing"
                    ) from exc
                raise CompilationError(f"invalid adapter registry {path}: {exc}") from exc
            matches.append((capability, path))
        if not matches:
            return None
        if len(matches) > 1:
            raise CompilationError(
                f"duplicate {adapter_kind} capability {adapter!r}"
            )
        capability, declaration_path = matches[0]
        try:
            source_root = resolve_source_root(self.project_root, capability.source)
            implementation_path = resolve_entrypoint(source_root, capability.entrypoint)
            closure_paths = import_closure(source_root, implementation_path)
            closure_paths_relative = tuple(
                path.relative_to(source_root).as_posix() for path in closure_paths
            )
            closure_refs = tuple(
                self._configuration_source_ref(path) for path in closure_paths
            )
            closure_hash = closure_digest(source_root, closure_paths)
        except AdapterSourceError as exc:
            raise CompilationError(str(exc)) from exc
        implementation_ref = self._configuration_source_ref(implementation_path)
        if implementation_ref.sha256 != capability.digest:
            raise CompilationError(
                f"adapter source digest mismatch: {capability.adapter!r}"
            )
        artifact = AdapterCapabilityArtifact(
            capability=capability,
            declaration_ref=self._configuration_source_ref(declaration_path),
            implementation_ref=implementation_ref,
            source_closure_refs=closure_refs,
            source_closure_paths=closure_paths_relative,
            source_closure_digest=closure_hash,
            artifact_digest="0" * 64,
        )
        return artifact.model_copy(
            update={"artifact_digest": artifact.canonical_digest()}
        )

    @staticmethod
    def _resolve_configuration_source(path: Path) -> Path:
        absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
        for candidate in (absolute, *absolute.parents):
            if candidate.is_symlink():
                raise CompilationError(
                    f"configuration source must not contain a symlink: {candidate}"
                )
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as exc:
            raise CompilationError(
                f"configuration source is missing or unreadable: {absolute}"
            ) from exc
        if not resolved.is_file():
            raise CompilationError(
                f"configuration source is not a file: {resolved}"
            )
        return resolved

    @staticmethod
    def _configuration_source_ref(path: Path) -> ArtifactRef:
        resolved = Compiler._resolve_configuration_source(path)
        content = resolved.read_bytes()
        return ArtifactRef(
            path=str(resolved),
            sha256=sha256_bytes(content),
            size_bytes=len(content),
        )

    @staticmethod
    def _merge_configuration(
        base: Mapping[str, Any], overlay: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(base))
        for key, value in overlay.items():
            current = result.get(key)
            if isinstance(current, Mapping) and isinstance(value, Mapping):
                result[key] = Compiler._merge_configuration(current, value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    @staticmethod
    def _merge_configuration_ownership(
        base: Mapping[str, str],
        overlay: Mapping[str, Any],
        owner: str,
        *,
        prefix: str = "",
    ) -> dict[str, str]:
        """Track the last adapter that contributed each resolved leaf path."""

        result = dict(base)
        for key, value in overlay.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            descendants = tuple(
                existing
                for existing in result
                if existing == path or existing.startswith(path + ".")
            )
            if isinstance(value, Mapping) and value:
                # Deep merge preserves untouched children from earlier layers;
                # only paths explicitly present in this overlay are reassigned.
                result.pop(path, None)
                result = Compiler._merge_configuration_ownership(
                    result, value, owner, prefix=path
                )
            elif isinstance(value, Mapping) and any(
                existing.startswith(path + ".") for existing in result
            ):
                # An empty table over an existing table is a no-op under the
                # value merge semantics, so it must not steal child ownership.
                continue
            else:
                for existing in descendants:
                    result.pop(existing, None)
                result[path] = owner
        return result

    def _resolve_configuration(
        self,
        raw: Any,
        *,
        base_dir: Path,
        additional_files: Iterable[str | os.PathLike[str]] = (),
        additional_raw_files: Iterable[str | os.PathLike[str]] = (),
        additional_profiles: Iterable[str] = (),
        additional_values: Mapping[str, Any] | None = None,
    ) -> ConfigurationArtifact | None:
        """Resolve profiles/files/inline values into one content-addressed tree."""

        extra_files = tuple(additional_files)
        extra_raw_files = tuple(additional_raw_files)
        extra_profiles = tuple(additional_profiles)
        extra_values = {} if additional_values is None else dict(additional_values)
        if len(set(map(os.fspath, extra_files))) != len(extra_files):
            raise CompilationError("additional configuration files must be unique")
        if len(set(map(os.fspath, extra_raw_files))) != len(extra_raw_files):
            raise CompilationError(
                "additional raw configuration files must be unique"
            )
        if len(set(extra_profiles)) != len(extra_profiles):
            raise CompilationError("additional configuration profiles must be unique")
        if raw is None and not (
            extra_files or extra_raw_files or extra_profiles or extra_values
        ):
            return None
        try:
            selection = ConfigurationSelection.model_validate(
                {} if raw is None else raw
            )
        except pydantic.ValidationError as exc:
            raise CompilationError(f"invalid [experiment.configuration]: {exc}") from exc
        if not (
            selection.profiles
            or selection.files
            or selection.raw_files
            or selection.values
            or extra_files
            or extra_raw_files
            or extra_profiles
            or extra_values
        ):
            raise CompilationError(
                "[experiment.configuration] must select a profile, file, or value"
            )

        values: Mapping[str, Any] = {}
        schema: Mapping[str, Any] = {}
        profile_ids: list[str] = []
        source_refs: list[ArtifactRef] = []
        adapter: str | None = None
        applied_adapters: list[str] = []
        ownership: dict[str, str] = {}
        composition: list[ConfigurationCompositionStep] = []
        visiting: list[str] = []
        applied_profile_sources: set[tuple[str, str, int]] = set()
        profile_source_by_id: dict[str, tuple[str, int]] = {}

        def load_profile(profile_id: str) -> tuple[ConfigurationSpec, Path, str]:
            spec, path = self._lookup("configuration", profile_id)
            return spec, path, "envelope"

        def apply_spec(
            spec: ConfigurationSpec,
            source_path: Path,
            mode: str,
            *,
            root: bool = True,
            layer_kind: str = "profile",
        ) -> None:
            nonlocal values, schema, adapter, ownership
            if spec.id in visiting:
                cycle = " -> ".join((*visiting, spec.id))
                raise CompilationError(f"configuration extends cycle: {cycle}")
            visiting.append(spec.id)
            for parent in spec.extends:
                parent_spec, parent_path, parent_mode = load_profile(parent)
                apply_spec(
                    parent_spec,
                    parent_path,
                    parent_mode,
                    root=False,
                    layer_kind="profile",
                )
            visiting.pop()
            try:
                validate_json_schema_document(spec.json_schema)
            except ConfigurationRegistryError as exc:
                raise CompilationError(
                    f"configuration profile {spec.id!r} fails its JSON Schema: {exc}"
                ) from exc
            values = self._merge_configuration(values, spec.values)
            schema = self._merge_configuration(schema, spec.json_schema)
            ownership = self._merge_configuration_ownership(
                ownership, spec.values, spec.adapter
            )
            if spec.adapter not in applied_adapters:
                applied_adapters.append(spec.adapter)
            if spec.id not in profile_ids:
                profile_ids.append(spec.id)
            reference = self._configuration_source_ref(source_path)
            previous_source = profile_source_by_id.get(spec.id)
            current_source = (reference.sha256, reference.size_bytes)
            if previous_source is not None and previous_source != current_source:
                raise CompilationError(
                    f"configuration profile id {spec.id!r} resolves to multiple source objects"
                )
            profile_source_by_id[spec.id] = current_source
            if (reference.sha256, reference.size_bytes) not in {
                (item.sha256, item.size_bytes) for item in source_refs
            }:
                source_refs.append(reference)
            source_key = (spec.id, reference.sha256, reference.size_bytes)
            if source_key not in applied_profile_sources:
                applied_profile_sources.add(source_key)
                composition.append(
                    ConfigurationCompositionStep(
                        kind=layer_kind,
                        id=spec.id,
                        source_ref=reference,
                        mode=mode,
                        root=root,
                        values=spec.values,
                        json_schema=spec.json_schema,
                        adapter=spec.adapter,
                        extends=spec.extends,
                    )
                )

        for profile_id in selection.profiles:
            spec, path, mode = load_profile(profile_id)
            apply_spec(spec, path, mode)

        for profile_id in extra_profiles:
            if profile_id in selection.profiles:
                raise CompilationError(
                    f"configuration profile {profile_id!r} was selected more than once"
                )
            spec, path, mode = load_profile(profile_id)
            apply_spec(spec, path, mode)

        envelope_paths: list[Path] = [
            self._resolve_configuration_source(base_dir / relative_path)
            for relative_path in selection.files
        ]
        envelope_paths.extend(
            self._resolve_configuration_source(
                Path(item).expanduser()
                if Path(item).expanduser().is_absolute()
                else base_dir / Path(item)
            )
            for item in extra_files
        )
        for path in envelope_paths:
            document = self._load_toml(path)
            if "configuration" not in document:
                raise CompilationError(
                    f"external configuration {path} requires a [configuration] envelope; "
                    "raw documents require explicit raw_files"
                )
            unexpected = sorted(set(document) - {"configuration"})
            if unexpected or not isinstance(document.get("configuration"), Mapping):
                raise CompilationError(
                    f"external configuration {path} contains a malformed [configuration] envelope"
                )
            table = document.get("configuration")
            if table.get("kind") != "configuration":
                raise CompilationError(
                    f"external configuration {path} has a malformed [configuration] envelope"
                )
            try:
                spec = ConfigurationSpec.model_validate(table)
            except pydantic.ValidationError as exc:
                raise CompilationError(
                    f"invalid external configuration {path}: {exc}"
                ) from exc
            apply_spec(spec, path, "envelope", layer_kind="file")

        raw_paths: list[Path] = [
            self._resolve_configuration_source(base_dir / relative_path)
            for relative_path in selection.raw_files
        ]
        raw_paths.extend(
            self._resolve_configuration_source(
                Path(item).expanduser()
                if Path(item).expanduser().is_absolute()
                else base_dir / Path(item)
            )
            for item in extra_raw_files
        )
        for path in raw_paths:
            document = self._load_toml(path)
            if "configuration" in document:
                raise CompilationError(
                    f"raw external configuration {path} contains a [configuration] "
                    "table; use files for an explicit envelope"
                )
            digest = sha256_bytes(path.read_bytes())
            spec = ConfigurationSpec(
                id=f"raw-{digest[:16]}",
                kind="configuration",
                adapter="generic",
                values=document,
            )
            apply_spec(spec, path, "raw", layer_kind="file")

        values = self._merge_configuration(values, selection.values)
        if extra_values and any("." in str(key) for key in extra_values):
            try:
                values = apply_dotted_overrides(values, extra_values)
                extra_values_tree = apply_dotted_overrides({}, extra_values)
            except ConfigurationRegistryError as exc:
                raise CompilationError(f"invalid configuration override: {exc}") from exc
        else:
            values = self._merge_configuration(values, extra_values)
            extra_values_tree = extra_values
        ownership = self._merge_configuration_ownership(
            ownership, selection.values, "generic"
        )
        ownership = self._merge_configuration_ownership(
            ownership, extra_values_tree, "generic"
        )
        if selection.values:
            composition.append(
                ConfigurationCompositionStep(
                    kind="inline", values=selection.values, adapter="generic"
                )
            )
        if extra_values:
            composition.append(
                ConfigurationCompositionStep(
                    kind="inline", values=extra_values_tree, adapter="generic"
                )
            )
        try:
            validate_json_schema_configuration(values, schema)
        except ConfigurationRegistryError as exc:
            raise CompilationError(f"resolved configuration fails its JSON Schema: {exc}") from exc
        non_generic_adapters = tuple(
            item for item in applied_adapters if item != "generic"
        )
        if len(set(non_generic_adapters)) == 1:
            adapter = non_generic_adapters[0]
        elif len(set(non_generic_adapters)) > 1:
            adapter = "composite"
        else:
            adapter = "generic"
        schema_digest = sha256_bytes(canonical_json_bytes(schema))
        artifact = ConfigurationArtifact(
            id=profile_ids[-1] if profile_ids else "inline",
            adapter=adapter,
            profiles=tuple(profile_ids),
            source_refs=tuple(source_refs),
            schema_digest=schema_digest,
            values=values,
            json_schema=schema,
            ownership=ownership,
            composition=tuple(composition),
            artifact_digest="0" * 64,
        )
        return artifact.model_copy(update={"artifact_digest": artifact.canonical_digest()})

    _SCHEDULER_ADAPTER = "magentabench.scheduler"
    # Conservative core fallbacks for adapters shipped with MagentaBench.
    # External adapters must carry these policies in their digest-bound TOML
    # capability instead of adding another tuple or adapter name here.
    _CORE_BACKEND_DEFAULT_READ_SETS = {
        "fake": frozenset(),
        "subprocess": frozenset(),
        "aose-docker": frozenset(),
        "harbor-shim": frozenset(
            {
                "agent_kwargs",
                "agent_override",
                "agent_timeout_multiplier",
                "environment_type",
            }
        ),
    }
    _CORE_NONE_MODEL_SENTINELS = {
        "fake": frozenset({"none/deterministic"}),
        "subprocess": frozenset({"none/echo"}),
        "aose-docker": frozenset({"none"}),
        "harbor-shim": frozenset({"none/echo"}),
    }
    _CORE_STATE_RESET_POLICIES = {
        "fake": frozenset({"never"}),
        "subprocess": frozenset({"per_rollout"}),
        "aose-docker": frozenset({"never"}),
        "harbor-shim": frozenset({"never"}),
    }
    _CORE_SUBJECT_COMPATIBILITY = frozenset(
        {
            ("fake", "fake", None),
            ("opaque_agent", "fake", "task_to_output"),
            ("opaque_agent", "cli-agent", "aosebench-container-v1"),
        }
    )
    _NONE_MODELS = frozenset({"none", "none/deterministic", "none/echo"})

    def _validate_comparison_subject(
        self, manifest: ResolvedBmpManifest
    ) -> None:
        """Keep execution packaging separate from the semantic research subject."""

        comparison_kind = manifest.claim_design.comparison_kind
        subject = manifest.subject
        if subject.kind == "fake":
            if comparison_kind is not None:
                raise CompilationError(
                    "fake conformance fixtures cannot declare a research comparison kind"
                )
            if manifest.claim_design.purpose != RunPurpose.exploratory:
                raise CompilationError(
                    "fake conformance fixtures require exploratory purpose"
                )
            return
        if comparison_kind is None:
            raise CompilationError(
                "a real subject requires one of the four comparison kinds"
            )
        declared = ComparisonKind(subject.comparison_kind)
        if comparison_kind != declared:
            raise CompilationError(
                f"comparison kind {comparison_kind.value!r} differs from the "
                f"subject registration {declared.value!r}"
            )
        legal = SUBJECT_KIND_COMPARISON_MATRIX.get(subject.kind, frozenset())
        if comparison_kind not in legal:
            raise CompilationError(
                f"comparison kind {comparison_kind.value!r} is incompatible with "
                f"subject artifact kind {subject.kind!r}"
            )
        if (
            comparison_kind == ComparisonKind.evolution_method
            and subject.kind != "evolver"
        ):
            raise CompilationError("evolution_method requires an evolver subject")
        if (
            comparison_kind == ComparisonKind.meta_evolution_method
            and subject.kind != "meta_evolver"
        ):
            raise CompilationError(
                "meta_evolution_method requires a meta_evolver subject"
            )

    def _compile_expanded(
        self,
        declaration: Mapping[str, Any],
        factor_values: Mapping[str, Any],
        factor_artifacts: tuple[FactorArtifact, ...],
        run_index: int,
        *,
        base_dir: Path,
        config_files: Iterable[str | os.PathLike[str]] = (),
        raw_config_files: Iterable[str | os.PathLike[str]] = (),
        config_profiles: Iterable[str] = (),
        config_overrides: Mapping[str, Any] | None = None,
    ) -> CompiledRun:
        unexpected_sections = sorted(
            set(declaration) - {"experiment", "execution"}
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
        required = (
            "id",
            "benchmark",
            "dataset",
            "evaluator",
            "metrics",
            "subject",
            "protocol",
        )
        missing = [name for name in required if not experiment.get(name)]
        if missing:
            raise CompilationError(f"[experiment] missing fields: {', '.join(missing)}")
        design_raw = experiment.get("design")
        if not isinstance(design_raw, dict):
            raise CompilationError(
                "[experiment.design] is required with comparison_kind and purpose"
            )
        if set(design_raw).intersection({"scope", "vary", "intervention_factor_id"}):
            raise CompilationError(
                "scope/vary/intervention_factor_id are derived or retired; "
                "select registered factors instead"
            )
        contrast = self._parse_contrast(experiment)
        resolved_design = dict(design_raw)
        resolved_design["intervention_factor_id"] = (
            contrast.factor_id if contrast.mode == "one_factor" else None
        )
        if self.allow_test_override:
            resolved_design["comparison_kind"] = None
            resolved_design["purpose"] = RunPurpose.exploratory
        try:
            claim_design = ClaimDesign.model_validate(resolved_design)
        except pydantic.ValidationError as exc:
            raise CompilationError(f"invalid [experiment.design]: {exc}") from exc
        factor_by_id = {
            artifact.factor.id: artifact for artifact in factor_artifacts
        }
        if contrast.factor_id is not None and contrast.factor_id not in factor_by_id:
            raise CompilationError(
                f"contrast factor {contrast.factor_id!r} is not selected by the experiment"
            )
        for artifact in factor_artifacts:
            factor = artifact.factor
            if factor.category in {
                FactorCategory.repetition,
                FactorCategory.conformance_fixture,
            }:
                if (
                    factor.category == FactorCategory.conformance_fixture
                    and claim_design.comparison_kind is not None
                ):
                    raise CompilationError(
                        "conformance_fixture factors cannot enter research comparisons"
                    )
                continue
            if claim_design.comparison_kind is None:
                raise CompilationError(
                    f"research factor {factor.id!r} requires a comparison kind"
                )
            if claim_design.comparison_kind not in factor.applies_to:
                raise CompilationError(
                    f"factor {factor.id!r} does not apply to comparison kind "
                    f"{claim_design.comparison_kind.value!r}"
                )
        configuration = self._resolve_configuration(
            experiment.get("configuration"),
            base_dir=base_dir,
            additional_files=config_files,
            additional_raw_files=raw_config_files,
            additional_profiles=config_profiles,
            additional_values=config_overrides,
        )
        if claim_design.purpose == RunPurpose.claim and configuration is not None:
            unregistered_layers = [
                layer.kind
                for layer in configuration.composition
                if layer.kind != "profile"
            ]
            if unregistered_layers:
                raise CompilationError(
                    "claim configuration accepts only registered TOML profile ids; "
                    f"unregistered layers: {unregistered_layers}"
                )

        try:
            execution = ExecutionSpec.model_validate(execution_raw)
        except pydantic.ValidationError as exc:
            raise CompilationError(f"invalid [execution]: {exc}") from exc
        unsupported_override_fields = sorted(
            set(execution.backend_overrides) - {"defaults"}
        )
        if unsupported_override_fields:
            raise CompilationError(
                "backend_overrides contains unbound backend identity fields: "
                f"{unsupported_override_fields}"
            )
        if claim_design.purpose == RunPurpose.claim and execution.backend_overrides:
            raise CompilationError(
                "claim execution forbids inline backend_overrides; register a backend"
            )
        benchmark = self._benchmark_artifact(str(experiment["benchmark"]))
        dataset = self._dataset_artifact(str(experiment["dataset"]))
        if dataset.adapter != benchmark.adapter:
            raise CompilationError(
                f"dataset adapter {dataset.adapter!r} does not match benchmark "
                f"adapter {benchmark.adapter!r}"
            )
        evaluator = self._evaluator_artifact(str(experiment["evaluator"]))
        if evaluator.evaluator.adapter != benchmark.adapter:
            raise CompilationError(
                f"evaluator adapter {evaluator.evaluator.adapter!r} does not match "
                f"benchmark adapter {benchmark.adapter!r}"
            )
        raw_metric_ids = experiment.get("metrics")
        if not isinstance(raw_metric_ids, (list, tuple)) or not raw_metric_ids:
            raise CompilationError("[experiment].metrics must be a non-empty array")
        if any(not isinstance(metric_id, str) or not metric_id for metric_id in raw_metric_ids):
            raise CompilationError("[experiment].metrics must contain registry ids")
        metric_ids = tuple(raw_metric_ids)
        if len(set(metric_ids)) != len(metric_ids):
            raise CompilationError("[experiment].metrics must contain unique ids")
        unresolved_metrics = tuple(
            self._metric_artifact(metric_id) for metric_id in metric_ids
        )
        metric_by_id = {
            artifact.metric.id: artifact for artifact in unresolved_metrics
        }
        selected_metric_ids = {
            artifact.metric.id for artifact in unresolved_metrics
        }
        emitted_metric_ids = {
            binding.metric_id for binding in evaluator.evaluator.metrics
        }
        missing_emitted = sorted(emitted_metric_ids - selected_metric_ids)
        if missing_emitted:
            raise CompilationError(
                f"experiment metrics omit evaluator bindings: {missing_emitted}"
            )
        missing_inputs = sorted(
            {
                input_id
                for artifact in unresolved_metrics
                for input_id in artifact.metric.inputs
                if input_id not in selected_metric_ids
            }
        )
        if missing_inputs:
            raise CompilationError(
                f"experiment metrics omit registered dependencies: {missing_inputs}"
            )
        ordered_metrics: list[MetricArtifact] = []
        visiting_metrics: list[str] = []
        visited_metrics: set[str] = set()

        def append_metric(metric_id: str) -> None:
            if metric_id in visited_metrics:
                return
            if metric_id in visiting_metrics:
                cycle = " -> ".join((*visiting_metrics, metric_id))
                raise CompilationError(f"registered metric dependency cycle: {cycle}")
            visiting_metrics.append(metric_id)
            artifact = metric_by_id[metric_id]
            for dependency in artifact.metric.inputs:
                append_metric(dependency)
            visiting_metrics.pop()
            visited_metrics.add(metric_id)
            ordered_metrics.append(artifact)

        for metric_id in metric_ids:
            append_metric(metric_id)
        metrics = tuple(ordered_metrics)
        binary_success_formulas = {
            MetricFormula.pass_at_1_v1,
            MetricFormula.pass_at_k_unbiased_v1,
            MetricFormula.pass_power_k_v1,
            MetricFormula.empirical_any_at_k_v1,
            MetricFormula.empirical_all_at_k_v1,
        }
        if evaluator.evaluator.scoring_kind.value != "binary" and any(
            artifact.metric.formula in binary_success_formulas
            for artifact in metrics
        ):
            raise CompilationError(
                "binary pass/reliability metrics require an evaluator with a "
                "registered success rule"
            )
        subject = self._subject_artifact(str(experiment["subject"]))
        evolver_artifact: EvolutionMethodArtifact | None = None
        meta_evolver_artifact: MetaEvolutionMethodArtifact | None = None
        evolver_id = experiment.get("evolver")
        meta_evolver_id = experiment.get("meta_evolver")
        if subject.kind == "evolver":
            if not isinstance(evolver_id, str) or not evolver_id:
                raise CompilationError(
                    "an evolver subject requires [experiment].evolver registry id"
                )
            if meta_evolver_id is not None:
                raise CompilationError(
                    "an evolver subject cannot select [experiment].meta_evolver"
                )
            if configuration is None:
                raise CompilationError(
                    "registered evolution methods require experiment configuration"
                )
            evolver_artifact = self._evolver_artifact(evolver_id, configuration)
            self._validate_evolution_method_binding(
                evolver_artifact,
                configuration=configuration,
                metrics=metrics,
                subject_adapter=subject.adapter,
            )
        elif subject.kind == "meta_evolver":
            if evolver_id is not None:
                raise CompilationError(
                    "a meta-evolver derives its parent from the meta method registry"
                )
            if not isinstance(meta_evolver_id, str) or not meta_evolver_id:
                raise CompilationError(
                    "a meta-evolver subject requires [experiment].meta_evolver registry id"
                )
            if configuration is None:
                raise CompilationError(
                    "registered meta-evolution methods require experiment configuration"
                )
            meta_spec, _ = self._lookup("meta_evolver", meta_evolver_id)
            evolver_artifact = self._evolver_artifact(
                meta_spec.parent_evolver_id,
                configuration,
            )
            meta_evolver_artifact = self._meta_evolver_artifact(
                meta_evolver_id,
                configuration,
                evolver_artifact,
            )
            for method in (evolver_artifact, meta_evolver_artifact):
                self._validate_evolution_method_binding(
                    method,
                    configuration=configuration,
                    metrics=metrics,
                    subject_adapter=subject.adapter,
                )
        elif evolver_id is not None or meta_evolver_id is not None:
            raise CompilationError(
                "only evolver subjects may select evolution method registries"
            )
        backend, _ = self._lookup("backend", execution.backend)
        protocol = self._resolved_protocol(str(experiment["protocol"]))
        repeated_wilson = [
            artifact.metric.id
            for artifact in metrics
            if artifact.metric.uncertainty is not None
            and artifact.metric.uncertainty.method
            == MetricUncertaintyMethod.wilson_score_v1
            and protocol.rollouts_per_case != 1
        ]
        if repeated_wilson:
            raise CompilationError(
                "rollout-level Wilson uncertainty requires exactly one rollout "
                "per task; select a task-cluster bootstrap metric instead: "
                f"{repeated_wilson}"
            )
        for artifact in metrics:
            parameters = artifact.metric.parameters
            if (
                parameters is not None
                and parameters.k is not None
                and parameters.k > protocol.rollouts_per_case
            ):
                raise CompilationError(
                    f"metric {artifact.metric.id!r} requires k={parameters.k}, "
                    f"but protocol plans only {protocol.rollouts_per_case} "
                    "rollouts per task"
                )
        regime_artifact: ExperimentRegimeArtifact | None = None
        regime_id = experiment.get("regime")
        regime_stage_id = experiment.get("stage")
        if (regime_id is None) != (regime_stage_id is None):
            raise CompilationError(
                "[experiment].regime and [experiment].stage must be selected together"
            )
        if regime_id is not None:
            if not isinstance(regime_id, str) or not isinstance(
                regime_stage_id, str
            ):
                raise CompilationError("regime and stage must contain registry ids")
            regime_artifact = self._regime_artifact(regime_id)
            try:
                active_stage = next(
                    stage
                    for stage in regime_artifact.regime.stages
                    if stage.id == regime_stage_id
                )
            except StopIteration as exc:
                raise CompilationError(
                    f"regime {regime_id!r} has no stage {regime_stage_id!r}"
                ) from exc
            active_registry_ids = {
                "benchmark": str(experiment["benchmark"]),
                "dataset": str(experiment["dataset"]),
                "evaluator": str(experiment["evaluator"]),
                "protocol": str(experiment["protocol"]),
            }
            stage_registry_ids = {
                "benchmark": active_stage.benchmark_id,
                "dataset": active_stage.dataset_id,
                "evaluator": active_stage.evaluator_id,
                "protocol": active_stage.protocol_id,
            }
            if active_registry_ids != stage_registry_ids:
                raise CompilationError(
                    "active experiment registry ids differ from the selected regime stage"
                )
            if tuple(metric_ids) != active_stage.metric_ids:
                raise CompilationError(
                    "active experiment metrics differ from the selected regime stage"
                )
        subject_interface = (
            None if subject.kind == "fake" else getattr(subject, "interface", None)
        )
        subject_adapter = subject.adapter
        subject_combo = (subject.kind, subject_adapter, subject_interface)
        compatibility = (benchmark.adapter, backend.adapter, subject_interface)
        requires_execution_capability = (
            benchmark.kind == "custom"
            or compatibility not in _BUILTIN_EXECUTION_COMPATIBILITY
            or subject.kind in {"evolver", "meta_evolver"}
            or execution.model not in self._NONE_MODELS
        )

        capability_artifacts: dict[
            tuple[str, str], AdapterCapabilityArtifact | None
        ] = {}

        def capability_artifact(
            adapter: str, adapter_kind: str
        ) -> AdapterCapabilityArtifact | None:
            key = (adapter, adapter_kind)
            if key not in capability_artifacts:
                capability_artifacts[key] = self._adapter_capability_artifact(
                    adapter, adapter_kind
                )
            return capability_artifacts[key]

        # Prefer a selected external execution declaration when present.  The
        # core policy is only a compatibility fallback for adapters shipped in
        # this package; a new harness never needs a Compiler tuple.
        execution_policy_artifact = None
        if (
            requires_execution_capability
            or backend.adapter not in self._CORE_NONE_MODEL_SENTINELS
            or subject_combo not in self._CORE_SUBJECT_COMPATIBILITY
        ):
            execution_policy_artifact = capability_artifact(
                benchmark.adapter, "execution"
            )
        if execution_policy_artifact is not None:
            execution_policy = execution_policy_artifact.capability
            if not execution_policy.supported_subject_adapters:
                raise CompilationError(
                    f"execution capability {execution_policy.id!r} must declare "
                    "supported_subject_adapters"
                )
            if (
                execution.model in self._NONE_MODELS
                and not execution_policy.none_model_sentinels
            ):
                raise CompilationError(
                    f"execution capability {execution_policy.id!r} must declare "
                    "none_model_sentinels"
                )
            if (
                execution.model not in self._NONE_MODELS
                and execution_policy.model_activation_source is None
            ):
                raise CompilationError(
                    f"execution capability {execution_policy.id!r} must declare "
                    "model_activation_source for real models; "
                    "ModelActivationReceipt missing"
                )
            if not execution_policy.supported_state_reset_policies:
                raise CompilationError(
                    f"execution capability {execution_policy.id!r} must declare "
                    "supported_state_reset_policies"
                )
            if not execution_policy.supports(
                benchmark_kind=benchmark.kind,
                subject_kind=subject.kind,
                subject_adapter=subject_adapter,
                backend_kind=backend.kind,
                backend_adapter=backend.adapter,
                subject_interface=subject_interface,
            ):
                raise CompilationError(
                    f"execution capability {execution_policy.id!r} rejects the "
                    "resolved benchmark/subject/backend tuple"
                )
            none_model_sentinels = frozenset(
                execution_policy.none_model_sentinels
            )
            state_reset_policies = frozenset(
                execution_policy.supported_state_reset_policies
            )
            subject_is_compatible = True
        else:
            none_model_sentinels = self._CORE_NONE_MODEL_SENTINELS.get(
                backend.adapter, frozenset()
            )
            state_reset_policies = self._CORE_STATE_RESET_POLICIES.get(
                backend.adapter, frozenset()
            )
            subject_is_compatible = (
                subject_combo in self._CORE_SUBJECT_COMPATIBILITY
            )

        real_model = execution.model not in self._NONE_MODELS
        if real_model and execution_policy_artifact is None:
            raise CompilationError(
                f"real model {execution.model!r} requires an execution capability "
                "with ModelActivationReceipt provenance"
            )

        if (
            execution.model in self._NONE_MODELS
            and execution.model not in none_model_sentinels
        ):
            raise CompilationError(
                f"model sentinel {execution.model!r} is not activated by "
                f"the selected execution adapter; ModelActivationReceipt missing"
            )
        if protocol.state_reset not in state_reset_policies:
            raise CompilationError(
                f"state_reset {protocol.state_reset!r} is not activated by "
                "the selected execution adapter; StateResetReceipt missing"
            )

        benchmark_pair = (benchmark.kind, benchmark.adapter)
        if benchmark_pair not in {
            ("task_suite", "fake"),
            ("tool_agent_suite", "aosebench"),
        } and benchmark.kind != "custom":
            raise CompilationError(
                f"unknown benchmark adapter combination: {benchmark_pair!r}"
            )
        authoritative_binding = evaluator.evaluator.authoritative_metric
        if (
            benchmark.adapter == "fake"
            and evaluator.evaluator.implementation == "fake.exact.v1"
            and authoritative_binding.source_key != "exact_match"
        ):
            raise CompilationError(
                "fake.exact.v1 requires authoritative source_key='exact_match'"
            )
        if benchmark.adapter == "aosebench" and (
            benchmark.input_contract != "/app/instruction.md; /app/data:ro"
            or tuple(benchmark.output_contract)
            != ("/app/trace.md", "/app/answer.txt")
            or evaluator.evaluator.implementation != "aosebench.rubric-judge"
        ):
            raise CompilationError("AOSE benchmark native task contract mismatch")
        if subject.kind in {"evolver", "meta_evolver"}:
            # Evolver subjects have no fixed wire interface.  A production
            # execution capability must bind their adapter/backend tuple below.
            if not subject.adapter:
                raise CompilationError("evolver subject adapter is missing")
        elif not subject_is_compatible:
            raise CompilationError(
                f"unknown subject adapter/interface combination: {subject_combo!r}"
            )

        deterministic = bool(getattr(protocol, "deterministic_conformance", False))
        deterministic_allowed = (
            protocol.kind == "mechanism_validation"
            and claim_design.purpose == RunPurpose.exploratory
            and claim_design.comparison_kind is None
            and benchmark.adapter == "fake"
            and subject.kind == "fake"
            and subject.adapter == "fake"
            and backend.adapter == "fake"
            and evaluator.evaluator.implementation == "fake.exact.v1"
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

        if claim_design.comparison_kind is None:
            if protocol.kind not in {"mechanism_validation", "test_time_scaling"}:
                raise CompilationError(
                    "comparison-free exploratory runs require mechanism_validation "
                    "or test_time_scaling"
                )
        elif protocol.kind == "mechanism_validation" and (
            claim_design.purpose != RunPurpose.exploratory
        ):
            raise CompilationError(
                "mechanism_validation cannot produce a research claim"
            )
        intervention = (
            None
            if claim_design.intervention_factor_id is None
            else factor_by_id[claim_design.intervention_factor_id].factor
        )
        if intervention is not None:
            if (
                intervention.category
                in {FactorCategory.model, FactorCategory.model_checkpoint}
                and execution.model in self._NONE_MODELS
            ):
                raise CompilationError(
                    "model factors require a real model and ModelActivationReceipt"
                )
            if (
                intervention.category == FactorCategory.agent_component
                and subject.kind != "hcp_harness"
            ):
                raise CompilationError(
                    "agent_component factors require an HCP-native subject"
                )
            if (
                intervention.category in {FactorCategory.protocol, FactorCategory.schedule}
                and protocol.kind != "test_time_scaling"
            ):
                raise CompilationError(
                    "protocol/schedule factors require a test_time_scaling protocol"
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
        backend_policy_artifact = None
        if (
            backend.adapter not in _BUILTIN_BACKEND_FACTORY_ADAPTERS
            or backend.adapter not in self._CORE_BACKEND_DEFAULT_READ_SETS
        ):
            backend_policy_artifact = capability_artifact(
                backend.adapter, "backend_factory"
            )
        if backend_policy_artifact is not None:
            declared_read_set = (
                backend_policy_artifact.capability.backend_default_read_set
            )
            if declared_read_set is None:
                raise CompilationError(
                    f"backend capability "
                    f"{backend_policy_artifact.capability.id!r} does not declare "
                    "backend_default_read_set"
                )
            allowed_default_keys = frozenset(declared_read_set)
        else:
            allowed_default_keys = self._CORE_BACKEND_DEFAULT_READ_SETS.get(
                backend.adapter
            )
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
        if selection == "exact" and evaluator.evaluator.scoring_kind.value != "binary":
            raise CompilationError(
                "candidate_selection='exact' requires binary benchmark scoring "
                "and ExactSelectionReceipt"
            )
        authoritative_direction = next(
            artifact.metric.direction
            for artifact in metrics
            if artifact.metric.id == authoritative_binding.metric_id
        )
        if selection == "best_of_n" and authoritative_direction.value == "neutral":
            raise CompilationError(
                "candidate_selection='best_of_n' requires a directional authoritative metric"
            )
        allowed_diff = (
            () if intervention is None else intervention.resolved_diff_paths
        )

        # The stable ordinal is defined over the canonical lexical sweep order.
        metadata = ResolvedManifestMetadata(
            experiment_id=experiment["id"],
            run_id=f"{experiment['id']}__run{run_index:04d}",
            regime_stage_id=(
                None if regime_stage_id is None else str(regime_stage_id)
            ),
            allowed_diff=allowed_diff,
            factors=dict(factor_values),
            factor_artifacts=factor_artifacts,
            configuration=configuration,
            evolver=evolver_artifact,
            meta_evolver=meta_evolver_artifact,
            adapter_capabilities=(),
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
            dataset=dataset,
            evaluator=evaluator,
            metrics=metrics,
            regime=regime_artifact,
            subject=subject,
            execution=resolved_execution,
            claim_design=claim_design,
            contrast=contrast,
            metadata=metadata,
        )
        self._validate_comparison_subject(manifest)
        # Test-only callers may inject a backend/adapter registry that is not
        # represented by project TOML declarations.  Production compilation
        # remains strict: every non-built-in loader, backend factory, and
        # execution compatibility tuple must have an explicit capability.
        required_capability_keys: list[tuple[str, str]] = []
        if not self.allow_test_override:
            if (
                benchmark.kind == "custom"
                or benchmark.adapter not in _BUILTIN_BENCHMARK_LOADER_ADAPTERS
            ):
                required_capability_keys.append((benchmark.adapter, "benchmark_loader"))
            if backend.adapter not in _BUILTIN_BACKEND_FACTORY_ADAPTERS:
                required_capability_keys.append((backend.adapter, "backend_factory"))
            if (
                benchmark.kind == "custom"
                or compatibility not in _BUILTIN_EXECUTION_COMPATIBILITY
                or subject.kind in {"evolver", "meta_evolver"}
                or real_model
            ):
                required_capability_keys.append((benchmark.adapter, "execution"))
            for artifact in metrics:
                if artifact.metric.formula == MetricFormula.external_adapter_v1:
                    required_capability_keys.append(
                        (artifact.metric.adapter, "metric_source")
                    )
        resolved_capabilities: list[AdapterCapabilityArtifact] = []
        missing_capabilities: list[tuple[str, str]] = []
        for capability_key in dict.fromkeys(required_capability_keys):
            adapter, adapter_kind = capability_key
            artifact = capability_artifact(adapter, adapter_kind)
            if artifact is None:
                missing_capabilities.append(capability_key)
            else:
                resolved_capabilities.append(artifact)
        if missing_capabilities:
            raise CompilationError(
                "missing required adapter capabilities: "
                + ", ".join(repr(item) for item in missing_capabilities)
            )
        for artifact in resolved_capabilities:
            capability = artifact.capability
            if not capability.supports(
                benchmark_kind=benchmark.kind,
                subject_kind=subject.kind,
                subject_adapter=subject_adapter,
                backend_kind=backend.kind,
                backend_adapter=backend.adapter,
                subject_interface=subject_interface,
            ):
                raise CompilationError(
                    f"adapter capability {capability.id!r} rejects the resolved "
                    "benchmark/subject/backend tuple"
                )
            if (
                configuration is not None
                and not capability.owns_configuration(configuration.values)
            ):
                raise CompilationError(
                    f"adapter capability {capability.id!r} does not own "
                    "any resolved configuration path"
                )
        capability_by_key = {
            (artifact.capability.adapter, artifact.capability.adapter_kind): artifact
            for artifact in resolved_capabilities
        }
        for metric_artifact in metrics:
            metric = metric_artifact.metric
            if metric.formula != MetricFormula.external_adapter_v1:
                continue
            capability_artifact_item = capability_by_key.get(
                (metric.adapter, "metric_source")
            )
            if capability_artifact_item is None:
                if self.allow_test_override:
                    continue
                raise CompilationError(
                    f"external metric {metric.id!r} lacks a metric_source capability"
                )
            capability = capability_artifact_item.capability
            if metric.source.value not in capability.supported_metric_sources:
                raise CompilationError(
                    f"metric adapter {capability.id!r} rejects source "
                    f"{metric.source.value!r}"
                )
            if metric.formula.value not in capability.supported_metric_formulas:
                raise CompilationError(
                    f"metric adapter {capability.id!r} rejects formula "
                    f"{metric.formula.value!r}"
                )
            try:
                validate_json_schema_document(capability.metric_config_schema)
                validate_json_schema_configuration(
                    capability.metric_config_schema,
                    metric.config,
                )
            except ConfigurationRegistryError as exc:
                raise CompilationError(
                    f"metric {metric.id!r} fails its adapter config schema: {exc}"
                ) from exc
        manifest = manifest.model_copy(
            update={
                "metadata": manifest.metadata.model_copy(
                    update={"adapter_capabilities": tuple(resolved_capabilities)}
                )
            }
        )
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
    def _enforce_registered_factor_diff(
        control: ResolvedBmpManifest,
        treatment: ResolvedBmpManifest,
        resolved_paths: tuple[str, ...],
        factor: FactorSpec,
    ) -> None:
        if control.claim_design != treatment.claim_design:
            raise CompilationError("claim design must be invariant across comparison arms")
        enforce_allowed_diff(
            control,
            treatment,
            factor.resolved_diff_paths,
            resolved_paths=resolved_paths,
        )
        unused = sorted(set(factor.resolved_diff_paths) - set(resolved_paths))
        if unused:
            raise CompilationError(
                f"registered factor paths are not activated by any arm: {unused}"
            )

    def _enforce_one_factor(
        self,
        runs: list[CompiledRun],
    ) -> None:
        contrast = runs[0].manifest.contrast
        if contrast.mode != "one_factor":
            return
        factor_id = contrast.factor_id
        control_level = contrast.control_level
        treatment_level = contrast.treatment_level
        if factor_id is None or control_level is None or treatment_level is None:
            raise CompilationError("one_factor contrast is incomplete")
        artifacts = {
            artifact.factor.id: artifact
            for artifact in runs[0].manifest.metadata.factor_artifacts
        }
        try:
            factor = artifacts[factor_id].factor
        except KeyError as exc:
            raise CompilationError(
                f"one_factor factor {factor_id!r} lacks a resolved registry artifact"
            ) from exc
        try:
            control_value = factor.level(control_level).value
            treatment_value = factor.level(treatment_level).value
        except ValueError as exc:
            raise CompilationError(str(exc)) from exc
        factor_path = factor.selector_path
        control_arm = canonical_json_bytes(control_value)
        treatment_arm = canonical_json_bytes(treatment_value)
        expected_arms = {control_arm, treatment_arm}

        def arm_value(run: CompiledRun) -> bytes:
            if factor_path not in run.factor_values:
                raise CompilationError(
                    f"one_factor contrast factor {factor_path!r} is not activated"
                )
            return canonical_json_bytes(run.factor_values[factor_path])

        by_pair: dict[bytes, dict[bytes, CompiledRun]] = {}
        for run in runs:
            arm = arm_value(run)
            if arm not in expected_arms:
                raise CompilationError(
                    "one_factor sweep contains an undeclared comparison arm "
                    f"{arm!r}"
                )
            factors = {
                key: value
                for key, value in run.factor_values.items()
                if key not in {"order_position", factor_path}
            }
            by_pair.setdefault(canonical_json_bytes(factors), {})[arm] = run
        if not by_pair:
            raise CompilationError("one_factor sweep contains no control/treatment runs")
        for pair in by_pair.values():
            if set(pair) != expected_arms:
                raise CompilationError("one_factor sweep has an unpaired control/treatment")
            control = pair[control_arm].manifest
            treatment = pair[treatment_arm].manifest
            paths = enforce_allowed_diff(
                control, treatment, control.metadata.allowed_diff
            )
            self._enforce_registered_factor_diff(
                control,
                treatment,
                paths,
                factor,
            )

    def compile(
        self,
        experiment_path: str | os.PathLike[str],
        *,
        record_root: str | os.PathLike[str] | None = None,
        config_files: Iterable[str | os.PathLike[str]] = (),
        raw_config_files: Iterable[str | os.PathLike[str]] = (),
        config_profiles: Iterable[str] = (),
        config_overrides: Mapping[str, Any] | None = None,
    ) -> list[CompiledRun]:
        """Compile and isolation-check every run in an experiment TOML."""

        path = Path(experiment_path).resolve()
        config_files = tuple(config_files)
        raw_config_files = tuple(raw_config_files)
        config_profiles = tuple(config_profiles)
        config_overrides = (
            None if config_overrides is None else dict(config_overrides)
        )
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
        raw_factor_ids = experiment.get("factors", ())
        if not isinstance(raw_factor_ids, (list, tuple)) or any(
            not isinstance(value, str) or not value
            for value in raw_factor_ids
        ):
            raise CompilationError("[experiment].factors must be an array of registry ids")
        factor_ids = tuple(raw_factor_ids)
        if len(set(factor_ids)) != len(factor_ids):
            raise CompilationError("[experiment].factors must contain unique ids")
        factor_artifacts = tuple(
            self._factor_artifact(factor_id) for factor_id in factor_ids
        )
        selectors = [artifact.factor.selector_path for artifact in factor_artifacts]
        if len(set(selectors)) != len(selectors):
            raise CompilationError(
                "selected factors must have unique selector_path values"
            )
        for artifact in factor_artifacts:
            factor = artifact.factor
            if factor.value_registry is None:
                continue
            for level in factor.levels:
                assert isinstance(level.value, str)
                self._lookup(factor.value_registry, level.value)

        factor_artifact_by_id = {
            artifact.factor.id: artifact for artifact in factor_artifacts
        }
        if contrast.mode == "one_factor":
            if contrast.factor_id not in factor_artifact_by_id:
                raise CompilationError(
                    f"one_factor contrast factor {contrast.factor_id!r} is not selected"
                )
            intervention = factor_artifact_by_id[contrast.factor_id].factor
            try:
                selected_level_ids = {
                    contrast.control_level,
                    contrast.treatment_level,
                }
                intervention_levels = tuple(
                    level
                    for level in intervention.levels
                    if level.id in selected_level_ids
                )
            except ValueError as exc:  # pragma: no cover - model already closes shape
                raise CompilationError(str(exc)) from exc
            if len(intervention_levels) != 2:
                raise CompilationError(
                    "one_factor contrast levels are absent from the factor registry"
                )
        else:
            intervention = None
            intervention_levels = ()

        factors: dict[str, list[Any]] = {}
        for artifact in factor_artifacts:
            factor = artifact.factor
            levels = (
                intervention_levels
                if intervention is not None and factor.id == intervention.id
                else factor.levels
            )
            factors[factor.selector_path] = [level.value for level in levels]
        base = dict(declaration)

        runs = [
            self._compile_expanded(
                expanded,
                selected,
                factor_artifacts,
                index,
                base_dir=path.parent,
                config_files=tuple(config_files),
                raw_config_files=tuple(raw_config_files),
                config_profiles=tuple(config_profiles),
                config_overrides=config_overrides,
            )
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
            self._enforce_one_factor(runs)
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
        comparison_kind = runs[0].manifest.claim_design.comparison_kind
        subject_kinds = tuple(
            sorted(
                {SubjectKind(run.manifest.subject.kind) for run in runs},
                key=lambda value: value.value,
            )
        )
        if purpose == RunPurpose.claim:
            if comparison_kind is None:  # ClaimDesign already forbids this.
                raise AssertionError("claim rejection lacks comparison_kind")
            not_executed = GateResult(
                valid=False, reason="not executed: isolation violation"
            )
            report: Any = ClaimReport(
                purpose=RunPurpose.claim,
                comparison_kind=comparison_kind,
                subject_kinds=subject_kinds,
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
                comparison_kind=comparison_kind,
                subject_kinds=subject_kinds,
                experiment_id=experiment_id,
                manifest_digest=sha256_bytes(digest_basis),
                isolation_valid=False,
                isolation_reasons=(reason,),
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
