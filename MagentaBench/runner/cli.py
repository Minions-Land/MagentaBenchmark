"""Command-line entry points for compiling and running BMP experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from MagentaBench.schemas import EvidenceBundle, RunStatus, verify_run_report

from .compiler import Compiler
from .pipeline import Pipeline, PipelineResult


_NON_EXECUTION_FAILURE_STATUSES = frozenset(
    {RunStatus.pass_, RunStatus.verified_fail, RunStatus.scored}
)


def _failed_attempts(result: PipelineResult) -> list[dict[str, Any]]:
    """Return structured locators for attempts that did not reach scoring."""

    failures = []
    seen: set[tuple[str, str]] = set()
    for completed in result.runs:
        receipt = completed.schedule_receipt
        for attempt in receipt.attempts:
            identity = (receipt.run_id, attempt.attempt_id)
            if (
                identity in seen
                or attempt.status in _NON_EXECUTION_FAILURE_STATUSES
            ):
                continue
            seen.add(identity)
            bundle_ref = attempt.evidence_bundle_ref
            if bundle_ref is None:  # verify_run_report rejects this first.
                continue
            bundle = EvidenceBundle.model_validate_json(
                Path(bundle_ref.path).read_bytes()
            )
            failures.append(
                {
                    "run_id": receipt.run_id,
                    "case_id": attempt.case_id,
                    "attempt_id": attempt.attempt_id,
                    "status": attempt.status.value,
                    "evidence_bundle": bundle_ref.path,
                    "log_artifacts": [ref.path for ref in bundle.log_refs],
                }
            )
    return failures


def _parse_set(value: str) -> tuple[str, Any]:
    """Parse ``--set dotted.path=value`` through TOML's scalar grammar."""

    if "=" not in value:
        raise argparse.ArgumentTypeError("--set values must be PATH=VALUE")
    path, encoded = value.split("=", 1)
    if not path or not encoded:
        raise argparse.ArgumentTypeError("--set values must be PATH=VALUE")
    try:
        parsed = tomllib.loads(f"value = {encoded}\n")["value"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise argparse.ArgumentTypeError(
            f"--set value is not valid TOML: {value!r}"
        ) from exc
    return path, parsed


def _configuration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="Add an external [configuration] TOML envelope",
    )
    parser.add_argument(
        "--raw-config",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="Add an explicitly raw TOML configuration document",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        metavar="ID",
        help="Add a named configuration registry profile",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        type=_parse_set,
        metavar="PATH=VALUE",
        help="Override a configuration dotted path (value uses TOML syntax)",
    )


def _configuration_overrides(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path, value in args.set:
        if path in result:
            raise ValueError(f"duplicate --set path: {path!r}")
        result[path] = value
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmp-run",
        description="Compile and execute one BMP experiment with production adapters",
    )
    parser.add_argument("experiment", type=Path, help="Experiment TOML path")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="MagentaBench project root (default: current directory)",
    )
    parser.add_argument(
        "--record-root",
        type=Path,
        required=True,
        help="Fresh record root for immutable execution evidence",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing checkpoint-compatible execution",
    )
    _configuration_arguments(parser)
    return parser


def run_main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        overrides = _configuration_overrides(args)
    except ValueError as exc:
        parser.error(str(exc))
    result = Pipeline(args.project_root, args.record_root).run(
        args.experiment,
        resume=args.resume,
        config_files=tuple(path.resolve() for path in args.config),
        raw_config_files=tuple(path.resolve() for path in args.raw_config),
        config_profiles=tuple(args.profile),
        config_overrides=overrides,
    )
    verified = verify_run_report(result.report_path)
    print(
        json.dumps(
            {
                "experiment_id": verified.report.experiment_id,
                "purpose": verified.report.purpose.value,
                "report": str(result.report_path.resolve()),
                "aggregate": str(result.aggregate_path.resolve()),
                "run_count": len(result.runs),
                "failed_attempts": _failed_attempts(result),
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def compile_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bmp-compile",
        description="Compile an experiment into canonical resolved BMP plans",
    )
    parser.add_argument("experiment", type=Path, help="Experiment TOML path")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="MagentaBench project root (default: current directory)",
    )
    _configuration_arguments(parser)
    args = parser.parse_args(argv)
    try:
        overrides = _configuration_overrides(args)
    except ValueError as exc:
        parser.error(str(exc))
    runs = Compiler(args.project_root).compile(
        args.experiment,
        config_files=tuple(path.resolve() for path in args.config),
        raw_config_files=tuple(path.resolve() for path in args.raw_config),
        config_profiles=tuple(args.profile),
        config_overrides=overrides,
    )
    payload = [
        {
            "run_id": run.manifest.metadata.run_id,
            "manifest_digest": run.manifest_digest,
            "manifest": run.manifest.model_dump(mode="json"),
        }
        for run in runs
    ]
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


__all__ = ["compile_main", "run_main"]
