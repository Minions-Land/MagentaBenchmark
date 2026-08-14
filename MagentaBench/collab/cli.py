"""Agent-facing commands for experiment design and repository coordination."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

from .repository import (
    CollaborationError,
    ExperimentRepository,
    ValidationReport,
    classify_changed_paths,
)
from .ledger import build_experiment_ledger, parse_path_maps, render_csv
from .imports import HistoricalImportValidation, validate_historical_imports


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmp-collab",
        description="Agent-facing experiment bundles and Git collaboration checks",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="MagentaBench project root (default: current directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser(
        "validate", help="validate every experiment bundle, lab link, and BMP pin"
    )
    validate.add_argument("--format", choices=("text", "json"), default="text")

    validate_imports = sub.add_parser(
        "validate-imports",
        help="validate content-addressed historical imports without network access",
    )
    validate_imports.add_argument(
        "--imports-dir",
        type=Path,
        default=Path("imports"),
        help="historical import root (default: PROJECT_ROOT/imports)",
    )
    validate_imports.add_argument(
        "--format", choices=("text", "json"), default="text"
    )

    listing = sub.add_parser(
        "list", help="render the derived bundle queue without a hand-edited board"
    )
    listing.add_argument("--format", choices=("table", "json"), default="table")

    next_parser = sub.add_parser(
        "next", help="show currently claimable work and blocked recovery candidates"
    )
    next_parser.add_argument("--format", choices=("text", "json"), default="text")

    modes = sub.add_parser(
        "modes",
        help="show local, Docker, AppContainer, E2B, and remote backend readiness",
    )
    modes.add_argument("--format", choices=("table", "json"), default="table")

    ledger = sub.add_parser(
        "ledger",
        help="derive experiment, run, and metric tables from bundles, lab, and verified reports",
    )
    ledger.add_argument("--format", choices=("table", "json", "csv"), default="table")
    ledger.add_argument(
        "--table",
        choices=(
            "experiments",
            "runs",
            "metrics",
            "sources",
            "catalog",
            "observations",
            "assets",
        ),
        default="experiments",
        help="table rendered for table or CSV output (JSON always includes all tables)",
    )
    ledger.add_argument(
        "--imports-dir",
        type=Path,
        default=Path("imports"),
        help="historical import root (default: PROJECT_ROOT/imports)",
    )
    ledger.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="relocate an absolute recorded artifact prefix",
    )

    scaffold = sub.add_parser(
        "scaffold",
        help="create one isolated collaboration bundle around an existing BMP TOML",
    )
    scaffold.add_argument("experiment_id")
    scaffold.add_argument("--bmp-spec", required=True)
    scaffold.add_argument("--lab-issue", required=True)
    scaffold.add_argument("--related-issue", action="append", default=[])
    scaffold.add_argument("--question", required=True)
    scaffold.add_argument("--hypothesis", required=True)
    scaffold.add_argument("--stop-condition", action="append", required=True)
    scaffold.add_argument("--required-env", action="append", default=[])
    scaffold.add_argument("--primary-metric", action="append", default=[])

    preflight = sub.add_parser(
        "preflight", help="validate one bundle, then run its argv-based preflight"
    )
    preflight.add_argument("experiment_id")
    preflight.add_argument(
        "--dry-run", action="store_true", help="print the command without executing it"
    )
    preflight.add_argument(
        "--actor", required=True, help="lease holder identity authorizing the preflight"
    )

    changes = sub.add_parser(
        "changes",
        help="classify a Git patch and enforce the BMP protocol review boundary",
    )
    changes.add_argument("--base-ref", required=True)
    changes.add_argument("--head-ref", default="HEAD")
    changes.add_argument("--allow-protocol-change", action="store_true")
    changes.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _validation_text(report: ValidationReport) -> str:
    lines = [
        f"collaboration validation: {'OK' if report.ok else 'FAILED'} "
        f"({len(report.bundles)} bundles, {len(report.errors)} errors, "
        f"{len(report.warnings)} warnings)"
    ]
    for finding in report.errors:
        location = "" if finding.path is None else f" [{finding.path}]"
        lines.append(f"ERROR {finding.code}{location}: {finding.message}")
    for finding in report.warnings:
        location = "" if finding.path is None else f" [{finding.path}]"
        lines.append(f"WARNING {finding.code}{location}: {finding.message}")
    for bundle in report.bundles:
        lines.append(
            f"- {bundle.id}: {bundle.lab_status}, mode source={bundle.bmp_spec}, "
            f"issue={bundle.lab_issue}, available={'yes' if bundle.available else 'no'}"
        )
    return "\n".join(lines) + "\n"


def _import_validation_text(report: HistoricalImportValidation) -> str:
    lines = [
        f"historical import validation: {'OK' if report.ok else 'FAILED'} "
        f"({len(report.snapshot.sources)} sources, "
        f"{len(report.snapshot.records)} records, {len(report.errors)} errors)"
    ]
    for finding in report.errors:
        location = "" if finding.path is None else f" [{finding.path}]"
        lines.append(f"ERROR {finding.code}{location}: {finding.message}")
    for finding in report.warnings:
        location = "" if finding.path is None else f" [{finding.path}]"
        lines.append(f"WARNING {finding.code}{location}: {finding.message}")
    return "\n".join(lines) + "\n"


def _bundle_table(report: ValidationReport) -> str:
    lines = [
        "| Available | Status | Bundle | Purpose | Protocol | Lab issue | Blockers |",
        "| --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for bundle in report.bundles:
        lines.append(
            "| "
            + " | ".join(
                (
                    "yes" if bundle.available else "no",
                    bundle.lab_status,
                    bundle.id,
                    bundle.purpose,
                    bundle.protocol_id,
                    bundle.lab_issue,
                    str(bundle.blocker_count),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _modes_table(modes: tuple[dict[str, Any], ...]) -> str:
    lines = [
        "| Mode | Configured | Verifier boundary | Maximum label | Work item | Backends |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in modes:
        backends = (
            ", ".join(
                entry["backend_id"]
                if entry["configured"]
                else f"{entry['backend_id']} (registered-only)"
                for entry in item["backends"]
            )
            or "-"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    item["mode"],
                    "yes" if item["configured"] else "no",
                    "closed" if item["standalone_verifier_boundary_closed"] else "open",
                    item["maximum_evidence_label"],
                    item["lab_issue"] or "-",
                    backends,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _ledger_table(rows: tuple[dict[str, Any], ...], table: str) -> str:
    selected = {
        "experiments": (
            "lab_status",
            "experiment_id",
            "benchmark_id",
            "subject_id",
            "model",
            "dataset_id",
            "protocol_id",
            "execution_mode",
            "purpose",
            "run_count",
        ),
        "runs": (
            "run_state",
            "experiment_id",
            "lab_run_id",
            "purpose",
            "standalone_verification",
            "claim_eligible",
            "metric_row_count",
        ),
        "metrics": (
            "experiment_id",
            "lab_run_id",
            "method_id",
            "dataset_id",
            "metric_id",
            "metric_state",
            "value",
            "uncertainty_lower",
            "uncertainty_upper",
            "planned_rollout_count",
        ),
        "sources": (
            "source_id",
            "record_origin",
            "repository",
            "commit_sha",
            "record_count",
            "evidence_tiers",
        ),
        "catalog": (
            "record_origin",
            "evidence_tier",
            "catalog_id",
            "benchmark_id",
            "dataset_id",
            "method_id",
            "model",
            "image_digest",
            "budget",
            "comparability",
            "claim_eligible",
        ),
        "observations": (
            "record_origin",
            "evidence_tier",
            "benchmark_id",
            "method_id",
            "dataset_id",
            "metric_id",
            "value",
            "image_digest",
            "budget",
            "observed_count",
            "comparability",
            "claim_eligible",
        ),
        "assets": (
            "record_origin",
            "asset_id",
            "role",
            "status",
            "materialization_state",
            "content_sha256",
        ),
    }[table]

    def cell(value: object) -> str:
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (list, dict)):
            value = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        return str(value).replace("|", "\\|").replace("\n", " ")

    labels = tuple(name.replace("_", " ").title() for name in selected)
    lines = [
        "| " + " | ".join(labels) + " |",
        "| " + " | ".join("---" for _ in selected) + " |",
    ]
    lines.extend(
        "| " + " | ".join(cell(row.get(name)) for name in selected) + " |"
        for row in rows
    )
    return "\n".join(lines) + "\n"


def _git_output(root: Path, arguments: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise CollaborationError(f"Git command failed: {detail}")
    return completed.stdout


def _git_changed_paths(root: Path, base_ref: str, head_ref: str) -> tuple[str, ...]:
    for label, ref in (("base-ref", base_ref), ("head-ref", head_ref)):
        if (
            not ref
            or "\x00" in ref
            or "\n" in ref
            or "\r" in ref
            or ref.startswith("-")
        ):
            raise CollaborationError(f"{label} is invalid")
    _git_output(root, ("rev-parse", "--verify", f"{head_ref}^{{commit}}"))
    if base_ref and set(base_ref) == {"0"}:
        output = _git_output(
            root,
            (
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                head_ref,
            ),
        )
    else:
        _git_output(root, ("rev-parse", "--verify", f"{base_ref}^{{commit}}"))
        output = _git_output(
            root,
            (
                "diff",
                "--name-only",
                "--diff-filter=ACMRDTUXB",
                "-z",
                f"{base_ref}...{head_ref}",
            ),
        )
    return tuple(value.decode("utf-8") for value in output.split(b"\0") if value)


def _changes_text(report: Any) -> str:
    lines = [f"change scope: {'OK' if report.ok else 'FAILED'}"]
    for key, paths in sorted(report.classes.items()):
        lines.append(f"- {key}: {len(paths)}")
        lines.extend(f"  {path}" for path in paths)
    for finding in report.errors:
        lines.append(f"ERROR {finding.code}: {finding.message}")
    for finding in report.warnings:
        lines.append(f"WARNING {finding.code}: {finding.message}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = tuple(sys.argv[1:])
        # A bare ``bmp-agent`` is a useful handoff command: show the queue
        # instead of making an agent learn a second spelling first.
        if Path(sys.argv[0]).name == "bmp-agent" and not argv:
            argv = ("next",)
    parser = _parser()
    args = parser.parse_args(argv)
    root = args.project_root.expanduser().resolve()
    try:
        repository = ExperimentRepository(root)
        if args.command == "validate":
            report = repository.validate()
            print(
                _json(report.as_dict())
                if args.format == "json"
                else _validation_text(report),
                end="",
            )
            return 0 if report.ok else 1
        if args.command == "validate-imports":
            report = validate_historical_imports(
                root,
                imports_dir=args.imports_dir,
            )
            print(
                _json(report.as_dict())
                if args.format == "json"
                else _import_validation_text(report),
                end="",
            )
            return 0 if report.ok else 1
        if args.command == "list":
            report = repository.validate()
            print(
                _json(report.as_dict())
                if args.format == "json"
                else _bundle_table(report),
                end="",
            )
            return 0 if report.ok else 1
        if args.command == "next":
            report = repository.validate()
            payload = {
                "available": [
                    item.as_dict() for item in report.bundles if item.available
                ],
                "blocked_or_owned": [
                    item.as_dict() for item in report.bundles if not item.available
                ],
                "format": "magentabench-agent-queue-v1",
                "ok": report.ok,
            }
            if args.format == "json":
                print(_json(payload), end="")
            else:
                if payload["available"]:
                    print("Available experiment work:")
                    for item in payload["available"]:
                        print(f"- {item['id']} ({item['lab_issue']})")
                else:
                    print("No experiment bundle is currently claimable.")
                if payload["blocked_or_owned"]:
                    print("Blocked or already-owned bundles:")
                    for item in payload["blocked_or_owned"]:
                        reason = (
                            f"status={item['lab_status']}, blockers={item['blocker_count']}, "
                            f"dependencies_complete={str(item['dependencies_complete']).lower()}"
                        )
                        print(f"- {item['id']} ({item['lab_issue']}): {reason}")
            return 0 if report.ok else 1
        if args.command == "modes":
            modes = repository.execution_modes()
            print(
                _json({"format": "magentabench-execution-modes-v1", "modes": modes})
                if args.format == "json"
                else _modes_table(modes),
                end="",
            )
            return 0
        if args.command == "ledger":
            ledger = build_experiment_ledger(
                root,
                path_map=parse_path_maps(args.map),
                imports_dir=args.imports_dir,
            )
            if args.format == "json":
                print(_json(ledger.as_dict()), end="")
            elif args.format == "csv":
                print(render_csv(ledger, args.table), end="")
            else:
                print(_ledger_table(getattr(ledger, args.table), args.table), end="")
            for finding in ledger.errors:
                print(
                    f"ERROR {finding['code']} [{finding['source']}]: "
                    f"{finding['message']}",
                    file=sys.stderr,
                )
            return 0 if ledger.ok else 1
        if args.command == "scaffold":
            path, changed = repository.scaffold(
                experiment_id=args.experiment_id,
                bmp_spec=args.bmp_spec,
                lab_issue=args.lab_issue,
                related_issues=tuple(args.related_issue),
                question=args.question,
                hypothesis=args.hypothesis,
                stop_conditions=tuple(args.stop_condition),
                required_env=tuple(args.required_env),
                primary_metrics=tuple(args.primary_metric),
            )
            print(
                _json(
                    {
                        "bundle": path.relative_to(root).as_posix(),
                        "changed": changed,
                        "experiment_id": args.experiment_id,
                    }
                ),
                end="",
            )
            return 0
        if args.command == "preflight":
            bundle = repository.authorize_preflight(
                args.experiment_id,
                actor=args.actor,
                environment=os.environ,
            )
            command = bundle.execution.preflight_argv
            if args.dry_run:
                print(
                    _json(
                        {
                            "actor": args.actor,
                            "argv": command,
                            "cwd": str(root),
                            "executed": False,
                        }
                    ),
                    end="",
                )
                return 0
            completed = subprocess.run(
                command,
                cwd=root,
                env={
                    **os.environ,
                    "BMP_LAB_ACTOR": args.actor,
                    "BMP_LAB_ISSUE_ID": bundle.lab_issue,
                },
                check=False,
            )
            return completed.returncode
        if args.command == "changes":
            paths = _git_changed_paths(root, args.base_ref, args.head_ref)
            report = classify_changed_paths(
                paths, allow_protocol_change=args.allow_protocol_change
            )
            print(
                _json(report.as_dict())
                if args.format == "json"
                else _changes_text(report),
                end="",
            )
            return 0 if report.ok else 1
        raise CollaborationError(f"unsupported command: {args.command}")
    except (CollaborationError, OSError) as exc:
        print(f"bmp-collab: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
