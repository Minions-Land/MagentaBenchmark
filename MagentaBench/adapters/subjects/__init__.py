"""Subject adapters."""

from .cli_agent import (
    CliAgentConfigurationError,
    CliInvocationResult,
    CliOutputArtifacts,
    MagentaConfigurationReceipt,
    MagentaJsonlError,
    MagentaJsonlTrace,
    MagentaLaunchConfiguration,
    build_magenta_cli_command,
    build_cli_command,
    extract_answer,
    parse_magenta_jsonl,
    resolve_magenta_configuration,
    run_cli_agent,
    scrubbed_environment,
    write_cli_outputs,
    write_magenta_settings,
)

__all__ = [
    "CliAgentConfigurationError",
    "CliInvocationResult",
    "CliOutputArtifacts",
    "MagentaConfigurationReceipt",
    "MagentaJsonlError",
    "MagentaJsonlTrace",
    "MagentaLaunchConfiguration",
    "build_magenta_cli_command",
    "build_cli_command",
    "extract_answer",
    "parse_magenta_jsonl",
    "resolve_magenta_configuration",
    "run_cli_agent",
    "scrubbed_environment",
    "write_cli_outputs",
    "write_magenta_settings",
]
