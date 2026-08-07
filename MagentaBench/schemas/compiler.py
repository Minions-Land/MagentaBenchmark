"""Compilation, canonical hashing, factor expansion, and isolation helpers."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel

from .models import (
    BackendSpec,
    BenchmarkArtifact,
    BenchmarkArtifactAdapter,
    BenchmarkSpec,
    BenchmarkSpecAdapter,
    Budget,
    ClaimReport,
    EvidenceBundle,
    ExecutionSpec,
    ProtocolSpec,
    ResolvedBmpManifest,
    ResolvedExecutionSpec,
    ResolvedManifestMetadata,
    SubjectArtifact,
    SubjectArtifactAdapter,
    SubjectSpec,
    SubjectSpecAdapter,
)

try:  # pragma: no cover - branch depends on the Python runtime
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


def canonical_data(value: BaseModel | Mapping[str, Any] | Sequence[Any]) -> Any:
    """Return the JSON-compatible canonical data used by BMP digests."""

    if isinstance(value, ResolvedBmpManifest):
        return value.model_dump(mode="json", exclude=value.IDENTITY_EXCLUDE)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return list(value)


def canonical_json(value: BaseModel | Mapping[str, Any] | Sequence[Any]) -> str:
    """Serialize using the protocol's canonical JSON profile."""

    return json.dumps(
        canonical_data(value),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_digest(value: BaseModel | Mapping[str, Any] | Sequence[Any]) -> str:
    """Return a lowercase SHA-256 digest of canonical UTF-8 JSON."""

    if isinstance(value, ResolvedBmpManifest):
        return value.canonical_digest()
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _resolve_existing_source(source: str, *, base_dir: Path | None = None) -> str:
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir or Path.cwd()) / candidate
    resolved = candidate.resolve(strict=True)
    return str(resolved)


def compile_benchmark_artifact(
    spec: BenchmarkSpec,
    *,
    base_dir: Path | None = None,
) -> BenchmarkArtifact:
    """Normalize and digest a hand-written benchmark declaration."""

    payload = spec.model_dump(mode="json")
    payload["source"] = _resolve_existing_source(spec.source, base_dir=base_dir)
    payload["artifact_digest"] = "0" * 64
    provisional = BenchmarkArtifactAdapter.validate_python(payload)
    payload = provisional.model_dump(mode="json", exclude={"artifact_digest"})
    payload["artifact_digest"] = canonical_digest(payload)
    return BenchmarkArtifactAdapter.validate_python(payload)


def compile_subject_artifact(
    spec: SubjectSpec,
    *,
    base_dir: Path | None = None,
) -> SubjectArtifact:
    """Normalize and digest a hand-written subject declaration."""

    payload = spec.model_dump(mode="json")
    if spec.kind != "fake":
        payload["source"] = _resolve_existing_source(spec.source, base_dir=base_dir)
    payload["artifact_digest"] = "0" * 64
    provisional = SubjectArtifactAdapter.validate_python(payload)
    payload = provisional.model_dump(mode="json", exclude={"artifact_digest"})
    payload["artifact_digest"] = canonical_digest(payload)
    return SubjectArtifactAdapter.validate_python(payload)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result


def resolve_execution_spec(
    spec: ExecutionSpec,
    *,
    backend: BackendSpec,
    protocol: ProtocolSpec | None = None,
) -> ResolvedExecutionSpec:
    """Inline backend/protocol entries and apply defaults then local overrides.

    Precedence is backend defaults, then protocol defaults, then the local
    execution declaration. Registry identity fields cannot be overridden.
    """

    if spec.backend != backend.id:
        raise ValueError(
            f"execution references backend {spec.backend!r}, got registry entry {backend.id!r}"
        )

    protected = {"id", "kind", "adapter", "bmp_version"}
    attempted = protected.intersection(spec.backend_overrides)
    if attempted:
        raise ValueError(f"backend identity fields cannot be overridden: {sorted(attempted)}")
    backend_payload = _deep_merge(backend.model_dump(mode="python"), spec.backend_overrides)
    resolved_backend = BackendSpec.model_validate(backend_payload)

    backend_budget = backend.defaults.get("budget", {})
    if backend_budget is None:
        backend_budget = {}
    if not isinstance(backend_budget, Mapping):
        raise ValueError("backend defaults.budget must be a table/object")
    budget_payload: dict[str, Any] = dict(backend_budget)
    if protocol is not None and protocol.budget is not None:
        budget_payload = _deep_merge(
            budget_payload,
            protocol.budget.model_dump(mode="python", exclude_none=True),
        )
    budget_payload = _deep_merge(
        budget_payload,
        spec.budget.model_dump(mode="python", exclude_none=True),
    )
    resolved_budget = Budget.model_validate(budget_payload)

    return ResolvedExecutionSpec(
        backend=resolved_backend,
        model=spec.model,
        seed=spec.seed,
        budget=resolved_budget,
        protocol=protocol,
    )


