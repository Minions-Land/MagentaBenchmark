"""Manifest-derived benchmark, backend, and execution adapter registry."""

from __future__ import annotations

import hashlib
import json
import random
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from MagentaBench.adapters.fake.task import FakeTask
from MagentaBench.schemas import (
    AdapterCapability,
    ArtifactRef,
    CaseArtifact,
    CaseSetArtifact,
)

from .backend.fake import CaseExecution, FakeBackend
from .backend.subprocess import SubprocessBackend
from .compiler import CompiledRun
from .evidence import (
    artifact_ref,
    atomic_write_bytes,
    sha256_file,
    source_closure_digest,
)


class AdapterRegistryError(RuntimeError):
    """A manifest has no exact production adapter binding."""


def _json_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _write_immutable(path: Path, data: bytes, *, label: str) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise AdapterRegistryError(f"existing {label} byte drift: {path}")
        return
    atomic_write_bytes(path, data)


def write_immutable_json(path: Path, value: Any, *, label: str) -> None:
    _write_immutable(path, _json_bytes(value), label=label)


@dataclass(frozen=True)
class ResolvedCaseSet:
    artifact: CaseSetArtifact
    artifact_path: Path
    artifact_sha256: str


@dataclass(frozen=True)
class _ActivatedFakeCase:
    task: FakeTask
    case_set_digest: str

    @property
    def task_id(self) -> str:
        return self.task.task_id


@dataclass(frozen=True)
class LoadedCaseSet:
    artifact: CaseSetArtifact
    artifact_path: Path
    artifact_sha256: str
    cases: tuple[Any, ...]


class BenchmarkLoader(Protocol):
    adapter: str
    digest: str

    def resolve(self, run: CompiledRun, artifact_root: Path) -> ResolvedCaseSet: ...

    def load(self, run: CompiledRun, resolved: ResolvedCaseSet) -> LoadedCaseSet: ...


class BackendRuntime(Protocol):
    adapter: str
    runner_digest: str

    def run_directory(self, run: CompiledRun) -> Path: ...

    def load_completed(
        self,
        run: CompiledRun,
        bundle_path: Path,
        *,
        expected_runner_digest: str,
    ) -> CaseExecution: ...


class BackendFactory(Protocol):
    adapter: str

    def build(
        self,
        run: CompiledRun,
        *,
        record_root: Path,
        workspace_root: Path,
    ) -> BackendRuntime: ...


class ExecutionAdapter(Protocol):
    benchmark_adapter: str
    backend_adapter: str
    subject_interface: str | None
    digest: str

    def execute(
        self,
        backend: Any,
        run: CompiledRun,
        case: Any,
        attempt: Any,
    ) -> CaseExecution: ...

    def reset_state(
        self, backend: Any, case_id: str, policy: str
    ) -> Any: ...


