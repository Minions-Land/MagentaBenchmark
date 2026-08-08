"""Unified non-interactive CLI subject contract for Claude, Codex, and Magenta."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from MagentaBench.schemas import (
    RunStatus,
    RuntimeAssemblySidecarRef,
    RuntimeManifestReceipt,
)
from MagentaBench.runner.evidence import artifact_ref, atomic_write_bytes


class CliAgentConfigurationError(ValueError):
    """CLI subject configuration is invalid or would expose a secret."""


class MagentaJsonlError(ValueError):
    """Magenta's public headless JSONL stream violates its terminal contract."""


@dataclass(frozen=True)
class MagentaLaunchConfiguration:
    """BMP-owned projection of Magenta's public startup/settings contract.

    The configuration registry is intentionally generic, so this adapter accepts
    both Magenta's camelCase names and the snake_case names commonly used by BMP
    TOML authors.  Only the fields represented here are translated; unrelated
    configuration namespaces remain opaque to the subject adapter.

    ``None`` means that the caller did not request an override.  This is
    materially different from Magenta's effective provider defaults and keeps
    requested-vs-observed evidence honest.
    """

    provider: str | None = None
    model: str | None = None
    transport: str | None = None
    cache_retention: str | None = None
    openai_prompt_cache_mode: str | None = None
    cache_telemetry: bool | None = None
    cache_diagnostics: bool | None = None
    anthropic_cache_affinity: str | None = None
    harness_workflows: bool | None = None
    harness_teammates: bool | None = None
    tool_search: bool | None = None
    lock_tools: bool | None = None
    retry_enabled: bool | None = None
    retry_max_retries: int | None = None
    retry_base_delay_ms: int | None = None
    provider_timeout_ms: int | None = None
    provider_max_retries: int | None = None
    provider_max_retry_delay_ms: int | None = None
    http_idle_timeout_ms: int | None = None
    websocket_connect_timeout_ms: int | None = None

    @property
    def provider_timeout_seconds(self) -> float | None:
        """Return the provider timeout in subprocess units when configured."""

        return (
            None
            if self.provider_timeout_ms is None
            else self.provider_timeout_ms / 1000.0
        )

    def requested_projection(self) -> dict[str, object]:
        """Return a stable, secret-free Magenta naming projection for evidence."""

        projection: dict[str, object] = {}
        scalar_fields = (
            ("provider", self.provider),
            ("model", self.model),
            ("transport", self.transport),
            ("cacheRetention", self.cache_retention),
            ("openaiPromptCacheMode", self.openai_prompt_cache_mode),
            ("cacheTelemetry", self.cache_telemetry),
            ("cacheDiagnostics", self.cache_diagnostics),
            ("anthropicCacheAffinity", self.anthropic_cache_affinity),
            ("toolSearch", self.tool_search),
            ("harnessWorkflows", self.harness_workflows),
            ("harnessTeammates", self.harness_teammates),
            ("lockTools", self.lock_tools),
            ("httpIdleTimeoutMs", self.http_idle_timeout_ms),
            ("websocketConnectTimeoutMs", self.websocket_connect_timeout_ms),
        )
        for name, value in scalar_fields:
            if value is not None:
                projection[name] = value
        retry: dict[str, object] = {}
        if self.retry_enabled is not None:
            retry["enabled"] = self.retry_enabled
        if self.retry_max_retries is not None:
            retry["maxRetries"] = self.retry_max_retries
        if self.retry_base_delay_ms is not None:
            retry["baseDelayMs"] = self.retry_base_delay_ms
        provider: dict[str, object] = {}
        if self.provider_timeout_ms is not None:
            provider["timeoutMs"] = self.provider_timeout_ms
        if self.provider_max_retries is not None:
            provider["maxRetries"] = self.provider_max_retries
        if self.provider_max_retry_delay_ms is not None:
            provider["maxRetryDelayMs"] = self.provider_max_retry_delay_ms
        if provider:
            retry["provider"] = provider
        if retry:
            projection["retry"] = retry
        return projection

    def settings_document(self) -> dict[str, object]:
        """Return Magenta ``settings.json`` values for non-CLI controls.

        Magenta v0.1.22 exposes retry/provider controls through settings rather
        than startup flags.  The document is also useful for older/newer hosts
        that choose to inject a temporary ``MAGENTA_CODING_AGENT_DIR``.
        """

        projection = self.requested_projection()
        document: dict[str, object] = {
            key: projection[key]
            for key in (
                "transport",
                "cacheRetention",
                "openaiPromptCacheMode",
                "cacheTelemetry",
                "cacheDiagnostics",
                "httpIdleTimeoutMs",
                "websocketConnectTimeoutMs",
                "retry",
            )
            if key in projection
        }
        harness: dict[str, object] = {}
        for source_name, output_name in (
            ("harnessWorkflows", "workflows"),
            ("harnessTeammates", "teammates"),
            ("toolSearch", "toolSearch"),
        ):
            if source_name in projection:
                harness[output_name] = projection[source_name]
        if harness:
            document["harness"] = harness
        # Startup flags and settings use the same public names.  Keep the
        # projection detached so callers cannot mutate the receipt accidentally.
        return json.loads(json.dumps(document, ensure_ascii=False, sort_keys=True))

    def command_overrides(self) -> dict[str, object]:
        """Return only values that have a Magenta-native startup switch."""

        return {
            key: value
            for key, value in self.requested_projection().items()
            if key
            in {
                "provider",
                "model",
                "transport",
                "cacheRetention",
                "openaiPromptCacheMode",
                "cacheTelemetry",
                "cacheDiagnostics",
                "anthropicCacheAffinity",
                "toolSearch",
                "harnessWorkflows",
                "harnessTeammates",
                "lockTools",
            }
        }


@dataclass(frozen=True)
class MagentaConfigurationReceipt:
    """Requested/effective Magenta settings bound to one headless attempt."""

    requested: Mapping[str, object]
    effective: Mapping[str, object]
    requested_sha256: str
    effective_sha256: str
    activation_receipt: Mapping[str, object] | None
    effective_sequence: int
    status: str
    path: Path | None = None

    def model_dump(self) -> dict[str, object]:
        """JSON-compatible representation used by status/evidence writers."""

        return {
            "requested": dict(self.requested),
            "effective": dict(self.effective),
            "requested_sha256": self.requested_sha256,
            "effective_sha256": self.effective_sha256,
            "activation_receipt": (
                None
                if self.activation_receipt is None
                else dict(self.activation_receipt)
            ),
            "effective_sequence": self.effective_sequence,
            "status": self.status,
            **({"path": str(self.path)} if self.path is not None else {}),
        }


