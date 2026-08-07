"""Unified non-interactive CLI subject contract for Claude, Codex, and Magenta."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from MagentaBench.schemas import RunStatus


class CliAgentConfigurationError(ValueError):
    """CLI subject configuration is invalid or would expose a secret."""


@dataclass(frozen=True)
class CliInvocationResult:
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def status(self) -> RunStatus:
        if self.timed_out:
            return RunStatus.timeout
        if self.returncode != 0:
            return RunStatus.agent_error
        return RunStatus.pass_ if self.answer_text() else RunStatus.no_output

    def answer_text(self, agent: str = "") -> str:
        return extract_answer(self.stdout, agent=agent)


def build_cli_command(
    agent: str,
    *,
    prompt: str,
    model: str | None = None,
    executable: str | Path | None = None,
) -> tuple[str, ...]:
    """Build provider-native non-interactive flags without invoking a shell."""

    normalized = agent.strip().lower().replace("_", "-")
    default_executables = {
        "claude": "claude",
        "claude-code": "claude",
        "codex": "codex",
        "magenta": "magenta",
        "pi": "magenta",
    }
    command_executable = str(executable or default_executables.get(normalized, ""))
    if not command_executable:
        raise CliAgentConfigurationError(f"unsupported CLI agent: {agent!r}")
    if not prompt.strip():
        raise CliAgentConfigurationError("CLI prompt must not be empty")
    if normalized in {"claude", "claude-code"}:
        return (command_executable, "-p", "--output-format", "stream-json", prompt)
    if normalized == "codex":
        return (command_executable, "exec", prompt)
    if normalized in {"magenta", "pi"}:
        if not model:
            raise CliAgentConfigurationError("Magenta CLI requires a model id")
        return (
            command_executable,
            "--print",
            "--no-session",
            "--mode",
            "json",
            "--model",
            model,
            prompt,
        )
    raise CliAgentConfigurationError(f"unsupported CLI agent: {agent!r}")


def scrubbed_environment(
    env_var_names: Sequence[str] = (), *, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return an allowlisted environment; names are accepted, values never persisted."""

    names = ("PATH", "HOME", "LANG", "LC_ALL", *env_var_names)
    environment: dict[str, str] = {}
    for name in names:
        if not name or "=" in name:
            raise CliAgentConfigurationError("environment accepts variable names only")
        if name in os.environ:
            environment[name] = os.environ[name]
    if extra:
        for name, value in extra.items():
            if "=" in name:
                raise CliAgentConfigurationError("environment accepts variable names only")
            environment[name] = str(value)
    return environment


def run_cli_agent(
    command: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: float,
    env_var_names: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> CliInvocationResult:
    """Execute one CLI attempt with timeout and secret-scrubbed environment."""

    if timeout_seconds <= 0:
        raise CliAgentConfigurationError("CLI timeout must be positive")
    executable = shutil.which(str(command[0])) or str(command[0])
    resolved_command = (executable, *map(str, command[1:]))
    started = time.monotonic()
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=Path(cwd),
            env=scrubbed_environment(env_var_names, extra=extra_env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return CliInvocationResult(
            command=resolved_command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        def as_text(value: str | bytes | None) -> str:
            if value is None:
                return ""
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

        return CliInvocationResult(
            command=resolved_command,
            returncode=None,
            stdout=as_text(exc.stdout),
            stderr=as_text(exc.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as exc:
        return CliInvocationResult(
            command=resolved_command,
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - started,
        )


def _text_from_json(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("result", "output_text", "text", "content", "message"):
            if key in value:
                found = _text_from_json(value[key])
                if found:
                    return found
        if isinstance(value.get("output"), Mapping):
            return _text_from_json(value["output"])
    if isinstance(value, list):
        parts = [_text_from_json(item) for item in value]
        return "\n".join(part for part in parts if part)
    return ""


def extract_answer(raw_stdout: str, *, agent: str = "") -> str:
    """Extract final text while retaining the raw provider stream as trace."""

    normalized = agent.strip().lower().replace("_", "-")
    if normalized in {"claude", "claude-code"}:
        parts: list[str] = []
        for line in raw_stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") in {"result", "final"}:
                text = _text_from_json(event)
                if text:
                    parts.append(text)
            elif event.get("type") == "assistant":
                text = _text_from_json(event.get("message", event))
                if text:
                    parts.append(text)
        return (parts[-1] if parts else "").strip()
    if normalized in {"magenta", "pi"}:
        try:
            return _text_from_json(json.loads(raw_stdout)).strip()
        except json.JSONDecodeError:
            return raw_stdout.strip()
    return raw_stdout.strip()


def write_cli_outputs(
    result: CliInvocationResult,
    output_root: str | Path,
    *,
    agent: str = "",
) -> tuple[Path, Path, Path]:
    """Persist trace, answer, and status files without persisting environment values."""

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    trace = root / "trace.md"
    answer = root / "answer.txt"
    status = root / "agent_runner_status.json"
    trace.write_text(result.stdout, encoding="utf-8")
    answer.write_text(result.answer_text(agent=agent) + "\n", encoding="utf-8")
    status.write_text(
        json.dumps(
            {
                "agent": agent,
                "command": list(result.command),
                "return_code": result.returncode,
                "timed_out": result.timed_out,
                "duration_sec": round(result.duration_seconds, 3),
                "trace_exists": trace.stat().st_size > 0,
                "answer_exists": answer.stat().st_size > 0,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return trace, answer, status


__all__ = [
    "CliAgentConfigurationError",
    "CliInvocationResult",
    "build_cli_command",
    "extract_answer",
    "run_cli_agent",
    "scrubbed_environment",
    "write_cli_outputs",
]
