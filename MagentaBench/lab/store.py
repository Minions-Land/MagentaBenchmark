"""Immutable, hash-chained storage for repository laboratory operations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

try:  # pragma: no cover - exercised on POSIX hosts
    import fcntl
except ImportError:  # pragma: no cover - unsupported platforms fail closed
    fcntl = None  # type: ignore[assignment]

from .models import (
    LAB_ID_PATTERN,
    LabArtifactRef,
    LabEvent,
    LabEventKind,
    LabIssue,
    LabIssueState,
    LabLease,
    LabRunLink,
    LabRunState,
    LabReviewVerdict,
    LabStatus,
    LabWriteScope,
)


class LabError(RuntimeError):
    """Base error for the repository laboratory control plane."""


class LabNotFoundError(LabError):
    """A requested immutable issue does not exist."""


class LabConflictError(LabError):
    """An idempotency key, lease, or hash-chain precondition conflicted."""


class LabDriftError(LabError):
    """Persisted lab bytes or paths violate their immutable contract."""


@dataclass(frozen=True)
class LabMutation:
    state: LabIssueState
    changed: bool
    record_path: Path


_Model = TypeVar("_Model", bound=BaseModel)
_GIT_HISTORY_TIMEOUT_SECONDS = 10
_GIT_OBJECT_ID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_GIT_HISTORY_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": os.environ.get("PATH", os.defpath),
}
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9/+_.${}-]{8,}"
    ),
    re.compile(r"https?://[^/\s:@]+:[^/@\s]+@"),
)

_STATUS_TRANSITIONS: dict[LabStatus, frozenset[LabStatus]] = {
    LabStatus.open: frozenset(
        {LabStatus.planned, LabStatus.ready, LabStatus.blocked, LabStatus.cancelled}
    ),
    LabStatus.planned: frozenset(
        {LabStatus.ready, LabStatus.blocked, LabStatus.cancelled}
    ),
    LabStatus.ready: frozenset(
        {LabStatus.running, LabStatus.planned, LabStatus.blocked, LabStatus.cancelled}
    ),
    LabStatus.running: frozenset(
        {LabStatus.verifying, LabStatus.ready, LabStatus.blocked, LabStatus.cancelled}
    ),
    LabStatus.verifying: frozenset(
        {LabStatus.done, LabStatus.running, LabStatus.blocked, LabStatus.cancelled}
    ),
    LabStatus.blocked: frozenset(
        {LabStatus.planned, LabStatus.ready, LabStatus.running, LabStatus.cancelled}
    ),
    LabStatus.done: frozenset(),
    LabStatus.cancelled: frozenset(),
}

_RUN_TRANSITIONS: dict[LabRunState | None, frozenset[LabRunState]] = {
    None: frozenset(
        {
            LabRunState.planned,
            LabRunState.running,
            LabRunState.failed,
            LabRunState.invalid,
            LabRunState.cancelled,
        }
    ),
    LabRunState.planned: frozenset(
        {LabRunState.running, LabRunState.failed, LabRunState.cancelled}
    ),
    LabRunState.running: frozenset(
        {
            LabRunState.finished,
            LabRunState.failed,
            LabRunState.invalid,
            LabRunState.cancelled,
        }
    ),
    LabRunState.failed: frozenset({LabRunState.running, LabRunState.cancelled}),
    LabRunState.invalid: frozenset(),
    LabRunState.finished: frozenset(),
    LabRunState.cancelled: frozenset(),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _next_revision(previous_revision: str, event_bytes: bytes) -> str:
    return hashlib.sha256(
        previous_revision.encode("ascii") + b"\n" + hashlib.sha256(event_bytes).digest()
    ).hexdigest()


def _validate_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(LAB_ID_PATTERN, value) is None:
        raise LabError(f"{label} must be a normalized lowercase identifier")
    return value


def _reject_secret_material(value: Any, *, path: str = "record") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_secret_material(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_material(child, path=f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise LabError(f"lab record appears to contain secret material at {path}")


def _path_overlap(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    limit = min(len(left_parts), len(right_parts))
    return left_parts[:limit] == right_parts[:limit]


def _scope_conflicts(left: LabWriteScope, right: LabWriteScope) -> bool:
    if set(left.resources).intersection(right.resources):
        return True
    return any(_path_overlap(a, b) for a in left.paths for b in right.paths)


def _directory_fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _absolute_path(path: str | Path) -> Path:
    """Return a lexical absolute path without hiding symlink components."""

    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _reject_symlink_ancestors(path: Path, *, label: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise LabDriftError(f"{label} contains a symlink: {candidate}")


def _git_history_contains_artifact(
    project_root: Path,
    locator: str,
    ref: LabArtifactRef,
) -> bool:
    """Return whether complete ``HEAD`` ancestry contains the exact path bytes."""

    try:
        relative = PurePosixPath(locator)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            return False
        top_level = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=project_root,
            env=_GIT_HISTORY_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            timeout=_GIT_HISTORY_TIMEOUT_SECONDS,
        )
        if top_level.returncode != 0:
            return False
        try:
            observed_root = Path(top_level.stdout.strip()).resolve(strict=True)
            expected_root = project_root.resolve(strict=True)
        except OSError:
            return False
        if observed_root != expected_root:
            return False
        shallow = subprocess.run(
            ("git", "rev-parse", "--is-shallow-repository"),
            cwd=project_root,
            env=_GIT_HISTORY_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            timeout=_GIT_HISTORY_TIMEOUT_SECONDS,
        )
        if shallow.returncode != 0 or shallow.stdout.strip() != "false":
            return False
        revisions = subprocess.run(
            ("git", "rev-list", "--first-parent", "HEAD", "--", locator),
            cwd=project_root,
            env=_GIT_HISTORY_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            timeout=_GIT_HISTORY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    if revisions.returncode != 0:
        return False
    for revision in revisions.stdout.splitlines():
        if _GIT_OBJECT_ID_PATTERN.fullmatch(revision) is None:
            return False
        try:
            blob = subprocess.run(
                ("git", "show", f"{revision}:{locator}"),
                cwd=project_root,
                env=_GIT_HISTORY_ENV,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_GIT_HISTORY_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if (
            blob.returncode == 0
            and len(blob.stdout) == ref.size_bytes
            and _sha256(blob.stdout) == ref.sha256
        ):
            return True
    return False


def artifact_ref_from_path(
    path: str | Path,
    *,
    project_root: str | Path | None = None,
    scan_text: bool = False,
) -> LabArtifactRef:
    lexical = _absolute_path(path)
    _reject_symlink_ancestors(lexical, label="lab artifact path")
    resolved = lexical.resolve(strict=True)
    if not resolved.is_file():
        raise LabError(f"lab artifact must be a regular non-symlink file: {resolved}")
    content = resolved.read_bytes()
    if scan_text:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LabError(f"text lab artifact is not UTF-8: {resolved}") from exc
        _reject_secret_material(text, path=str(resolved))
    locator = str(resolved)
    if project_root is not None:
        root = Path(project_root).expanduser().resolve()
        try:
            locator = resolved.relative_to(root).as_posix()
        except ValueError:
            pass
    return LabArtifactRef(
        locator=locator,
        sha256=_sha256(content),
        size_bytes=len(content),
    )


def _atomic_create(path: Path, content: bytes) -> bool:
    """Create immutable bytes atomically; return False for identical existing bytes."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise LabDriftError(
                    f"existing immutable lab record is non-regular or a symlink: {path}"
                )
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise LabDriftError(f"existing immutable lab record is unreadable: {path}") from exc
            if existing != content:
                raise LabConflictError(f"immutable lab record already exists with other bytes: {path}")
            return False
        _directory_fsync(path.parent)
        return True
    finally:
        removed = False
        try:
            temporary.unlink()
            removed = True
        except FileNotFoundError:
            pass
        if removed:
            _directory_fsync(path.parent)


