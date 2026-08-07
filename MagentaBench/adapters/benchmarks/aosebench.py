"""AOSEBench/BiomniBench-DA task and container contract adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from MagentaBench.schemas import ArtifactRef, RunStatus

from ...runner.evidence import artifact_ref, atomic_write_json


class AoseBenchConfigurationError(ValueError):
    """A task or container contract is incomplete."""


@dataclass(frozen=True)
class AoseTask:
    task_id: str
    task_dir: Path
    instruction_path: Path
    rubric_path: Path
    task_config: Mapping[str, Any]
    instruction_digest: str
    data_path: Path | None = None

    @property
    def agent_timeout_seconds(self) -> float:
        return float((self.task_config.get("agent") or {}).get("timeout_sec", 3600.0))

    @property
    def verifier_timeout_seconds(self) -> float:
        return float((self.task_config.get("verifier") or {}).get("timeout_sec", 900.0))

    @property
    def environment(self) -> Mapping[str, Any]:
        return self.task_config.get("environment") or {}


@dataclass(frozen=True)
class AoseOutputCheck:
    status: RunStatus
    output_refs: tuple[ArtifactRef, ...]
    reason: str


def load_task(
    benchmark_source: str | Path,
    task_id: str,
    *,
    data_root: str | Path | None = None,
) -> AoseTask:
    """Load a released task contract without loading hidden rubric content."""

    if re.fullmatch(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", task_id) is None:
        raise AoseBenchConfigurationError(f"invalid AOSEBench task id: {task_id!r}")
    root = Path(benchmark_source).expanduser().resolve()
    task_root = (root / "benchmark" / "tasks").resolve()
    task_dir = (task_root / task_id).resolve()
    if not task_dir.is_relative_to(task_root):
        raise AoseBenchConfigurationError(f"AOSEBench task path escapes task root: {task_id}")
    if not task_dir.is_dir():
        raise AoseBenchConfigurationError(f"unknown AOSEBench task: {task_id}")
    instruction = task_dir / "instruction.md"
    rubric = task_dir / "tests" / "rubric.txt"
    config_path = task_dir / "task.toml"
    missing = [str(path) for path in (instruction, rubric, config_path) if not path.is_file()]
    if missing:
        raise AoseBenchConfigurationError(f"task contract files missing: {missing}")
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    data_path = None
    if data_root is not None:
        data_path = Path(data_root).expanduser().resolve() / task_id / "environment" / "data"
        if not data_path.is_dir():
            raise AoseBenchConfigurationError(f"AOSEBench task data is missing: {data_path}")
    return AoseTask(
        task_id=task_id,
        task_dir=task_dir,
        instruction_path=instruction,
        rubric_path=rubric,
        task_config=config,
        instruction_digest=hashlib.sha256(instruction.read_bytes()).hexdigest(),
        data_path=data_path,
    )


def stage_task(task: AoseTask, workspace: str | Path) -> Path:
    """Stage `/app`-equivalent files and a read-only data view for local runs."""

    root = Path(workspace).expanduser().resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=False)
    instruction = root / "instruction.md"
    shutil.copy2(task.instruction_path, instruction)
    instruction.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    if task.data_path is None:
        raise AoseBenchConfigurationError("data_root is required to stage an AOSE task")
    data_link = root / "data"
    data_link.symlink_to(task.data_path, target_is_directory=True)
    return root


def build_docker_command(
    task: AoseTask,
    *,
    image: str,
    output_root: str | Path,
    runner_command: str | Sequence[str],
    network: str = "bridge",
    pass_env: Sequence[str] = (),
    enforce_storage_quota: bool = False,
) -> list[str]:
    """Build the native AOSEBench Docker invocation with read-only data mounts."""

    if not image.strip():
        raise AoseBenchConfigurationError("container image must not be empty")
    if network not in {"bridge", "host", "none"}:
        raise AoseBenchConfigurationError("network must be bridge, host, or none")
    if task.data_path is None:
        raise AoseBenchConfigurationError("task data path is required for Docker")
    output = Path(output_root).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = task.environment
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"aosebench-{task.task_id.replace('_', '-')}",
        "--cpus",
        str(env.get("cpus", 2)),
        "--memory",
        f"{int(env.get('memory_mb', 16384))}m",
        "--network",
        network if bool(env.get("allow_internet", True)) else "none",
    ]
    if enforce_storage_quota:
        command += ["--storage-opt", f"size={int(env.get('storage_mb', 20480))}m"]
    command += [
        "-v",
        f"{output}:/app",
        "-v",
        f"{task.data_path.resolve()}:/app/data:ro",
        "-v",
        f"{task.instruction_path.resolve()}:/app/instruction.md:ro",
        "-e",
        "PYTHONUNBUFFERED=1",
    ]
    for name in pass_env:
        if not name or "=" in name:
            raise AoseBenchConfigurationError("pass_env accepts variable names only")
        if name not in os.environ:
            raise AoseBenchConfigurationError(f"host environment variable is missing: {name}")
        command += ["-e", name]
    command.append(image)
    command += shlex.split(runner_command) if isinstance(runner_command, str) else list(runner_command)
    return command


def check_outputs(output_root: str | Path) -> AoseOutputCheck:
    """Validate the two required output files without invoking the rubric judge."""

    root = Path(output_root).expanduser().resolve()
    trace = root / "trace.md"
    answer = root / "answer.txt"
    refs: list[ArtifactRef] = []
    missing_or_empty = [str(path.name) for path in (trace, answer) if not path.is_file() or path.stat().st_size == 0]
    if missing_or_empty:
        return AoseOutputCheck(
            status=RunStatus.no_output,
            output_refs=(),
            reason="required outputs missing or empty: " + ", ".join(missing_or_empty),
        )
    refs.extend((artifact_ref(trace), artifact_ref(answer)))
    return AoseOutputCheck(
        status=RunStatus.pass_,
        output_refs=tuple(refs),
        reason="trace.md and answer.txt satisfy the AOSEBench output contract",
    )


__all__ = [
    "AoseBenchConfigurationError",
    "AoseOutputCheck",
    "AoseTask",
    "build_docker_command",
    "check_outputs",
    "load_task",
    "stage_task",
]