@dataclass(frozen=True)
class MagentaJsonlTrace:
    """Validated public Magenta headless records consumed by the BMP adapter."""

    records: tuple[Mapping[str, object], ...]
    runtime_manifests: tuple[Mapping[str, object], ...]
    assembly_sidecars: tuple[Mapping[str, object], ...]
    final_assistant_message: Mapping[str, object] | None
    run_end: Mapping[str, object]

    @property
    def effective_runtime_manifest(self) -> Mapping[str, object]:
        """Return the final manifest after any extension-driven replacement."""

        return self.runtime_manifests[-1]

    @property
    def effective_assembly_sidecar(self) -> Mapping[str, object] | None:
        """Return the sidecar for the runtime that actually completed the run."""

        assembly = self.effective_runtime_manifest.get("assembly")
        return assembly if isinstance(assembly, Mapping) else None

    @property
    def effective_configuration(self) -> Mapping[str, object]:
        """Return the public v0.1.22 settings projection from the final manifest."""

        return MappingProxyType(
            _project_effective_magenta_configuration(self.effective_runtime_manifest)
        )

    @property
    def effective_settings(self) -> Mapping[str, object]:
        """Compatibility alias for hosts that call the projection "settings"."""

        return self.effective_configuration

    @property
    def activation_receipt(self) -> Mapping[str, object] | None:
        """Return the effective HCP activation receipt without interpreting HCP rows."""

        sidecar = self.effective_assembly_sidecar
        receipt = None if sidecar is None else sidecar.get("activationReceipt")
        return receipt if isinstance(receipt, Mapping) else None

    @property
    def hcp_activation_receipt(self) -> Mapping[str, object] | None:
        """Compatibility alias for the effective HCP assembly receipt."""

        return self.activation_receipt

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


@dataclass(frozen=True)
class CliOutputArtifacts:
    """Persisted public outputs plus optional Magenta runtime lineage."""

    trace_path: Path
    answer_path: Path
    status_path: Path
    runtime_manifest_receipt: RuntimeManifestReceipt | None = None
    configuration_receipt: MagentaConfigurationReceipt | None = None

    def __iter__(self):
        # Preserve historical three-path unpacking for non-BMP callers.
        yield self.trace_path
        yield self.answer_path
        yield self.status_path

    def bind_provenance(self, provenance):
        """Attach observed runtime lineage to a production provenance record."""

        if self.runtime_manifest_receipt is None:
            raise MagentaJsonlError(
                "Magenta CLI outputs lack a valid runtime manifest receipt"
            )
        return provenance.model_copy(
            update={"runtime_manifest_receipt": self.runtime_manifest_receipt}
        )

    @property
    def runtime_evidence(self) -> Mapping[str, object]:
        """Return adapter evidence that is not part of generic BMP provenance."""

        return MappingProxyType(
            {
                "runtime_manifest_receipt": (
                    None
                    if self.runtime_manifest_receipt is None
                    else self.runtime_manifest_receipt.model_dump(mode="json")
                ),
                "configuration_receipt": (
                    None
                    if self.configuration_receipt is None
                    else self.configuration_receipt.model_dump()
                ),
            }
        )


def _normalize_agent(agent: str) -> str:
    return agent.strip().lower().replace("_", "-")


def _infer_agent(command: Sequence[str]) -> str:
    """Infer only unambiguous canonical executable names."""

    executable = Path(str(command[0])).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable if executable in {"claude", "codex", "magenta", "pi"} else ""


_MAGENTA_ALLOWED_ROOTS: frozenset[tuple[str, ...]] = frozenset(
    {
        (),
        ("agent",),
        ("magenta",),
        ("magenta_cli",),
        ("cli_agent",),
        ("execution",),
        ("settings",),
    }
)
_MAGENTA_ALLOWED_NESTED_SEGMENTS: frozenset[str] = frozenset(
    {"agent", "magenta", "magenta_cli", "cli_agent", "execution", "settings", "harness", "capabilities", "harness_capabilities", "retry", "provider", "model"}
)


def _configuration_prefix_allowed(prefix: tuple[str, ...]) -> bool:
    # ``retry.provider`` is a settings table, not the model provider selector.
    # Keep one-segment aliases from interpreting that table as ``provider``.
    return (
        bool(prefix)
        and prefix[-1] != "retry"
        and all(segment in _MAGENTA_ALLOWED_NESTED_SEGMENTS for segment in prefix)
    )


def _normalize_configuration_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CliAgentConfigurationError(
            "Magenta configuration keys must be non-empty strings"
        )
    normalized = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    return normalized.replace("-", "_").lower()


