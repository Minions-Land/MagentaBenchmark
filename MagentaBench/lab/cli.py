"""Command-line control plane for recoverable multi-person benchmark work."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

from pydantic import ValidationError

from .models import (
    LabBlocker,
    LabBlockerCategory,
    LabCheckpoint,
    LabCriterion,
    LabEventKind,
    LabIssue,
    LabPriority,
    LabReview,
    LabReviewVerdict,
    LabRunLink,
    LabRunState,
    LabStatus,
    LabWriteScope,
)
from .store import (
    LabError,
    LabMutation,
    LabStore,
    artifact_ref_from_path,
    utc_now,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise argparse.ArgumentTypeError("timestamp must use the UTC offset")
    return parsed


def _parse_criterion(value: str) -> LabCriterion:
    criterion_id, separator, description = value.partition("=")
    if not separator or not criterion_id or not description:
        raise argparse.ArgumentTypeError("acceptance criteria must use ID=DESCRIPTION")
    try:
        return LabCriterion(criterion_id=criterion_id, description=description)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _store(args: argparse.Namespace) -> LabStore:
    return LabStore(args.project_root, args.lab_root)


def _git_snapshot(project_root: Path) -> tuple[str, str, tuple[str, ...]]:
    def run(*arguments: str, strip: bool = True) -> str:
        completed = subprocess.run(
            ("git", "-C", str(project_root), *arguments),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise LabError(
                f"cannot inspect Git state: {completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout.strip() if strip else completed.stdout

    head = run("rev-parse", "HEAD")
    branch = run("branch", "--show-current") or f"detached-{head[:12]}"
    porcelain = run(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        strip=False,
    )
    dirty: list[str] = []
    entries = porcelain.split("\x00")
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry:
            index += 1
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise LabError("cannot parse Git porcelain status for checkpoint")
        status, path = entry[:2], entry[3:]
        dirty.append(path)
        if "R" in status or "C" in status:
            index += 1
            if index >= len(entries) or not entries[index]:
                raise LabError("Git rename/copy status is missing its source path")
            dirty.append(entries[index])
        index += 1
    return head, branch, tuple(sorted(set(dirty)))


def _mutation_payload(result: LabMutation) -> dict[str, Any]:
    return {
        "changed": result.changed,
        "event_count": result.state.event_count,
        "issue_id": result.state.issue.issue_id,
        "record": str(result.record_path),
        "revision": result.state.revision,
        "status": result.state.status.value,
    }


def _append_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("issue_id")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--note")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmp-lab",
        description="Idempotent collaboration ledger for MagentaBench operations",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="MagentaBench project root (default: current directory)",
    )
    parser.add_argument(
        "--lab-root",
        type=Path,
        default=None,
        help="Override the lab ledger root (default: PROJECT_ROOT/lab)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create or verify the repository lab layout")

    open_parser = sub.add_parser("open", help="create an immutable issue definition")
    open_parser.add_argument("issue_id")
    open_parser.add_argument("--title", required=True)
    open_parser.add_argument("--objective", required=True)
    open_parser.add_argument("--actor", required=True)
    open_parser.add_argument("--owner")
    open_parser.add_argument(
        "--priority", choices=[value.value for value in LabPriority], default="p1"
    )
    open_parser.add_argument("--benchmark")
    open_parser.add_argument("--experiment")
    open_parser.add_argument("--label", action="append", default=[])
    open_parser.add_argument("--depends-on", action="append", default=[])
    open_parser.add_argument("--write-path", action="append", default=[])
    open_parser.add_argument("--resource", action="append", default=[])
    open_parser.add_argument(
        "--acceptance",
        action="append",
        type=_parse_criterion,
        required=True,
        help="acceptance criterion as ID=DESCRIPTION; repeat for each criterion",
    )

    status_parser = sub.add_parser("status", help="render the derived progress board")
    status_parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    status_parser.add_argument("--as-of", type=_parse_utc)

    show_parser = sub.add_parser("show", help="show one fully reduced issue state")
    show_parser.add_argument("issue_id")
    show_parser.add_argument("--as-of", type=_parse_utc)

    set_status = sub.add_parser("set-status", help="append a workflow status transition")
    _append_common(set_status)
    set_status.add_argument(
        "--status",
        required=True,
        choices=[
            value.value for value in LabStatus if value != LabStatus.blocked
        ],
        help="target workflow status; use the block command to enter blocked",
    )

    assign = sub.add_parser("assign", help="assign long-term issue ownership")
    _append_common(assign)
    assign.add_argument("--owner", required=True)

    claim = sub.add_parser("claim", help="acquire a short, scope-aware work lease")
    _append_common(claim)
    claim.add_argument("--holder", required=True)
    claim.add_argument("--lease-id", required=True)
    claim.add_argument("--ttl-seconds", type=int, default=14400)

    renew = sub.add_parser("renew", help="extend a matching active work lease")
    _append_common(renew)
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--ttl-seconds", type=int, default=14400)

    release = sub.add_parser("release", help="release a matching work lease")
    _append_common(release)
    release.add_argument("--lease-id", required=True)

    block = sub.add_parser("block", help="record a structured recoverable blocker")
    _append_common(block)
    block.add_argument("--blocker-id", required=True)
    block.add_argument(
        "--category",
        required=True,
        choices=[value.value for value in LabBlockerCategory],
    )
    block.add_argument("--summary", required=True)
    block.add_argument("--recovery-action", required=True)
    block.add_argument("--external-ref")
    block.add_argument("--expected")
    block.add_argument("--observed")
    block.add_argument(
        "--reproduce-arg",
        action="append",
        help=(
            "one reproduction argv element; repeat (use "
            "--reproduce-arg=--flag for leading dashes)"
        ),
    )
    block.add_argument("--exit-code", type=int)
    block.add_argument("--evidence", action="append", type=Path)
    block.add_argument("--unblock-condition")

    resolve = sub.add_parser("resolve-blocker", help="resolve a named blocker")
    _append_common(resolve)
    resolve.add_argument("--blocker-id", required=True)

    checkpoint = sub.add_parser(
        "checkpoint", help="record a structured, content-bound recovery checkpoint"
    )
    _append_common(checkpoint)
    checkpoint.add_argument("--experiment")
    checkpoint.add_argument("--record-root")
    checkpoint.add_argument(
        "--resume-arg",
        action="append",
        required=True,
        help="one argv element; repeat (use --resume-arg=--flag for leading dashes)",
    )
    checkpoint.add_argument("--require-env", action="append", default=[])
    checkpoint.add_argument("--next-action", required=True)
    checkpoint.add_argument("--artifact", action="append", type=Path, default=[])
    checkpoint.add_argument(
        "--patch",
        type=Path,
        help="reviewed UTF-8 patch required when the Git worktree is dirty",
    )

    link_run = sub.add_parser("link-run", help="bind an operational run to an issue")
    link_run.add_argument("issue_id")
    link_run.add_argument("--event-id", required=True)
    link_run.add_argument("--actor", required=True)
    link_run.add_argument("--run-id", required=True)
    link_run.add_argument("--state", required=True, choices=[value.value for value in LabRunState])
    link_run.add_argument("--record-root", required=True)
    link_run.add_argument("--manifest-digest")
    link_run.add_argument("--report", type=Path)
    link_run.add_argument("--note")

    review = sub.add_parser("review", help="append a review over explicit acceptance criteria")
    _append_common(review)
    review.add_argument(
        "--verdict", required=True, choices=[value.value for value in LabReviewVerdict]
    )
    review.add_argument("--summary", required=True)
    review.add_argument("--accept-criterion", action="append", default=[])
    review.add_argument("--evidence", action="append", type=Path, default=[])

    note = sub.add_parser("note", help="append an immutable progress or decision note")
    note.add_argument("issue_id")
    note.add_argument("--event-id", required=True)
    note.add_argument("--actor", required=True)
    note.add_argument("--note", required=True)

    recover = sub.add_parser("recover", help="verify and print a recovery plan without executing it")
    recover.add_argument("issue_id")
    recover.add_argument("--as-of", type=_parse_utc)

    doctor = sub.add_parser("doctor", help="validate all issue chains and collaboration invariants")
    doctor.add_argument("--as-of", type=_parse_utc)
    return parser


def _markdown_board(board: dict[str, Any]) -> str:
    def cell(value: object) -> str:
        if value is None:
            return "-"
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "# MagentaBench Lab Status",
        "",
        "| Priority | Status | Issue | Owner | Lease | Blockers | Updated | Title |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for item in board["issues"]:
        lines.append(
            "| "
            + " | ".join(
                cell(value)
                for value in (
                    item["priority"],
                    item["status"],
                    item["issue_id"],
                    item["owner"],
                    item["lease_holder"],
                    item["blocker_count"],
                    item["updated_at"],
                    item["title"],
                )
            )
            + " |"
        )
    counts = ", ".join(f"{key}={value}" for key, value in board["counts"].items())
    lines.extend(("", f"Issues: {board['issue_count']} ({counts})", ""))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    args.project_root = args.project_root.expanduser().resolve()
    if args.lab_root is not None:
        args.lab_root = args.lab_root.expanduser().resolve()
    try:
        store = _store(args)
        if args.command == "init":
            print(_json({"changed": False, "lab_root": str(store.root), **store.doctor()}), end="")
            return 0
        if args.command == "open":
            issue = LabIssue(
                issue_id=args.issue_id,
                title=args.title,
                objective=args.objective,
                priority=LabPriority(args.priority),
                created_by=args.actor,
                created_at=utc_now(),
                owner=args.owner,
                benchmark=args.benchmark,
                experiment=args.experiment,
                labels=tuple(args.label),
                dependencies=tuple(args.depends_on),
                write_scope=LabWriteScope(
                    paths=tuple(args.write_path), resources=tuple(args.resource)
                ),
                acceptance_criteria=tuple(args.acceptance),
            )
            print(_json(_mutation_payload(store.open_issue(issue))), end="")
            return 0
        if args.command == "status":
            board = store.board(at=args.as_of)
            print(_json(board) if args.format == "json" else _markdown_board(board), end="")
            return 0
        if args.command == "show":
            state = store.load(args.issue_id)
            at = args.as_of or utc_now()
            payload = state.model_dump(mode="json", exclude_none=True)
            payload["lease_active"] = state.active_lease(at) is not None
            print(_json(payload), end="")
            return 0
        if args.command == "set-status":
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.status,
                args.actor,
                status=LabStatus(args.status),
                note=args.note,
            )
        elif args.command == "assign":
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.assign,
                args.actor,
                owner=args.owner,
                note=args.note,
            )
        elif args.command == "claim":
            head, branch, _ = _git_snapshot(args.project_root)
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.claim,
                args.actor,
                owner=args.holder,
                lease_id=args.lease_id,
                lease_ttl_seconds=args.ttl_seconds,
                lease_base_commit=head,
                lease_branch=branch,
                note=args.note,
            )
        elif args.command == "renew":
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.renew,
                args.actor,
                lease_id=args.lease_id,
                lease_ttl_seconds=args.ttl_seconds,
                note=args.note,
            )
        elif args.command == "release":
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.release,
                args.actor,
                lease_id=args.lease_id,
                note=args.note,
            )
        elif args.command == "block":
            blocker_evidence = (
                None
                if args.evidence is None
                else tuple(
                    artifact_ref_from_path(path, project_root=args.project_root)
                    for path in args.evidence
                )
            )
            blocker = LabBlocker(
                blocker_id=args.blocker_id,
                category=LabBlockerCategory(args.category),
                summary=args.summary,
                recovery_action=args.recovery_action,
                external_ref=args.external_ref,
                expected=args.expected,
                observed=args.observed,
                reproduce_argv=(
                    None
                    if args.reproduce_arg is None
                    else tuple(args.reproduce_arg)
                ),
                exit_code=args.exit_code,
                evidence_refs=blocker_evidence,
                unblock_condition=args.unblock_condition,
            )
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.block,
                args.actor,
                blocker=blocker,
                note=args.note,
            )
        elif args.command == "resolve-blocker":
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.resolve_blocker,
                args.actor,
                blocker_id=args.blocker_id,
                note=args.note,
            )
        elif args.command == "checkpoint":
            head, branch, dirty_paths = _git_snapshot(args.project_root)
            patch_ref = (
                None
                if args.patch is None
                else artifact_ref_from_path(
                    args.patch, project_root=args.project_root, scan_text=True
                )
            )
            artifact_refs = tuple(
                artifact_ref_from_path(path, project_root=args.project_root)
                for path in args.artifact
            )
            checkpoint_record = LabCheckpoint(
                git_head=head,
                git_branch=branch,
                worktree_clean=not dirty_paths,
                dirty_paths=dirty_paths,
                experiment=args.experiment or store.load(args.issue_id).issue.experiment,
                record_root=args.record_root,
                resume_argv=tuple(args.resume_arg),
                required_env=tuple(args.require_env),
                next_action=args.next_action,
                artifact_refs=artifact_refs,
                patch_ref=patch_ref,
            )
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.checkpoint,
                args.actor,
                checkpoint=checkpoint_record,
                note=args.note,
            )
        elif args.command == "link-run":
            report_ref = (
                None
                if args.report is None
                else artifact_ref_from_path(args.report, project_root=args.project_root)
            )
            run = LabRunLink(
                run_id=args.run_id,
                state=LabRunState(args.state),
                record_root=args.record_root,
                manifest_digest=args.manifest_digest,
                report_ref=report_ref,
                note=args.note,
            )
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.link_run,
                args.actor,
                run=run,
            )
        elif args.command == "review":
            review_record = LabReview(
                verdict=LabReviewVerdict(args.verdict),
                summary=args.summary,
                accepted_criteria=tuple(args.accept_criterion),
                evidence_refs=tuple(
                    artifact_ref_from_path(path, project_root=args.project_root)
                    for path in args.evidence
                ),
            )
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.review,
                args.actor,
                review=review_record,
                note=args.note,
            )
        elif args.command == "note":
            result = store.append_event(
                args.issue_id,
                args.event_id,
                LabEventKind.note,
                args.actor,
                note=args.note,
            )
        elif args.command == "recover":
            print(_json(store.recovery_view(args.issue_id, at=args.as_of)), end="")
            return 0
        elif args.command == "doctor":
            result_payload = store.doctor(at=args.as_of)
            print(_json(result_payload), end="")
            return 0 if result_payload["ok"] else 1
        else:  # pragma: no cover - argparse makes this unreachable
            raise LabError(f"unsupported lab command: {args.command}")
        print(_json(_mutation_payload(result)), end="")
        return 0
    except (LabError, ValidationError, OSError, ValueError) as exc:
        print(f"bmp-lab: {exc}", file=sys.stderr)
        return 2


__all__ = ["main"]
