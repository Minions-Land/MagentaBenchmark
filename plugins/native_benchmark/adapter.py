"""Evidence-bound adapter for benchmark-native drivers and evaluators.

The plugin deliberately treats an upstream driver as an opaque process.  BMP
owns case activation, the fresh workspace, command/environment binding, and
evidence persistence; the driver owns model execution and native evaluation.
The two sides exchange one closed ``result.json`` document.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import string
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from MagentaBench.runner.adapter_registry import (
    AdapterRegistryError,
    LoadedCaseSet,
    ResolvedCaseSet,
    _write_immutable,
    write_immutable_json,
)
from MagentaBench.runner.backend.fake import CaseExecution, FakeBackend
from MagentaBench.runner.compiler import CompiledRun
from MagentaBench.runner.case_order import (
    CaseOrderError,
    custom_order_binding,
    selected_case_ids,
)
from MagentaBench.runner.evidence import (
    artifact_ref,
    atomic_write_bytes,
    atomic_write_json,
    sha256_file,
    source_closure_digest,
)
from MagentaBench.runner.model_activation import make_model_activation_receipt
from MagentaBench.runner.network import record_unobservable_network
from MagentaBench.schemas import (
    ArtifactRef,
    CaseArtifact,
    CaseSetArtifact,
    EvidenceBundle,
    ModelActivationEvidence,
    NetworkBoundary,
    NetworkPolicySource,
    ProvenanceRecord,
    RunStatus,
    ScoringKind,
    UsageRecord,
    VerifierEvidence,
)


_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODULE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_RESULT_SCHEMA = "magentabench.native-result.v1"
_CASE_SCHEMA = "magentabench.native-cases.v1"
_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "metrics",
        "usage",
        "artifacts",
        "trace",
        "model_activation",
        "verifier",
    }
)
_CASE_KEYS = frozenset(
    {
        "id",
        "public_input",
        "task_contracts",
        "verifier_contracts",
        "allow_internet",
    }
)
_PLACEHOLDERS = frozenset(
    {
        "case_id",
        "public_input",
        "output_dir",
        "workspace",
        "dataset_source",
        "subject_source",
        "model",
        "attempt_id",
        "max_tokens",
        "max_cost",
    }
)
_INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
_DEFAULT_MAX_CAPTURE_BYTES = 16 * 1024 * 1024


class NativeBenchmarkConfigurationError(ValueError):
    """A native benchmark declaration or result violates the closed contract."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _safe_source(root: str | Path, *, label: str) -> Path:
    raw = Path(root)
    if raw.is_symlink():
        raise AdapterRegistryError(f"{label} source must not be a symlink: {raw}")
    resolved = raw.resolve(strict=True)
    if not resolved.is_dir():
        raise AdapterRegistryError(f"{label} source is not a directory: {resolved}")
    return resolved


def _logical_relative_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str):
        raise NativeBenchmarkConfigurationError(f"{label} must be a string")
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise NativeBenchmarkConfigurationError(
            f"{label} must be a normalized relative path"
        )
    return path


def _safe_file(root: Path, value: Any, *, label: str) -> Path:
    relative = _logical_relative_path(value, label=label)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise NativeBenchmarkConfigurationError(f"{label} traverses a symlink")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise NativeBenchmarkConfigurationError(f"{label} escapes its source") from exc
    if not resolved.is_file():
        raise NativeBenchmarkConfigurationError(f"{label} is not a regular file")
    return resolved


def _artifact_matches(ref: ArtifactRef, path: Path) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == ref.size_bytes
        and sha256_file(path) == ref.sha256
    )


def _ordered(run: CompiledRun, values: Sequence[Any]) -> tuple[Any, ...]:
    protocol = run.manifest.execution.protocol
    if protocol is None:
        raise AdapterRegistryError("native benchmark case resolution requires a protocol")
    ordered = list(values)
    if protocol.case_order == "seeded_random":
        if run.manifest.execution.seed is None:
            raise AdapterRegistryError("seeded native case order requires a seed")
        random.Random(run.manifest.execution.seed).shuffle(ordered)
    elif protocol.case_order == "random":
        random.SystemRandom().shuffle(ordered)
    elif protocol.case_order in {"custom", "explicit"}:
        try:
            requested = selected_case_ids(protocol)
        except CaseOrderError as exc:
            raise AdapterRegistryError(str(exc)) from exc
        assert requested is not None
        by_id = {item.case_id: item for item in ordered}
        missing = [case_id for case_id in requested if case_id not in by_id]
        if missing:
            raise AdapterRegistryError(
                "native benchmark explicit case ids are missing: " + ", ".join(missing)
            )
        ordered = [by_id[case_id] for case_id in requested]
    elif protocol.case_order != "fixed":
        raise AdapterRegistryError(
            f"unsupported native benchmark case order: {protocol.case_order}"
        )
    return tuple(ordered)


