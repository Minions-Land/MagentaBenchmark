"""Unified non-interactive CLI subject contract for Claude, Codex, and Magenta."""

from __future__ import annotations

import hashlib
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


class MagentaJsonlError(ValueError):
    """Magenta's public headless JSONL stream violates its terminal contract."""


@dataclass(frozen=True)
class MagentaJsonlTrace:
    """Validated public Magenta headless records consumed by the BMP adapter."""

    records: tuple[Mapping[str, object], ...]
    runtime_manifests: tuple[Mapping[str, object], ...]
    assembly_sidecars: tuple[Mapping[str, object], ...]
    final_assistant_message: Mapping[str, object] | None
    run_end: Mapping[str, object]

    @property
    def successful(self) -> bool:
        return self.run_end.get("status") == "success" and self.run_end.get(
            "exitCode"
        ) == 0

    @property
    def answer(self) -> str:
        if not self.successful or self.final_assistant_message is None:
            return ""
        return _text_from_json(
            self.final_assistant_message.get("content", self.final_assistant_message)
        ).strip()


@dataclass(frozen=True)
class CliInvocationResult:
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    agent: str = ""

    @property
    def status(self) -> RunStatus:
        return self.status_for()

    def status_for(self, agent: str | None = None) -> RunStatus:
        """Classify the invocation using the selected provider's wire contract."""

        selected_agent = self.agent if agent is None or not agent.strip() else agent
        if self.timed_out:
            return RunStatus.timeout
        normalized = _normalize_agent(selected_agent)
        if normalized in {"magenta", "pi"}:
            if not self.stdout:
                return (
                    RunStatus.no_output
                    if self.returncode == 0
                    else RunStatus.agent_error
                )
            try:
                trace = parse_magenta_jsonl(self.stdout)
            except MagentaJsonlError:
                return RunStatus.invalid_output
            if self.returncode != trace.run_end["exitCode"]:
                return RunStatus.invalid_output
            if not trace.successful:
                return RunStatus.agent_error
            return RunStatus.pass_ if trace.answer else RunStatus.no_output
        if self.returncode != 0:
            return RunStatus.agent_error
        return RunStatus.pass_ if self.answer_text() else RunStatus.no_output

    def answer_text(self, agent: str | None = None) -> str:
        selected_agent = self.agent if agent is None or not agent.strip() else agent
        return extract_answer(
            self.stdout,
            agent=selected_agent,
        )


def _normalize_agent(agent: str) -> str:
    return agent.strip().lower().replace("_", "-")


def _infer_agent(command: Sequence[str]) -> str:
    """Infer only unambiguous canonical executable names."""

    executable = Path(str(command[0])).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable if executable in {"claude", "codex", "magenta", "pi"} else ""


