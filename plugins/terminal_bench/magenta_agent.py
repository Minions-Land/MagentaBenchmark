"""Pinned Magenta CLI Agent for Harbor 0.20 Terminal-Bench jobs.

The class is intentionally small: Harbor owns the container, phase timeouts,
network policy, and verifier, while this adapter owns only the Magenta release
installation and its public JSONL headless invocation.  Release assets are
downloaded through the configured GitHub mirror and checked against the pinned
release ``SHA256SUMS`` digest before installation.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any, Mapping

try:  # ``typing.override`` is only available on Python 3.12+.
    from typing import override
except ImportError:  # pragma: no cover - exercised on Python 3.10/3.11
    def override(function: Any) -> Any:
        return function

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


MAGENTA_VERSION = "0.1.23"
MAGENTA_TAG = f"v{MAGENTA_VERSION}"
MAGENTA_RELEASE_REPOSITORY = "Minions-Land/Magenta-CLI"
MAGENTA_BINARY_ASSET = "magenta-linux-x64"
MAGENTA_BINARY_SHA256 = "9e04ef394e87284791bfc5aabec87ab4a6611bde7b11b51bcd87a3b197e57180"
MAGENTA_RESOURCES_ASSET = "magenta-resources-universal.tar.gz"
MAGENTA_RESOURCES_SHA256 = "318e21f46bb2cff89e39687836ac83f729a656df8ee822cdb32da46f8033b874"
MAGENTA_CHECKSUMS_SHA256 = "d304bc32b4ca522f34499bb0396fa508e8f3cec85d304a62846112f3da3e88d2"
MAGENTA_IMPORT_PATH = "plugins.terminal_bench.magenta_agent:MagentaAgent"
MAGENTA_CONTAINER_TRACE_PATH = "/logs/agent/magenta.jsonl"


class MagentaAgentConfigurationError(ValueError):
    """The pinned Magenta configuration cannot be executed safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_magenta_command(model_name: str, instruction: str) -> str:
    """Build one non-interactive Magenta invocation without shell interpolation."""

    provider, separator, model = model_name.partition("/")
    if not separator or not provider or not model:
        raise MagentaAgentConfigurationError(
            "Magenta model_name must use the provider/model form"
        )
    return " ".join(
        (
            "magenta",
            "--print",
            "--no-session",
            "--mode",
            "json",
            "--provider",
            shlex.quote(provider),
            "--model",
            shlex.quote(model),
            shlex.quote(instruction),
        )
    )


def _asset_url(asset: str) -> str:
    return (
        f"https://github.com/{MAGENTA_RELEASE_REPOSITORY}/releases/download/"
        f"{MAGENTA_TAG}/{asset}"
    )


def _mirrored_url(asset: str, mirror: str) -> str:
    direct = _asset_url(asset)
    return f"{mirror.rstrip('/')}/{direct}"


def _usage_from_trace(path: Path) -> tuple[int | None, int | None, int | None, float | None]:
    input_tokens = output_tokens = cache_tokens = None
    cost: float | None = None
    try:
        records = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None, None, None
    input_total = output_total = cache_total = 0
    saw_usage = False
    cost_total = 0.0
    for line in records:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        saw_usage = True
        input_total += int(usage.get("input", 0) or 0)
        output_total += int(usage.get("output", 0) or 0)
        cache_total += int(usage.get("cacheRead", 0) or 0)
        raw_cost = usage.get("cost")
        if isinstance(raw_cost, Mapping):
            cost_total += float(raw_cost.get("total", 0.0) or 0.0)
    if not saw_usage:
        return None, None, None, None
    input_tokens = input_total + cache_total
    output_tokens = output_total
    cache_tokens = cache_total
    cost = cost_total
    return input_tokens, output_tokens, cache_tokens, cost


