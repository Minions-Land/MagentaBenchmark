"""Zero-cost AOSEBench Docker execution and evidence ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from MagentaBench.adapters.benchmarks.aosebench import AoseTask, check_outputs
from MagentaBench.schemas import EvidenceBundle, ProvenanceRecord, RunStatus, UsageRecord

from ..compiler import CompiledRun
from ..evidence import artifact_ref, atomic_write_bytes, atomic_write_json, sha256_file
from .fake import CaseExecution


class AoseDockerError(RuntimeError):
    """The immutable image or mount contract failed before agent attribution."""


@dataclass(frozen=True)
class AoseDockerExecution:
    case: CaseExecution
    workspace: Path
    workspace_kept: bool
    docker_argv: tuple[str, ...]


class AoseDockerBackend:
    """Run a shell-free agent command in the cached AOSE image.

    Provider credentials are never forwarded by this zero-cost path and Docker
    networking is disabled, making a judge/provider call structurally
    impossible during the dry-run.
    """

    def __init__(
        self,
        record_root: str | Path,
        *,
        workspace_root: str | Path,
        docker_executable: str | Path = "/usr/bin/docker",
    ) -> None:
        docker = Path(docker_executable)
        if not docker.is_absolute():
            raise AoseDockerError("docker executable must be an absolute pinned path")
        self.docker_executable = str(docker.resolve(strict=True))
        if not os.access(self.docker_executable, os.X_OK):
            raise AoseDockerError("docker executable is not executable")
        self.record_root = Path(record_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.runner_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            tuple(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def _image_id(self, image: str) -> str:
        inspected = self._run(
            [self.docker_executable, "image", "inspect", image, "--format", "{{.Id}}"],
            timeout=30,
        )
        if inspected.returncode != 0:
            raise AoseDockerError(f"cannot inspect cached image: {inspected.stderr}")
        return inspected.stdout.strip()

    def _image_file_hash(self, image: str, executable: str) -> str:
        observed = self._run(
            [
                self.docker_executable,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/usr/bin/sha256sum",
                image,
                executable,
            ],
            timeout=60,
        )
        if observed.returncode != 0:
            raise AoseDockerError(
                f"cannot hash agent executable {executable}: {observed.stderr}"
            )
        match = re.match(r"^([0-9a-f]{64})\s+", observed.stdout)
        if not match:
            raise AoseDockerError("agent executable hash output is malformed")
        return match.group(1)

    def _probe_mounts(self, image: str, task: AoseTask) -> dict[str, object]:
        if task.data_path is None:
            raise AoseDockerError("AOSE task data path is required")
        instruction = self._run(
            [
                self.docker_executable,
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{task.instruction_path.resolve()}:/app/instruction.md:ro",
                "--entrypoint",
                "/usr/bin/sha256sum",
                image,
                "/app/instruction.md",
            ],
            timeout=60,
        )
        host_instruction_digest = sha256_file(task.instruction_path)
        instruction_ok = (
            instruction.returncode == 0
            and instruction.stdout.startswith(host_instruction_digest)
        )
        readonly = self._run(
            [
                self.docker_executable,
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{task.data_path.resolve()}:/app/data:ro",
                "--entrypoint",
                "/usr/bin/touch",
                image,
                "/app/data/.bmp-write-probe",
            ],
            timeout=60,
        )
        if not instruction_ok:
            raise AoseDockerError("instruction mount digest did not match staged input")
        if readonly.returncode == 0:
            raise AoseDockerError("read-only data mount accepted a write probe")
        return {
            "instruction_sha256": host_instruction_digest,
            "instruction_probe_returncode": instruction.returncode,
            "data_readonly_probe_returncode": readonly.returncode,
            "data_readonly_probe_stderr": readonly.stderr[-1000:],
            "network_mode": "none",
        }

    def execute(
        self,
        run: CompiledRun,
        task: AoseTask,
        *,
        agent_argv: Sequence[str],
    ) -> AoseDockerExecution:
        if not agent_argv or not str(agent_argv[0]).startswith("/"):
            raise AoseDockerError("agent argv requires an absolute in-image executable")
        image = run.manifest.execution.backend.image
        if not image or not image.startswith("sha256:"):
            raise AoseDockerError("AOSE Docker image must be pinned by sha256 image ID")
        observed_image_id = self._image_id(image)
        if observed_image_id != image:
            raise AoseDockerError(
                f"image digest drift: manifest {image}, observed {observed_image_id}"
            )
        mount_receipt = self._probe_mounts(image, task)
        executable_digest = self._image_file_hash(image, str(agent_argv[0]))

        workspace = (
            self.workspace_root
            / run.manifest.metadata.run_id
            / task.task_id
        )
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=False)
        evidence_dir = (
            self.record_root
            / run.manifest.metadata.experiment_id
            / run.manifest_digest
            / "cases"
            / task.task_id
        )
        if evidence_dir.exists():
            shutil.rmtree(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=False)

        container_name = (
            f"bmp-{run.manifest.metadata.run_id}-{task.task_id}"
            .lower()
            .replace("_", "-")[:120]
        )
        docker_argv = (
            self.docker_executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "-v",
            f"{workspace}:/app",
            "-v",
            f"{task.data_path.resolve()}:/app/data:ro",
            "-v",
            f"{task.instruction_path.resolve()}:/app/instruction.md:ro",
            "--entrypoint",
            str(agent_argv[0]),
            image,
            *map(str, agent_argv[1:]),
        )
        started = time.monotonic()
        timed_out = False
        try:
            completed = self._run(
                docker_argv,
                timeout=run.manifest.execution.budget.max_wall_seconds or 3600.0,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr or "")
            )
            returncode = None
            self._run([self.docker_executable, "rm", "-f", container_name], timeout=60)
        duration = time.monotonic() - started
        stdout_path = evidence_dir / "container.stdout.log"
        stderr_path = evidence_dir / "container.stderr.log"
        atomic_write_bytes(stdout_path, stdout.encode("utf-8"))
        atomic_write_bytes(stderr_path, stderr.encode("utf-8"))

        output_check = check_outputs(workspace)
        if timed_out:
            status = RunStatus.timeout
        elif returncode != 0:
            status = RunStatus.agent_error
        elif output_check.status == RunStatus.no_output:
            status = RunStatus.no_output
        else:
            # The rubric judge was intentionally not invoked. Outputs are
            # evidence, but no pass/fail claim is supported.
            status = RunStatus.unsupported

        output_refs = []
        trace_ref = None
        outputs_dir = evidence_dir / "outputs"
        for name in ("trace.md", "answer.txt"):
            source = workspace / name
            if source.is_file() and source.stat().st_size > 0:
                destination = outputs_dir / name
                atomic_write_bytes(destination, source.read_bytes())
                ref = artifact_ref(destination)
                output_refs.append(ref)
                if name == "trace.md":
                    trace_ref = ref
        execution_complete = returncode == 0 and len(output_refs) == 2
        workspace_kept = not execution_complete
        if not workspace_kept:
            shutil.rmtree(workspace)

        container_check = self._run(
            [self.docker_executable, "inspect", container_name], timeout=30
        )
        receipt_path = evidence_dir / "container_receipt.json"
        receipt = {
            "image_id": observed_image_id,
            "docker_argv": list(docker_argv),
            "agent_executable": str(agent_argv[0]),
            "agent_executable_sha256": executable_digest,
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_seconds": duration,
            "judge_invocations": 0,
            "provider_environment_names": [],
            "container_removed": container_check.returncode != 0,
            "workspace": str(workspace),
            "workspace_kept": workspace_kept,
            **mount_receipt,
        }
        atomic_write_json(receipt_path, receipt)
        status_path = evidence_dir / "status.json"
        atomic_write_json(status_path, {"status": status.value, "case_id": task.task_id})
        bundle = EvidenceBundle(
            run_id=run.manifest.metadata.run_id,
            status=status,
            output_refs=tuple(output_refs),
            trace_ref=trace_ref,
            log_refs=(
                artifact_ref(stdout_path),
                artifact_ref(stderr_path),
                artifact_ref(receipt_path),
                artifact_ref(status_path),
            ),
            usage=UsageRecord(wall_clock_seconds=duration),
            provenance=ProvenanceRecord(
                manifest_digest=run.manifest_digest,
                runner_digest=self.runner_digest,
                benchmark_digest=run.manifest.benchmark.artifact_digest,
                subject_digest=run.manifest.subject.artifact_digest,
                backend_digest=observed_image_id.removeprefix("sha256:"),
                executable=str(agent_argv[0]),
                distribution="docker",
                version=run.manifest.execution.backend.version,
                image_digest=observed_image_id,
                trace_emission_claimed=run.manifest.subject.emits_trace,
                backend_kind="docker",
                network_mode="none",
                workspace_namespace=str(workspace.parent),
            ),
        )
        bundle_path = evidence_dir / "evidence_bundle.json"
        atomic_write_json(bundle_path, bundle)
        case = CaseExecution(
            case_id=task.task_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=sha256_file(bundle_path),
        )
        return AoseDockerExecution(
            case=case,
            workspace=workspace,
            workspace_kept=workspace_kept,
            docker_argv=docker_argv,
        )


__all__ = ["AoseDockerBackend", "AoseDockerError", "AoseDockerExecution"]
