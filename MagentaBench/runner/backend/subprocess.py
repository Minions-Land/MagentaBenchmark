"""Isolated local subprocess backend for opaque command subjects."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

from MagentaBench.adapters.fake import FakeTask, FakeVerifier, FakeVerifierError
from MagentaBench.schemas import (
    BudgetAllocation,
    EnvironmentReceipt,
    EvidenceBundle,
    ProvenanceRecord,
    RunStatus,
    UsageRecord,
    VerifierEvidence,
)

from ..compiler import CompiledRun
from ..evidence import artifact_ref, atomic_write_bytes, atomic_write_json, sha256_file
from .fake import CaseExecution, EvidenceDriftError, FakeBackend


class SubprocessConfigurationError(ValueError):
    """The resolved manifest cannot be translated to a subprocess command."""


class SubprocessBackend:
    """Run one command in a private workspace and collect structured evidence.

    The command never uses a shell. Only a minimal environment is inherited,
    task input is staged read-only, and successful workspaces are removed after
    all outputs have been copied into the evidence directory.
    """

    adapter = "subprocess"

    def __init__(
        self,
        record_root: str | Path,
        *,
        workspace_root: str | Path = "/tmp/bmp",
        keep_workspace_on_failure: bool = True,
        environment_receipt: EnvironmentReceipt | None = None,
        allow_test_override: bool = False,
    ) -> None:
        self.record_root = Path(record_root).resolve()
        self.environment_receipt = environment_receipt
        self.workspace_root = Path(workspace_root).resolve()
        self.keep_workspace_on_failure = keep_workspace_on_failure
        self.allow_test_override = allow_test_override
        self.runner_digest = self._runtime_digest()
        self._manifest_write_lock = threading.Lock()

    @staticmethod
    def _runtime_digest() -> str:
        package_root = Path(__file__).parents[2]
        paths = tuple(sorted((
            Path(__file__),
            Path(__file__).with_name("fake.py"),
            package_root / "runner" / "evidence.py",
            package_root / "adapters" / "fake" / "task.py",
            package_root / "adapters" / "fake" / "subject.py",
            package_root / "adapters" / "fake" / "verifier.py",
        )))
        digest = hashlib.sha256()
        for path in paths:
            digest.update(str(path.relative_to(package_root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _command(run: CompiledRun) -> tuple[str, ...]:
        subject = run.manifest.subject
        backend = run.manifest.execution.backend
        if subject.kind == "fake":
            if not backend.executable:
                raise SubprocessConfigurationError(
                    "fake subprocess conformance requires backend.executable"
                )
            return (backend.executable, subject.fixed_answer)
        launch_argv = getattr(subject, "launch_argv", None)
        if launch_argv is not None:
            command = tuple(str(item) for item in launch_argv)
            if not command or not command[0]:
                raise SubprocessConfigurationError(
                    f"subject {subject.id!r} has an empty launch_argv"
                )
            return command
        entrypoint = getattr(subject, "entrypoint", None)
        if not entrypoint:
            raise SubprocessConfigurationError(
                f"subject {subject.id!r} has no subprocess entrypoint"
            )
        command = tuple(shlex.split(entrypoint))
        if not command:
            raise SubprocessConfigurationError("subject entrypoint resolved to no command")
        return command

    def _child_environment(
        self, overrides: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        # Inheritance is an allowlist: API keys and credentials such as
        # OPENAI_API_KEY, ANTHROPIC_API_KEY, AWS_*, AZURE_*, GOOGLE_*, tokens,
        # and arbitrary parent variables are always blocked unless the caller
        # explicitly supplies a value through the reviewed ``overrides`` path.
        inherited_names = ("PATH", "LANG", "LC_ALL", "TZ")
        environment = {
            name: os.environ[name] for name in inherited_names if name in os.environ
        }
        environment.setdefault("LANG", "C.UTF-8")
        if self.environment_receipt is not None:
            python_bin = Path(self.environment_receipt.python_executable).parent
            environment_root = python_bin.parent
            environment["VIRTUAL_ENV"] = str(environment_root)
            environment["PATH"] = os.pathsep.join(
                (str(python_bin), environment.get("PATH", ""))
            ).rstrip(os.pathsep)
        if overrides:
            environment.update({str(key): str(value) for key, value in overrides.items()})
        return environment

    @staticmethod
    def _load_task(run: CompiledRun) -> FakeTask:
        return FakeBackend._load_task(run)

    def load_completed(
        self, run: CompiledRun, bundle_path: Path, *, expected_runner_digest: str
    ) -> CaseExecution | None:
        case = FakeBackend.load_completed(
            run,
            bundle_path,
            expected_runner_digest=expected_runner_digest,
        )
        if case is not None and self.environment_receipt is not None:
            actual = case.bundle.provenance.environment_receipt
            if actual is None or actual.model_dump(mode="json") != self.environment_receipt.model_dump(mode="json"):
                raise EvidenceDriftError("resume evidence environment receipt drift")
        return case

    def run_directory(self, run: CompiledRun) -> Path:
        return self.record_root / run.manifest.metadata.experiment_id / run.manifest_digest

    def workspace_directory(
        self, run: CompiledRun, task: FakeTask, *, case_id: str | None = None
    ) -> Path:
        return self.workspace_root / run.manifest.metadata.run_id / (case_id or task.task_id)

    @staticmethod
    def reset_state(case_id: str, policy: str) -> dict[str, str]:
        """Receipt that each scheduled subprocess attempt gets a fresh workspace."""

        return {"case_id": case_id, "policy": policy, "mechanism": "fresh_workspace"}

    def _provenance(
        self, run: CompiledRun, workspace: Path, executable: str
    ) -> ProvenanceRecord:
        backend = run.manifest.execution.backend
        executable_path = shutil.which(executable) or executable
        return ProvenanceRecord(
            manifest_digest=run.manifest_digest,
            runner_digest=self.runner_digest,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            subject_digest=run.manifest.subject.artifact_digest,
            backend_digest=backend.digest,
            trace_emission_claimed=bool(
                getattr(run.manifest.subject, "emits_trace", False)
            ),
            executable=str(Path(executable_path).resolve()),
            executable_digest=sha256_file(Path(executable_path).resolve()),
            distribution="magentabench",
            version="0.1.0",
            backend_kind="subprocess",
            network_mode="none",
            workspace_namespace=str(workspace.parent),
            environment_receipt=self.environment_receipt,
            test_override=run.manifest.metadata.test_override,
        )

    def execute(
        self,
        run: CompiledRun,
        task: FakeTask | None = None,
        *,
        command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        activated_case_set_digest: str | None = None,
        case_id: str | None = None,
        execution_run_id: str | None = None,
        attempt_budget: BudgetAllocation | None = None,
        remaining_wall_seconds: float | None = None,
    ) -> CaseExecution:
        if activated_case_set_digest is not None:
            if task is None or len(activated_case_set_digest) != 64:
                raise SubprocessConfigurationError(
                    "activated case-set task binding is malformed"
                )
        else:
            registered_task = FakeBackend._load_task(run)
            task = task or registered_task
            if task != registered_task:
                raise SubprocessConfigurationError(
                    f"task {task.task_id!r} is not registered by benchmark "
                    f"{run.manifest.benchmark.id!r}"
                )
        assert task is not None
        has_test_override = command is not None or environment is not None
        if has_test_override and not self.allow_test_override:
            raise SubprocessConfigurationError(
                "caller command/environment override requires "
                "allow_test_override=true"
            )
        if has_test_override and (
            run.manifest.claim_design.scope.value != "conformance"
            or run.manifest.claim_design.purpose.value != "exploratory"
        ):
            raise SubprocessConfigurationError(
                "caller command/environment override is restricted to "
                "exploratory conformance"
            )
        environment_spec = run.manifest.execution.backend.environment
        if environment_spec is not None and self.environment_receipt is None:
            raise SubprocessConfigurationError(
                "resolved backend requires an EnvironmentReceipt"
            )
        if (
            environment_spec is not None
            and self.environment_receipt is not None
            and self.environment_receipt.spec_digest
            != environment_spec.canonical_digest()
        ):
            raise SubprocessConfigurationError("EnvironmentReceipt does not match backend environment")
        resolved_command = tuple(command) if command is not None else self._command(run)
        if not resolved_command or not resolved_command[0]:
            raise SubprocessConfigurationError("subprocess command must not be empty")
        executable_path = Path(
            shutil.which(resolved_command[0]) or resolved_command[0]
        ).resolve()
        backend = run.manifest.execution.backend
        if not has_test_override:
            if backend.executable is None or executable_path != Path(backend.executable).resolve():
                raise SubprocessConfigurationError(
                    "manifest backend executable does not match resolved command"
                )
            observed_digest = sha256_file(executable_path)
            if observed_digest != backend.digest:
                raise SubprocessConfigurationError(
                    "subprocess executable digest does not match backend pin"
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

        workspace = self.workspace_directory(run, task, case_id=evidence_case_id)
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=False)
        input_path = workspace / "input.txt"
        atomic_write_bytes(input_path, (task.instruction + "\n").encode("utf-8"))
        input_path.chmod(0o444)
        atomic_write_json(
            case_dir / "input.json",
            {"case_id": evidence_case_id, "instruction": task.instruction},
        )

        timeout = (
            remaining_wall_seconds
            if remaining_wall_seconds is not None
            else run.manifest.execution.budget.max_wall_seconds
        )
        stdout = ""
        stderr = ""
        returncode: int | None = None
        status: RunStatus
        started = time.monotonic()
        try:
            completed = subprocess.run(
                resolved_command,
                cwd=workspace,
                env=self._child_environment(environment),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
            status = RunStatus.pass_ if returncode == 0 else RunStatus.agent_error
        except subprocess.TimeoutExpired as exc:
            stdout = self._timeout_text(exc.stdout)
            stderr = self._timeout_text(exc.stderr)
            status = RunStatus.timeout
        except (OSError, UnicodeError) as exc:
            stderr = str(exc) + "\n"
            status = RunStatus.infra_error
        wall_seconds = time.monotonic() - started

        stdout_path = case_dir / "stdout.log"
        stderr_path = case_dir / "stderr.log"
        atomic_write_bytes(stdout_path, stdout.encode("utf-8"))
        atomic_write_bytes(stderr_path, stderr.encode("utf-8"))

        output_refs = ()
        verifier_evidence = None
        extra_logs = []
        if status == RunStatus.pass_:
            normalized = stdout.rstrip("\r\n")
            if not normalized:
                status = RunStatus.no_output
            else:
                output_path = case_dir / task.output_filename
                atomic_write_bytes(output_path, normalized.encode("utf-8"))
                try:
                    result = FakeVerifier().verify(task, output_path)
                except FakeVerifierError as exc:  # pragma: no cover - no injected error
                    stderr += str(exc) + "\n"
                    atomic_write_bytes(stderr_path, stderr.encode("utf-8"))
                    status = RunStatus.verifier_error
                    extra_logs.append(artifact_ref(output_path))
                except ValueError as exc:
                    stderr += str(exc) + "\n"
                    atomic_write_bytes(stderr_path, stderr.encode("utf-8"))
                    status = RunStatus.invalid_output
                    extra_logs.append(artifact_ref(output_path))
                else:
                    status = RunStatus.pass_ if result.passed else RunStatus.verified_fail
                    output_refs = (artifact_ref(output_path),)
                    verifier_path = case_dir / "verifier_evidence.json"
                    atomic_write_json(
                        verifier_path,
                        {
                            "passed": result.passed,
                            "score": result.score,
                            "expected": result.expected,
                            "actual": result.actual,
                        },
                    )
                    verifier_evidence = VerifierEvidence(
                        verifier="fake.exact.v1",
                        passed=result.passed,
                        score=result.score,
                        metrics={"exact_match": result.score},
                        artifact_refs=(artifact_ref(verifier_path),),
                        details={"expected": result.expected, "actual": result.actual},
                    )

        successful_execution = status in {RunStatus.pass_, RunStatus.verified_fail}
        workspace_kept = self.keep_workspace_on_failure and not successful_execution
        if not workspace_kept:
            shutil.rmtree(workspace, ignore_errors=False)

        receipt_path = case_dir / "subject_receipt.json"
        atomic_write_json(
            receipt_path,
            {
                "subject_id": run.manifest.subject.id,
                "command": list(resolved_command),
                "returncode": returncode,
                "workspace": str(workspace),
                "workspace_kept": workspace_kept,
            },
        )
        status_path = case_dir / "status.json"
        atomic_write_json(status_path, {"case_id": evidence_case_id, "status": status.value})
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
                cache_read_tokens=0,
                cache_write_tokens=0,
                total_tokens=0,
                cost=0.0,
                wall_clock_seconds=wall_seconds,
            ),
            provenance=self._provenance(run, workspace, resolved_command[0]),
        )
        bundle_path = case_dir / "evidence_bundle.json"
        atomic_write_json(bundle_path, bundle)
        return CaseExecution(
            case_id=task.task_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=sha256_file(bundle_path),
        )

    @staticmethod
    def _timeout_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


__all__ = ["SubprocessBackend", "SubprocessConfigurationError"]