def _assert_safe_configuration(value: object, *, path: str = "configuration") -> None:
    """Reject obvious credential-bearing keys before any adapter projection."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if re.search(r"(?:api[_-]?key|access[_-]?token|secret|password|credential)", key_text, re.I):
                raise CliAgentConfigurationError(
                    f"{path} must not contain secret-like key {key_text!r}"
                )
            _assert_safe_configuration(child, path=f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_configuration(child, path=f"{path}[{index}]")


def _flatten_configuration(
    value: Mapping[str, object],
    *,
    prefix: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, ...], object], ...]:
    flattened: list[tuple[tuple[str, ...], object]] = []
    for key, child in value.items():
        normalized = (*prefix, _normalize_configuration_key(key))
        flattened.append((normalized, child))
        if isinstance(child, Mapping):
            flattened.extend(_flatten_configuration(child, prefix=normalized))
    return tuple(flattened)


def _configuration_candidates(
    flattened: Sequence[tuple[tuple[str, ...], object]],
    aliases: Sequence[tuple[str, ...]],
) -> tuple[tuple[tuple[str, ...], object], ...]:
    found: list[tuple[tuple[str, ...], object]] = []
    for path, value in flattened:
        for alias in aliases:
            if path == alias:
                found.append((path, value))
                break
            if len(path) > len(alias) and path[-len(alias) :] == alias:
                prefix = path[: -len(alias)]
                if prefix in _MAGENTA_ALLOWED_ROOTS or _configuration_prefix_allowed(prefix):
                    found.append((path, value))
                    break
    return tuple(found)


def _canonical_equal(left: object, right: object) -> bool:
    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ) == json.dumps(
            right,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return left == right


def _read_configuration_value(
    flattened: Sequence[tuple[tuple[str, ...], object]],
    aliases: Sequence[tuple[str, ...]],
    *,
    label: str,
) -> object | None:
    candidates = _configuration_candidates(flattened, aliases)
    if not candidates:
        return None
    first_path, first_value = candidates[0]
    for path, value in candidates[1:]:
        if not _canonical_equal(first_value, value):
            raise CliAgentConfigurationError(
                f"conflicting Magenta configuration values for {label}: "
                f"{'.'.join(first_path)!r} and {'.'.join(path)!r}"
            )
    return first_value


def _strict_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise CliAgentConfigurationError(f"Magenta {label} must be a boolean")
    return value


def _strict_nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CliAgentConfigurationError(f"Magenta {label} must be a non-empty string")
    return value


def _strict_nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CliAgentConfigurationError(
            f"Magenta {label} must be a non-negative integer"
        )
    return value


def _strict_choice(value: object, choices: frozenset[str], *, label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        choices_text = ", ".join(sorted(choices))
        raise CliAgentConfigurationError(
            f"Magenta {label} must be one of: {choices_text}"
        )
    return value


def _mapping_for_configuration(
    configuration: Mapping[str, object] | MagentaLaunchConfiguration,
) -> Mapping[str, object]:
    if isinstance(configuration, MagentaLaunchConfiguration):
        return MappingProxyType(configuration.requested_projection())
    if hasattr(configuration, "model_dump"):
        dumped = configuration.model_dump(mode="json")  # type: ignore[union-attr]
        if not isinstance(dumped, Mapping):
            raise CliAgentConfigurationError("Magenta configuration model must dump an object")
        configuration = dumped
    if not isinstance(configuration, Mapping):
        raise CliAgentConfigurationError("Magenta configuration must be an object")
    _assert_safe_configuration(configuration)
    # Accept a ConfigurationArtifact/model envelope without making the adapter
    # depend on BMP's compiler internals.
    if set(configuration) == {"configuration"} and isinstance(
        configuration.get("configuration"), Mapping
    ):
        configuration = configuration["configuration"]  # type: ignore[assignment,index]
    if "values" in configuration and isinstance(configuration.get("values"), Mapping):
        configuration = configuration["values"]  # type: ignore[assignment,index]
    return configuration


def resolve_magenta_configuration(
    configuration: Mapping[str, object] | MagentaLaunchConfiguration | None,
) -> MagentaLaunchConfiguration:
    """Normalize a generic BMP configuration into Magenta v0.1.22 controls.

    Aliases are intentionally finite and conflict-checked.  This prevents TOML
    key spelling or insertion order from changing the provider command.
    """

    if configuration is None:
        return MagentaLaunchConfiguration()
    if isinstance(configuration, MagentaLaunchConfiguration):
        return configuration
    mapping = _mapping_for_configuration(configuration)
    flattened = _flatten_configuration(mapping)

    def read(name: str, *aliases: tuple[str, ...]) -> object | None:
        return _read_configuration_value(flattened, aliases, label=name)

    transport = read("transport", ("transport",))
    provider = read(
        "provider",
        ("provider",),
        ("model", "provider"),
    )
    model = read(
        "model",
        ("model",),
        ("model_id",),
        ("model", "id"),
    )
    cache_retention = read("cacheRetention", ("cache_retention",))
    openai_cache_mode = read(
        "openaiPromptCacheMode", ("openai_prompt_cache_mode",)
    )
    cache_telemetry = read("cacheTelemetry", ("cache_telemetry",))
    cache_diagnostics = read("cacheDiagnostics", ("cache_diagnostics",))
    affinity = read(
        "anthropicCacheAffinity", ("anthropic_cache_affinity",)
    )
    workflows = read("harnessWorkflows", ("harness_workflows",))
    teammates = read("harnessTeammates", ("harness_teammates",))
    tool_search = read(
        "toolSearch",
        ("tool_search",),
        ("harness_tool_search",),
        ("harness", "tool_search"),
        ("harness_capabilities", "tool_search"),
        ("harness", "capabilities", "tool_search"),
    )
    lock_tools = read("lockTools", ("lock_tools",))
    retry_enabled = read("retry.enabled", ("retry", "enabled"))
    retry_max = read("retry.maxRetries", ("retry", "max_retries"))
    retry_base = read("retry.baseDelayMs", ("retry", "base_delay_ms"))
    provider_timeout = read(
        "retry.provider.timeoutMs",
        ("retry", "provider", "timeout_ms"),
        ("provider_timeout_ms",),
    )
    provider_max = read(
        "retry.provider.maxRetries",
        ("retry", "provider", "max_retries"),
        ("provider_max_retries",),
    )
    provider_max_delay = read(
        "retry.provider.maxRetryDelayMs",
        ("retry", "provider", "max_retry_delay_ms"),
        ("provider_max_retry_delay_ms",),
    )
    http_idle = read("httpIdleTimeoutMs", ("http_idle_timeout_ms",))
    websocket_timeout = read(
        "websocketConnectTimeoutMs", ("websocket_connect_timeout_ms",)
    )

    return MagentaLaunchConfiguration(
        provider=(
            None
            if provider is None
            else _strict_nonempty_string(provider, label="provider")
        ),
        model=(
            None
            if model is None
            else _strict_nonempty_string(model, label="model")
        ),
        transport=(
            None
            if transport is None
            else _strict_choice(
                transport,
                frozenset({"auto", "sse", "websocket", "websocket-cached"}),
                label="transport",
            )
        ),
        cache_retention=(
            None
            if cache_retention is None
            else _strict_choice(
                cache_retention,
                frozenset({"none", "short", "long", "provider-default"}),
                label="cacheRetention",
            )
        ),
        openai_prompt_cache_mode=(
            None
            if openai_cache_mode is None
            else _strict_choice(
                openai_cache_mode,
                frozenset({"off", "implicit", "explicit", "provider-default"}),
                label="openaiPromptCacheMode",
            )
        ),
        cache_telemetry=(
            None
            if cache_telemetry is None
            else _strict_bool(cache_telemetry, label="cacheTelemetry")
        ),
        cache_diagnostics=(
            None
            if cache_diagnostics is None
            else _strict_bool(cache_diagnostics, label="cacheDiagnostics")
        ),
        anthropic_cache_affinity=(
            None
            if affinity is None
            else _strict_choice(
                affinity,
                frozenset({"auto", "on", "off"}),
                label="anthropicCacheAffinity",
            )
        ),
        harness_workflows=(
            None if workflows is None else _strict_bool(workflows, label="harnessWorkflows")
        ),
        harness_teammates=(
            None if teammates is None else _strict_bool(teammates, label="harnessTeammates")
        ),
        tool_search=(
            None if tool_search is None else _strict_bool(tool_search, label="toolSearch")
        ),
        lock_tools=(
            None if lock_tools is None else _strict_bool(lock_tools, label="lockTools")
        ),
        retry_enabled=(
            None if retry_enabled is None else _strict_bool(retry_enabled, label="retry.enabled")
        ),
        retry_max_retries=(
            None
            if retry_max is None
            else _strict_nonnegative_int(retry_max, label="retry.maxRetries")
        ),
        retry_base_delay_ms=(
            None
            if retry_base is None
            else _strict_nonnegative_int(retry_base, label="retry.baseDelayMs")
        ),
        provider_timeout_ms=(
            None
            if provider_timeout is None
            else _strict_nonnegative_int(provider_timeout, label="retry.provider.timeoutMs")
        ),
        provider_max_retries=(
            None
            if provider_max is None
            else _strict_nonnegative_int(provider_max, label="retry.provider.maxRetries")
        ),
        provider_max_retry_delay_ms=(
            None
            if provider_max_delay is None
            else _strict_nonnegative_int(
                provider_max_delay, label="retry.provider.maxRetryDelayMs"
            )
        ),
        http_idle_timeout_ms=(
            None
            if http_idle is None
            else _strict_nonnegative_int(http_idle, label="httpIdleTimeoutMs")
        ),
        websocket_connect_timeout_ms=(
            None
            if websocket_timeout is None
            else _strict_nonnegative_int(
                websocket_timeout, label="websocketConnectTimeoutMs"
            )
        ),
    )


def _equivalent_magenta_configurations(
    left: Mapping[str, object] | MagentaLaunchConfiguration,
    right: Mapping[str, object] | MagentaLaunchConfiguration,
) -> bool:
    try:
        return resolve_magenta_configuration(left) == resolve_magenta_configuration(right)
    except CliAgentConfigurationError:
        return _canonical_equal(left, right)


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
    configuration: Mapping[str, object] | MagentaLaunchConfiguration | None = None,
    config: Mapping[str, object] | MagentaLaunchConfiguration | None = None,
    provider: str | None = None,
    transport: str | None = None,
    cache_retention: str | None = None,
    openai_prompt_cache_mode: str | None = None,
    cache_telemetry: bool | None = None,
    cache_diagnostics: bool | None = None,
    tool_search: bool | None = None,
    cacheRetention: str | None = None,
    openaiPromptCacheMode: str | None = None,
    cacheTelemetry: bool | None = None,
    cacheDiagnostics: bool | None = None,
    toolSearch: bool | None = None,
) -> tuple[str, ...]:
    """Build provider-native non-interactive flags without invoking a shell.

    For Magenta, ``configuration``/``config`` is the preferred integration
    point.  Explicit keyword overrides are accepted for compatibility with the
    original adapter API.  The resulting argv order is fixed by the Magenta
    startup contract and is independent of TOML key order.
    """

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
        if cache_retention is None:
            cache_retention = cacheRetention
        if openai_prompt_cache_mode is None:
            openai_prompt_cache_mode = openaiPromptCacheMode
        if cache_telemetry is None:
            cache_telemetry = cacheTelemetry
        if cache_diagnostics is None:
            cache_diagnostics = cacheDiagnostics
        if tool_search is None:
            tool_search = toolSearch
        if configuration is not None and config is not None:
            if not _equivalent_magenta_configurations(configuration, config):
                raise CliAgentConfigurationError(
                    "Magenta configuration and config arguments disagree"
                )
        resolved = resolve_magenta_configuration(
            configuration if configuration is not None else config
        )
        # Explicit values take precedence.  Existing compatibility defaults are
        # treated as unset when a configuration mapping supplies a value.
        if provider is None:
            provider = resolved.provider
        if model is None:
            model = resolved.model
        effective_affinity = (
            resolved.anthropic_cache_affinity
            if resolved.anthropic_cache_affinity is not None
            and anthropic_cache_affinity == "auto"
            else anthropic_cache_affinity
        )
        effective_workflows = (
            resolved.harness_workflows
            if resolved.harness_workflows is not None and harness_workflows is False
            else harness_workflows
        )
        effective_teammates = (
            resolved.harness_teammates
            if resolved.harness_teammates is not None and harness_teammates is False
            else harness_teammates
        )
        effective_lock_tools = (
            resolved.lock_tools
            if resolved.lock_tools is not None and lock_tools is True
            else lock_tools
        )
        effective_transport = (
            resolved.transport if transport is None else transport
        )
        effective_cache_retention = (
            resolved.cache_retention
            if cache_retention is None
            else cache_retention
        )
        effective_openai_cache_mode = (
            resolved.openai_prompt_cache_mode
            if openai_prompt_cache_mode is None
            else openai_prompt_cache_mode
        )
        effective_cache_telemetry = (
            resolved.cache_telemetry if cache_telemetry is None else cache_telemetry
        )
        effective_cache_diagnostics = (
            resolved.cache_diagnostics
            if cache_diagnostics is None
            else cache_diagnostics
        )
        effective_tool_search = (
            resolved.tool_search if tool_search is None else tool_search
        )
        if provider is not None:
            provider = _strict_nonempty_string(provider, label="provider")
        if not model:
            raise CliAgentConfigurationError("Magenta CLI requires a model id")
        if effective_affinity not in {None, "auto", "on", "off"}:
            raise CliAgentConfigurationError(
                "Magenta cache affinity must be auto, on, off, or None"
            )
        if effective_transport is not None:
            effective_transport = _strict_choice(
                effective_transport,
                frozenset({"auto", "sse", "websocket", "websocket-cached"}),
                label="transport",
            )
        if effective_cache_retention is not None:
            effective_cache_retention = _strict_choice(
                effective_cache_retention,
                frozenset({"none", "short", "long", "provider-default"}),
                label="cacheRetention",
            )
        if effective_openai_cache_mode is not None:
            effective_openai_cache_mode = _strict_choice(
                effective_openai_cache_mode,
                frozenset({"off", "implicit", "explicit", "provider-default"}),
                label="openaiPromptCacheMode",
            )
        for value, label in (
            (effective_cache_telemetry, "cacheTelemetry"),
            (effective_cache_diagnostics, "cacheDiagnostics"),
            (effective_tool_search, "toolSearch"),
            (effective_workflows, "harnessWorkflows"),
            (effective_teammates, "harnessTeammates"),
            (effective_lock_tools, "lockTools"),
        ):
            if value is not None:
                _strict_bool(value, label=label)
        command = [
            command_executable,
            "--print",
            "--no-session",
            "--mode",
            "json",
            "--model",
            model,
        ]
        if provider is not None:
            command.extend(("--provider", provider))
        if effective_transport is not None:
            if effective_transport != "auto" or configuration is not None:
                command.extend(("--transport", effective_transport))
        if (
            effective_cache_retention is not None
            and effective_cache_retention != "provider-default"
        ):
            command.extend(("--cache-retention", effective_cache_retention))
        if (
            effective_openai_cache_mode is not None
            and effective_openai_cache_mode != "provider-default"
        ):
            command.extend(("--openai-prompt-cache-mode", effective_openai_cache_mode))
        if effective_cache_telemetry is not None:
            command.append(
                "--cache-telemetry"
                if effective_cache_telemetry
                else "--no-cache-telemetry"
            )
        if effective_cache_diagnostics is not None:
            command.append(
                "--cache-diagnostics"
                if effective_cache_diagnostics
                else "--no-cache-diagnostics"
            )
        if effective_affinity is not None:
            command.extend(("--anthropic-cache-affinity", effective_affinity))
        if effective_workflows is not None:
            command.append(
                "--harness-workflows"
                if effective_workflows
                else "--no-harness-workflows"
            )
        if effective_teammates is not None:
            command.append(
                "--harness-teammates"
                if effective_teammates
                else "--no-harness-teammates"
            )
        if effective_tool_search is not None:
            command.append(
                "--harness-tool-search"
                if effective_tool_search
                else "--no-harness-tool-search"
            )
        if effective_lock_tools is not None:
            command.append(
                "--lock-tools" if effective_lock_tools else "--no-lock-tools"
            )
        command.append(prompt)
        return tuple(command)
    raise CliAgentConfigurationError(f"unsupported CLI agent: {agent!r}")


def build_magenta_cli_command(
    *,
    prompt: str,
    model: str | None = None,
    configuration: Mapping[str, object] | MagentaLaunchConfiguration | None = None,
    **kwargs: object,
) -> tuple[str, ...]:
    """Named convenience wrapper for configuration-driven Magenta launches."""

    return build_cli_command(
        "magenta",
        prompt=prompt,
        model=model,
        configuration=configuration,
        **kwargs,  # type: ignore[arg-type]
    )


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


def write_magenta_settings(
    agent_dir: str | Path,
    configuration: Mapping[str, object] | MagentaLaunchConfiguration,
) -> Path:
    """Materialize a deterministic Magenta ``settings.json`` projection.

    This is the explicit bridge for settings-only v0.1.22 controls such as
    ``retry.provider.timeoutMs``.  The caller owns the directory lifecycle and
    can bind it through ``MAGENTA_CODING_AGENT_DIR`` for one process.
    """

    resolved = resolve_magenta_configuration(configuration)
    raw_directory = Path(agent_dir).expanduser()
    for candidate in (raw_directory, *raw_directory.parents):
        if candidate.is_symlink():
            raise CliAgentConfigurationError(
                f"Magenta agent directory contains a symlink: {candidate}"
            )
    directory = raw_directory.resolve()
    if directory.exists() and not directory.is_dir():
        raise CliAgentConfigurationError(
            f"Magenta agent directory is not a stable directory: {directory}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    settings_path = directory / "settings.json"
    payload = (
        json.dumps(
            resolved.settings_document(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    if settings_path.exists():
        if (
            settings_path.is_symlink()
            or not settings_path.is_file()
            or settings_path.read_bytes() != payload
        ):
            raise CliAgentConfigurationError(
                "existing Magenta settings.json differs from requested configuration"
            )
    else:
        atomic_write_bytes(settings_path, payload)
    return settings_path


def run_cli_agent(
    command: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: float | None = None,
    agent: str = "",
    env_var_names: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    configuration: Mapping[str, object] | MagentaLaunchConfiguration | None = None,
    magenta_agent_dir: str | Path | None = None,
) -> CliInvocationResult:
    """Execute one CLI attempt with timeout and secret-scrubbed environment.

    When ``timeout_seconds`` is ``None`` and a Magenta configuration carries a
    provider timeout, that timeout is used as the subprocess deadline.  A
    ``magenta_agent_dir`` opts into settings.json materialization for controls
    that have no Magenta-native startup flag.
    """

    if not command:
        raise CliAgentConfigurationError("CLI command must not be empty")
    effective_configuration = resolve_magenta_configuration(configuration)
    effective_agent = agent or _infer_agent(command)
    if timeout_seconds is None:
        if _normalize_agent(effective_agent) in {"magenta", "pi"}:
            timeout_seconds = effective_configuration.provider_timeout_seconds
    if timeout_seconds is None:
        raise CliAgentConfigurationError(
            "CLI timeout must be positive or supplied by Magenta retry.provider.timeoutMs"
        )
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise CliAgentConfigurationError("CLI timeout must be positive")
    launch_env = dict(extra_env or {})
    if magenta_agent_dir is not None:
        if _normalize_agent(effective_agent) not in {"magenta", "pi"}:
            raise CliAgentConfigurationError(
                "magenta_agent_dir is only supported for the Magenta CLI subject"
            )
        if configuration is not None:
            write_magenta_settings(magenta_agent_dir, effective_configuration)
        else:
            raw_agent_dir_path = Path(magenta_agent_dir).expanduser()
            for candidate in (raw_agent_dir_path, *raw_agent_dir_path.parents):
                if candidate.is_symlink():
                    raise CliAgentConfigurationError(
                        f"Magenta agent directory contains a symlink: {candidate}"
                    )
            agent_dir_path = raw_agent_dir_path.resolve()
            if agent_dir_path.exists() and not agent_dir_path.is_dir():
                raise CliAgentConfigurationError(
                    f"Magenta agent directory is not a stable directory: {agent_dir_path}"
                )
            agent_dir_path.mkdir(parents=True, exist_ok=True)
        launch_env["MAGENTA_CODING_AGENT_DIR"] = str(
            Path(magenta_agent_dir).expanduser().resolve()
        )
    executable = shutil.which(str(command[0])) or str(command[0])
    resolved_command = (executable, *map(str, command[1:]))
    started = time.monotonic()
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=Path(cwd),
            env=scrubbed_environment(env_var_names, extra=launch_env),
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


def _validate_sha256_digest(value: object, *, label: str, nullable: bool) -> None:
    if value is None and nullable:
        return
    digest = _require_mapping(value, label=label)
    if digest.get("algorithm") != "sha256":
        raise MagentaJsonlError(f"{label}.algorithm must be 'sha256'")
    observed = digest.get("value")
    if (
        not isinstance(observed, str)
        or len(observed) != 64
        or any(character not in "0123456789abcdef" for character in observed)
    ):
        raise MagentaJsonlError(f"{label}.value must be a lowercase SHA-256")


def _validate_assembly_sidecar(
    assembly: Mapping[str, object], *, label: str
) -> None:
    """Validate Magenta's public sidecar envelope without interpreting HCP rows."""

    required = {
        "type",
        "schemaVersion",
        "canonicalAssemblyDigest",
        "dependencyFileClosure",
        "components",
        "resolvedAddresses",
        "activeTools",
        "activeCapabilities",
        "systemPromptDigest",
        "packageProvenance",
        "diagnostics",
        "versions",
        "activationReceipt",
        "namespaces",
    }
    missing = sorted(required - set(assembly))
    if missing:
        raise MagentaJsonlError(f"{label} lacks required fields: {missing}")
    if assembly.get("type") != "hcp_assembly":
        raise MagentaJsonlError(f"{label}.type must be 'hcp_assembly'")
    if assembly.get("schemaVersion") != 1:
        raise MagentaJsonlError(f"{label}.schemaVersion must be 1")
    _validate_sha256_digest(
        assembly.get("canonicalAssemblyDigest"),
        label=f"{label}.canonicalAssemblyDigest",
        nullable=True,
    )
    if assembly.get("dependencyFileClosure") is not None:
        raise MagentaJsonlError(
            f"{label}.dependencyFileClosure must be null in schema version 1"
        )
    _validate_sha256_digest(
        assembly.get("systemPromptDigest"),
        label=f"{label}.systemPromptDigest",
        nullable=False,
    )
    for field in (
        "components",
        "resolvedAddresses",
        "activeTools",
        "activeCapabilities",
        "packageProvenance",
        "diagnostics",
    ):
        if not isinstance(assembly.get(field), list):
            raise MagentaJsonlError(f"{label}.{field} must be a list")
    for field in ("resolvedAddresses", "activeTools", "activeCapabilities"):
        values = assembly[field]
        if any(not isinstance(value, str) or not value for value in values):
            raise MagentaJsonlError(f"{label}.{field} must contain strings")
        if values != sorted(set(values)):
            raise MagentaJsonlError(f"{label}.{field} must be sorted and unique")

    versions = _require_mapping(assembly.get("versions"), label=f"{label}.versions")
    for field in ("magenta", "runtime"):
        if field not in versions:
            raise MagentaJsonlError(f"{label}.versions.{field} is required")
        value = versions[field]
        if value is not None and (not isinstance(value, str) or not value):
            raise MagentaJsonlError(
                f"{label}.versions.{field} must be a non-empty string or null"
            )
    activation = _require_mapping(
        assembly.get("activationReceipt"), label=f"{label}.activationReceipt"
    )
    if activation.get("status") not in {"observed", "unknown"}:
        raise MagentaJsonlError(f"{label}.activationReceipt.status is invalid")
    activation_addresses = activation.get("addresses")
    if not isinstance(activation_addresses, list) or any(
        not isinstance(value, str) or not value for value in activation_addresses
    ):
        raise MagentaJsonlError(
            f"{label}.activationReceipt.addresses must contain strings"
        )
    if activation_addresses != sorted(set(activation_addresses)):
        raise MagentaJsonlError(
            f"{label}.activationReceipt.addresses must be sorted and unique"
        )
    namespaces = _require_mapping(
        assembly.get("namespaces"), label=f"{label}.namespaces"
    )
    for field in ("state", "cache", "workspace"):
        value = namespaces.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise MagentaJsonlError(
                f"{label}.namespaces.{field} must be a string or null"
            )