def build_resolved_manifest(
    *,
    experiment_id: str,
    run_id: str,
    benchmark: BenchmarkArtifact,
    subject: SubjectArtifact,
    execution: ResolvedExecutionSpec,
    allowed_diff: Iterable[str] = (),
    factors: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> ResolvedBmpManifest:
    """Assemble the fully inlined, validated run identity."""

    return ResolvedBmpManifest(
        benchmark=benchmark,
        subject=subject,
        execution=execution,
        metadata=ResolvedManifestMetadata(
            experiment_id=experiment_id,
            run_id=run_id,
            allowed_diff=tuple(allowed_diff),
            factors={} if factors is None else factors,
        ),
        created_at=created_at,
    )


def load_toml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)


def _required_table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"TOML document must contain a [{name}] table")
    return value


def load_benchmark_spec(path: str | Path) -> BenchmarkSpec:
    return BenchmarkSpecAdapter.validate_python(_required_table(load_toml(path), "benchmark"))


def load_subject_spec(path: str | Path) -> SubjectSpec:
    return SubjectSpecAdapter.validate_python(_required_table(load_toml(path), "subject"))


def load_backend_spec(path: str | Path) -> BackendSpec:
    return BackendSpec.model_validate(_required_table(load_toml(path), "backend"))


def load_protocol_spec(path: str | Path) -> ProtocolSpec:
    return ProtocolSpec.model_validate(_required_table(load_toml(path), "protocol"))


def load_execution_spec(path: str | Path) -> ExecutionSpec:
    return ExecutionSpec.model_validate(_required_table(load_toml(path), "execution"))


def load_evidence_bundle(path: str | Path) -> EvidenceBundle:
    return EvidenceBundle.model_validate(_required_table(load_toml(path), "evidence"))


def load_claim_report(path: str | Path) -> ClaimReport:
    return ClaimReport.model_validate(_required_table(load_toml(path), "claim"))


@dataclass(frozen=True)
class FactorRun:
    """One deterministic member of a factor Cartesian product."""

    index: int
    run_id: str
    factors: Mapping[str, Any]


def expand_factor_sweep(
    experiment_id: str,
    factors: Mapping[str, Sequence[Any]],
) -> tuple[FactorRun, ...]:
    """Expand factors using BMP's deterministic ordering rule."""

    names = sorted(factors)
    ordered_values: list[list[Any]] = []
    for name in names:
        values = list(factors[name])
        if not values:
            raise ValueError(f"factor {name!r} has no values")
        ordered_values.append(sorted(values, key=lambda value: str(value)))

    products = itertools.product(*ordered_values) if names else [()]
    return tuple(
        FactorRun(
            index=index,
            run_id=f"{experiment_id}__run{index:04d}",
            factors=dict(zip(names, values)),
        )
        for index, values in enumerate(products)
    )


def _dump_for_diff(value: BaseModel | Mapping[str, Any]) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def differing_paths(
    control: BaseModel | Mapping[str, Any],
    treatment: BaseModel | Mapping[str, Any],
) -> tuple[str, ...]:
    """Enumerate exact dotted paths whose JSON values differ."""

    differences: set[str] = set()

    def visit(left: Any, right: Any, path: str) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                child = f"{path}.{key}" if path else str(key)
                if key not in left or key not in right:
                    differences.add(child)
                else:
                    visit(left[key], right[key], child)
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                child = f"{path}.{index}" if path else str(index)
                if index >= len(left) or index >= len(right):
                    differences.add(child)
                else:
                    visit(left[index], right[index], child)
            return
        if left != right:
            differences.add(path)

    visit(_dump_for_diff(control), _dump_for_diff(treatment), "")
    return tuple(sorted(differences))