def _closure_digest(paths: tuple[Path, ...], package_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(package_root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def case_set_refs(artifact: CaseSetArtifact) -> tuple[Any, ...]:
    refs = list(artifact.source_content_refs)
    for case in artifact.cases:
        refs.extend(
            (
                case.public_input_ref,
                *case.task_contract_refs,
                *case.verifier_contract_refs,
            )
        )
    return tuple(refs)


def verify_resolved_case_set(
    run: CompiledRun,
    resolved: ResolvedCaseSet,
    *,
    expected_loader_adapter: str,
    expected_loader_digest: str,
) -> None:
    path = resolved.artifact_path
    if (
        not path.is_file()
        or sha256_file(path) != resolved.artifact_sha256
    ):
        raise AdapterRegistryError("case-set artifact byte drift")
    try:
        persisted = CaseSetArtifact.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise AdapterRegistryError("case-set artifact is malformed") from exc
    if persisted != resolved.artifact:
        raise AdapterRegistryError(
            "persisted case-set artifact differs from resolved artifact"
        )
    artifact = resolved.artifact
    if artifact.benchmark_id != run.manifest.benchmark.id:
        raise AdapterRegistryError("case-set benchmark id drift")
    if artifact.benchmark_digest != run.manifest.benchmark.artifact_digest:
        raise AdapterRegistryError("case-set benchmark digest drift")
    if artifact.loader_adapter != expected_loader_adapter:
        raise AdapterRegistryError("case-set loader adapter drift")
    if artifact.loader_digest != expected_loader_digest:
        raise AdapterRegistryError("case-set loader digest drift")
    protocol = run.manifest.execution.protocol
    if protocol is None or artifact.case_order != protocol.case_order:
        raise AdapterRegistryError("case-set order policy drift")
    expected_selection_method = (
        "explicit_case_ids"
        if protocol.case_order in {"custom", "explicit"}
        else "all_cases"
    )
    if artifact.selection_method != expected_selection_method:
        raise AdapterRegistryError("case-set selection method drift")
    if protocol.case_order in {"custom", "explicit"}:
        if artifact.ordered_case_ids != protocol.explicit_case_ids:
            raise AdapterRegistryError(
                "case-set explicit case order does not match resolved protocol"
            )
    expected_order_seed = (
        run.manifest.execution.seed
        if protocol.case_order == "seeded_random"
        else None
    )
    if artifact.order_seed != expected_order_seed:
        raise AdapterRegistryError("case-set order seed drift")
    for ref in case_set_refs(artifact):
        ref_path = Path(ref.path)
        if (
            not ref_path.is_file()
            or ref_path.stat().st_size != ref.size_bytes
            or sha256_file(ref_path) != ref.sha256
        ):
            raise AdapterRegistryError(
                f"case-set content reference drift: {ref.path}"
            )
    source = getattr(run.manifest.benchmark, "source", None)
    compiled_source_digest = getattr(
        run.manifest.benchmark, "source_content_digest", None
    )
    if not source or not compiled_source_digest:
        raise AdapterRegistryError(
            "case-set activation requires compiled source content identity"
        )
    try:
        observed_source_digest = source_closure_digest(
            Path(source), artifact.source_content_refs
        )
    except (OSError, ValueError) as exc:
        raise AdapterRegistryError(
            f"case-set source closure is invalid: {exc}"
        ) from exc
    if (
        artifact.source_content_digest != compiled_source_digest
        or observed_source_digest != compiled_source_digest
    ):
        raise AdapterRegistryError(
            "case-set source closure differs from compiled benchmark"
        )


class FakeBenchmarkLoader:
    adapter = "fake"

    def __init__(self) -> None:
        package_root = Path(__file__).parents[1]
        self.digest = _closure_digest(
            (
                Path(__file__),
                package_root / "adapters" / "fake" / "task.py",
                package_root / "runner" / "evidence.py",
                package_root / "schemas" / "models.py",
            ),
            package_root,
        )

    @staticmethod
    def _task_manifest(run: CompiledRun) -> Path:
        benchmark = run.manifest.benchmark
        source = getattr(benchmark, "source", None)
        task_manifest = getattr(benchmark, "task_manifest", None)
        if not source or not task_manifest:
            raise AdapterRegistryError(
                "fake benchmark loader requires a task-suite manifest"
            )
        path = (Path(source) / str(task_manifest)).resolve()
        if not path.is_file():
            raise AdapterRegistryError(f"fake task manifest is missing: {path}")
        return path

    @staticmethod
    def _tasks_from_bytes(content: bytes) -> tuple[FakeTask, ...]:
        try:
            document = tomllib.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise AdapterRegistryError("fake task manifest is malformed") from exc
        raw_tasks = document.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise AdapterRegistryError("fake task manifest requires [[tasks]]")
        tasks = []
        for raw in raw_tasks:
            if not isinstance(raw, Mapping):
                raise AdapterRegistryError("fake task entries must be tables")
            tasks.append(
                FakeTask(
                    task_id=str(raw["id"]),
                    instruction=str(raw["instruction"]),
                    expected=str(raw["expected"]),
                    output_filename=str(raw["output"]),
                )
            )
        task_ids = [task.task_id for task in tasks]
        if len(set(task_ids)) != len(task_ids):
            raise AdapterRegistryError("fake task manifest contains duplicate case ids")
        return tuple(tasks)

    @staticmethod
    def _ordered_tasks(
        run: CompiledRun, tasks: tuple[FakeTask, ...]
    ) -> tuple[FakeTask, ...]:
        tasks = list(tasks)
        protocol = run.manifest.execution.protocol
        if protocol is None:
            raise AdapterRegistryError(
                "case-set resolution requires a resolved protocol"
            )
        if protocol.case_order == "seeded_random":
            seed = run.manifest.execution.seed
            if seed is None:
                raise AdapterRegistryError(
                    "seeded case-set resolution requires an execution seed"
                )
            random.Random(seed).shuffle(tasks)
        elif protocol.case_order == "random":
            # Unseeded order is intentionally non-replayable; the activated
            # case-set artifact records the observed permutation so every
            # downstream verifier can still bind the run to what was used.
            random.SystemRandom().shuffle(tasks)
        elif protocol.case_order in {"custom", "explicit"}:
            requested = tuple(protocol.explicit_case_ids)
            if not requested:
                raise AdapterRegistryError(
                    "explicit case-set resolution requires explicit_case_ids"
                )
            if len(set(requested)) != len(requested):
                raise AdapterRegistryError("explicit case ids must be unique")
            by_id = {task.task_id: task for task in tasks}
            missing = [case_id for case_id in requested if case_id not in by_id]
            if missing:
                raise AdapterRegistryError(
                    "explicit case ids are missing from task manifest: "
                    + ", ".join(missing)
                )
            tasks = [by_id[case_id] for case_id in requested]
        elif protocol.case_order != "fixed":
            raise AdapterRegistryError(
                "case-set identity requires fixed or seeded_random case order"
            )
        return tuple(tasks)

    def resolve(self, run: CompiledRun, artifact_root: Path) -> ResolvedCaseSet:
        task_manifest = self._task_manifest(run)
        source_bytes = task_manifest.read_bytes()
        source_ref = ArtifactRef(
            path=str(task_manifest),
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            size_bytes=len(source_bytes),
        )
        compiled_source_digest = run.manifest.benchmark.source_content_digest
        if source_closure_digest(
            Path(run.manifest.benchmark.source), (source_ref,)
        ) != compiled_source_digest:
            raise AdapterRegistryError(
                "case-set source closure differs from compiled benchmark"
            )
        tasks = self._tasks_from_bytes(source_bytes)
        cases = []
        for task in self._ordered_tasks(run, tasks):
            public_payload = _json_bytes(asdict(task.public_input()))
            public_digest = hashlib.sha256(public_payload).hexdigest()
            public_path = (
                artifact_root
                / "content"
                / f"{task.task_id}-{public_digest}.json"
            )
            _write_immutable(
                public_path,
                public_payload,
                label="case-set public input",
            )
            verifier_payload = _json_bytes(asdict(task))
            verifier_digest = hashlib.sha256(verifier_payload).hexdigest()
            verifier_path = (
                artifact_root
                / "content"
                / f"{task.task_id}-verifier-{verifier_digest}.json"
            )
            _write_immutable(
                verifier_path,
                verifier_payload,
                label="case-set verifier contract",
            )
            cases.append(
                CaseArtifact(
                    case_id=task.task_id,
                    public_input_ref=artifact_ref(public_path),
                    verifier_contract_refs=(artifact_ref(verifier_path),),
                )
            )
        artifact = CaseSetArtifact(
            benchmark_id=run.manifest.benchmark.id,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            loader_adapter=self.adapter,
            loader_digest=self.digest,
            selection_method=(
                "explicit_case_ids"
                if run.manifest.execution.protocol.case_order in {"custom", "explicit"}
                else "all_cases"
            ),
            case_order=run.manifest.execution.protocol.case_order,
            order_seed=(
                run.manifest.execution.seed
                if run.manifest.execution.protocol.case_order == "seeded_random"
                else None
            ),
            source_content_digest=(
                run.manifest.benchmark.source_content_digest
            ),
            source_content_refs=(source_ref,),
            ordered_case_ids=tuple(case.case_id for case in cases),
            cases=tuple(cases),
        )
        artifact_path = (
            artifact_root
            / artifact.canonical_digest()
            / "case_set.json"
        )
        write_immutable_json(
            artifact_path,
            artifact,
            label="case-set artifact",
        )
        return ResolvedCaseSet(
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_sha256=sha256_file(artifact_path),
        )

    def load(self, run: CompiledRun, resolved: ResolvedCaseSet) -> LoadedCaseSet:
        tasks = []
        for case in resolved.artifact.cases:
            if len(case.verifier_contract_refs) != 1:
                raise AdapterRegistryError(
                    f"fake case requires one verifier contract: {case.case_id}"
                )
            try:
                public_payload = json.loads(
                    Path(case.public_input_ref.path).read_text(encoding="utf-8")
                )
                verifier_payload = json.loads(
                    Path(case.verifier_contract_refs[0].path).read_text(
                        encoding="utf-8"
                    )
                )
                task = FakeTask(**verifier_payload)
            except (OSError, TypeError, ValueError) as exc:
                raise AdapterRegistryError(
                    f"activated case content is unreadable: {case.case_id}"
                ) from exc
            if task.task_id != case.case_id:
                raise AdapterRegistryError(
                    f"activated verifier case id drift: {case.case_id}"
                )
            if public_payload != asdict(task.public_input()):
                raise AdapterRegistryError(
                    f"public/verifier contract disagreement: {case.case_id}"
                )
            tasks.append(task)
        if tuple(task.task_id for task in tasks) != resolved.artifact.ordered_case_ids:
            raise AdapterRegistryError("loaded fake case order differs from case set")
        return LoadedCaseSet(
            artifact=resolved.artifact,
            artifact_path=resolved.artifact_path,
            artifact_sha256=resolved.artifact_sha256,
            cases=tuple(
                _ActivatedFakeCase(
                    task=task,
                    case_set_digest=resolved.artifact.canonical_digest(),
                )
                for task in tasks
            ),
        )


class FakeBackendFactory:
    adapter = "fake"

    def build(
        self,
        run: CompiledRun,
        *,
        record_root: Path,
        workspace_root: Path,
    ) -> FakeBackend:
        if run.manifest.execution.backend.adapter != self.adapter:
            raise AdapterRegistryError("fake factory received non-fake manifest")
        return FakeBackend(record_root)


class FakeExecutionAdapter:
    benchmark_adapter = "fake"
    backend_adapter = "fake"
    subject_interface = None

    def __init__(self) -> None:
        package_root = Path(__file__).parents[1]
        self.digest = _closure_digest(
            (
                Path(__file__),
                package_root / "runner" / "backend" / "fake.py",
                package_root / "runner" / "evidence.py",
                package_root / "schemas" / "models.py",
            ),
            package_root,
        )

    def execute(
        self,
        backend: FakeBackend,
        run: CompiledRun,
        case: _ActivatedFakeCase,
        attempt: Any,
    ) -> CaseExecution:
        return backend.execute(
            run,
            case.task,
            activated_case_set_digest=case.case_set_digest,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
            attempt_budget=attempt.allocation,
            remaining_wall_seconds=attempt.remaining_wall_seconds,
        )

    def reset_state(
        self, backend: FakeBackend, case_id: str, policy: str
    ) -> Any:
        return backend.reset_state(case_id, policy)


class SubprocessBackendFactory:
    adapter = "subprocess"

    def build(
        self,
        run: CompiledRun,
        *,
        record_root: Path,
        workspace_root: Path,
    ) -> SubprocessBackend:
        if run.manifest.execution.backend.adapter != self.adapter:
            raise AdapterRegistryError(
                "subprocess factory received non-subprocess manifest"
            )
        return SubprocessBackend(record_root, workspace_root=workspace_root)


class SubprocessExecutionAdapter:
    benchmark_adapter = "fake"
    backend_adapter = "subprocess"
    subject_interface = None

    def __init__(self) -> None:
        package_root = Path(__file__).parents[1]
        self.digest = _closure_digest(
            (
                Path(__file__),
                package_root / "runner" / "backend" / "subprocess.py",
                package_root / "runner" / "evidence.py",
                package_root / "schemas" / "models.py",
            ),
            package_root,
        )

    def execute(
        self,
        backend: SubprocessBackend,
        run: CompiledRun,
        case: _ActivatedFakeCase,
        attempt: Any,
    ) -> CaseExecution:
        return backend.execute(
            run,
            case.task,
            activated_case_set_digest=case.case_set_digest,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
            attempt_budget=attempt.allocation,
            remaining_wall_seconds=attempt.remaining_wall_seconds,
        )

    def reset_state(
        self, backend: SubprocessBackend, case_id: str, policy: str
    ) -> Any:
        return backend.reset_state(case_id, policy)


class AdapterRegistry:
    """Exact adapter registry with explicit, digest-bound extension points.

    The built-in production registry remains deterministic.  A benchmark
    package can extend a copy by registering its loader/backend/executor and a
    matching ``AdapterCapability``; there is no name-based fallback.
    """

    def __init__(
        self,
        *,
        benchmark_loaders: Mapping[str, BenchmarkLoader],
        backend_factories: Mapping[str, BackendFactory],
        execution_adapters: Mapping[
            tuple[str, str, str | None], ExecutionAdapter
        ],
        capabilities: Mapping[str, AdapterCapability] | None = None,
    ) -> None:
        for key, loader in benchmark_loaders.items():
            if key != loader.adapter:
                raise AdapterRegistryError(
                    f"BenchmarkLoader registry key mismatch: {key!r}"
                )
        for key, factory in backend_factories.items():
            if key != factory.adapter:
                raise AdapterRegistryError(
                    f"backend factory registry key mismatch: {key!r}"
                )
        for key, adapter in execution_adapters.items():
            declared = (
                adapter.benchmark_adapter,
                adapter.backend_adapter,
                adapter.subject_interface,
            )
            if key != declared:
                raise AdapterRegistryError(
                    f"ExecutionAdapter registry key mismatch: {key!r}"
                )
        self._benchmark_loaders = MappingProxyType(dict(benchmark_loaders))
        self._backend_factories = MappingProxyType(dict(backend_factories))
        self._execution_adapters = MappingProxyType(dict(execution_adapters))
        capability_values = dict(capabilities or {})
        for key, capability in capability_values.items():
            if key != capability.adapter:
                raise AdapterRegistryError(
                    f"AdapterCapability registry key mismatch: {key!r}"
                )
        self._capabilities = MappingProxyType(capability_values)

    @classmethod
    def production(cls) -> "AdapterRegistry":
        loader = FakeBenchmarkLoader()
        fake_factory = FakeBackendFactory()
        fake_executor = FakeExecutionAdapter()
        subprocess_factory = SubprocessBackendFactory()
        subprocess_executor = SubprocessExecutionAdapter()
        return cls(
            benchmark_loaders={loader.adapter: loader},
            backend_factories={
                fake_factory.adapter: fake_factory,
                subprocess_factory.adapter: subprocess_factory,
            },
            execution_adapters={
                (
                    fake_executor.benchmark_adapter,
                    fake_executor.backend_adapter,
                    fake_executor.subject_interface,
                ): fake_executor,
                (
                    subprocess_executor.benchmark_adapter,
                    subprocess_executor.backend_adapter,
                    subprocess_executor.subject_interface,
                ): subprocess_executor,
            },
        )

    def extend(
        self,
        *,
        capability: AdapterCapability,
        benchmark_loader: BenchmarkLoader | None = None,
        backend_factory: BackendFactory | None = None,
        execution_adapter: ExecutionAdapter | None = None,
    ) -> "AdapterRegistry":
        """Return a registry with one explicit plugin capability installed."""

        if capability.adapter_kind == "benchmark_loader":
            if benchmark_loader is None or benchmark_loader.adapter != capability.adapter:
                raise AdapterRegistryError(
                    "benchmark_loader capability requires a matching loader"
                )
            if benchmark_loader.digest != capability.digest:
                raise AdapterRegistryError("benchmark loader digest does not match capability")
        elif benchmark_loader is not None:
            raise AdapterRegistryError("benchmark_loader is forbidden for this capability kind")
        if capability.adapter_kind == "backend_factory":
            if backend_factory is None or backend_factory.adapter != capability.adapter:
                raise AdapterRegistryError(
                    "backend_factory capability requires a matching factory"
                )
        elif backend_factory is not None:
            raise AdapterRegistryError("backend_factory is forbidden for this capability kind")
        if capability.adapter_kind == "execution":
            if execution_adapter is None or execution_adapter.benchmark_adapter != capability.adapter:
                raise AdapterRegistryError(
                    "execution capability requires an adapter with matching benchmark id"
                )
        elif execution_adapter is not None:
            raise AdapterRegistryError("execution_adapter is forbidden for this capability kind")

        benchmark_loaders = dict(self._benchmark_loaders)
        backend_factories = dict(self._backend_factories)
        execution_adapters = dict(self._execution_adapters)
        capabilities = dict(self._capabilities)
        capabilities[capability.adapter] = capability
        if benchmark_loader is not None:
            benchmark_loaders[benchmark_loader.adapter] = benchmark_loader
        if backend_factory is not None:
            backend_factories[backend_factory.adapter] = backend_factory
        if execution_adapter is not None:
            key = (
                execution_adapter.benchmark_adapter,
                execution_adapter.backend_adapter,
                execution_adapter.subject_interface,
            )
            execution_adapters[key] = execution_adapter
        return type(self)(
            benchmark_loaders=benchmark_loaders,
            backend_factories=backend_factories,
            execution_adapters=execution_adapters,
            capabilities=capabilities,
        )

    def capability(self, adapter: str) -> AdapterCapability:
        try:
            return self._capabilities[adapter]
        except KeyError as exc:
            raise AdapterRegistryError(
                f"adapter capability {adapter!r} is not registered"
            ) from exc

    @property
    def capabilities(self) -> tuple[AdapterCapability, ...]:
        return tuple(self._capabilities[key] for key in sorted(self._capabilities))

    @staticmethod
    def compatibility_key(run: CompiledRun) -> tuple[str, str, str | None]:
        subject = run.manifest.subject
        interface = None if subject.kind == "fake" else getattr(subject, "interface", None)
        return (
            run.manifest.benchmark.adapter,
            run.manifest.execution.backend.adapter,
            interface,
        )

    def benchmark_loader(self, run: CompiledRun) -> BenchmarkLoader:
        adapter = run.manifest.benchmark.adapter
        try:
            return self._benchmark_loaders[adapter]
        except KeyError as exc:
            raise AdapterRegistryError(
                f"no production BenchmarkLoader for adapter {adapter!r}"
            ) from exc

    def backend_factory(self, run: CompiledRun) -> BackendFactory:
        adapter = run.manifest.execution.backend.adapter
        try:
            return self._backend_factories[adapter]
        except KeyError as exc:
            raise AdapterRegistryError(
                f"no production backend factory for adapter {adapter!r}"
            ) from exc

    def execution_adapter(self, run: CompiledRun) -> ExecutionAdapter:
        key = self.compatibility_key(run)
        try:
            return self._execution_adapters[key]
        except KeyError as exc:
            raise AdapterRegistryError(
                f"no production ExecutionAdapter for compatibility tuple {key!r}"
            ) from exc


__all__ = [
    "AdapterRegistry",
    "AdapterRegistryError",
    "BackendFactory",
    "BackendRuntime",
    "BenchmarkLoader",
    "ExecutionAdapter",
    "LoadedCaseSet",
    "ResolvedCaseSet",
    "case_set_refs",
    "verify_resolved_case_set",
    "write_immutable_json",
]
