from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("harbor", reason="Magenta Agent tests require Harbor 0.20.0")

from plugins.terminal_bench.magenta_agent import (
    MAGENTA_BINARY_SHA256,
    MAGENTA_CHECKSUMS_SHA256,
    MAGENTA_CONTAINER_TRACE_PATH,
    MAGENTA_RESOURCES_SHA256,
    MAGENTA_VERSION,
    MagentaAgent,
    MagentaAgentConfigurationError,
    _mirrored_url,
    _usage_from_trace,
    build_magenta_command,
)


def test_magenta_command_is_non_interactive_and_shell_safe() -> None:
    command = build_magenta_command(
        "openai/gpt-5.6",
        "inspect this file; then print $HOME and 'done'",
    )
    assert command.startswith(
        "magenta --print --no-session --mode json --provider openai --model gpt-5.6 "
    )
    assert "inspect this file; then print $HOME and 'done'" not in command
    assert " --session " not in command


@pytest.mark.parametrize("model_name", ["", "gpt-5.6", "/gpt-5.6", "openai/"])
def test_magenta_command_requires_provider_and_model(model_name: str) -> None:
    with pytest.raises(MagentaAgentConfigurationError, match="provider/model"):
        build_magenta_command(model_name, "task")


def test_magenta_release_pins_are_explicit() -> None:
    assert MAGENTA_VERSION == "0.1.23"
    for digest in (MAGENTA_BINARY_SHA256, MAGENTA_RESOURCES_SHA256, MAGENTA_CHECKSUMS_SHA256):
        assert len(digest) == 64
        assert digest == digest.lower()


def test_magenta_agent_rejects_release_drift(tmp_path: Path) -> None:
    with pytest.raises(MagentaAgentConfigurationError, match="only Magenta"):
        MagentaAgent(
            logs_dir=tmp_path,
            model_name="openai/gpt-5.6",
            release_version="0.1.24",
        )
    with pytest.raises(MagentaAgentConfigurationError, match=r"HTTP\(S\)"):
        MagentaAgent(
            logs_dir=tmp_path,
            model_name="openai/gpt-5.6",
            github_mirror="mirror.invalid",
        )


def test_magenta_agent_writes_trace_to_harbor_mount(tmp_path: Path) -> None:
    agent = MagentaAgent(logs_dir=tmp_path, model_name="openai/gpt-5.6")
    commands: list[str] = []

    async def capture(_environment: object, *, command: str, **_kwargs: object) -> None:
        commands.append(command)

    agent.exec_as_agent = capture  # type: ignore[method-assign]
    asyncio.run(agent.run("do the task", object(), object()))

    assert commands
    assert MAGENTA_CONTAINER_TRACE_PATH in commands[0]
    assert str(tmp_path) not in commands[0]


def test_release_downloads_use_configured_github_mirror(tmp_path: Path) -> None:
    agent = MagentaAgent(logs_dir=tmp_path, model_name="openai/gpt-5.6")
    commands: list[str] = []

    async def capture(_environment: object, *, command: str, **_kwargs: object) -> None:
        commands.append(command)

    agent.exec_as_root = capture  # type: ignore[method-assign]
    asyncio.run(agent._download_release_assets(object()))

    assert commands
    command = commands[0]
    mirrored_prefix = "https://ghfast.top/https://github.com/Minions-Land/Magenta-CLI/releases/download"
    assert command.count(mirrored_prefix) == 3
    assert _mirrored_url("SHA256SUMS", "https://ghfast.top") in command


def test_usage_projection_sums_magenta_message_end_events(tmp_path: Path) -> None:
    trace = tmp_path / "magenta.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "message_end", "message": {"role": "user", "usage": {"input": 1}}},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 10, "output": 4, "cacheRead": 2, "cost": {"total": 0.3}},
                    },
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 5, "output": 6, "cacheRead": 1, "cost": {"total": 0.2}},
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert _usage_from_trace(trace) == (18, 10, 3, 0.5)