@dataclass(frozen=True)
class _DeclaredCase:
    case_id: str
    public_input: Path
    task_contracts: tuple[Path, ...]
    verifier_contracts: tuple[Path, ...]
    allow_internet: bool


@dataclass(frozen=True)
class NativeBenchmarkCase:
    """One activated native case with content-addressed contracts."""

    task_id: str
    public_input_ref: ArtifactRef
    task_contract_refs: tuple[ArtifactRef, ...]
    verifier_contract_refs: tuple[ArtifactRef, ...]
    dataset_source: str
    subject_source: str
    case_set_digest: str
    allow_internet: bool


class NativeBenchmarkLoader:
    """Resolve a closed JSON case manifest without importing benchmark code."""

    adapter = "native_benchmark"
    digest = _MODULE_DIGEST

    @staticmethod
    def _source(run: CompiledRun) -> Path:
        source = run.manifest.dataset.source
        if not source:
            raise AdapterRegistryError("native benchmark dataset source is missing")
        return _safe_source(source, label="native benchmark")

    @classmethod
    def _source_refs(cls, run: CompiledRun) -> tuple[ArtifactRef, ...]:
        source = cls._source(run)
        patterns = tuple(run.manifest.dataset.content_globs)
        if not patterns:
            raise AdapterRegistryError("native benchmark content_globs must be non-empty")
        files: set[Path] = set()
        for pattern in patterns:
            for raw_path in source.glob(pattern):
                if raw_path.is_symlink():
                    raise AdapterRegistryError(
                        f"native benchmark content dependency is a symlink: {raw_path}"
                    )
                if not raw_path.is_file():
                    continue
                relative = raw_path.relative_to(source)
                path = _safe_file(
                    source,
                    relative.as_posix(),
                    label="native benchmark content dependency",
                )
                files.add(path)
        if not files:
            raise AdapterRegistryError("native benchmark content_globs matched no files")
        refs = tuple(artifact_ref(path) for path in sorted(files))
        if source_closure_digest(source, refs) != run.manifest.dataset.source_content_digest:
            raise AdapterRegistryError(
                "native benchmark source closure differs from compiled dataset"
            )
        return refs

    @classmethod
    def _declared_cases(
        cls, run: CompiledRun, source_refs: tuple[ArtifactRef, ...]
    ) -> tuple[_DeclaredCase, ...]:
        source = cls._source(run)
        manifest_value = run.manifest.dataset.config.get("case_manifest")
        manifest_path = _safe_file(
            source, manifest_value, label="native benchmark case_manifest"
        )
        refs_by_path = {Path(ref.path).resolve(): ref for ref in source_refs}
        if manifest_path not in refs_by_path:
            raise AdapterRegistryError(
                "native benchmark case_manifest is outside the compiled content closure"
            )
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AdapterRegistryError("native benchmark case_manifest is malformed") from exc
        if not isinstance(document, Mapping) or set(document) != {
            "schema_version",
            "cases",
        }:
            raise AdapterRegistryError(
                "native benchmark case_manifest must contain only schema_version and cases"
            )
        if document["schema_version"] != _CASE_SCHEMA:
            raise AdapterRegistryError("unsupported native benchmark case manifest schema")
        raw_cases = document["cases"]
        if not isinstance(raw_cases, list) or not raw_cases:
            raise AdapterRegistryError("native benchmark case_manifest requires cases")
        declared: list[_DeclaredCase] = []
        for index, raw in enumerate(raw_cases):
            if not isinstance(raw, Mapping) or not set(raw).issubset(_CASE_KEYS):
                raise AdapterRegistryError(
                    f"native benchmark case {index} has unsupported fields"
                )
            required = {
                "id",
                "public_input",
                "task_contracts",
                "verifier_contracts",
            }
            if not required.issubset(raw):
                raise AdapterRegistryError(
                    f"native benchmark case {index} is missing required fields"
                )
            case_id = raw["id"]
            if not isinstance(case_id, str) or _ID.fullmatch(case_id) is None:
                raise AdapterRegistryError(f"invalid native benchmark case id: {case_id!r}")
            allow_internet = raw.get(
                "allow_internet",
                run.manifest.dataset.config.get("allow_internet", False),
            )
            if not isinstance(allow_internet, bool):
                raise AdapterRegistryError(
                    f"native benchmark case {case_id} allow_internet must be boolean"
                )

            def paths(field: str) -> tuple[Path, ...]:
                raw_values = raw[field]
                if not isinstance(raw_values, list) or not raw_values:
                    raise AdapterRegistryError(
                        f"native benchmark case {case_id} {field} must be non-empty"
                    )
                values = tuple(
                    _safe_file(
                        source,
                        value,
                        label=f"native benchmark case {case_id} {field}",
                    )
                    for value in raw_values
                )
                if len(set(values)) != len(values):
                    raise AdapterRegistryError(
                        f"native benchmark case {case_id} {field} contains duplicates"
                    )
                return values

            public_input = _safe_file(
                source,
                raw["public_input"],
                label=f"native benchmark case {case_id} public_input",
            )
            task_contracts = paths("task_contracts")
            verifier_contracts = paths("verifier_contracts")
            referenced = (public_input, *task_contracts, *verifier_contracts)
            missing = [path for path in referenced if path not in refs_by_path]
            if missing:
                raise AdapterRegistryError(
                    f"native benchmark case {case_id} references files outside content_globs"
                )
            declared.append(
                _DeclaredCase(
                    case_id=case_id,
                    public_input=public_input,
                    task_contracts=task_contracts,
                    verifier_contracts=verifier_contracts,
                    allow_internet=allow_internet,
                )
            )
        ids = [case.case_id for case in declared]
        if len(ids) != len(set(ids)):
            raise AdapterRegistryError("native benchmark case ids must be unique")
        return tuple(declared)

    def resolve(self, run: CompiledRun, artifact_root: Path) -> ResolvedCaseSet:
        source_refs = self._source_refs(run)
        declared = _ordered(run, self._declared_cases(run, source_refs))
        cases: list[CaseArtifact] = []
        for case in declared:
            public_bytes = case.public_input.read_bytes()
            public_digest = hashlib.sha256(public_bytes).hexdigest()
            public_path = (
                artifact_root
                / "content"
                / f"{case.case_id}-public-{public_digest}{case.public_input.suffix}"
            )
            _write_immutable(
                public_path,
                public_bytes,
                label="native benchmark public input",
            )
            cases.append(
                CaseArtifact(
                    case_id=case.case_id,
                    public_input_ref=artifact_ref(public_path),
                    task_contract_refs=tuple(
                        artifact_ref(path) for path in case.task_contracts
                    ),
                    verifier_contract_refs=tuple(
                        artifact_ref(path) for path in case.verifier_contracts
                    ),
                )
            )
        protocol = run.manifest.execution.protocol
        assert protocol is not None
        artifact = CaseSetArtifact(
            benchmark_id=run.manifest.benchmark.id,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            dataset_id=run.manifest.dataset.id,
            dataset_digest=run.manifest.dataset.artifact_digest,
            loader_adapter=self.adapter,
            loader_digest=self.digest,
            selection_method={
                "custom": "custom_order_artifact",
                "explicit": "explicit_case_ids",
            }.get(protocol.case_order, "all_cases"),
            case_order=protocol.case_order,
            order_seed=(
                run.manifest.execution.seed
                if protocol.case_order == "seeded_random"
                else None
            ),
            order_strategy_adapter=(
                protocol.custom_order.adapter
                if protocol.case_order == "custom" and protocol.custom_order is not None
                else None
            ),
            order_strategy_ref=(
                custom_order_binding(protocol)[2]
                if protocol.case_order == "custom"
                else None
            ),
            source_content_digest=run.manifest.dataset.source_content_digest,
            source_content_refs=source_refs,
            ordered_case_ids=tuple(case.case_id for case in cases),
            cases=tuple(cases),
        )
        artifact_path = artifact_root / artifact.canonical_digest() / "case_set.json"
        write_immutable_json(
            artifact_path, artifact, label="native benchmark case-set artifact"
        )
        return ResolvedCaseSet(
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_sha256=sha256_file(artifact_path),
        )

    def load(self, run: CompiledRun, resolved: ResolvedCaseSet) -> LoadedCaseSet:
        source_refs = self._source_refs(run)
        by_id = {
            case.case_id: case for case in self._declared_cases(run, source_refs)
        }
        loaded: list[NativeBenchmarkCase] = []
        for activated in resolved.artifact.cases:
            try:
                declared = by_id[activated.case_id]
            except KeyError as exc:
                raise AdapterRegistryError(
                    f"native benchmark activated case disappeared: {activated.case_id}"
                ) from exc
            expected_task = tuple(artifact_ref(path) for path in declared.task_contracts)
            expected_verifier = tuple(
                artifact_ref(path) for path in declared.verifier_contracts
            )
            if (
                tuple(activated.task_contract_refs) != expected_task
                or tuple(activated.verifier_contract_refs) != expected_verifier
            ):
                raise AdapterRegistryError(
                    f"native benchmark contract drift: {activated.case_id}"
                )
            public_path = Path(activated.public_input_ref.path)
            if (
                not _artifact_matches(activated.public_input_ref, public_path)
                or public_path.read_bytes() != declared.public_input.read_bytes()
            ):
                raise AdapterRegistryError(
                    f"native benchmark public input drift: {activated.case_id}"
                )
            subject_source = getattr(run.manifest.subject, "source", None)
            if not isinstance(subject_source, str) or not subject_source:
                raise AdapterRegistryError("native benchmark subject source is missing")
            loaded.append(
                NativeBenchmarkCase(
                    task_id=activated.case_id,
                    public_input_ref=activated.public_input_ref,
                    task_contract_refs=expected_task,
                    verifier_contract_refs=expected_verifier,
                    dataset_source=str(self._source(run)),
                    subject_source=str(
                        _safe_source(subject_source, label="native benchmark subject")
                    ),
                    case_set_digest=resolved.artifact.canonical_digest(),
                    allow_internet=declared.allow_internet,
                )
            )
        if tuple(case.task_id for case in loaded) != resolved.artifact.ordered_case_ids:
            raise AdapterRegistryError("native benchmark activated case order drift")
        return LoadedCaseSet(
            artifact=resolved.artifact,
            artifact_path=resolved.artifact_path,
            artifact_sha256=resolved.artifact_sha256,
            cases=tuple(loaded),
        )


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized.endswith("_sha256") or normalized.endswith("_name"):
        return False
    return any(
        marker in normalized
        for marker in (
            "api_key",
            "auth_token",
            "access_token",
            "secret",
            "password",
            "authorization",
            "cookie",
            "credential_value",
        )
    )