class LabStore:
    """Hash-chain reducer and transactional writer for ``lab/issues``."""

    def __init__(self, project_root: str | Path, lab_root: str | Path | None = None) -> None:
        self.project_root = _absolute_path(project_root)
        _reject_symlink_ancestors(self.project_root, label="lab project path")
        selected = Path(lab_root) if lab_root is not None else self.project_root / "lab"
        self.root = _absolute_path(selected)
        _reject_symlink_ancestors(self.root, label="lab root path")
        self.issues_root = self.root / "issues"
        self.lock_path = self.root / ".lab.lock"
        self._thread_lock = threading.RLock()
        self._initialize_layout()
        self._root_identity = self._path_identity(self.root)
        self._issues_identity = self._path_identity(self.issues_root)
        self._lock_identity = self._path_identity(self.lock_path)

    def _initialize_layout(self) -> None:
        for path in (self.root, self.issues_root):
            if path.is_symlink():
                raise LabDriftError(f"lab path must not be a symlink: {path}")
            if path.exists() and not path.is_dir():
                raise LabDriftError(f"lab path must be a directory: {path}")
            path.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_symlink():
            raise LabDriftError("lab lock path must not be a symlink")
        if self.lock_path.exists() and not self.lock_path.is_file():
            raise LabDriftError("lab lock path is not a regular file")
        self.lock_path.touch(exist_ok=True)

    @staticmethod
    def _path_identity(path: Path) -> tuple[int, int]:
        stat = path.stat(follow_symlinks=False)
        return stat.st_dev, stat.st_ino

    def _assert_layout(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise LabDriftError("lab root path drift")
        if self.issues_root.is_symlink() or not self.issues_root.is_dir():
            raise LabDriftError("lab issues path drift")
        if self.lock_path.is_symlink() or not self.lock_path.is_file():
            raise LabDriftError("lab lock path drift")
        if self._path_identity(self.root) != self._root_identity:
            raise LabDriftError("lab root path identity drift")
        if self._path_identity(self.issues_root) != self._issues_identity:
            raise LabDriftError("lab issues path identity drift")
        if self._path_identity(self.lock_path) != self._lock_identity:
            raise LabDriftError("lab lock path identity drift")

    @contextmanager
    def _process_lock(self, *, exclusive: bool):
        self._assert_layout()
        if fcntl is None:
            raise LabDriftError(
                "lab operations require POSIX fcntl locking; refusing an unsafe fallback"
            )
        handle = self.lock_path.open("r+b")
        try:
            try:
                fcntl.flock(
                    handle.fileno(),
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )
            except OSError as exc:
                raise LabDriftError("lab process lock acquisition failed") from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError as exc:
                    raise LabDriftError("lab process lock release failed") from exc
        except OSError as exc:
            raise LabDriftError("lab operation failed while holding the process lock") from exc
        finally:
            handle.close()

    def _issue_dir(self, issue_id: str) -> Path:
        normalized = _validate_id(issue_id, label="issue id")
        return self.issues_root / normalized

    def _event_path(self, issue_id: str, event_id: str) -> Path:
        normalized = _validate_id(event_id, label="event id")
        return self._issue_dir(issue_id) / "events" / f"{normalized}.json"

    @staticmethod
    def _read_model(path: Path, model: type[_Model]) -> tuple[_Model, bytes]:
        if path.is_symlink() or not path.is_file():
            raise LabDriftError(f"lab record is missing, non-regular, or a symlink: {path}")
        try:
            content = path.read_bytes()
            record = model.model_validate_json(content, strict=True)
        except (OSError, ValidationError, ValueError) as exc:
            raise LabDriftError(f"lab record is unreadable or malformed: {path}: {exc}") from exc
        canonical = canonical_json_bytes(record)
        if canonical != content:
            raise LabDriftError(f"lab record is not canonical JSON: {path}")
        _reject_secret_material(record.model_dump(mode="json", exclude_none=True), path=str(path))
        return record, content

    @staticmethod
    def _issue_request(issue: LabIssue) -> dict[str, Any]:
        return issue.model_dump(mode="json", exclude={"created_at"}, exclude_none=True)

    def open_issue(self, issue: LabIssue) -> LabMutation:
        _reject_secret_material(issue.model_dump(mode="json", exclude_none=True))
        issue_dir = self._issue_dir(issue.issue_id)
        issue_path = issue_dir / "issue.json"
        events_dir = issue_dir / "events"
        with self._thread_lock:
            with self._process_lock(exclusive=True):
                if issue_dir.is_symlink():
                    raise LabDriftError(f"issue directory is a symlink: {issue_dir}")
                issue_dir_was_missing = not issue_dir.exists()
                issue_dir.mkdir(exist_ok=True)
                if issue_dir_was_missing:
                    _directory_fsync(self.issues_root)
                if events_dir.is_symlink():
                    raise LabDriftError(f"event directory is a symlink: {events_dir}")
                events_dir.mkdir(exist_ok=True)
                changed = False
                if issue_path.exists():
                    existing, _ = self._read_model(issue_path, LabIssue)
                    if self._issue_request(existing) != self._issue_request(issue):
                        raise LabConflictError(
                            f"issue id {issue.issue_id!r} already has a different definition"
                        )
                else:
                    unknown = set(issue_dir.iterdir()) - {events_dir}
                    if unknown:
                        raise LabDriftError(
                            f"partial issue directory contains unexpected paths: {issue_dir}"
                        )
                    _atomic_create(issue_path, canonical_json_bytes(issue))
                    changed = True
                return LabMutation(
                    state=self._load_unlocked(issue.issue_id),
                    changed=changed,
                    record_path=issue_path,
                )

    def _event_files(self, issue_dir: Path) -> tuple[Path, ...]:
        if issue_dir.is_symlink() or not issue_dir.is_dir():
            raise LabDriftError(f"issue directory path drift: {issue_dir}")
        allowed = {"issue.json", "events"}
        unknown = {path.name for path in issue_dir.iterdir()} - allowed
        if unknown:
            raise LabDriftError(
                f"issue directory contains unexpected entries {sorted(unknown)}: {issue_dir}"
            )
        events_dir = issue_dir / "events"
        if events_dir.is_symlink() or not events_dir.is_dir():
            raise LabDriftError(f"event directory path drift: {events_dir}")
        paths = tuple(sorted(events_dir.iterdir(), key=lambda value: value.name))
        for path in paths:
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise LabDriftError(f"event path is not a regular JSON file: {path}")
        return paths

    @staticmethod
    def _active_at(lease: LabLease | None, at: datetime) -> LabLease | None:
        return lease if lease is not None and lease.expires_at > at else None

    def _apply_event(self, state: LabIssueState, event: LabEvent) -> LabIssueState:
        if event.created_at < state.updated_at:
            raise LabConflictError(
                f"lab event timestamp moves backwards for {event.issue_id}: "
                f"{event.created_at.isoformat()} < {state.updated_at.isoformat()}"
            )
        status = state.status
        owner = state.owner
        lease = state.lease
        active_lease = self._active_at(lease, event.created_at)
        blockers = {item.blocker_id: item for item in state.blockers}
        checkpoint = state.latest_checkpoint
        runs = {item.run_id: item for item in state.runs}
        review = state.latest_review

        controlled_kinds = {
            LabEventKind.status,
            LabEventKind.assign,
            LabEventKind.claim,
            LabEventKind.block,
            LabEventKind.resolve_blocker,
            LabEventKind.checkpoint,
            LabEventKind.link_run,
            LabEventKind.review,
        }
        if event.kind in controlled_kinds:
            authorized = {owner if owner is not None else state.issue.created_by}
            if active_lease is not None:
                authorized = {active_lease.owner}
            elif (
                owner is None
                and event.kind in {LabEventKind.assign, LabEventKind.claim}
                and event.owner == event.actor
            ):
                authorized.add(event.actor)
            if event.actor not in authorized:
                raise LabConflictError(
                    f"event {event.kind.value} requires the active lease holder or issue owner"
                )

        if event.kind == LabEventKind.status:
            assert event.status is not None
            if event.status == LabStatus.blocked:
                raise LabConflictError(
                    "blocked status must be entered with a structured block event"
                )
            if event.status not in _STATUS_TRANSITIONS[status]:
                raise LabConflictError(
                    f"invalid lab status transition for {event.issue_id}: "
                    f"{status.value} -> {event.status.value}"
                )
            if event.status in {LabStatus.running, LabStatus.verifying}:
                if active_lease is None or active_lease.owner != event.actor:
                    raise LabConflictError(
                        f"status {event.status.value} requires an active lease held by actor"
                    )
            if (
                status == LabStatus.blocked
                and blockers
                and event.status != LabStatus.cancelled
            ):
                raise LabConflictError(
                    "all blockers must be resolved before leaving blocked status"
                )
            if event.status == LabStatus.verifying and checkpoint is None:
                raise LabConflictError(
                    "verifying status requires a recovery checkpoint"
                )
            if event.status == LabStatus.done:
                if blockers:
                    raise LabConflictError("done status requires all blockers to be resolved")
                criteria = {
                    item.criterion_id for item in state.issue.acceptance_criteria
                }
                if (
                    review is None
                    or review.verdict != LabReviewVerdict.approved
                    or set(review.accepted_criteria) != criteria
                ):
                    raise LabConflictError(
                        "done status requires an approved review covering every acceptance criterion"
                    )
                if lease is not None:
                    raise LabConflictError("done status requires the active lease to be released")
                if checkpoint is None:
                    raise LabConflictError("done status requires a recovery checkpoint")
            if status == LabStatus.verifying and event.status != LabStatus.done:
                review = None
            status = event.status
        elif event.kind == LabEventKind.assign:
            if status in {LabStatus.done, LabStatus.cancelled}:
                raise LabConflictError("terminal issue ownership is immutable")
            assert event.owner is not None
            owner = event.owner
            review = None
        elif event.kind == LabEventKind.claim:
            if status in {LabStatus.done, LabStatus.cancelled}:
                raise LabConflictError("terminal issue cannot be claimed")
            if active_lease is not None:
                raise LabConflictError(
                    f"issue already has active lease {active_lease.lease_id!r} "
                    f"held by {active_lease.owner!r}"
                )
            assert event.owner is not None
            assert event.lease_id is not None
            assert event.lease_ttl_seconds is not None
            assert event.lease_base_commit is not None
            assert event.lease_branch is not None
            lease = LabLease(
                lease_id=event.lease_id,
                owner=event.owner,
                acquired_at=event.created_at,
                expires_at=event.created_at + timedelta(seconds=event.lease_ttl_seconds),
                event_id=event.event_id,
                base_commit=event.lease_base_commit,
                branch=event.lease_branch,
                write_scope=state.issue.write_scope,
            )
            review = None
        elif event.kind == LabEventKind.renew:
            assert event.lease_id is not None
            assert event.lease_ttl_seconds is not None
            if (
                active_lease is None
                or active_lease.lease_id != event.lease_id
                or active_lease.owner != event.actor
            ):
                raise LabConflictError("lease renewal requires the matching active holder")
            expires_at = event.created_at + timedelta(seconds=event.lease_ttl_seconds)
            if expires_at <= active_lease.expires_at:
                raise LabConflictError("lease renewal must extend the existing expiry")
            lease = active_lease.model_copy(
                update={"expires_at": expires_at, "event_id": event.event_id}
            )
        elif event.kind == LabEventKind.release:
            assert event.lease_id is not None
            persisted = state.lease
            if persisted is None or persisted.lease_id != event.lease_id:
                raise LabConflictError("lease release does not match the current lease")
            authorized = {persisted.owner}
            if owner is not None:
                authorized.add(owner)
            if event.actor not in authorized:
                raise LabConflictError("lease release requires its holder or issue owner")
            lease = None
        elif event.kind == LabEventKind.block:
            if status in {LabStatus.done, LabStatus.cancelled}:
                raise LabConflictError("terminal issue cannot gain a blocker")
            assert event.blocker is not None
            if event.blocker.blocker_id in blockers:
                raise LabConflictError(
                    f"blocker already exists: {event.blocker.blocker_id}"
                )
            blockers[event.blocker.blocker_id] = event.blocker
            status = LabStatus.blocked
            review = None
        elif event.kind == LabEventKind.resolve_blocker:
            assert event.blocker_id is not None
            if event.blocker_id not in blockers:
                raise LabConflictError(f"blocker does not exist: {event.blocker_id}")
            del blockers[event.blocker_id]
            review = None
        elif event.kind == LabEventKind.checkpoint:
            if status in {LabStatus.running, LabStatus.verifying} and (
                active_lease is None or active_lease.owner != event.actor
            ):
                raise LabConflictError("active work checkpoint requires the matching lease holder")
            assert event.checkpoint is not None
            checkpoint = event.checkpoint
            review = None
        elif event.kind == LabEventKind.link_run:
            assert event.run is not None
            previous = runs.get(event.run.run_id)
            previous_state = None if previous is None else previous.state
            if previous is not None:
                if event.run.record_root != previous.record_root:
                    raise LabConflictError(
                        f"lab run {event.run.run_id} cannot change record root"
                    )
                if (
                    previous.manifest_digest is not None
                    and event.run.manifest_digest != previous.manifest_digest
                ):
                    raise LabConflictError(
                        f"lab run {event.run.run_id} cannot remove or change manifest digest"
                    )
            for other_run_id, other_run in runs.items():
                if (
                    other_run_id != event.run.run_id
                    and other_run.record_root == event.run.record_root
                ):
                    raise LabConflictError(
                        f"record root is already bound to lab run {other_run_id}"
                    )
            if event.run.state not in _RUN_TRANSITIONS[previous_state]:
                before = "none" if previous_state is None else previous_state.value
                raise LabConflictError(
                    f"invalid lab run transition for {event.run.run_id}: "
                    f"{before} -> {event.run.state.value}"
                )
            runs[event.run.run_id] = event.run
            review = None
        elif event.kind == LabEventKind.review:
            assert event.review is not None
            declared = {
                item.criterion_id for item in state.issue.acceptance_criteria
            }
            accepted = set(event.review.accepted_criteria)
            if not accepted.issubset(declared):
                raise LabConflictError("review names undeclared acceptance criteria")
            if event.review.verdict == LabReviewVerdict.approved:
                if status != LabStatus.verifying:
                    raise LabConflictError("approved review requires verifying status")
                if blockers:
                    raise LabConflictError("approved review requires all blockers resolved")
                if checkpoint is None:
                    raise LabConflictError("approved review requires a recovery checkpoint")
                if accepted != declared:
                    raise LabConflictError(
                        "approved review must cover every acceptance criterion"
                    )
            review = event.review
        elif event.kind != LabEventKind.note:  # pragma: no cover - enum exhaustiveness
            raise LabConflictError(f"unsupported lab event kind: {event.kind}")

        return state.model_copy(
            update={
                "status": status,
                "owner": owner,
                "lease": lease,
                "blockers": tuple(blockers[key] for key in sorted(blockers)),
                "latest_checkpoint": checkpoint,
                "runs": tuple(runs[key] for key in sorted(runs)),
                "latest_review": review,
                "updated_at": event.created_at,
            }
        )

    def _load_unlocked(self, issue_id: str) -> LabIssueState:
        issue_dir = self._issue_dir(issue_id)
        if not issue_dir.exists():
            raise LabNotFoundError(f"lab issue does not exist: {issue_id}")
        issue_record, issue_bytes = self._read_model(issue_dir / "issue.json", LabIssue)
        if issue_record.issue_id != issue_id:
            raise LabDriftError(f"issue directory and record id disagree: {issue_dir}")

        parsed: list[tuple[LabEvent, bytes, Path]] = []
        for path in self._event_files(issue_dir):
            event, content = self._read_model(path, LabEvent)
            if path.stem != event.event_id:
                raise LabDriftError(f"event filename and id disagree: {path}")
            if event.issue_id != issue_id:
                raise LabDriftError(f"event issue id drift: {path}")
            parsed.append((event, content, path))
        parsed.sort(key=lambda item: (item[0].sequence, item[0].event_id))
        sequences = [item[0].sequence for item in parsed]
        expected = list(range(1, len(parsed) + 1))
        if sequences != expected:
            raise LabConflictError(
                f"lab event chain for {issue_id} is forked or has a gap: "
                f"observed={sequences}, expected={expected}"
            )

        revision = _sha256(issue_bytes)
        state = LabIssueState(
            issue=issue_record,
            status=LabStatus.open,
            owner=issue_record.owner,
            revision=revision,
            event_count=0,
            updated_at=issue_record.created_at,
        )
        for event, content, path in parsed:
            if event.previous_revision != revision:
                raise LabConflictError(
                    f"lab event previous revision mismatch at {path}: "
                    f"expected {revision}, got {event.previous_revision}"
                )
            state = self._apply_event(state, event)
            revision = _next_revision(revision, content)
            state = state.model_copy(
                update={"revision": revision, "event_count": event.sequence}
            )
        return state

    def load(self, issue_id: str) -> LabIssueState:
        with self._thread_lock:
            with self._process_lock(exclusive=False):
                return self._load_unlocked(issue_id)

    def _list_unlocked(self) -> tuple[LabIssueState, ...]:
        states: list[LabIssueState] = []
        for path in sorted(self.issues_root.iterdir(), key=lambda value: value.name):
            if path.is_symlink() or not path.is_dir():
                raise LabDriftError(f"lab issue entry is not a regular directory: {path}")
            _validate_id(path.name, label="issue directory")
            states.append(self._load_unlocked(path.name))
        return tuple(states)

    def list(self) -> tuple[LabIssueState, ...]:
        with self._thread_lock:
            with self._process_lock(exclusive=False):
                return self._list_unlocked()

    def append_event(
        self,
        issue_id: str,
        event_id: str,
        kind: LabEventKind | str,
        actor: str,
        *,
        created_at: datetime | None = None,
        **fields: Any,
    ) -> LabMutation:
        normalized_kind = LabEventKind(kind)
        if normalized_kind == LabEventKind.status and "status" in fields:
            fields["status"] = LabStatus(fields["status"])
        event_path = self._event_path(issue_id, event_id)
        timestamp = created_at or utc_now()
        with self._thread_lock:
            with self._process_lock(exclusive=True):
                state = self._load_unlocked(issue_id)
                if event_path.exists():
                    existing, _ = self._read_model(event_path, LabEvent)
                    candidate = LabEvent(
                        issue_id=issue_id,
                        event_id=event_id,
                        sequence=existing.sequence,
                        previous_revision=existing.previous_revision,
                        kind=normalized_kind,
                        actor=actor,
                        created_at=existing.created_at,
                        **fields,
                    )
                    if candidate.request_identity() != existing.request_identity():
                        raise LabConflictError(
                            f"event id {event_id!r} was already used for another operation"
                        )
                    return LabMutation(
                        state=state,
                        changed=False,
                        record_path=event_path,
                    )

                event = LabEvent(
                    issue_id=issue_id,
                    event_id=event_id,
                    sequence=state.event_count + 1,
                    previous_revision=state.revision,
                    kind=normalized_kind,
                    actor=actor,
                    created_at=timestamp,
                    **fields,
                )
                _reject_secret_material(event.model_dump(mode="json", exclude_none=True))
                if event.kind == LabEventKind.status and event.status in {
                    LabStatus.ready,
                    LabStatus.running,
                    LabStatus.verifying,
                    LabStatus.done,
                }:
                    by_id = {
                        item.issue.issue_id: item for item in self._list_unlocked()
                    }
                    incomplete = [
                        dependency
                        for dependency in state.issue.dependencies
                        if dependency not in by_id
                        or by_id[dependency].status != LabStatus.done
                    ]
                    if incomplete:
                        raise LabConflictError(
                            f"status {event.status.value} requires completed dependencies: "
                            + ", ".join(sorted(incomplete))
                        )
                if event.kind == LabEventKind.claim:
                    for other in self._list_unlocked():
                        if other.issue.issue_id == issue_id:
                            continue
                        active = other.active_lease(event.created_at)
                        if active is not None and _scope_conflicts(
                            state.issue.write_scope, active.write_scope
                        ):
                            raise LabConflictError(
                                f"write scope conflicts with active lease {active.lease_id!r} "
                                f"on issue {other.issue.issue_id!r}"
                            )
                if event.kind == LabEventKind.link_run:
                    assert event.run is not None
                    for other in self._list_unlocked():
                        if other.issue.issue_id == issue_id:
                            continue
                        for linked in other.runs:
                            if (
                                linked.run_id == event.run.run_id
                                and linked.record_root != event.run.record_root
                            ):
                                raise LabConflictError(
                                    f"lab run {event.run.run_id} is already bound to "
                                    f"record root {linked.record_root}"
                                )
                            if (
                                linked.run_id != event.run.run_id
                                and linked.record_root == event.run.record_root
                            ):
                                raise LabConflictError(
                                    f"record root {event.run.record_root} is already bound "
                                    f"to lab run {linked.run_id}"
                                )
                            if (
                                linked.run_id == event.run.run_id
                                and linked.manifest_digest is not None
                                and event.run.manifest_digest is not None
                                and linked.manifest_digest
                                != event.run.manifest_digest
                            ):
                                raise LabConflictError(
                                    f"lab run {event.run.run_id} has conflicting "
                                    "manifest digests"
                                )
                self._apply_event(state, event)
                _atomic_create(event_path, canonical_json_bytes(event))
                return LabMutation(
                    state=self._load_unlocked(issue_id),
                    changed=True,
                    record_path=event_path,
                )

    def doctor(self, *, at: datetime | None = None) -> dict[str, Any]:
        evaluated_at = at or utc_now()
        errors: list[str] = []
        warnings: list[str] = []
        try:
            states = self.list()
        except LabError as exc:
            return {
                "format": "magentabench-lab-doctor-v1",
                "ok": False,
                "issue_count": 0,
                "errors": [str(exc)],
                "warnings": [],
            }
        by_id = {state.issue.issue_id: state for state in states}

        def check_ref(
            issue_id: str,
            label: str,
            ref: LabArtifactRef,
            *,
            verify_report: bool = False,
            allow_terminal_git_history: bool = False,
        ) -> None:
            if "://" in ref.locator:
                message = (
                    f"{issue_id}: {label} uses external locator not verified locally: "
                    f"{ref.locator}"
                )
                (errors if verify_report else warnings).append(message)
                return
            declared = Path(ref.locator)
            portable = not declared.is_absolute()
            lexical = _absolute_path(
                self.project_root / declared if portable else declared
            )
            try:
                _reject_symlink_ancestors(
                    lexical,
                    label=f"{issue_id}: {label} artifact path",
                )
            except LabDriftError as exc:
                errors.append(str(exc))
                return
            path = lexical.resolve()
            if ".runs" in path.parts:
                warnings.append(
                    f"{issue_id}: {label} points at scratch .runs storage: {ref.locator}"
                )
            if not path.is_file() or path.is_symlink():
                message = f"{issue_id}: {label} artifact is unavailable: {ref.locator}"
                errors.append(message)
                return
            content = path.read_bytes()
            if len(content) != ref.size_bytes or _sha256(content) != ref.sha256:
                if allow_terminal_git_history and portable and _git_history_contains_artifact(
                    self.project_root, ref.locator, ref
                ):
                    return
                errors.append(f"{issue_id}: {label} artifact digest drift: {ref.locator}")
                return
            if verify_report:
                try:
                    from MagentaBench.schemas import verify_run_report

                    verify_run_report(path)
                except Exception as exc:  # standalone verifier owns the error taxonomy
                    errors.append(
                        f"{issue_id}: linked finished report does not verify: {ref.locator}: {exc}"
                    )

        for state in states:
            issue_id = state.issue.issue_id
            for dependency in state.issue.dependencies:
                if dependency not in by_id:
                    errors.append(f"{issue_id}: dependency does not exist: {dependency}")
                elif state.status in {
                    LabStatus.ready,
                    LabStatus.running,
                    LabStatus.verifying,
                    LabStatus.done,
                } and by_id[dependency].status != LabStatus.done:
                    errors.append(
                        f"{issue_id}: active/terminal state has incomplete dependency {dependency}"
                    )
            if (
                state.status in {LabStatus.running, LabStatus.verifying}
                and state.active_lease(evaluated_at) is None
            ):
                warnings.append(f"{issue_id}: active work has no live lease; recovery required")
            for blocker in state.blockers:
                for index, ref in enumerate(blocker.evidence_refs or ()):
                    check_ref(
                        issue_id,
                        f"blocker {blocker.blocker_id} evidence[{index}]",
                        ref,
                    )
            if state.latest_checkpoint is not None:
                if not state.latest_checkpoint.worktree_clean:
                    warnings.append(f"{issue_id}: latest checkpoint has a dirty worktree")
                if state.latest_checkpoint.record_root and (
                    state.latest_checkpoint.record_root == ".runs"
                    or state.latest_checkpoint.record_root.startswith(".runs/")
                ):
                    warnings.append(
                        f"{issue_id}: latest checkpoint only names scratch .runs storage"
                    )
                if state.latest_checkpoint.patch_ref is not None:
                    check_ref(
                        issue_id,
                        "checkpoint patch",
                        state.latest_checkpoint.patch_ref,
                    )
                for index, ref in enumerate(state.latest_checkpoint.artifact_refs):
                    check_ref(
                        issue_id,
                        f"checkpoint artifact[{index}]",
                        ref,
                        allow_terminal_git_history=state.status
                        in {LabStatus.done, LabStatus.cancelled},
                    )
            if state.latest_review is not None:
                for index, ref in enumerate(state.latest_review.evidence_refs):
                    check_ref(
                        issue_id,
                        f"review evidence[{index}]",
                        ref,
                        allow_terminal_git_history=state.status
                        in {LabStatus.done, LabStatus.cancelled},
                    )
            for run in state.runs:
                if run.report_ref is not None:
                    check_ref(
                        issue_id,
                        f"run {run.run_id} report",
                        run.report_ref,
                        verify_report=run.state == LabRunState.finished,
                    )

        run_bindings: dict[str, LabRunLink] = {}
        record_root_bindings: dict[str, str] = {}
        for state in states:
            for run in state.runs:
                previous = run_bindings.get(run.run_id)
                if previous is not None:
                    if previous.record_root != run.record_root:
                        errors.append(
                            f"run {run.run_id} has conflicting record roots across issues"
                        )
                    if (
                        previous.manifest_digest is not None
                        and run.manifest_digest is not None
                        and previous.manifest_digest != run.manifest_digest
                    ):
                        errors.append(
                            f"run {run.run_id} has conflicting manifest digests across issues"
                        )
                    if (
                        previous.manifest_digest is None
                        and run.manifest_digest is not None
                    ):
                        run_bindings[run.run_id] = run
                else:
                    run_bindings[run.run_id] = run
                bound_run_id = record_root_bindings.get(run.record_root)
                if bound_run_id is not None and bound_run_id != run.run_id:
                    errors.append(
                        f"record root {run.record_root} is bound to multiple run ids: "
                        f"{bound_run_id}, {run.run_id}"
                    )
                else:
                    record_root_bindings[run.record_root] = run.run_id

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(issue_id: str, chain: tuple[str, ...]) -> None:
            if issue_id in visiting:
                errors.append("dependency cycle: " + " -> ".join((*chain, issue_id)))
                return
            if issue_id in visited or issue_id not in by_id:
                return
            visiting.add(issue_id)
            for dependency in by_id[issue_id].issue.dependencies:
                visit(dependency, (*chain, issue_id))
            visiting.remove(issue_id)
            visited.add(issue_id)

        for issue_id in sorted(by_id):
            visit(issue_id, ())

        active = [
            (state, state.active_lease(evaluated_at))
            for state in states
            if state.active_lease(evaluated_at) is not None
        ]
        for index, (left_state, left_lease) in enumerate(active):
            assert left_lease is not None
            for right_state, right_lease in active[index + 1 :]:
                assert right_lease is not None
                if _scope_conflicts(left_lease.write_scope, right_lease.write_scope):
                    errors.append(
                        "active lease scope conflict: "
                        f"{left_state.issue.issue_id}/{left_lease.lease_id} and "
                        f"{right_state.issue.issue_id}/{right_lease.lease_id}"
                    )

        return {
            "format": "magentabench-lab-doctor-v1",
            "ok": not errors,
            "issue_count": len(states),
            "errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
        }

    def recovery_view(
        self, issue_id: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        evaluated_at = at or utc_now()
        state = self.load(issue_id)
        dependencies = {
            dependency: self.load(dependency).status.value
            for dependency in state.issue.dependencies
        }
        active = state.active_lease(evaluated_at)
        actions: list[str] = []
        if active is not None:
            actions.append(
                f"coordinate with active lease holder {active.owner} ({active.lease_id})"
            )
        elif state.lease is not None and state.status in {
            LabStatus.running,
            LabStatus.verifying,
        }:
            actions.append("acquire a new lease before resuming interrupted work")
        for blocker in state.blockers:
            actions.append(f"resolve {blocker.blocker_id}: {blocker.recovery_action}")
            if blocker.reproduce_argv is not None:
                actions.append(
                    f"reproduce {blocker.blocker_id}: "
                    + shlex.join(blocker.reproduce_argv)
                )
            if blocker.unblock_condition is not None:
                actions.append(
                    f"unblock condition {blocker.blocker_id}: "
                    f"{blocker.unblock_condition}"
                )
        incomplete = [key for key, value in dependencies.items() if value != LabStatus.done.value]
        if incomplete:
            actions.append("complete dependencies: " + ", ".join(sorted(incomplete)))
        if state.latest_checkpoint is not None:
            actions.append(
                "resume command: " + shlex.join(state.latest_checkpoint.resume_argv)
            )
        elif state.status not in {LabStatus.done, LabStatus.cancelled}:
            actions.append("record a work checkpoint before the next interruptible step")
        return {
            "format": "magentabench-lab-recovery-v1",
            "issue_id": issue_id,
            "status": state.status.value,
            "owner": state.owner,
            "revision": state.revision,
            "event_count": state.event_count,
            "active_lease": (
                None if active is None else active.model_dump(mode="json", exclude_none=True)
            ),
            "last_lease": (
                None
                if state.lease is None
                else state.lease.model_dump(mode="json", exclude_none=True)
            ),
            "blockers": [item.model_dump(mode="json") for item in state.blockers],
            "dependencies": dependencies,
            "latest_checkpoint": (
                None
                if state.latest_checkpoint is None
                else state.latest_checkpoint.model_dump(mode="json", exclude_none=True)
            ),
            "runs": [item.model_dump(mode="json", exclude_none=True) for item in state.runs],
            "actions": actions,
        }

    def board(self, *, at: datetime | None = None) -> dict[str, Any]:
        evaluated_at = at or utc_now()
        rows: list[dict[str, Any]] = []
        counts = {status.value: 0 for status in LabStatus}
        for state in self.list():
            counts[state.status.value] += 1
            active = state.active_lease(evaluated_at)
            rows.append(
                {
                    "issue_id": state.issue.issue_id,
                    "title": state.issue.title,
                    "priority": state.issue.priority.value,
                    "status": state.status.value,
                    "owner": state.owner,
                    "lease_holder": None if active is None else active.owner,
                    "lease_id": None if active is None else active.lease_id,
                    "blocker_count": len(state.blockers),
                    "dependency_count": len(state.issue.dependencies),
                    "event_count": state.event_count,
                    "updated_at": state.updated_at.isoformat().replace("+00:00", "Z"),
                    "revision": state.revision,
                }
            )
        priority_order = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
        status_order = {
            "blocked": 0,
            "running": 1,
            "verifying": 2,
            "ready": 3,
            "planned": 4,
            "open": 5,
            "done": 6,
            "cancelled": 7,
        }
        rows.sort(
            key=lambda item: (
                priority_order[str(item["priority"])],
                status_order[str(item["status"])],
                str(item["issue_id"]),
            )
        )
        return {
            "format": "magentabench-lab-board-v1",
            "issue_count": len(rows),
            "counts": counts,
            "issues": rows,
        }


__all__ = [
    "LabConflictError",
    "LabDriftError",
    "LabError",
    "LabMutation",
    "LabNotFoundError",
    "LabStore",
    "artifact_ref_from_path",
    "canonical_json_bytes",
    "utc_now",
]
