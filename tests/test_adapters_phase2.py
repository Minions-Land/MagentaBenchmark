from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from MagentaBench.adapters.benchmarks.aosebench import (
    AoseBenchConfigurationError,
    build_docker_command,
    check_outputs,
    load_task,
)
from MagentaBench.adapters.subjects.cli_agent import (
    build_cli_command,
    extract_answer,
    run_cli_agent,
    scrubbed_environment,
    write_cli_outputs,
)
from MagentaBench.schemas import RunStatus


AOSE = Path("/mnt/aliyunsb/BioAgent/AOSEBench")


def test_aosebench_task_contract_and_output_check(tmp_path: Path) -> None:
    task = load_task(AOSE, "da-1-3")
    assert task.instruction_path.name == "instruction.md"
    assert task.rubric_path.name == "rubric.txt"
    assert task.agent_timeout_seconds == 3600.0
    assert task.verifier_timeout_seconds == 900.0
    assert check_outputs(tmp_path).status == RunStatus.no_output
    (tmp_path / "trace.md").write_text("analysis", encoding="utf-8")
    (tmp_path / "answer.txt").write_text("answer", encoding="utf-8")
    checked = check_outputs(tmp_path)
    assert checked.status == RunStatus.pass_
    assert len(checked.output_refs) == 2


def test_aosebench_docker_command_uses_readonly_contract(tmp_path: Path, monkeypatch) -> None:
    task_dir = tmp_path / "da-1-3"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("do task", encoding="utf-8")
    (task_dir / "tests" / "rubric.txt").write_text("rubric", encoding="utf-8")
    (task_dir / "task.toml").write_text(
        "[environment]\ncpus=2\nmemory_mb=100\nstorage_mb=200\nallow_internet=false\n",
        encoding="utf-8",
    )
    data = tmp_path / "data" / "da-1-3" / "environment" / "data"
    data.mkdir(parents=True)
    from MagentaBench.adapters.benchmarks.aosebench import AoseTask
    import tomllib

    task = AoseTask(
        task_id="da-1-3",
        task_dir=task_dir,
        instruction_path=task_dir / "instruction.md",
        rubric_path=task_dir / "tests" / "rubric.txt",
        task_config=tomllib.loads((task_dir / "task.toml").read_text()),
        data_path=data,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    command = build_docker_command(
        task,
        image="biomnibench-da-magenta-0.0.22",
        output_root=tmp_path / "out",
        runner_command="/opt/agent/run_task",
        pass_env=("OPENAI_API_KEY",),
    )
    joined = " ".join(command)
    assert "/app/data:ro" in joined
    assert "/app/instruction.md:ro" in joined
    assert "biomnibench-da-magenta-0.0.22" in command
    assert "OPENAI_API_KEY" in command
    assert "secret-value" not in joined


def test_cli_command_variants_and_provider_extraction() -> None:
    assert build_cli_command("claude", prompt="p")[:4] == (
        "claude",
        "-p",
        "--output-format",
        "stream-json",
    )
    assert build_cli_command("codex", prompt="p")[:2] == ("codex", "exec")
    assert "--mode" in build_cli_command("magenta", prompt="p", model="m")
    claude = "\n".join(
        [
            json.dumps({"type": "assistant", "message": {"content": [{"text": "draft"}]}}),
            json.dumps({"type": "result", "result": "final answer"}),
        ]
    )
    assert extract_answer(claude, agent="claude") == "final answer"
    assert extract_answer(json.dumps({"result": "magenta answer"}), agent="magenta") == "magenta answer"
    assert extract_answer("codex answer", agent="codex") == "codex answer"


def test_cli_echo_smoke_allowlists_environment_and_writes_outputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLI_SECRET_TOKEN", "do-not-leak")
    result = run_cli_agent(
        ("/bin/echo", "BMP_OK"),
        cwd=tmp_path,
        timeout_seconds=5,
        env_var_names=("CLI_SECRET_TOKEN",),
    )
    # Names are accepted as configuration, but arbitrary parent variables are
    # not inherited by the child process.
    assert result.status == RunStatus.pass_
    assert result.stdout == "BMP_OK\n"
    trace, answer, status = write_cli_outputs(result, tmp_path, agent="codex")
    assert trace.read_text() == "BMP_OK\n"
    assert answer.read_text() == "BMP_OK\n"
    assert json.loads(status.read_text())["answer_exists"] is True
    assert scrubbed_environment(("CLI_SECRET_TOKEN",)).get("CLI_SECRET_TOKEN") == "do-not-leak"