def build_cli_command(
    agent: str,
    *,
    prompt: str,
    model: str | None = None,
    executable: str | Path | None = None,
    anthropic_cache_affinity: str | None = "auto",
    harness_workflows: bool | None = False,
    harness_teammates: bool | None = False,
    lock_tools: bool | None = True,
) -> tuple[str, ...]:
    """Build provider-native non-interactive flags without invoking a shell."""

    normalized = _normalize_agent(agent)
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
        if anthropic_cache_affinity not in {None, "auto", "on", "off"}:
            raise CliAgentConfigurationError(
                "Magenta cache affinity must be auto, on, off, or None"
            )
        command = [
            command_executable,
            "--print",
            "--no-session",
            "--mode",
            "json",
            "--model",
            model,
        ]
        if anthropic_cache_affinity is not None:
            command.extend(("--anthropic-cache-affinity", anthropic_cache_affinity))
        if harness_workflows is not None:
            command.append(
                "--harness-workflows" if harness_workflows else "--no-harness-workflows"
            )
        if harness_teammates is not None:
            command.append(
                "--harness-teammates" if harness_teammates else "--no-harness-teammates"
            )
        if lock_tools is not None:
            command.append("--lock-tools" if lock_tools else "--no-lock-tools")
        command.append(prompt)
        return tuple(command)
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
    agent: str = "",
    env_var_names: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> CliInvocationResult:
    """Execute one CLI attempt with timeout and secret-scrubbed environment."""

    if timeout_seconds <= 0:
        raise CliAgentConfigurationError("CLI timeout must be positive")
    if not command:
        raise CliAgentConfigurationError("CLI command must not be empty")
    effective_agent = agent or _infer_agent(command)
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
            agent=effective_agent,
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
            agent=effective_agent,
        )
    except OSError as exc:
        return CliInvocationResult(
            command=resolved_command,
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - started,
            agent=effective_agent,
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


def _require_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MagentaJsonlError(f"{label} must be an object")
    return value


def _validate_runtime_manifest(
    manifest: Mapping[str, object], *, position: int
) -> None:
    label = f"runtime_manifest[{position}]"
    if manifest.get("protocolVersion") != 1:
        raise MagentaJsonlError(f"{label} requires protocolVersion=1")
    if manifest.get("mode") != "json":
        raise MagentaJsonlError(f"{label} requires mode='json'")
    product = _require_mapping(manifest.get("product"), label=f"{label}.product")
    for field in ("name", "version", "infrastructureVersion"):
        if not isinstance(product.get(field), str) or not product[field]:
            raise MagentaJsonlError(f"{label}.product.{field} is required")
    execution = _require_mapping(
        manifest.get("execution"), label=f"{label}.execution"
    )
    if execution.get("anthropicCacheAffinity") not in {"auto", "on", "off"}:
        raise MagentaJsonlError(
            f"{label}.execution.anthropicCacheAffinity is required"
        )
    capability_policy = _require_mapping(
        execution.get("capabilityPolicy"),
        label=f"{label}.execution.capabilityPolicy",
    )
    _require_mapping(
        capability_policy.get("capabilities"),
        label=f"{label}.execution.capabilityPolicy.capabilities",
    )
    decisions = _require_mapping(
        capability_policy.get("capabilityDecisions"),
        label=f"{label}.execution.capabilityPolicy.capabilityDecisions",
    )
    for name, raw_decision in decisions.items():
        decision = _require_mapping(
            raw_decision,
            label=(
                f"{label}.execution.capabilityPolicy.capabilityDecisions.{name}"
            ),
        )
        if not isinstance(decision.get("enabled"), bool):
            raise MagentaJsonlError(f"{label} capability decision lacks enabled")
        if not isinstance(decision.get("source"), str) or not decision["source"]:
            raise MagentaJsonlError(f"{label} capability decision lacks source")
        if not isinstance(decision.get("locked"), bool):
            raise MagentaJsonlError(f"{label} capability decision lacks locked")
    tool_policy = _require_mapping(
        capability_policy.get("tools"),
        label=f"{label}.execution.capabilityPolicy.tools",
    )
    if not isinstance(tool_policy.get("deny"), list):
        raise MagentaJsonlError(f"{label} tool policy requires deny list")
    if "allow" in tool_policy and not isinstance(tool_policy["allow"], list):
        raise MagentaJsonlError(f"{label} tool policy allow must be a list")
    for field in ("builtinTools", "runtimeMutable"):
        if not isinstance(tool_policy.get(field), bool):
            raise MagentaJsonlError(f"{label} tool policy requires {field}")
    tools = _require_mapping(manifest.get("tools"), label=f"{label}.tools")
    if not isinstance(tools.get("active"), list) or not isinstance(
        tools.get("available"), list
    ):
        raise MagentaJsonlError(f"{label}.tools requires active and available lists")
    _require_mapping(manifest.get("resources"), label=f"{label}.resources")

    # HCP assembly is intentionally opaque to BMP.  Validate only the public
    # sidecar envelope so malformed evidence cannot masquerade as a valid
    # Magenta runtime manifest; all component fields remain uninterpreted.
    assembly = manifest.get("assembly")
    if assembly is not None:
        assembly_obj = _require_mapping(assembly, label=f"{label}.assembly")
        if assembly_obj.get("type") != "hcp_assembly":
            raise MagentaJsonlError(
                f"{label}.assembly.type must be 'hcp_assembly'"
            )
        if assembly_obj.get("schemaVersion") != 1:
            raise MagentaJsonlError(
                f"{label}.assembly.schemaVersion must be 1"
            )
        for field in ("components", "resolvedAddresses", "activeTools", "activeCapabilities", "diagnostics"):
            if not isinstance(assembly_obj.get(field), list):
                raise MagentaJsonlError(
                    f"{label}.assembly.{field} must be a list"
                )


def _validate_run_end(run_end: Mapping[str, object]) -> None:
    """Validate fields Magenta declares mandatory on its protocol-v1 terminator."""

    for field in ("startedAt", "endedAt"):
        if not isinstance(run_end.get(field), str) or not run_end[field]:
            raise MagentaJsonlError(f"Magenta run_end requires {field}")
    duration = run_end.get("durationMs")
    if (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise MagentaJsonlError("Magenta run_end requires non-negative durationMs")
    _require_mapping(run_end.get("stats"), label="Magenta run_end.stats")

    background = _require_mapping(
        run_end.get("background"), label="Magenta run_end.background"
    )
    if background.get("policy") not in {"cancel", "wait", "error"}:
        raise MagentaJsonlError("Magenta run_end background policy is invalid")
    if not isinstance(background.get("settled"), bool):
        raise MagentaJsonlError("Magenta run_end background.settled is required")
    events = background.get("events")
    if not isinstance(events, list):
        raise MagentaJsonlError("Magenta run_end background.events must be a list")
    for position, event in enumerate(events):
        _require_mapping(event, label=f"Magenta run_end.background.events[{position}]")
    if run_end.get("status") == "success" and not background["settled"]:
        raise MagentaJsonlError(
            "successful Magenta run_end requires settled background work"
        )

    non_interactive_ui = _require_mapping(
        run_end.get("nonInteractiveUi"),
        label="Magenta run_end.nonInteractiveUi",
    )
    if non_interactive_ui.get("policy") not in {"deny", "error"}:
        raise MagentaJsonlError("Magenta run_end nonInteractiveUi policy is invalid")
    request_count = non_interactive_ui.get("requestCount")
    if (
        not isinstance(request_count, int)
        or isinstance(request_count, bool)
        or request_count < 0
    ):
        raise MagentaJsonlError(
            "Magenta run_end nonInteractiveUi.requestCount must be non-negative"
        )


def parse_magenta_jsonl(raw_stdout: str) -> MagentaJsonlTrace:
    """Parse Magenta's public protocol-v1 JSONL without reading HCP internals."""

    if not raw_stdout:
        raise MagentaJsonlError("Magenta JSONL stream is empty")
    records: list[Mapping[str, object]] = []
    for line_number, line in enumerate(raw_stdout.splitlines(), start=1):
        if not line.strip():
            raise MagentaJsonlError(
                f"Magenta JSONL line {line_number} is unexpectedly blank"
            )
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MagentaJsonlError(
                f"Magenta JSONL line {line_number} is not valid JSON"
            ) from exc
        records.append(
            _require_mapping(parsed, label=f"Magenta JSONL line {line_number}")
        )
    if not records:
        raise MagentaJsonlError("Magenta JSONL stream has no records")

    runtime_manifests = tuple(
        record for record in records if record.get("type") == "runtime_manifest"
    )
    assembly_sidecars = tuple(
        _require_mapping(record["assembly"], label="Magenta runtime_manifest.assembly")
        for record in runtime_manifests
        if record.get("assembly") is not None
    )
    if not runtime_manifests:
        raise MagentaJsonlError("Magenta JSONL stream lacks runtime_manifest")
    run_ends = tuple(record for record in records if record.get("type") == "run_end")
    if len(run_ends) != 1:
        raise MagentaJsonlError("Magenta JSONL stream requires exactly one run_end")
    run_end = run_ends[0]
    if records[-1] is not run_end:
        raise MagentaJsonlError("Magenta run_end must be the terminal record")
    if run_end.get("protocolVersion") != 1:
        raise MagentaJsonlError("Magenta run_end requires protocolVersion=1")
    run_id = run_end.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise MagentaJsonlError("Magenta run_end requires runId")
    status = run_end.get("status")
    exit_code = run_end.get("exitCode")
    if status not in {"success", "error", "aborted"}:
        raise MagentaJsonlError("Magenta run_end has unknown status")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise MagentaJsonlError("Magenta run_end requires integer exitCode")
    if (status == "success") != (exit_code == 0):
        raise MagentaJsonlError("Magenta run_end status and exitCode disagree")
    _validate_run_end(run_end)

    for position, manifest in enumerate(runtime_manifests, start=1):
        _validate_runtime_manifest(manifest, position=position)
        if manifest.get("runId") != run_id:
            raise MagentaJsonlError("Magenta runtime_manifest runId drift")
        sequence = manifest.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence != position
        ):
            raise MagentaJsonlError(
                "Magenta runtime_manifest sequence must be contiguous from one"
            )

    assistant_messages = tuple(
        _require_mapping(record.get("message"), label="assistant message_end.message")
        for record in records
        if record.get("type") == "message_end"
        and isinstance(record.get("message"), Mapping)
        and record["message"].get("role") == "assistant"  # type: ignore[union-attr]
    )
    final_message = assistant_messages[-1] if assistant_messages else None
    if status == "success" and final_message is None:
        raise MagentaJsonlError(
            "successful Magenta JSONL stream lacks assistant message_end"
        )
    return MagentaJsonlTrace(
        records=tuple(records),
        runtime_manifests=runtime_manifests,
        assembly_sidecars=assembly_sidecars,
        final_assistant_message=final_message,
        run_end=run_end,
    )


def extract_answer(raw_stdout: str, *, agent: str = "") -> str:
    """Extract final text while retaining the raw provider stream as trace."""

    normalized = _normalize_agent(agent)
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
            return parse_magenta_jsonl(raw_stdout).answer
        except MagentaJsonlError:
            return ""
    return raw_stdout.strip()


def write_cli_outputs(
    result: CliInvocationResult,
    output_root: str | Path,
    *,
    agent: str | None = None,
) -> tuple[Path, Path, Path]:
    """Persist trace, answer, and status files without persisting environment values."""

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    trace = root / "trace.md"
    answer = root / "answer.txt"
    status = root / "agent_runner_status.json"
    effective_agent = (
        result.agent if agent is None or not agent.strip() else agent
    )
    trace.write_text(result.stdout, encoding="utf-8")
    answer_text = result.answer_text(agent=effective_agent)
    answer.write_text(answer_text + ("\n" if answer_text else ""), encoding="utf-8")
    status_payload: dict[str, object] = {
        "agent": effective_agent,
        "command": list(result.command),
        "return_code": result.returncode,
        "status": result.status_for(effective_agent).value,
        "timed_out": result.timed_out,
        "duration_sec": round(result.duration_seconds, 3),
        "trace_exists": bool(result.stdout),
        "answer_exists": bool(answer_text),
    }
    if _normalize_agent(effective_agent) in {"magenta", "pi"}:
        try:
            parsed_trace = parse_magenta_jsonl(result.stdout)
        except MagentaJsonlError as exc:
            status_payload["headless_protocol_valid"] = False
            status_payload["headless_protocol_error"] = str(exc)
        else:
            manifests = list(parsed_trace.runtime_manifests)
            status_payload.update(
                {
                    "headless_protocol_valid": True,
                    "headless_run_end": dict(parsed_trace.run_end),
                    "runtime_manifests": manifests,
                    "runtime_manifest_sha256": [
                        hashlib.sha256(
                            json.dumps(
                                manifest,
                                ensure_ascii=False,
                                allow_nan=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        for manifest in manifests
                    ],
                    "assembly_sidecar_count": len(parsed_trace.assembly_sidecars),
                    "assembly_sidecar_sha256": [
                        hashlib.sha256(
                            json.dumps(
                                sidecar,
                                ensure_ascii=False,
                                allow_nan=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        for sidecar in parsed_trace.assembly_sidecars
                    ],
                }
            )
    status.write_text(
        json.dumps(status_payload, indent=2),
        encoding="utf-8",
    )
    return trace, answer, status


__all__ = [
    "CliAgentConfigurationError",
    "CliInvocationResult",
    "MagentaJsonlError",
    "MagentaJsonlTrace",
    "build_cli_command",
    "extract_answer",
    "parse_magenta_jsonl",
    "run_cli_agent",
    "scrubbed_environment",
    "write_cli_outputs",
]
