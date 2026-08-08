"""Command-line entry points for compiling and running BMP experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from MagentaBench.schemas import verify_run_report

from .compiler import Compiler
from .pipeline import Pipeline


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
    return parser


def run_main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = Pipeline(args.project_root, args.record_root).run(
        args.experiment,
        resume=args.resume,
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
    args = parser.parse_args(argv)
    runs = Compiler(args.project_root).compile(args.experiment)
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