@dataclass(frozen=True)
class AllowedDiffResult:
    valid: bool
    differing_paths: tuple[str, ...]
    disallowed_paths: tuple[str, ...]


def check_allowed_diff(
    control: BaseModel | Mapping[str, Any],
    treatment: BaseModel | Mapping[str, Any],
    allowed_diff: Iterable[str],
) -> AllowedDiffResult:
    """Check that every actual leaf difference is explicitly authorized."""

    actual = differing_paths(control, treatment)
    allowed = frozenset(allowed_diff)
    disallowed = tuple(path for path in actual if path not in allowed)
    return AllowedDiffResult(
        valid=not disallowed,
        differing_paths=actual,
        disallowed_paths=disallowed,
    )


class ManifestCompiler:
    """Filesystem registry compiler for a single resolved BMP run."""

    def __init__(self, registry_root: str | Path) -> None:
        self.registry_root = Path(registry_root).resolve()

    def _entry_path(self, collection: str, entry_id: str) -> Path:
        directory = self.registry_root / collection
        direct = directory / f"{entry_id}.toml"
        if direct.is_file():
            return direct

        matches: list[Path] = []
        for candidate in sorted(directory.glob("*.toml")):
            document = load_toml(candidate)
            singular = collection[:-1] if collection.endswith("s") else collection
            table = document.get(singular)
            if isinstance(table, Mapping) and table.get("id") == entry_id:
                matches.append(candidate)
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one {collection} registry entry for {entry_id!r}; "
                f"found {len(matches)}"
            )
        return matches[0]

    def compile(
        self,
        *,
        experiment_id: str,
        run_id: str,
        benchmark_id: str,
        subject_id: str,
        execution: ExecutionSpec,
        protocol_id: str | None = None,
        allowed_diff: Iterable[str] = (),
        factors: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> ResolvedBmpManifest:
        benchmark_path = self._entry_path("benchmarks", benchmark_id)
        subject_path = self._entry_path("subjects", subject_id)
        backend_path = self._entry_path("backends", execution.backend)
        protocol_path = (
            self._entry_path("protocols", protocol_id) if protocol_id is not None else None
        )

        benchmark = compile_benchmark_artifact(
            load_benchmark_spec(benchmark_path), base_dir=benchmark_path.parent
        )
        subject = compile_subject_artifact(
            load_subject_spec(subject_path), base_dir=subject_path.parent
        )
        backend = load_backend_spec(backend_path)
        protocol = load_protocol_spec(protocol_path) if protocol_path is not None else None
        fake_subject = subject.kind == "fake"
        deterministic_conformance = (
            protocol is not None and protocol.deterministic_conformance
        )
        if fake_subject and not deterministic_conformance:
            raise ValueError(
                "fake subjects require a protocol with deterministic_conformance=true"
            )
        if deterministic_conformance and not fake_subject:
            raise ValueError(
                "deterministic_conformance protocols may only be used with fake subjects"
            )
        resolved_execution = resolve_execution_spec(
            execution,
            backend=backend,
            protocol=protocol,
        )
        return build_resolved_manifest(
            experiment_id=experiment_id,
            run_id=run_id,
            benchmark=benchmark,
            subject=subject,
            execution=resolved_execution,
            allowed_diff=allowed_diff,
            factors=factors,
            created_at=created_at,
        )


__all__ = [
    "AllowedDiffResult",
    "FactorRun",
    "ManifestCompiler",
    "build_resolved_manifest",
    "canonical_data",
    "canonical_digest",
    "canonical_json",
    "check_allowed_diff",
    "compile_benchmark_artifact",
    "compile_subject_artifact",
    "differing_paths",
    "expand_factor_sweep",
    "load_backend_spec",
    "load_benchmark_spec",
    "load_claim_report",
    "load_evidence_bundle",
    "load_execution_spec",
    "load_protocol_spec",
    "load_subject_spec",
    "load_toml",
    "resolve_execution_spec",
]