def _project_effective_magenta_configuration(
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Project only settings Magenta publicly declares as runtime-effective.

    The projector is deliberately tolerant of additive v1 fields and older
    protocol-v1 manifests.  A missing field means "not observed", rather than
    silently assuming a provider default.
    """

    execution = manifest.get("execution")
    execution_obj = execution if isinstance(execution, Mapping) else {}
    projection: dict[str, object] = {}
    model = manifest.get("model")
    if isinstance(model, Mapping):
        if "provider" in model:
            projection["provider"] = model["provider"]
        if "id" in model:
            projection["model"] = model["id"]
    for source_name, output_name in (
        ("transport", "transport"),
        ("cacheRetention", "cacheRetention"),
        ("openaiPromptCacheMode", "openaiPromptCacheMode"),
        ("cacheTelemetry", "cacheTelemetry"),
        ("cacheDiagnostics", "cacheDiagnostics"),
        ("anthropicCacheAffinity", "anthropicCacheAffinity"),
    ):
        if source_name in execution_obj:
            projection[output_name] = execution_obj[source_name]

    capabilities: Mapping[str, object] = {}
    direct_capabilities = execution_obj.get("harnessCapabilities")
    if isinstance(direct_capabilities, Mapping):
        capabilities = direct_capabilities
    else:
        policy = execution_obj.get("capabilityPolicy")
        if isinstance(policy, Mapping) and isinstance(
            policy.get("capabilities"), Mapping
        ):
            capabilities = policy["capabilities"]  # type: ignore[assignment]
    for source_name, output_name in (
        ("toolSearch", "toolSearch"),
        ("workflows", "harnessWorkflows"),
        ("teammates", "harnessTeammates"),
    ):
        if source_name in capabilities:
            projection[output_name] = capabilities[source_name]

    # Future Magenta protocol-v1 additions may expose retry/provider values in
    # either policies or execution.  Preserve them when present, without
    # treating the existing boolean ``policies.autoRetry`` as a full receipt.
    retry: Mapping[str, object] | None = None
    for container_name in ("retry", "providerRetry"):
        candidate = execution_obj.get(container_name)
        if isinstance(candidate, Mapping):
            retry = candidate
            break
    policies = manifest.get("policies")
    if retry is None and isinstance(policies, Mapping):
        for container_name in ("retry", "providerRetry"):
            candidate = policies.get(container_name)
            if isinstance(candidate, Mapping):
                retry = candidate
                break
    if retry is not None:
        normalized_retry: dict[str, object] = {}
        for source_name, output_name in (
            ("enabled", "enabled"),
            ("maxRetries", "maxRetries"),
            ("baseDelayMs", "baseDelayMs"),
        ):
            if source_name in retry:
                normalized_retry[output_name] = retry[source_name]
        provider = retry.get("provider")
        if isinstance(provider, Mapping):
            normalized_provider = {
                name: provider[name]
                for name in ("timeoutMs", "maxRetries", "maxRetryDelayMs")
                if name in provider
            }
            if normalized_provider:
                normalized_retry["provider"] = normalized_provider
        if normalized_retry:
            projection["retry"] = normalized_retry
    return projection


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
    optional_choices = (
        ("transport", {"auto", "sse", "websocket", "websocket-cached"}),
        ("cacheRetention", {"none", "short", "long", "provider-default"}),
        (
            "openaiPromptCacheMode",
            {"off", "implicit", "explicit", "provider-default"},
        ),
    )
    for field, choices in optional_choices:
        if field in execution and execution[field] not in choices:
            raise MagentaJsonlError(
                f"{label}.execution.{field} has an invalid value"
            )
    if "cacheTelemetry" in execution and type(execution["cacheTelemetry"]) is not bool:
        raise MagentaJsonlError(f"{label}.execution.cacheTelemetry must be boolean")
    if "cacheDiagnostics" in execution and not (
        type(execution["cacheDiagnostics"]) is bool
        or execution["cacheDiagnostics"] == "provider-default"
    ):
        raise MagentaJsonlError(
            f"{label}.execution.cacheDiagnostics has an invalid value"
        )
    manifest_capabilities = execution.get("harnessCapabilities")
    if manifest_capabilities is not None:
        manifest_capabilities = _require_mapping(
            manifest_capabilities,
            label=f"{label}.execution.harnessCapabilities",
        )
        for field in ("workflows", "teammates", "toolSearch"):
            if field in manifest_capabilities and type(manifest_capabilities[field]) is not bool:
                raise MagentaJsonlError(
                    f"{label}.execution.harnessCapabilities.{field} must be boolean"
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

    policies = manifest.get("policies")
    if policies is not None:
        policies_obj = _require_mapping(policies, label=f"{label}.policies")
        for retry_name in ("retry", "providerRetry"):
            retry = policies_obj.get(retry_name)
            if retry is None:
                continue
            retry_obj = _require_mapping(retry, label=f"{label}.policies.{retry_name}")
            for field in ("enabled",):
                if field in retry_obj and type(retry_obj[field]) is not bool:
                    raise MagentaJsonlError(
                        f"{label}.policies.{retry_name}.{field} must be boolean"
                    )
            for field in ("maxRetries", "baseDelayMs", "timeoutMs", "maxRetryDelayMs"):
                if field in retry_obj:
                    value = retry_obj[field]
                    if type(value) is not int or value < 0:
                        raise MagentaJsonlError(
                            f"{label}.policies.{retry_name}.{field} must be non-negative integer"
                        )
            provider = retry_obj.get("provider")
            if provider is not None:
                provider_obj = _require_mapping(
                    provider,
                    label=f"{label}.policies.{retry_name}.provider",
                )
                for field in ("timeoutMs", "maxRetries", "maxRetryDelayMs"):
                    if field in provider_obj:
                        value = provider_obj[field]
                        if type(value) is not int or value < 0:
                            raise MagentaJsonlError(
                                f"{label}.policies.{retry_name}.provider.{field} must be non-negative integer"
                            )

    # Component rows remain opaque to BMP. Validate only Magenta's public
    # envelope so malformed sidecar evidence cannot masquerade as valid.
    assembly = manifest.get("assembly")
    if assembly is not None:
        assembly_obj = _require_mapping(assembly, label=f"{label}.assembly")
        _validate_assembly_sidecar(assembly_obj, label=f"{label}.assembly")


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


def _projection_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _projection_paths(
    value: Mapping[str, object], *, prefix: str = ""
) -> dict[str, object]:
    paths: dict[str, object] = {}
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, Mapping):
            paths.update(_projection_paths(child, prefix=path))
        else:
            paths[path] = child
    return paths


def _configuration_activation_status(
    requested: Mapping[str, object],
    effective: Mapping[str, object],
    activation_receipt: Mapping[str, object] | None,
) -> str:
    if not requested:
        return "unrequested"
    if activation_receipt is None:
        return "missing_activation_receipt"
    requested_paths = _projection_paths(requested)
    effective_paths = _projection_paths(effective)
    mismatches = [
        path
        for path, value in requested_paths.items()
        if path in effective_paths and effective_paths[path] != value
    ]
    missing = [path for path in requested_paths if path not in effective_paths]
    if mismatches:
        return "mismatch"
    if missing:
        return "unobserved"
    if activation_receipt.get("status") not in {"observed", "unknown"}:
        return "invalid_activation_receipt"
    if activation_receipt.get("status") == "unknown":
        return "activation_unknown"
    return "matched"


def _make_configuration_receipt(
    requested: MagentaLaunchConfiguration,
    trace: MagentaJsonlTrace,
) -> MagentaConfigurationReceipt:
    requested_projection = requested.requested_projection()
    effective_projection = dict(trace.effective_configuration)
    return MagentaConfigurationReceipt(
        requested=requested_projection,
        effective=effective_projection,
        requested_sha256=_projection_digest(requested_projection),
        effective_sha256=_projection_digest(effective_projection),
        activation_receipt=trace.activation_receipt,
        effective_sequence=int(trace.effective_runtime_manifest["sequence"]),
        status=_configuration_activation_status(
            requested_projection,
            effective_projection,
            trace.activation_receipt,
        ),
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
    configuration: Mapping[str, object] | MagentaLaunchConfiguration | None = None,
    config: Mapping[str, object] | MagentaLaunchConfiguration | None = None,
    requested_configuration: Mapping[str, object]
    | MagentaLaunchConfiguration
    | None = None,
) -> CliOutputArtifacts:
    """Persist trace, answer, and status files without persisting environment values.

    Valid Magenta attempts additionally persist requested/effective settings and
    the effective HCP activation receipt as a dedicated configuration receipt.
    """

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    trace = root / "trace.md"
    answer = root / "answer.txt"
    status = root / "agent_runner_status.json"
    effective_agent = (
        result.agent if agent is None or not agent.strip() else agent
    )
    supplied_configurations = [
        item
        for item in (configuration, config, requested_configuration)
        if item is not None
    ]
    requested_config = MagentaLaunchConfiguration()
    if supplied_configurations:
        first_configuration = supplied_configurations[0]
        for other_configuration in supplied_configurations[1:]:
            if not _equivalent_magenta_configurations(
                first_configuration, other_configuration
            ):
                raise CliAgentConfigurationError(
                    "Magenta configuration arguments disagree"
                )
        requested_config = resolve_magenta_configuration(first_configuration)
    if (
        _normalize_agent(effective_agent) not in {"magenta", "pi"}
        and supplied_configurations
    ):
        raise CliAgentConfigurationError(
            "Magenta configuration is only supported for the Magenta CLI subject"
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
    runtime_manifest_receipt = None
    configuration_receipt = None
    if _normalize_agent(effective_agent) in {"magenta", "pi"}:
        try:
            parsed_trace = parse_magenta_jsonl(result.stdout)
        except MagentaJsonlError as exc:
            status_payload["headless_protocol_valid"] = False
            status_payload["headless_protocol_error"] = str(exc)
            if requested_config.requested_projection():
                requested_projection = requested_config.requested_projection()
                status_payload["requested_configuration"] = requested_projection
                status_payload["requested_configuration_sha256"] = _projection_digest(
                    requested_projection
                )
        else:
            configuration_receipt = _make_configuration_receipt(
                requested_config, parsed_trace
            )
            manifests = list(parsed_trace.runtime_manifests)
            sidecar_refs: list[RuntimeAssemblySidecarRef] = []
            sidecar_dir = root / "assembly_sidecars"
            for manifest in parsed_trace.runtime_manifests:
                sidecar = manifest.get("assembly")
                if not isinstance(sidecar, Mapping):
                    continue
                sequence = manifest["sequence"]
                encoded = json.dumps(
                    sidecar,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                sidecar_dir.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(encoded).hexdigest()
                sidecar_path = sidecar_dir / f"{digest}.json"
                if sidecar_path.exists():
                    if (
                        sidecar_path.is_symlink()
                        or not sidecar_path.is_file()
                        or sidecar_path.read_bytes() != encoded
                    ):
                        raise MagentaJsonlError(
                            "persisted Magenta assembly sidecar content drift"
                        )
                else:
                    atomic_write_bytes(sidecar_path, encoded)
                sidecar_refs.append(
                    RuntimeAssemblySidecarRef(
                        sequence=int(sequence),
                        path=str(sidecar_path.resolve()),
                        sha256=digest,
                        size_bytes=len(encoded),
                    )
                )
            manifest_digests = tuple(
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
            )
            runtime_manifest_receipt = RuntimeManifestReceipt(
                run_id=str(parsed_trace.run_end["runId"]),
                manifest_sha256=manifest_digests,
                trace_ref=artifact_ref(trace),
                assembly_sidecar_refs=tuple(sidecar_refs),
                effective_sequence=int(
                    parsed_trace.effective_runtime_manifest["sequence"]
                ),
                effective_assembly_sidecar_ref=(
                    sidecar_refs[-1]
                    if parsed_trace.effective_assembly_sidecar is not None
                    else None
                ),
            )
            status_payload.update(
                {
                    "headless_protocol_valid": True,
                    "headless_run_end": dict(parsed_trace.run_end),
                    "runtime_manifests": manifests,
                    "runtime_manifest_sha256": list(manifest_digests),
                    "assembly_sidecar_count": len(parsed_trace.assembly_sidecars),
                    "assembly_sidecar_refs": [
                        ref.model_dump(mode="json") for ref in sidecar_refs
                    ],
                    "effective_runtime_manifest_sequence": (
                        parsed_trace.effective_runtime_manifest["sequence"]
                    ),
                    "effective_assembly_sidecar_ref": (
                        sidecar_refs[-1].model_dump(mode="json")
                        if parsed_trace.effective_assembly_sidecar is not None
                        else None
                    ),
                    "magenta_configuration": configuration_receipt.model_dump(),
                    "requested_configuration": dict(
                        configuration_receipt.requested
                    ),
                    "effective_configuration": dict(
                        configuration_receipt.effective
                    ),
                    "configuration_activation_status": configuration_receipt.status,
                    "activation_receipt": (
                        None
                        if configuration_receipt.activation_receipt is None
                        else dict(configuration_receipt.activation_receipt)
                    ),
                }
            )
            receipt_path = root / "magenta_configuration_receipt.json"
            receipt_payload = (
                json.dumps(
                    configuration_receipt.model_dump(),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            if receipt_path.exists():
                if (
                    receipt_path.is_symlink()
                    or not receipt_path.is_file()
                    or receipt_path.read_bytes() != receipt_payload
                ):
                    raise MagentaJsonlError(
                        "persisted Magenta configuration receipt content drift"
                    )
            else:
                atomic_write_bytes(receipt_path, receipt_payload)
            configuration_receipt = replace(
                configuration_receipt,
                path=receipt_path.resolve(),
            )
            status_payload["magenta_configuration"] = configuration_receipt.model_dump()
    status.write_text(
        json.dumps(status_payload, indent=2),
        encoding="utf-8",
    )
    return CliOutputArtifacts(
        trace_path=trace,
        answer_path=answer,
        status_path=status,
        runtime_manifest_receipt=runtime_manifest_receipt,
        configuration_receipt=configuration_receipt,
    )


__all__ = [
    "CliAgentConfigurationError",
    "CliInvocationResult",
    "CliOutputArtifacts",
    "MagentaConfigurationReceipt",
    "MagentaJsonlError",
    "MagentaLaunchConfiguration",
    "build_magenta_cli_command",
    "MagentaJsonlTrace",
    "build_cli_command",
    "extract_answer",
    "parse_magenta_jsonl",
    "resolve_magenta_configuration",
    "run_cli_agent",
    "scrubbed_environment",
    "write_cli_outputs",
]
