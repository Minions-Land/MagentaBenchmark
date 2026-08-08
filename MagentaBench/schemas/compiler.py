"""Compilation, canonical hashing, factor expansion, and isolation helpers."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import subprocess
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
    ConfigurationSelection,
    ConfigurationSpec,
    Budget,
    ClaimReport,
    EvidenceBundle,
    EvolutionRunEvidence,
    ExecutionSpec,
    ProtocolSpec,
    ResolvedBmpManifest,
    ResolvedExecutionSpec,
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
        return value.identity_data()
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


_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _declared_content_patterns(
    spec: BenchmarkSpec | SubjectSpec,
) -> tuple[str, ...]:
    """Return required globs for the adapter-owned content closure."""

    if spec.kind == "task_suite":
        return (spec.task_manifest,)
    if spec.kind == "tool_agent_suite":
        root = spec.task_root.rstrip("/")
        return (
            f"{root}/*/task.toml",
            f"{root}/*/instruction.md",
            f"{root}/*/tests/rubric.txt",
            f"{root}/*/tests/llm_judge.py",
            f"{root}/*/tests/test.sh",
        )
    if spec.kind == "custom":
        return tuple(spec.content_globs)
    # Programmatic subjects are identified by their declared fields and launch
    # argv. No undeclared source-tree walk is permitted.
    return ()


def _git_head(source: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _source_content_digest(
    source: Path,
    *,
    patterns: tuple[str, ...],
    declared_commit: str | None,
    adapter: str,
) -> tuple[str | None, str]:
    head = _git_head(source)
    normalized_commit = (
        declared_commit
        if declared_commit is not None and _GIT_COMMIT_PATTERN.fullmatch(declared_commit)
        else None
    )
    if normalized_commit is not None:
        if head is None:
            raise ValueError("declared git commit requires a git source checkout")
        if head != normalized_commit:
            raise ValueError(
                f"declared commit {normalized_commit} does not match checkout HEAD {head}"
            )

    matched: set[Path] = set()
    for pattern in patterns:
        pattern_matches = {path for path in source.glob(pattern) if path.is_file()}
        if not pattern_matches:
            raise ValueError(
                f"adapter {adapter!r} required content pattern matched no files: {pattern!r}"
            )
        matched.update(pattern_matches)

    if normalized_commit is not None:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if status.returncode != 0:
            raise ValueError(
                "could not inspect source checkout status: "
                + status.stderr.decode("utf-8", errors="replace").strip()
            )
        records = status.stdout.split(b"\0")
        dirty_paths: set[str] = set()
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            code = record[:2]
            dirty_paths.add(record[3:].decode("utf-8"))
            if b"R" in code or b"C" in code:
                if index < len(records) and records[index]:
                    dirty_paths.add(records[index].decode("utf-8"))
                    index += 1
        declared_paths = {path.relative_to(source).as_posix() for path in matched}
        dirty_declared = sorted(declared_paths.intersection(dirty_paths))
        if dirty_declared:
            raise ValueError(
                f"declared content dependency is dirty or untracked: {dirty_declared}"
            )

    entries: list[dict[str, Any]] = []
    for path in sorted(matched, key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        current = source
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"declared content dependency is a symlink: {relative}")
        content = path.read_bytes()
        entries.append(
            {
                "path": relative.as_posix(),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    payload = json.dumps(
        entries,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return normalized_commit, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_existing_source(source: str, *, base_dir: Path | None = None) -> str:
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir or Path.cwd()) / candidate
    resolved = candidate.resolve(strict=True)
    return str(resolved)


def _compile_benchmark_artifact(
    spec: BenchmarkSpec,
    *,
    base_dir: Path | None = None,
) -> BenchmarkArtifact:
    """Normalize and digest a hand-written benchmark declaration."""

    payload = spec.model_dump(mode="json")
    source = Path(_resolve_existing_source(spec.source, base_dir=base_dir))
    commit, content_digest = _source_content_digest(
        source,
        patterns=_declared_content_patterns(spec),
        declared_commit=spec.commit,
        adapter=spec.adapter,
    )
    payload["source"] = str(source)
    payload["commit"] = commit
    payload["source_content_digest"] = content_digest
    payload["artifact_digest"] = "0" * 64
    provisional = BenchmarkArtifactAdapter.validate_python(payload)
    identity = provisional.model_dump(
        mode="json", exclude={"artifact_digest", "source"}
    )
    payload["artifact_digest"] = canonical_digest(identity)
    return BenchmarkArtifactAdapter.validate_python(payload)


def _compile_subject_artifact(
    spec: SubjectSpec,
    *,
    base_dir: Path | None = None,
) -> SubjectArtifact:
    """Normalize and digest a hand-written subject declaration."""

    payload = spec.model_dump(mode="json")
    sidecar = payload.get("sidecar_ref")
    if sidecar is not None:
        # BMP treats the HCP assembly sidecar as opaque evidence.  Admission
        # still binds the declared path to its bytes so a later report cannot
        # silently substitute a different assembly projection.
        sidecar_path = Path(str(sidecar["path"])).expanduser()
        if not sidecar_path.is_absolute() or not sidecar_path.is_file():
            raise ValueError("assembly sidecar path must identify an existing absolute file")
        observed_sidecar_digest = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
        if observed_sidecar_digest != sidecar.get("sha256"):
            raise ValueError("assembly sidecar digest does not match its bytes")
        if sidecar_path.stat().st_size != sidecar.get("size_bytes"):
            raise ValueError("assembly sidecar size does not match its bytes")
    if spec.kind != "fake":
        source = Path(_resolve_existing_source(spec.source, base_dir=base_dir))
        commit, content_digest = _source_content_digest(
            source,
            patterns=_declared_content_patterns(spec),
            declared_commit=spec.commit,
            adapter=spec.adapter,
        )
        payload["source"] = str(source)
        payload["commit"] = commit
        payload["source_content_digest"] = content_digest
    payload["artifact_digest"] = "0" * 64
    provisional = SubjectArtifactAdapter.validate_python(payload)
    identity = provisional.model_dump(
        mode="json", exclude={"artifact_digest", "source"}
    )
    payload["artifact_digest"] = canonical_digest(identity)
    return SubjectArtifactAdapter.validate_python(payload)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result


def _resolve_execution_spec(
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

    if spec.budget is not None:
        resolved_budget = spec.budget
    elif protocol is not None and protocol.budget is not None:
        resolved_budget = protocol.budget
    else:
        raise ValueError(
            "execution must declare budget or reference a protocol with a budget"
        )

    # The fallback source is declaration-only. Manifest identity carries the
    # effective budget exactly once at ResolvedExecutionSpec.budget.
    resolved_protocol = (
        None if protocol is None else protocol.model_copy(update={"budget": None})
    )
    return ResolvedExecutionSpec(
        backend=resolved_backend,
        model=spec.model,
        seed=spec.seed,
        budget=resolved_budget,
        protocol=resolved_protocol,
    )


def _load_toml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)


def _required_table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    unknown_roots = sorted(set(document) - {name})
    if unknown_roots:
        raise ValueError(
            f"TOML document for [{name}] has unknown top-level keys: {unknown_roots}"
        )
    value = document.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"TOML document must contain a [{name}] table")
    return value


def load_benchmark_spec(path: str | Path) -> BenchmarkSpec:
    table = _required_table(_load_toml(path), "benchmark")
    return BenchmarkSpecAdapter.validate_python(table)


def load_configuration_spec(path: str | Path) -> ConfigurationSpec:
    table = _required_table(_load_toml(path), "configuration")
    return ConfigurationSpec.model_validate(table)


def load_configuration_selection(path: str | Path) -> ConfigurationSelection:
    table = _required_table(_load_toml(path), "configuration")
    return ConfigurationSelection.model_validate(table)


def load_subject_spec(path: str | Path) -> SubjectSpec:
    table = _required_table(_load_toml(path), "subject")
    return SubjectSpecAdapter.validate_python(table)


def load_backend_spec(path: str | Path) -> BackendSpec:
    return BackendSpec.model_validate(_required_table(_load_toml(path), "backend"))


def load_protocol_spec(path: str | Path) -> ProtocolSpec:
    return ProtocolSpec.model_validate(_required_table(_load_toml(path), "protocol"))


def load_execution_spec(path: str | Path) -> ExecutionSpec:
    return ExecutionSpec.model_validate(_required_table(_load_toml(path), "execution"))


def load_evidence_bundle(path: str | Path) -> EvidenceBundle:
    return EvidenceBundle.model_validate(_required_table(_load_toml(path), "evidence"))


def load_evolution_run_evidence(path: str | Path) -> EvolutionRunEvidence:
    """Load one evolution evidence record from JSON or a ``[evolution]`` TOML envelope."""

    source = Path(path)
    if source.suffix.lower() == ".json":
        return EvolutionRunEvidence.model_validate_json(source.read_bytes())
    return EvolutionRunEvidence.model_validate(
        _required_table(_load_toml(source), "evolution")
    )


def load_claim_report(path: str | Path) -> ClaimReport:
    return ClaimReport.model_validate(_required_table(_load_toml(path), "claim"))


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


__all__ = [
    "AllowedDiffResult",
    "FactorRun",
    "canonical_data",
    "canonical_digest",
    "canonical_json",
    "check_allowed_diff",
    "differing_paths",
    "expand_factor_sweep",
    "load_backend_spec",
    "load_benchmark_spec",
    "load_claim_report",
    "load_evidence_bundle",
    "load_evolution_run_evidence",
    "load_execution_spec",
    "load_protocol_spec",
    "load_subject_spec",
]