def _redact_tree(value: Any, secrets: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _sensitive_key(str(key))
                else _redact_tree(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_tree(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _redact_text(value: str, secrets: Mapping[str, str]) -> str:
    redacted = value
    candidates = sorted(
        ((name, secret) for name, secret in secrets.items() if secret),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    for name, secret in candidates:
        redacted = redacted.replace(secret, f"[REDACTED:{name}]")
    return redacted


def _expand_command(
    run: CompiledRun,
    case: NativeBenchmarkCase,
    workspace: Path,
    attempt_id: str,
) -> tuple[str, ...]:
    launch = getattr(run.manifest.subject, "launch_argv", None)
    if not launch:
        raise NativeBenchmarkConfigurationError(
            f"subject {run.manifest.subject.id!r} requires launch_argv"
        )
    output_dir = workspace / "output"
    values = {
        "case_id": case.task_id,
        "public_input": str(workspace / "public_input" / Path(case.public_input_ref.path).name),
        "output_dir": str(output_dir),
        "workspace": str(workspace),
        "dataset_source": case.dataset_source,
        "subject_source": case.subject_source,
        "model": run.manifest.execution.model,
        "attempt_id": attempt_id,
        "max_tokens": (
            ""
            if run.manifest.execution.budget.max_tokens is None
            else str(run.manifest.execution.budget.max_tokens)
        ),
        "max_cost": (
            ""
            if run.manifest.execution.budget.max_cost is None
            else str(run.manifest.execution.budget.max_cost)
        ),
    }
    command: list[str] = []
    formatter = string.Formatter()
    for raw_arg in launch:
        fields = {
            field_name
            for _, field_name, format_spec, conversion in formatter.parse(raw_arg)
            if field_name is not None
            and not format_spec
            and conversion is None
        }
        parsed_fields = {
            field_name
            for _, field_name, _, _ in formatter.parse(raw_arg)
            if field_name is not None
        }
        if fields != parsed_fields or not parsed_fields.issubset(_PLACEHOLDERS):
            unknown = sorted(parsed_fields - _PLACEHOLDERS)
            detail = ", ".join(unknown) if unknown else "format specifier/conversion"
            raise NativeBenchmarkConfigurationError(
                f"subject launch_argv uses unsupported placeholder: {detail}"
            )
        try:
            command.append(raw_arg.format_map(values))
        except (KeyError, ValueError) as exc:
            raise NativeBenchmarkConfigurationError(
                "subject launch_argv placeholder expansion failed"
            ) from exc
    if not command or not command[0]:
        raise NativeBenchmarkConfigurationError("native benchmark command is empty")
    return tuple(command)


def _binary_passed(run: CompiledRun, score: float) -> bool:
    binding = run.manifest.authoritative_metric_binding
    threshold = binding.success_threshold
    operator = binding.success_operator
    tolerance = binding.absolute_tolerance
    if threshold is None or operator is None:
        raise NativeBenchmarkConfigurationError(
            "binary native evaluator lacks an authoritative success rule"
        )
    if operator == "eq":
        return abs(score - threshold) <= tolerance
    if operator == "gte":
        return score >= threshold - tolerance
    if operator == "lte":
        return score <= threshold + tolerance
    if operator == "gt":
        return score > threshold + tolerance
    if operator == "lt":
        return score < threshold - tolerance
    if operator == "range":
        upper = binding.success_upper_bound
        if upper is None:
            raise NativeBenchmarkConfigurationError(
                "binary range evaluator lacks success_upper_bound"
            )
        return threshold - tolerance <= score <= upper + tolerance
    raise NativeBenchmarkConfigurationError(
        f"unsupported native evaluator success operator: {operator}"
    )


class NativeProcessBackend:
    """Run one native driver in a fresh host workspace without a shell."""

    adapter = "native_process"
    runner_digest = _MODULE_DIGEST

    def __init__(
        self,
        record_root: Path,
        *,
        workspace_root: Path,
        environment_variable_names: Sequence[str] = (),
        keep_workspace_on_failure: bool = True,
        max_capture_bytes: int = _DEFAULT_MAX_CAPTURE_BYTES,
    ) -> None:
        names = tuple(str(name) for name in environment_variable_names)
        if (
            len(set(names)) != len(names)
            or any(_ENV_NAME.fullmatch(name) is None for name in names)
        ):
            raise NativeBenchmarkConfigurationError(
                "native process environment_variable_names are invalid"
            )
        if type(keep_workspace_on_failure) is not bool:
            raise NativeBenchmarkConfigurationError(
                "native process keep_workspace_on_failure must be boolean"
            )
        if type(max_capture_bytes) is not int or max_capture_bytes <= 0:
            raise NativeBenchmarkConfigurationError(
                "native process max_capture_bytes must be a positive integer"
            )
        self.record_root = record_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.environment_variable_names = names
        self.keep_workspace_on_failure = keep_workspace_on_failure
        self.max_capture_bytes = max_capture_bytes
        self._manifest_write_lock = threading.Lock()

    def run_directory(self, run: CompiledRun) -> Path:
        return (
            self.record_root
            / run.manifest.metadata.experiment_id
            / run.manifest_digest
        )

    def workspace_directory(self, run: CompiledRun, attempt_id: str) -> Path:
        if _ID.fullmatch(attempt_id) is None:
            raise NativeBenchmarkConfigurationError("native attempt id is invalid")
        return (
            self.workspace_root
            / run.manifest.metadata.experiment_id
            / run.manifest_digest
            / attempt_id
        )

    @staticmethod
    def reset_state(case_id: str, policy: str) -> dict[str, str]:
        return {"case_id": case_id, "policy": policy, "mechanism": "fresh_workspace"}

    def load_completed(
        self, run: CompiledRun, bundle_path: Path, *, expected_runner_digest: str
    ) -> CaseExecution | None:
        return FakeBackend.load_completed(
            run,
            bundle_path,
            expected_runner_digest=expected_runner_digest,
        )

    def _environment(self) -> tuple[dict[str, str], dict[str, str], tuple[str, ...]]:
        environment = {
            name: os.environ[name] for name in _INHERITED_ENV if name in os.environ
        }
        environment.setdefault("LANG", "C.UTF-8")
        forwarded: dict[str, str] = {}
        missing: list[str] = []
        for name in self.environment_variable_names:
            if name in os.environ:
                forwarded[name] = os.environ[name]
                environment[name] = os.environ[name]
            else:
                missing.append(name)
        return environment, forwarded, tuple(missing)

    def _provenance(
        self, run: CompiledRun, workspace: Path, executable: Path
    ) -> ProvenanceRecord:
        backend = run.manifest.execution.backend
        return ProvenanceRecord(
            manifest_digest=run.manifest_digest,
            runner_digest=self.runner_digest,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            subject_digest=run.manifest.subject.artifact_digest,
            backend_digest=backend.digest or self.runner_digest,
            trace_emission_claimed=bool(
                getattr(run.manifest.subject, "emits_trace", False)
            ),
            executable=str(executable),
            executable_digest=sha256_file(executable),
            distribution="magentabench-native-process",
            version="1",
            backend_kind="local",
            network_mode="unobservable",
            workspace_namespace=str(workspace.parent),
            test_override=run.manifest.metadata.test_override,
        )

    def _copy_result_artifact(
        self,
        output_dir: Path,
        target_root: Path,
        value: Any,
        forwarded: Mapping[str, str],
        *,
        label: str,
    ) -> ArtifactRef:
        source = _safe_file(output_dir, value, label=label)
        content = source.read_bytes()
        for secret in forwarded.values():
            if secret and secret.encode("utf-8") in content:
                raise NativeBenchmarkConfigurationError(
                    f"{label} contains a forwarded environment value"
                )
        relative = source.relative_to(output_dir)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise NativeBenchmarkConfigurationError(
                f"native result artifact target already exists: {relative.as_posix()}"
            )
        shutil.copy2(source, target)
        return artifact_ref(target)

    def _native_result(
        self,
        run: CompiledRun,
        case: NativeBenchmarkCase,
        result_path: Path,
        case_dir: Path,
        forwarded: Mapping[str, str],
        wall_seconds: float,
    ) -> tuple[
        RunStatus,
        tuple[ArtifactRef, ...],
        ArtifactRef | None,
        VerifierEvidence,
        UsageRecord,
        Any,
        tuple[ArtifactRef, ...],
    ]:
        if result_path.stat().st_size > self.max_capture_bytes:
            raise NativeBenchmarkConfigurationError("native result exceeds max_capture_bytes")
        raw_bytes = result_path.read_bytes()
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        try:
            document = json.loads(raw_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NativeBenchmarkConfigurationError(
                f"native result is malformed JSON; sha256={raw_sha256}"
            ) from exc
        if not isinstance(document, Mapping):
            raise NativeBenchmarkConfigurationError("native result must be a JSON object")
        required_result_keys = {"schema_version", "case_id", "metrics"}
        if set(document) - _RESULT_KEYS or not required_result_keys.issubset(document):
            raise NativeBenchmarkConfigurationError("native result has unsupported fields")
        if document.get("schema_version") != _RESULT_SCHEMA:
            raise NativeBenchmarkConfigurationError("unsupported native result schema")
        if document.get("case_id") != case.task_id:
            raise NativeBenchmarkConfigurationError("native result case_id drift")
        metrics_raw = document.get("metrics")
        if not isinstance(metrics_raw, Mapping) or not metrics_raw:
            raise NativeBenchmarkConfigurationError("native result metrics must be non-empty")
        metrics: dict[str, float] = {}
        for key, value in metrics_raw.items():
            if (
                not isinstance(key, str)
                or not key.strip()
                or type(value) not in {int, float}
                or not math.isfinite(float(value))
            ):
                raise NativeBenchmarkConfigurationError("native result metric is invalid")
            metrics[key] = float(value)
        reward_key = run.manifest.authoritative_reward_metric
        if reward_key not in metrics:
            raise NativeBenchmarkConfigurationError(
                f"native result lacks authoritative metric {reward_key!r}"
            )
        score = metrics[reward_key]
        if run.manifest.scoring_kind == ScoringKind.binary:
            passed = _binary_passed(run, score)
            status = RunStatus.pass_ if passed else RunStatus.verified_fail
        else:
            passed = None
            status = RunStatus.scored

        usage_raw = document.get("usage", {})
        if not isinstance(usage_raw, Mapping):
            raise NativeBenchmarkConfigurationError("native result usage must be an object")
        unknown_usage = set(usage_raw) - set(UsageRecord.model_fields)
        if unknown_usage:
            raise NativeBenchmarkConfigurationError(
                "native result usage has unsupported fields: "
                + ", ".join(sorted(unknown_usage))
            )
        usage_values = dict(usage_raw)
        usage_values["wall_clock_seconds"] = wall_seconds
        if (
            "total_tokens" not in usage_values
            and type(usage_values.get("input_tokens")) is int
            and type(usage_values.get("output_tokens")) is int
        ):
            usage_values["total_tokens"] = (
                usage_values["input_tokens"] + usage_values["output_tokens"]
            )
        try:
            usage = UsageRecord.model_validate(usage_values)
        except ValueError as exc:
            raise NativeBenchmarkConfigurationError("native result usage is invalid") from exc

        raw_artifacts = document.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise NativeBenchmarkConfigurationError("native result artifacts must be a list")
        if any(not isinstance(value, str) for value in raw_artifacts):
            raise NativeBenchmarkConfigurationError(
                "native result artifact paths must be strings"
            )
        artifact_values = tuple(raw_artifacts)
        if len(set(artifact_values)) != len(artifact_values):
            raise NativeBenchmarkConfigurationError("native result artifacts contain duplicates")
        trace_value = document.get("trace")
        if trace_value is not None and not isinstance(trace_value, str):
            raise NativeBenchmarkConfigurationError("native result trace must be a string or null")
        activation_evidence = None
        activation = document.get("model_activation")
        if activation is not None:
            try:
                activation_evidence = ModelActivationEvidence.model_validate(activation)
            except ValueError as exc:
                raise NativeBenchmarkConfigurationError(
                    "native result model_activation is invalid"
                ) from exc
            if activation_evidence.activation_source != "native_result":
                raise NativeBenchmarkConfigurationError(
                    "native result model_activation source must be native_result"
                )
        verifier_name = document.get("verifier")
        if verifier_name is None:
            verifier_name = run.manifest.evaluator.evaluator.implementation
        if not isinstance(verifier_name, str) or not verifier_name.strip():
            raise NativeBenchmarkConfigurationError("native result verifier is invalid")

        sanitized = _redact_tree(document, forwarded)
        persisted_result = case_dir / "native_result.json"
        atomic_write_json(persisted_result, sanitized)
        result_ref = artifact_ref(persisted_result)
        output_dir = result_path.parent
        retained_root = case_dir / "native_outputs"
        retained = [result_ref]
        by_value: dict[str, ArtifactRef] = {}
        for value in artifact_values:
            ref = self._copy_result_artifact(
                output_dir,
                retained_root,
                value,
                forwarded,
                label="native result artifact",
            )
            retained.append(ref)
            by_value[value] = ref
        trace_ref = None
        if trace_value is not None:
            trace_ref = by_value.get(trace_value)
            if trace_ref is None:
                trace_ref = self._copy_result_artifact(
                    output_dir,
                    retained_root,
                    trace_value,
                    forwarded,
                    label="native result trace",
                )
                retained.append(trace_ref)

        activation_receipt = None
        activation_refs: tuple[ArtifactRef, ...] = ()
        if activation_evidence is not None:
            activation_path = case_dir / "model_activation.json"
            atomic_write_json(activation_path, activation_evidence)
            activation_ref = artifact_ref(activation_path)
            activation_refs = (activation_ref,)
            activation_receipt = make_model_activation_receipt(
                run, evidence_refs=activation_refs
            )
        verifier = VerifierEvidence(
            verifier=verifier_name,
            passed=passed,
            score=score,
            metrics=metrics,
            artifact_refs=(result_ref, *retained[1:]),
            details={
                "schema_version": _RESULT_SCHEMA,
                "metric_names": sorted(metrics),
            },
        )
        return (
            status,
            tuple(retained),
            trace_ref,
            verifier,
            usage,
            activation_receipt,
            activation_refs,
        )

    def execute(
        self,
        run: CompiledRun,
        case: NativeBenchmarkCase,
        attempt: Any,
    ) -> CaseExecution:
        attempt_id = attempt.attempt_id
        run_dir = self.run_directory(run)
        case_dir = run_dir / "cases" / attempt_id
        if case_dir.exists():
            raise NativeBenchmarkConfigurationError(
                f"native evidence directory already exists: {case_dir}"
            )
        case_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = run_dir / "resolved_manifest.json"
        with self._manifest_write_lock:
            if not manifest_path.exists():
                atomic_write_bytes(manifest_path, run.wire_json + b"\n")

        workspace = self.workspace_directory(run, attempt_id)
        if workspace.exists():
            raise NativeBenchmarkConfigurationError(
                f"native workspace already exists: {workspace}"
            )
        input_dir = workspace / "public_input"
        output_dir = workspace / "output"
        input_dir.mkdir(parents=True, exist_ok=False)
        output_dir.mkdir(parents=True, exist_ok=False)
        staged_input = input_dir / Path(case.public_input_ref.path).name
        if not _artifact_matches(case.public_input_ref, Path(case.public_input_ref.path)):
            raise NativeBenchmarkConfigurationError("activated public input byte drift")
        shutil.copy2(case.public_input_ref.path, staged_input)
        staged_input.chmod(0o444)
        atomic_write_json(
            case_dir / "input.json",
            {
                "case_id": case.task_id,
                "public_input_ref": case.public_input_ref.model_dump(mode="json"),
                "task_contract_refs": [
                    ref.model_dump(mode="json") for ref in case.task_contract_refs
                ],
                "verifier_contract_refs": [
                    ref.model_dump(mode="json") for ref in case.verifier_contract_refs
                ],
            },
        )

        command = list(_expand_command(run, case, workspace, attempt_id))
        executable = Path(shutil.which(command[0]) or command[0]).resolve(strict=True)
        if not executable.is_file() or executable.is_symlink():
            raise NativeBenchmarkConfigurationError(
                "native command executable must be a regular non-symlink file"
            )
        environment, forwarded, missing_env = self._environment()
        raw_stdout = workspace / "stdout.raw"
        raw_stderr = workspace / "stderr.raw"
        returncode: int | None = None
        timed_out = False
        launch_error: str | None = None
        started = time.monotonic()
        try:
            with raw_stdout.open("wb") as stdout_handle, raw_stderr.open("wb") as stderr_handle:
                process = subprocess.Popen(
                    command,
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
                try:
                    returncode = process.wait(timeout=attempt.remaining_wall_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        except OSError as exc:
            launch_error = str(exc)
        wall_seconds = time.monotonic() - started

        def retained_log(raw: Path, target: Path) -> tuple[ArtifactRef, bool]:
            content = raw.read_bytes() if raw.is_file() else b""
            truncated = len(content) > self.max_capture_bytes
            text = content[: self.max_capture_bytes].decode("utf-8", errors="replace")
            atomic_write_bytes(
                target,
                _redact_text(text, forwarded).encode("utf-8"),
            )
            return artifact_ref(target), truncated

        stdout_ref, stdout_truncated = retained_log(raw_stdout, case_dir / "stdout.log")
        stderr_ref, stderr_truncated = retained_log(raw_stderr, case_dir / "stderr.log")
        status = RunStatus.infra_error
        output_refs: tuple[ArtifactRef, ...] = ()
        trace_ref = None
        verifier = None
        activation_receipt = None
        activation_refs: tuple[ArtifactRef, ...] = ()
        usage = UsageRecord(wall_clock_seconds=wall_seconds)
        result_error: str | None = None
        result_path = output_dir / "result.json"
        if timed_out:
            status = RunStatus.timeout
        elif launch_error is not None:
            status = RunStatus.infra_error
        elif returncode != 0:
            status = RunStatus.agent_error
        elif not result_path.is_file():
            status = RunStatus.no_output
        else:
            try:
                (
                    status,
                    output_refs,
                    trace_ref,
                    verifier,
                    usage,
                    activation_receipt,
                    activation_refs,
                ) = self._native_result(
                    run,
                    case,
                    result_path,
                    case_dir,
                    forwarded,
                    wall_seconds,
                )
            except (NativeBenchmarkConfigurationError, OSError, ValueError) as exc:
                status = RunStatus.invalid_output
                result_error = str(exc)

        network_path = case_dir / "network_observation.json"
        network = record_unobservable_network(
            network_path,
            resolver_adapter="native_benchmark",
            execution_adapter=self.adapter,
            case_id=case.task_id,
            boundary=NetworkBoundary.process,
            allow_internet=case.allow_internet,
            source=NetworkPolicySource.case_set_artifact,
            source_artifact_digest=case.case_set_digest,
            reason="native process egress was not observed by BMP",
        )
        successful = status in {
            RunStatus.pass_,
            RunStatus.verified_fail,
            RunStatus.scored,
        }
        workspace_kept = (
            self.keep_workspace_on_failure
            and not successful
            and result_error is None
        )
        receipt_path = case_dir / "subject_receipt.json"
        receipt = {
            "protocol_version": 1,
            "case_id": case.task_id,
            "attempt_id": attempt_id,
            "command": [_redact_text(arg, forwarded) for arg in command],
            "returncode": returncode,
            "timed_out": timed_out,
            "launch_error": launch_error,
            "result_error": result_error,
            "environment_variable_names": sorted(forwarded),
            "missing_environment_variable_names": sorted(missing_env),
            "workspace": str(workspace),
            "workspace_kept": workspace_kept,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        atomic_write_json(receipt_path, receipt)
        status_path = case_dir / "status.json"
        atomic_write_json(status_path, {"case_id": case.task_id, "status": status.value})
        if not workspace_kept:
            shutil.rmtree(workspace, ignore_errors=False)
        log_refs = (
            stdout_ref,
            stderr_ref,
            artifact_ref(receipt_path),
            artifact_ref(status_path),
            artifact_ref(network_path),
            *activation_refs,
        )
        provenance = self._provenance(run, workspace, executable)
        if activation_receipt is not None:
            provenance = provenance.model_copy(
                update={"model_activation": activation_receipt}
            )
        bundle = EvidenceBundle(
            run_id=attempt_id,
            status=status,
            output_refs=output_refs,
            trace_ref=trace_ref,
            log_refs=log_refs,
            verifier_evidence=verifier,
            usage=usage,
            network_policy=network.policy,
            network_observation=network.observation,
            provenance=provenance,
        )
        bundle_path = case_dir / "evidence_bundle.json"
        atomic_write_json(bundle_path, bundle)
        return CaseExecution(
            case_id=case.task_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=sha256_file(bundle_path),
        )


class NativeProcessFactory:
    adapter = "native_process"
    digest = _MODULE_DIGEST

    def build(
        self,
        run: CompiledRun,
        *,
        record_root: Path,
        workspace_root: Path,
    ) -> NativeProcessBackend:
        if run.manifest.execution.backend.adapter != self.adapter:
            raise AdapterRegistryError("native process factory received another backend")
        defaults = run.manifest.execution.backend.defaults
        return NativeProcessBackend(
            record_root,
            workspace_root=workspace_root,
            environment_variable_names=defaults.get("environment_variable_names", ()),
            keep_workspace_on_failure=defaults.get(
                "keep_workspace_on_failure", True
            ),
            max_capture_bytes=defaults.get(
                "max_capture_bytes", _DEFAULT_MAX_CAPTURE_BYTES
            ),
        )


class NativeBenchmarkExecutionAdapter:
    benchmark_adapter = "native_benchmark"
    backend_adapter = "native_process"
    subject_interface = "native-benchmark-v1"
    digest = _MODULE_DIGEST

    def execute(
        self,
        backend: NativeProcessBackend,
        run: CompiledRun,
        case: NativeBenchmarkCase,
        attempt: Any,
    ) -> CaseExecution:
        return backend.execute(run, case, attempt)

    def reset_state(
        self,
        backend: NativeProcessBackend,
        case_id: str,
        policy: str,
    ) -> Any:
        return backend.reset_state(case_id, policy)


__all__ = [
    "NativeBenchmarkCase",
    "NativeBenchmarkConfigurationError",
    "NativeBenchmarkExecutionAdapter",
    "NativeBenchmarkLoader",
    "NativeProcessBackend",
    "NativeProcessFactory",
]
