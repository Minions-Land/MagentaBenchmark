"""Subject adapters."""

from .cli_agent import (
    CliAgentConfigurationError,
    CliInvocationResult,
    MagentaJsonlError,
    MagentaJsonlTrace,
    build_cli_command,
    extract_answer,
    parse_magenta_jsonl,
    run_cli_agent,
    scrubbed_environment,
    write_cli_outputs,
)

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
