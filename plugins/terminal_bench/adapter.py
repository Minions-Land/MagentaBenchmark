"""Terminal-Bench 2.x adapter for the native Harbor backend.

The adapter deliberately keeps Terminal-Bench's task implementation opaque to
BMP.  It only records the task source closure, exposes a stable case id and
passes the activated local task directory to Harbor.  Harbor remains the
authority for container setup, agent execution and verifier semantics.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from MagentaBench.runner.adapter_registry import (
    AdapterRegistryError,
    LoadedCaseSet,
    ResolvedCaseSet,
    _write_immutable,
    write_immutable_json,
)
from MagentaBench.runner.backend.fake import CaseExecution
from MagentaBench.runner.backend.harbor import HarborBackend, HarborConfigurationError
from MagentaBench.runner.compiler import CompiledRun
from MagentaBench.runner.case_order import (
    CaseOrderError,
    custom_order_binding,
    selected_case_ids,
)
from MagentaBench.runner.evidence import (
    artifact_ref,
    atomic_write_json,
    sha256_file,
    source_closure_digest,
)
from MagentaBench.runner.network import record_unobservable_network
from MagentaBench.schemas import (
    ArtifactRef,
    CaseArtifact,
    CaseSetArtifact,
    NetworkBoundary,
    NetworkPolicySource,
)


_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_MODULE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class TerminalBenchCase:
    """One activated Terminal-Bench task and its immutable source binding."""

    task_id: str
    task_name: str
    task_path: str
    task_manifest_ref: ArtifactRef
    task_contract_refs: tuple[ArtifactRef, ...]
    verifier_contract_refs: tuple[ArtifactRef, ...]
    case_set_digest: str
    allow_internet: bool


class TerminalBenchLoader:
    """Load local Terminal-Bench tasks without importing Harbor internals."""

    adapter = "terminal_bench"
    digest = _MODULE_DIGEST

    @staticmethod
    def _source(run: CompiledRun) -> Path:
        source = run.manifest.dataset.source
        if not source:
            raise AdapterRegistryError("Terminal-Bench benchmark source is missing")
        root = Path(source).resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise AdapterRegistryError(f"Terminal-Bench source is not a real directory: {root}")
        return root

    @classmethod
    def _source_refs(cls, run: CompiledRun) -> tuple[ArtifactRef, ...]:
        source_artifact = run.manifest.dataset
        source = cls._source(run)
        patterns = tuple(getattr(source_artifact, "content_globs", ()))
        if not patterns:
            raise AdapterRegistryError("Terminal-Bench content_globs must be non-empty")
        files: set[Path] = set()
        for pattern in patterns:
            for path in source.glob(pattern):
                if path.is_file():
                    resolved = path.resolve(strict=True)
                    try:
                        relative = resolved.relative_to(source)
                    except ValueError as exc:
                        raise AdapterRegistryError(
                            f"Terminal-Bench content path escapes source: {path}"
                        ) from exc
                    if any(part in {"", ".", ".."} for part in relative.parts):
                        raise AdapterRegistryError(
                            f"Terminal-Bench content path is not normalized: {path}"
                        )
                    if path.is_symlink():
                        raise AdapterRegistryError(
                            f"Terminal-Bench content dependency is a symlink: {path}"
                        )
                    files.add(resolved)
        if not files:
            raise AdapterRegistryError("Terminal-Bench content_globs matched no files")
        refs = tuple(artifact_ref(path) for path in sorted(files))
        observed = source_closure_digest(source, refs)
        if observed != source_artifact.source_content_digest:
            raise AdapterRegistryError(
                "Terminal-Bench source closure differs from compiled dataset"
            )
        return refs

    @classmethod
    def _tasks(
        cls, run: CompiledRun
    ) -> tuple[
        tuple[
            str,
            str,
            Path,
            tuple[ArtifactRef, ...],
            tuple[ArtifactRef, ...],
            bool,
        ],
        ...,
    ]:
        source = cls._source(run)
        dataset = run.manifest.dataset
        task_source = dataset.config.get("task_source")
        if not isinstance(task_source, str) or not task_source:
            raise AdapterRegistryError("Terminal-Bench dataset task_source is missing")
        task_root = source / task_source
        if not task_root.is_dir() or task_root.is_symlink():
            raise AdapterRegistryError(f"Terminal-Bench task root is missing: {task_root}")
        tasks: list[
            tuple[str, str, Path, tuple[ArtifactRef, ...], tuple[ArtifactRef, ...], bool]
        ] = []
        for task_dir in sorted(task_root.iterdir(), key=lambda p: p.name):
            if not task_dir.is_dir() or task_dir.is_symlink():
                continue
            manifest_path = task_dir / "task.toml"
            instruction_path = task_dir / "instruction.md"
            if not manifest_path.is_file() or not instruction_path.is_file():
                continue
            try:
                document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
                task_name = str(document["task"]["name"])
            except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
                raise AdapterRegistryError(
                    f"malformed Terminal-Bench task manifest: {manifest_path}"
                ) from exc
            slug = task_name.rsplit("/", 1)[-1]
            if _ID.fullmatch(slug) is None:
                raise AdapterRegistryError(f"invalid Terminal-Bench case id: {slug!r}")
            task_paths = tuple(
                path.resolve()
                for path in sorted(task_dir.rglob("*"))
                if path.is_file()
                and not path.is_symlink()
                and (
                    path.name in {"task.toml", "instruction.md"}
                    or path.relative_to(task_dir).parts[0] == "environment"
                )
            )
            task_refs = tuple(artifact_ref(path) for path in task_paths)
            verifier_paths = tuple(
                path.resolve()
                for path in sorted((task_dir / "tests").rglob("*"))
                if path.is_file() and not path.is_symlink()
            )
            verifier_refs = tuple(artifact_ref(path) for path in verifier_paths)
            environment = document.get("environment", {})
            if not isinstance(environment, Mapping):
                raise AdapterRegistryError(
                    f"Terminal-Bench [environment] must be a table: {manifest_path}"
                )
            raw_allow_internet = environment.get("allow_internet")
            network_mode = environment.get("network_mode")
            if raw_allow_internet is not None and not isinstance(raw_allow_internet, bool):
                raise AdapterRegistryError(
                    f"Terminal-Bench environment.allow_internet must be boolean: {manifest_path}"
                )
            if network_mode is not None:
                if not isinstance(network_mode, str):
                    raise AdapterRegistryError(
                        f"Terminal-Bench environment.network_mode must be a string: {manifest_path}"
                    )
                mode = network_mode.casefold()
                if mode not in {"no-network", "public", "allowlist"}:
                    raise AdapterRegistryError(
                        f"unsupported Terminal-Bench network_mode {network_mode!r}: {manifest_path}"
                    )
                mode_allow = mode != "no-network"
                if raw_allow_internet is not None and raw_allow_internet != mode_allow:
                    raise AdapterRegistryError(
                        f"conflicting Terminal-Bench network policy fields: {manifest_path}"
                    )
                raw_allow_internet = mode_allow
            if raw_allow_internet is None:
                raw_allow_internet = True
            if not task_refs or not verifier_refs:
                raise AdapterRegistryError(
                    f"Terminal-Bench task lacks task/verifier contract files: {task_dir}"
                )
            tasks.append(
                (
                    slug,
                    task_name,
                    task_dir.resolve(),
                    task_refs,
                    verifier_refs,
                    raw_allow_internet,
                )
            )
        if not tasks:
            raise AdapterRegistryError("Terminal-Bench task root contains no valid tasks")
        ids = [item[0] for item in tasks]
        if len(ids) != len(set(ids)):
            raise AdapterRegistryError("Terminal-Bench task names collide after slugging")
        return tuple(tasks)

    @staticmethod
    def _ordered(
        run: CompiledRun,
        tasks: tuple[
            tuple[str, str, Path, tuple[ArtifactRef, ...], tuple[ArtifactRef, ...], bool],
            ...,
        ],
    ) -> tuple[
        tuple[str, str, Path, tuple[ArtifactRef, ...], tuple[ArtifactRef, ...], bool],
        ...,
    ]:
        protocol = run.manifest.execution.protocol
        if protocol is None:
            raise AdapterRegistryError("Terminal-Bench case resolution requires a protocol")
        values = list(tasks)
        if protocol.case_order == "seeded_random":
            if run.manifest.execution.seed is None:
                raise AdapterRegistryError("seeded Terminal-Bench order requires a seed")
            random.Random(run.manifest.execution.seed).shuffle(values)
        elif protocol.case_order == "random":
            random.SystemRandom().shuffle(values)
        elif protocol.case_order in {"custom", "explicit"}:
            try:
                requested = selected_case_ids(protocol)
            except CaseOrderError as exc:
                raise AdapterRegistryError(str(exc)) from exc
            assert requested is not None
            by_id = {item[0]: item for item in values}
            missing = [case_id for case_id in requested if case_id not in by_id]
            if missing:
                raise AdapterRegistryError(
                    "Terminal-Bench explicit case ids are missing: " + ", ".join(missing)
                )
            values = [by_id[case_id] for case_id in requested]
        elif protocol.case_order != "fixed":
            raise AdapterRegistryError(f"unsupported Terminal-Bench case order: {protocol.case_order}")
        return tuple(values)

    def resolve(self, run: CompiledRun, artifact_root: Path) -> ResolvedCaseSet:
        source_refs = self._source_refs(run)
        ordered = self._ordered(run, self._tasks(run))
        cases: list[CaseArtifact] = []
        for task_id, task_name, task_path, task_refs, verifier_refs, _ in ordered:
            instruction_path = task_path / "instruction.md"
            instruction = instruction_path.read_text(encoding="utf-8")
            public_payload = _json_bytes(
                {
                    "case_id": task_id,
                    "task_name": task_name,
                    "task_relpath": task_path.relative_to(self._source(run)).as_posix(),
                    "instruction": instruction,
                }
            )
            public_digest = hashlib.sha256(public_payload).hexdigest()
            public_path = artifact_root / "content" / f"{task_id}-{public_digest}.json"
            _write_immutable(public_path, public_payload, label="Terminal-Bench public input")
            cases.append(
                CaseArtifact(
                    case_id=task_id,
                    public_input_ref=artifact_ref(public_path),
                    task_contract_refs=task_refs,
                    verifier_contract_refs=verifier_refs,
                )
            )
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
            }.get(run.manifest.execution.protocol.case_order, "all_cases"),
            case_order=run.manifest.execution.protocol.case_order,
            order_seed=(
                run.manifest.execution.seed
                if run.manifest.execution.protocol.case_order == "seeded_random"
                else None
            ),
            order_strategy_adapter=(
                run.manifest.execution.protocol.custom_order.adapter
                if run.manifest.execution.protocol.case_order == "custom"
                and run.manifest.execution.protocol.custom_order is not None
                else None
            ),
            order_strategy_ref=(
                custom_order_binding(run.manifest.execution.protocol)[2]
                if run.manifest.execution.protocol.case_order == "custom"
                else None
            ),
            source_content_digest=run.manifest.dataset.source_content_digest,
            source_content_refs=source_refs,
            ordered_case_ids=tuple(case.case_id for case in cases),
            cases=tuple(cases),
        )
        artifact_path = artifact_root / artifact.canonical_digest() / "case_set.json"
        write_immutable_json(artifact_path, artifact, label="Terminal-Bench case-set artifact")
        return ResolvedCaseSet(
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_sha256=sha256_file(artifact_path),
        )

    def load(self, run: CompiledRun, resolved: ResolvedCaseSet) -> LoadedCaseSet:
        source = self._source(run)
        by_id = {item[0]: item for item in self._tasks(run)}
        loaded: list[TerminalBenchCase] = []
        case_set_digest = resolved.artifact.canonical_digest()
        for case in resolved.artifact.cases:
            try:
                public = json.loads(Path(case.public_input_ref.path).read_text(encoding="utf-8"))
                task_id, task_name, task_path, task_refs, verifier_refs, allow_internet = by_id[
                    case.case_id
                ]
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
                raise AdapterRegistryError(
                    f"Terminal-Bench activated case is unreadable: {case.case_id}"
                ) from exc
            if public.get("case_id") != task_id or public.get("task_name") != task_name:
                raise AdapterRegistryError(f"Terminal-Bench public input drift: {case.case_id}")
            if (
                tuple(case.task_contract_refs) != task_refs
                or tuple(case.verifier_contract_refs) != verifier_refs
            ):
                raise AdapterRegistryError(f"Terminal-Bench task contract drift: {case.case_id}")
            loaded.append(
                TerminalBenchCase(
                    task_id=task_id,
                    task_name=task_name,
                    task_path=str(task_path),
                    task_manifest_ref=next(
                        ref
                        for ref in task_refs
                        if Path(ref.path).name == "task.toml"
                    ),
                    task_contract_refs=task_refs,
                    verifier_contract_refs=verifier_refs,
                    case_set_digest=case_set_digest,
                    allow_internet=allow_internet,
                )
            )
        if tuple(item.task_id for item in loaded) != resolved.artifact.ordered_case_ids:
            raise AdapterRegistryError("Terminal-Bench activated case order drift")
        return LoadedCaseSet(
            artifact=resolved.artifact,
            artifact_path=resolved.artifact_path,
            artifact_sha256=resolved.artifact_sha256,
            cases=tuple(loaded),
        )


class TerminalBenchHarborBackend:
    """Pipeline-facing wrapper that launches exactly one native Harbor task."""

    adapter = "harbor"

    def __init__(self, record_root: Path, *, timeout_seconds: float | None = None) -> None:
        self._backend = HarborBackend(
            record_root,
            timeout_seconds=timeout_seconds or 3600.0,
        )
        self.runner_digest = self._backend.runner_digest

    def run_directory(self, run: CompiledRun) -> Path:
        return self._backend.run_directory(run)

    def reset_state(self, case_id: str, policy: str) -> Any:
        return self._backend.reset_state(case_id, policy)

    def load_completed(
        self, run: CompiledRun, bundle_path: Path, *, expected_runner_digest: str
    ) -> CaseExecution | None:
        return self._backend.load_completed(
            run, bundle_path, expected_runner_digest=expected_runner_digest
        )

    def execute(
        self,
        run: CompiledRun,
        case: TerminalBenchCase,
        attempt: Any,
    ) -> CaseExecution:
        staged_path, staging_receipt = self._stage_task(run, case, attempt.attempt_id)
        execution = self._backend.run(
            run,
            task_path=staged_path,
            case_id=attempt.attempt_id,
            execution_id=attempt.attempt_id,
            attempts=1,
            timeout_seconds=attempt.remaining_wall_seconds,
        )
        if len(execution.cases) != 1:
            raise HarborConfigurationError(
                "Terminal-Bench adapter expected exactly one Harbor trial per attempt"
            )
        native = execution.cases[0]
        # Scheduler identity belongs to BMP's attempt, while the native trial
        # name remains in VerifierEvidence.details and copied artifacts.
        network_receipt_path = native.bundle_path.parent / "network_observation.json"
        network = record_unobservable_network(
            network_receipt_path,
            resolver_adapter="terminal_bench",
            execution_adapter="harbor",
            case_id=case.task_id,
            boundary=NetworkBoundary.task_container,
            allow_internet=case.allow_internet,
            source=NetworkPolicySource.case_set_artifact,
            source_artifact_digest=case.case_set_digest,
            reason="Harbor task container network boundary was not observed by BMP",
        )
        bundle = native.bundle.model_copy(
            update={
                "run_id": attempt.attempt_id,
                "log_refs": (
                    *native.bundle.log_refs,
                    artifact_ref(staging_receipt),
                    artifact_ref(network_receipt_path),
                ),
                "network_policy": network.policy,
                "network_observation": network.observation,
            }
        )
        atomic_write_json(native.bundle_path, bundle)
        return CaseExecution(
            case_id=case.task_id,
            bundle=bundle,
            bundle_path=native.bundle_path,
            bundle_digest=sha256_file(native.bundle_path),
        )

    def _stage_task(
        self,
        run: CompiledRun,
        case: TerminalBenchCase,
        attempt_id: str,
    ) -> tuple[Path, Path]:
        """Create a task view that cannot expose Terminal-Bench solutions."""

        source = Path(case.task_path).resolve(strict=True)
        if not source.is_dir() or source.is_symlink():
            raise HarborConfigurationError(f"Terminal-Bench task path is invalid: {source}")
        destination = (
            self._backend.run_directory(run)
            / "staged_tasks"
            / re.sub(r"[^A-Za-z0-9_.-]+", "_", attempt_id)
        )
        if destination.exists():
            raise HarborConfigurationError(
                f"staged task path already exists and is immutable: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=False)
        copied: list[Path] = []
        seen_paths: set[Path] = set()
        # Revalidate exactly the activated source refs before copying.  The
        # loader's resolution and this staging step are separate trust
        # boundaries, so a source mutation must fail closed.
        for ref in (*case.task_contract_refs, *case.verifier_contract_refs):
            raw_path = Path(ref.path)
            current = raw_path.anchor and Path(raw_path.anchor) or Path("/")
            for component in raw_path.parts[1:]:
                current = current / component
                if current.is_symlink():
                    raise HarborConfigurationError(
                        f"symlink in activated task ref: {raw_path}"
                    )
            path = raw_path.resolve(strict=True)
            try:
                relative = path.relative_to(source)
            except ValueError as exc:
                raise HarborConfigurationError(
                    f"activated task ref escapes task root: {ref.path}"
                ) from exc
            if path in seen_paths:
                raise HarborConfigurationError("activated task refs contain duplicate paths")
            seen_paths.add(path)
            if path.is_symlink() or not path.is_file():
                raise HarborConfigurationError(
                    f"activated task ref is not a regular file: {path}"
                )
            if path.stat().st_size != ref.size_bytes or sha256_file(path) != ref.sha256:
                raise HarborConfigurationError(
                    f"activated task ref byte drift: {relative.as_posix()}"
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(target)
        required = (destination / "task.toml", destination / "instruction.md")
        if any(not path.is_file() for path in required):
            raise HarborConfigurationError("staged Terminal-Bench task is missing required files")
        staged_refs = tuple(artifact_ref(path) for path in sorted(copied))
        staged_digest = source_closure_digest(destination, staged_refs)
        receipt_path = destination.parent / f"{destination.name}-staging-receipt.json"
        receipt = {
            "protocol_version": 1,
            "case_id": case.task_id,
            "attempt_id": attempt_id,
            "source_task_path": str(source),
            "source_task_manifest_ref": case.task_manifest_ref.model_dump(mode="json"),
            "staged_task_path": str(destination),
            "staged_content_digest": staged_digest,
            "staged_content_refs": [ref.model_dump(mode="json") for ref in staged_refs],
            "excluded_paths": ["solution", "README.md", ".git"],
            "solution_excluded": (source / "solution").exists()
            and not (destination / "solution").exists(),
        }
        atomic_write_json(receipt_path, receipt)
        return destination, receipt_path


class TerminalBenchHarborFactory:
    adapter = "harbor"
    digest = _MODULE_DIGEST

    def build(
        self,
        run: CompiledRun,
        *,
        record_root: Path,
        workspace_root: Path,
    ) -> TerminalBenchHarborBackend:
        if run.manifest.execution.backend.adapter != self.adapter:
            raise AdapterRegistryError("Terminal-Bench Harbor factory received a non-Harbor run")
        return TerminalBenchHarborBackend(
            record_root,
            timeout_seconds=run.manifest.execution.budget.max_wall_seconds,
        )


class TerminalBenchHarborExecutionAdapter:
    benchmark_adapter = "terminal_bench"
    backend_adapter = "harbor"
    subject_interface = "terminal-bench-harbor-v1"
    digest = _MODULE_DIGEST

    def execute(
        self,
        backend: TerminalBenchHarborBackend,
        run: CompiledRun,
        case: TerminalBenchCase,
        attempt: Any,
    ) -> CaseExecution:
        return backend.execute(run, case, attempt)

    def reset_state(self, backend: TerminalBenchHarborBackend, case_id: str, policy: str) -> Any:
        return backend.reset_state(case_id, policy)


__all__ = [
    "TerminalBenchCase",
    "TerminalBenchLoader",
    "TerminalBenchHarborBackend",
    "TerminalBenchHarborFactory",
    "TerminalBenchHarborExecutionAdapter",
]