class MagentaAgent(BaseInstalledAgent):
    """Install and run the exact Magenta release selected by BMP."""

    _OUTPUT_FILENAME = "magenta.jsonl"
    SUPPORTS_RESUME = False

    def __init__(
        self,
        *args: Any,
        release_version: str = MAGENTA_VERSION,
        github_mirror: str = "https://ghfast.top",
        **kwargs: Any,
    ) -> None:
        if release_version != MAGENTA_VERSION:
            raise MagentaAgentConfigurationError(
                f"only Magenta {MAGENTA_VERSION} is admitted, got {release_version!r}"
            )
        if not github_mirror.startswith(("http://", "https://")):
            raise MagentaAgentConfigurationError("github_mirror must be an HTTP(S) URL")
        self.release_version = release_version
        self.github_mirror = github_mirror
        super().__init__(*args, **kwargs)

    @staticmethod
    @override
    def name() -> str:
        return "magentabench-magenta"

    @override
    def get_version_command(self) -> str:
        return "magenta --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    async def _download_release_assets(self, environment: BaseEnvironment) -> None:
        """Download payloads through the configured mirror and checksum root directly."""

        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "command -v curl >/dev/null; command -v sha256sum >/dev/null; "
                "mkdir -p /opt/magentabench/input; "
                f"curl -fsSL --retry 3 {_mirrored_url(MAGENTA_BINARY_ASSET, self.github_mirror)!r} "
                "> /opt/magentabench/input/magenta-linux-x64; "
                f"curl -fsSL --retry 3 {_mirrored_url(MAGENTA_RESOURCES_ASSET, self.github_mirror)!r} "
                "> /opt/magentabench/input/magenta-resources-universal.tar.gz; "
                f"curl -fsSL --retry 3 {_mirrored_url('SHA256SUMS', self.github_mirror)!r} "
                "> /opt/magentabench/input/SHA256SUMS; "
                f"printf '%s  %s\\n' {MAGENTA_BINARY_SHA256} {MAGENTA_BINARY_ASSET} "
                "> /opt/magentabench/input/expected.binary; "
                f"printf '%s  %s\\n' {MAGENTA_RESOURCES_SHA256} {MAGENTA_RESOURCES_ASSET} "
                "> /opt/magentabench/input/expected.resources; "
                f"printf '%s  %s\\n' {MAGENTA_CHECKSUMS_SHA256} SHA256SUMS "
                "> /opt/magentabench/input/expected.checksums; "
                "cd /opt/magentabench/input; "
                "sha256sum -c expected.binary; "
                "sha256sum -c /opt/magentabench/input/expected.resources; "
                "sha256sum -c expected.checksums"
            ),
            timeout_sec=900,
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self._download_release_assets(environment)
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "chmod 0755 /opt/magentabench/input/magenta-linux-x64; "
                "/opt/magentabench/input/magenta-linux-x64 _install-unix "
                "--install-dir /opt/magentabench/release "
                "--entrypoint-path /usr/local/bin/magenta "
                "--legacy-install-dir /opt/magentabench/legacy "
                "--resource-archive /opt/magentabench/input/magenta-resources-universal.tar.gz "
                "--checksums /opt/magentabench/input/SHA256SUMS "
                f"--binary-asset {MAGENTA_BINARY_ASSET} "
                f"--expected-version {MAGENTA_VERSION}"
            ),
            timeout_sec=300,
        )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        command = build_magenta_command(self.model_name or "", instruction)
        trace_path = self.logs_dir / self._OUTPUT_FILENAME
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; mkdir -p /tmp/magentabench-agent; "
                "MAGENTA_CODING_AGENT_DIR=/tmp/magentabench-agent "
                f"{command} | tee {shlex.quote(MAGENTA_CONTAINER_TRACE_PATH)}"
            ),
            timeout_sec=None,
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        trace = self.logs_dir / self._OUTPUT_FILENAME
        input_tokens, output_tokens, cache_tokens, cost = _usage_from_trace(trace)
        if input_tokens is not None:
            context.n_input_tokens = input_tokens
            context.n_output_tokens = output_tokens
            context.n_cache_tokens = cache_tokens
        if cost is not None:
            context.cost_usd = cost


__all__ = [
    "MAGENTA_BINARY_ASSET",
    "MAGENTA_BINARY_SHA256",
    "MAGENTA_CHECKSUMS_SHA256",
    "MAGENTA_CONTAINER_TRACE_PATH",
    "MAGENTA_IMPORT_PATH",
    "MAGENTA_RELEASE_REPOSITORY",
    "MAGENTA_RESOURCES_ASSET",
    "MAGENTA_RESOURCES_SHA256",
    "MAGENTA_TAG",
    "MAGENTA_VERSION",
    "MagentaAgent",
    "MagentaAgentConfigurationError",
    "build_magenta_command",
]
