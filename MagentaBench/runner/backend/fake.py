"""Deterministic in-process backend for the BMP conformance benchmark."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
from typing import Any
import threading

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from MagentaBench.adapters.fake import (
    FakeFault,
    FakeSubject,
    FakeSubjectError,
    FakeTask,
    FakeVerifier,
    FakeVerifierError,
)
from MagentaBench.schemas import (
    EvidenceBundle,
    ProvenanceRecord,
    RunStatus,
    UsageRecord,
    VerifierEvidence,
)

from ..compiler import CompiledRun, canonical_json_bytes, sha256_bytes
from ..evidence import artifact_ref, atomic_write_bytes, atomic_write_json, sha256_file


@dataclass(frozen=True)
class CaseExecution:
    case_id: str
    bundle: EvidenceBundle
    bundle_path: Path
    bundle_digest: str
    reused: bool = False


class EvidenceDriftError(RuntimeError):
    """A complete bundle's pinned provenance disagrees with the resume plan."""


class FakeBackend:
    """Execute fake cases and materialize complete, hash-linked evidence."""

    adapter = "fake"

    def __init__(
        self,
        record_root: str | Path,
        *,
        allow_test_task_override: bool = False,
    ) -> None:
        self.record_root = Path(record_root).resolve()
        self.runner_digest = self._runtime_digest()
        self.allow_test_task_override = allow_test_task_override
        self._manifest_write_lock = threading.Lock()

    @staticmethod
    def _runtime_digest() -> str:
        package_root = Path(__file__).parents[2]
        paths = (
            package_root / "runner" / "compiler.py",
            package_root / "runner" / "pipeline.py",
            package_root / "runner" / "scheduler.py",
            package_root / "runner" / "gates.py",
            package_root / "runner" / "evidence.py",
            package_root / "runner" / "backend" / "fake.py",
            package_root / "adapters" / "fake" / "task.py",
            package_root / "adapters" / "fake" / "subject.py",
            package_root / "adapters" / "fake" / "verifier.py",
        )
        digest_input = b"".join(
            str(path.relative_to(package_root)).encode("utf-8")
            + b"\0"
            + path.read_bytes()
            + b"\0"
            for path in paths
        )
        return sha256_bytes(digest_input)

    @staticmethod
    def _factor(run: CompiledRun, *names: str, default: Any = None) -> Any:
        for name in names:
            if name in run.factor_values:
                return run.factor_values[name]
        return default

    @staticmethod
    def _subject_response(run: CompiledRun) -> str:
        configured = getattr(run.manifest.subject, "fixed_answer", None)
        if configured is not None:
            return str(configured)
        entrypoint = getattr(run.manifest.subject, "entrypoint", "")
        if isinstance(entrypoint, str) and entrypoint.startswith("fixed:"):
            return entrypoint.partition(":")[2]
        return "BMP_BAD" if "control" in run.manifest.subject.id else "BMP_OK"

    @staticmethod
    def _subject_fault(run: CompiledRun) -> FakeFault:
        configured = getattr(run.manifest.subject, "fault_mode", None)
        raw = FakeBackend._factor(
            run,
            "fault_mode",
            "subject.fault_mode",
            default=configured or FakeFault.none.value,
        )
        try:
            return FakeFault(str(raw))
        except ValueError as exc:
            raise ValueError(f"unknown fake fault mode {raw!r}") from exc

    def run_directory(self, run: CompiledRun) -> Path:
        return (
            self.record_root
            / run.manifest.metadata.experiment_id
            / run.manifest_digest
        )

    @staticmethod
    def _load_task(run: CompiledRun) -> FakeTask:
        benchmark = run.manifest.benchmark
        source = getattr(benchmark, "source", None)
        task_manifest = getattr(benchmark, "task_manifest", None)
        if not source or not task_manifest:
            raise ValueError("fake backend requires a task-suite manifest")
        path = Path(source) / str(task_manifest)
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        tasks = document.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
            raise ValueError("fake conformance manifest must contain exactly one [[tasks]]")
        task = tasks[0]
        return FakeTask(
            task_id=str(task["id"]),
            instruction=str(task["instruction"]),
            expected=str(task["expected"]),
            output_filename=str(task["output"]),
        )

    @staticmethod
    def reset_state(case_id: str, policy: str) -> None:
        """Fake subjects are stateless; invoking the hook is the reset receipt."""

    def _provenance(self, run: CompiledRun) -> ProvenanceRecord:
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
            executable=backend.executable,
            distribution="magentabench",
            version="0.1.0",
            backend_kind="fake",
            network_mode=str(
                backend.defaults.get("network_mode", backend.defaults.get("network", "none"))
            ),
            workspace_namespace=None,
        )

    def execute(
        self,
        run: CompiledRun,
        task: FakeTask | None = None,
        *,
        case_id: str | None = None,
        execution_run_id: str | None = None,
        attempt_budget: Any | None = None,
        remaining_wall_seconds: float | None = None,
    ) -> CaseExecution:
        registered_task = self._load_task(run)
        task = task or registered_task
        if task != registered_task and not self.allow_test_task_override:
            raise ValueError(
                f"task {task.task_id!r} is not registered by benchmark "
                f"{run.manifest.benchmark.id!r}"
            )
        evidence_case_id = case_id or task.task_id
        run_dir = self.run_directory(run)
        case_dir = run_dir / "cases" / evidence_case_id
        if case_dir.exists():
            shutil.rmtree(case_dir)
        case_dir.mkdir(parents=True, exist_ok=False)

        manifest_path = run_dir / "resolved_manifest.json"
        with self._manifest_write_lock:
            if not manifest_path.exists():
                atomic_write_bytes(manifest_path, run.wire_json + b"\n")
        atomic_write_json(
            case_dir / "input.json",
            {"case_id": evidence_case_id, "instruction": task.instruction},
        )
        stdout_path = case_dir / "stdout.log"
        stderr_path = case_dir / "stderr.log"
        atomic_write_bytes(stdout_path, b"")
        atomic_write_bytes(stderr_path, b"")

        fault = self._subject_fault(run)
        subject = FakeSubject(
            subject_id=run.manifest.subject.id,
            response=self._subject_response(run),
            fault=fault,
        )
        verifier = FakeVerifier()
        status: RunStatus
        verifier_evidence = None
        output_refs = ()
        extra_logs = []
        receipt_data: dict[str, Any]

        try:
            receipt = subject.run(task.public_input(), case_dir)
            receipt_data = asdict(receipt)
            receipt_data["fault"] = receipt.fault.value
            output_path = case_dir / task.output_filename
            if fault == FakeFault.no_output or not output_path.exists():
                status = RunStatus.no_output
            elif fault == FakeFault.invalid_output:
                status = RunStatus.invalid_output
                extra_logs.append(artifact_ref(output_path))
            else:
                try:
                    result = verifier.verify(
                        task,
                        output_path,
                        inject_error=fault == FakeFault.verifier_error,
                    )
                except FakeVerifierError as exc:
                    status = RunStatus.verifier_error
                    atomic_write_bytes(stderr_path, (str(exc) + "\n").encode("utf-8"))
                    extra_logs.append(artifact_ref(output_path))
                except ValueError as exc:
                    status = RunStatus.invalid_output
                    atomic_write_bytes(stderr_path, (str(exc) + "\n").encode("utf-8"))
                    extra_logs.append(artifact_ref(output_path))
                else:
                    status = RunStatus.pass_ if result.passed else RunStatus.verified_fail
                    output_refs = (artifact_ref(output_path),)
                    verifier_path = case_dir / "verifier_evidence.json"
                    atomic_write_json(verifier_path, asdict(result))
                    verifier_artifact = artifact_ref(verifier_path)
                    verifier_evidence = VerifierEvidence(
                        verifier=verifier.verifier_id,
                        passed=result.passed,
                        score=result.score,
                        metrics={"exact_match": result.score},
                        artifact_refs=(verifier_artifact,),
                        details={"expected": result.expected, "actual": result.actual},
                    )
        except FakeSubjectError as exc:
            status = RunStatus(exc.fault.value)
            receipt_data = {
                "subject_id": subject.subject_id,
                "activated": exc.fault != FakeFault.harness_fault,
                "output_path": None,
                "response": None,
                "fault": exc.fault.value,
                "error": str(exc),
            }
            atomic_write_bytes(stderr_path, (str(exc) + "\n").encode("utf-8"))

        receipt_path = case_dir / "subject_receipt.json"
        atomic_write_json(receipt_path, receipt_data)
        status_path = case_dir / "status.json"
        atomic_write_json(status_path, {"case_id": evidence_case_id, "status": status.value})

        # Hash logs only after all injected error messages have been persisted.
        log_refs = (
            artifact_ref(stdout_path),
            artifact_ref(stderr_path),
            artifact_ref(receipt_path),
            artifact_ref(status_path),
            *extra_logs,
        )
        bundle = EvidenceBundle(
            run_id=execution_run_id or run.manifest.metadata.run_id,
            status=status,
            output_refs=output_refs,
            log_refs=log_refs,
            verifier_evidence=verifier_evidence,
            usage=UsageRecord(
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cost=0.0,
                wall_clock_seconds=0.0,
            ),
            provenance=self._provenance(run),
        )
        bundle_path = case_dir / "evidence_bundle.json"
        atomic_write_json(bundle_path, bundle)
        digest = sha256_file(bundle_path)
        return CaseExecution(
            case_id=task.task_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=digest,
        )

    @staticmethod
    def load_completed(
        run: CompiledRun, bundle_path: Path, *, expected_runner_digest: str
    ) -> CaseExecution | None:
        """Return validated reusable evidence, or ``None`` for corrupt/incomplete data."""

        import json

        try:
            raw = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle = EvidenceBundle.model_validate(raw)
        except (OSError, ValueError):
            return None
        provenance = bundle.provenance
        expected = {
            "manifest_digest": run.manifest_digest,
            "runner_digest": expected_runner_digest,
            "benchmark_digest": run.manifest.benchmark.artifact_digest,
            "subject_digest": run.manifest.subject.artifact_digest,
            "backend_digest": (
                run.manifest.execution.backend.digest or expected_runner_digest
            ),
        }
        actual = {name: getattr(provenance, name) for name in expected}
        drift = [name for name in expected if actual[name] != expected[name]]
        if drift:
            raise EvidenceDriftError(
                "resume evidence provenance drift: " + ", ".join(drift)
            )
        if bundle.status == RunStatus.pass_ and (
            bundle.verifier_evidence is None or not bundle.verifier_evidence.passed
        ):
            return None
        if bundle.status == RunStatus.verified_fail and (
            bundle.verifier_evidence is None or bundle.verifier_evidence.passed
        ):
            return None
        refs = [*bundle.output_refs, *bundle.log_refs]
        if bundle.trace_ref is not None:
            refs.append(bundle.trace_ref)
        if bundle.checkpoint_ref is not None:
            refs.append(bundle.checkpoint_ref)
        if bundle.verifier_evidence is not None:
            refs.extend(bundle.verifier_evidence.artifact_refs)
        for ref in refs:
            path = Path(ref.path)
            if (
                not path.is_file()
                or path.stat().st_size != ref.size_bytes
                or sha256_file(path) != ref.sha256
            ):
                return None
        case_id = bundle_path.parent.name
        return CaseExecution(
            case_id=case_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=sha256_file(bundle_path),
            reused=True,
        )


__all__ = ["CaseExecution", "EvidenceDriftError", "FakeBackend"]
