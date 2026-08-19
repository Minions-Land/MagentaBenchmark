"""Strict records for the MagentaBench laboratory collaboration ledger.

These records coordinate people and processes. They are deliberately separate
from BMP evidence and never make a benchmark result claim-ready.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
import re
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


LAB_ISSUE_FORMAT = "magentabench-lab-issue-v1"
LAB_EVENT_FORMAT = "magentabench-lab-event-v1"
LAB_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
ACTOR_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._@/-]{0,126}[A-Za-z0-9])?$"
LAB_FINAL_REVIEWER = "PoorOtterBob"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"
ENV_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{0,126}$"


class LabModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("lab timestamps must include a UTC offset")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("lab timestamps must be normalized to UTC")
    return value


def _unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    return values


def _portable_relative_path(value: str, *, label: str) -> str:
    if "\\" in value:
        raise ValueError(f"{label} must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a normalized repository-relative path")
    return path.as_posix()


def _single_line(value: str, *, label: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a single line")
    return value


def _safe_locator(value: str, *, label: str) -> str:
    """Validate one local path or credential-free external URI."""

    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be one non-null line")
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"{label} URI must not contain userinfo")
        secret_names = {
            "credential",
            "key",
            "password",
            "secret",
            "sig",
            "signature",
            "token",
        }
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = key.casefold().replace("-", "_")
            if any(part in secret_names for part in normalized.split("_")):
                raise ValueError(f"{label} URI query must not contain credential fields")
        return value
    if "\\" in value:
        raise ValueError(f"{label} paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute():
        if any(part in {"", ".", ".."} for part in path.parts[1:]):
            raise ValueError(f"{label} must be a normalized path")
        return path.as_posix()
    return _portable_relative_path(value, label=label)


def _safe_argv(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if any(not value or "\x00" in value for value in values):
        raise ValueError(f"{label} must contain non-empty arguments")
    credential_option = re.compile(
        r"(?i)(?:^|[-_])(?:api[-_]?key|access[-_]?token|auth[-_]?token|"
        r"bearer[-_]?token|token|password|secret|credential)s?$"
    )
    authorization_header = re.compile(
        r"(?i)\b(?:proxy-)?authorization\s*:\s*(?:basic|bearer)\s+\S+"
    )
    for value in values:
        option = value.split("=", 1)[0].lstrip("-")
        assignment = value.split("=", 1)[0]
        if credential_option.search(option) or credential_option.search(assignment):
            raise ValueError(
                f"{label} must not contain credential-bearing options or assignments"
            )
        if authorization_header.search(value):
            raise ValueError(f"{label} must not contain authorization header values")
    return values


class LabPriority(str, Enum):
    p0 = "p0"
    p1 = "p1"
    p2 = "p2"
    p3 = "p3"


class LabStatus(str, Enum):
    open = "open"
    planned = "planned"
    ready = "ready"
    running = "running"
    verifying = "verifying"
    blocked = "blocked"
    done = "done"
    cancelled = "cancelled"


class LabEventKind(str, Enum):
    status = "status"
    assign = "assign"
    claim = "claim"
    renew = "renew"
    release = "release"
    block = "block"
    resolve_blocker = "resolve_blocker"
    checkpoint = "checkpoint"
    link_run = "link_run"
    review = "review"
    note = "note"


class LabBlockerCategory(str, Enum):
    infrastructure = "infrastructure"
    dependency = "dependency"
    data = "data"
    code = "code"
    process = "process"
    external = "external"
    unknown = "unknown"


class LabRunState(str, Enum):
    planned = "planned"
    running = "running"
    finished = "finished"
    failed = "failed"
    invalid = "invalid"
    cancelled = "cancelled"


class LabReviewVerdict(str, Enum):
    approved = "approved"
    changes_requested = "changes_requested"


class LabWriteScope(LabModel):
    paths: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()

    @field_validator("paths")
    @classmethod
    def paths_are_portable(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _portable_relative_path(value, label="write scope path") for value in values
        )
        return _unique(normalized, label="write scope paths")

    @field_validator("resources")
    @classmethod
    def resources_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if re.fullmatch(LAB_ID_PATTERN, value) is None:
                raise ValueError("write scope resources must be normalized identifiers")
        return _unique(values, label="write scope resources")


class LabCriterion(LabModel):
    criterion_id: str = Field(pattern=LAB_ID_PATTERN)
    description: str = Field(min_length=1, max_length=2000)


class LabArtifactRef(LabModel):
    locator: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0, strict=True)

    @field_validator("locator")
    @classmethod
    def locator_has_no_credentials(cls, value: str) -> str:
        return _safe_locator(value, label="artifact locator")


class LabReview(LabModel):
    verdict: LabReviewVerdict
    summary: str = Field(min_length=1, max_length=4000)
    accepted_criteria: tuple[str, ...] = ()
    evidence_refs: tuple[LabArtifactRef, ...] = ()

    @field_validator("accepted_criteria")
    @classmethod
    def criteria_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(LAB_ID_PATTERN, value) is None for value in values):
            raise ValueError("accepted criteria must be normalized identifiers")
        return _unique(values, label="accepted criteria")

    @field_validator("evidence_refs")
    @classmethod
    def evidence_locators_are_unique(
        cls, values: tuple[LabArtifactRef, ...]
    ) -> tuple[LabArtifactRef, ...]:
        _unique(tuple(value.locator for value in values), label="review evidence locators")
        return values


class LabIssue(LabModel):
    format: Literal["magentabench-lab-issue-v1"] = LAB_ISSUE_FORMAT
    issue_id: str = Field(pattern=LAB_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4000)
    priority: LabPriority = LabPriority.p1
    created_by: str = Field(pattern=ACTOR_PATTERN)
    created_at: datetime
    owner: str | None = Field(default=None, pattern=ACTOR_PATTERN)
    benchmark: str | None = Field(default=None, min_length=1, max_length=200)
    experiment: str | None = None
    labels: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    write_scope: LabWriteScope = LabWriteScope()
    acceptance_criteria: tuple[LabCriterion, ...] = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("title", "benchmark")
    @classmethod
    def headings_are_single_line(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _single_line(value, label=info.field_name)

    @field_validator("experiment")
    @classmethod
    def experiment_is_portable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _portable_relative_path(value, label="experiment")

    @field_validator("labels")
    @classmethod
    def labels_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if re.fullmatch(LAB_ID_PATTERN, value) is None:
                raise ValueError("lab labels must be normalized identifiers")
        return _unique(values, label="lab labels")

    @field_validator("dependencies")
    @classmethod
    def dependencies_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if re.fullmatch(LAB_ID_PATTERN, value) is None:
                raise ValueError("lab dependencies must be normalized issue ids")
        return _unique(values, label="lab dependencies")

    @model_validator(mode="after")
    def issue_does_not_depend_on_itself(self) -> "LabIssue":
        if self.issue_id in self.dependencies:
            raise ValueError("lab issue cannot depend on itself")
        _unique(
            tuple(item.criterion_id for item in self.acceptance_criteria),
            label="acceptance criteria ids",
        )
        return self


class LabBlocker(LabModel):
    blocker_id: str = Field(pattern=LAB_ID_PATTERN)
    category: LabBlockerCategory
    summary: str = Field(min_length=1, max_length=1000)
    recovery_action: str = Field(min_length=1, max_length=4000)
    external_ref: str | None = Field(default=None, min_length=1, max_length=1000)
    expected: str | None = Field(default=None, min_length=1, max_length=4000)
    observed: str | None = Field(default=None, min_length=1, max_length=4000)
    reproduce_argv: tuple[str, ...] | None = None
    exit_code: int | None = Field(default=None, strict=True)
    evidence_refs: tuple[LabArtifactRef, ...] | None = None
    unblock_condition: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("external_ref")
    @classmethod
    def external_ref_has_no_credentials(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_locator(value, label="blocker external ref")

    @field_validator("reproduce_argv")
    @classmethod
    def reproduce_argv_is_safe(
        cls, values: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if values is None:
            return None
        return _safe_argv(values, label="reproduce_argv")

    @field_validator("evidence_refs")
    @classmethod
    def evidence_locators_are_unique(
        cls, values: tuple[LabArtifactRef, ...] | None
    ) -> tuple[LabArtifactRef, ...] | None:
        if values is None:
            return None
        _unique(tuple(value.locator for value in values), label="blocker evidence locators")
        return values


class LabCheckpoint(LabModel):
    git_head: str = Field(pattern=GIT_COMMIT_PATTERN)
    git_branch: str = Field(min_length=1, max_length=300)
    worktree_clean: bool
    dirty_paths: tuple[str, ...] = ()
    experiment: str | None = None
    record_root: str | None = Field(default=None, min_length=1, max_length=4000)
    resume_argv: tuple[str, ...] = Field(min_length=1)
    required_env: tuple[str, ...] = ()
    next_action: str = Field(min_length=1, max_length=4000)
    artifact_refs: tuple[LabArtifactRef, ...] = ()
    patch_ref: LabArtifactRef | None = None

    @field_validator("experiment")
    @classmethod
    def experiment_is_portable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _portable_relative_path(value, label="checkpoint experiment")

    @field_validator("git_branch")
    @classmethod
    def git_branch_is_one_line(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("git_branch must not contain a null byte")
        return _single_line(value, label="git_branch")

    @field_validator("record_root")
    @classmethod
    def record_root_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_locator(value, label="checkpoint record root")

    @field_validator("dirty_paths")
    @classmethod
    def path_lists_are_unique(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        normalized = tuple(
            _portable_relative_path(value, label="dirty worktree path")
            for value in values
        )
        return _unique(normalized, label=info.field_name)

    @field_validator("resume_argv")
    @classmethod
    def resume_argv_is_structured(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _safe_argv(values, label="resume_argv")

    @field_validator("required_env")
    @classmethod
    def required_env_names_only(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(ENV_NAME_PATTERN, value) is None for value in values):
            raise ValueError("required_env must contain environment variable names only")
        return _unique(values, label="required_env")

    @field_validator("artifact_refs")
    @classmethod
    def artifact_locators_are_unique(
        cls, values: tuple[LabArtifactRef, ...]
    ) -> tuple[LabArtifactRef, ...]:
        _unique(tuple(value.locator for value in values), label="artifact locators")
        return values

    @model_validator(mode="after")
    def clean_checkpoint_has_no_dirty_paths(self) -> "LabCheckpoint":
        if self.worktree_clean and self.dirty_paths:
            raise ValueError("clean checkpoint cannot list dirty paths")
        if self.worktree_clean and self.patch_ref is not None:
            raise ValueError("clean checkpoint must not carry a worktree patch")
        if not self.worktree_clean and self.patch_ref is None:
            raise ValueError("dirty checkpoint requires a content-addressed patch_ref")
        return self


class LabRunLink(LabModel):
    run_id: str = Field(pattern=LAB_ID_PATTERN)
    state: LabRunState
    record_root: str = Field(min_length=1, max_length=4000)
    manifest_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    report_ref: LabArtifactRef | None = None
    note: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("record_root")
    @classmethod
    def record_root_is_safe(cls, value: str) -> str:
        return _safe_locator(value, label="run record root")

    @model_validator(mode="after")
    def report_binding_is_complete(self) -> "LabRunLink":
        if self.state == LabRunState.finished and self.report_ref is None:
            raise ValueError("finished lab run link requires a report binding")
        return self


class LabEvent(LabModel):
    format: Literal["magentabench-lab-event-v1"] = LAB_EVENT_FORMAT
    issue_id: str = Field(pattern=LAB_ID_PATTERN)
    event_id: str = Field(pattern=LAB_ID_PATTERN)
    sequence: int = Field(ge=1, strict=True)
    previous_revision: str = Field(pattern=SHA256_PATTERN)
    kind: LabEventKind
    actor: str = Field(pattern=ACTOR_PATTERN)
    created_at: datetime
    status: LabStatus | None = None
    owner: str | None = Field(default=None, pattern=ACTOR_PATTERN)
    lease_id: str | None = Field(default=None, pattern=LAB_ID_PATTERN)
    lease_ttl_seconds: int | None = Field(default=None, ge=60, le=604800, strict=True)
    lease_base_commit: str | None = Field(default=None, pattern=GIT_COMMIT_PATTERN)
    lease_branch: str | None = Field(default=None, min_length=1, max_length=300)
    blocker: LabBlocker | None = None
    blocker_id: str | None = Field(default=None, pattern=LAB_ID_PATTERN)
    checkpoint: LabCheckpoint | None = None
    run: LabRunLink | None = None
    review: LabReview | None = None
    note: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def event_shape_matches_kind(self) -> "LabEvent":
        present = {
            name
            for name in (
                "status",
                "owner",
                "lease_id",
                "lease_ttl_seconds",
                "lease_base_commit",
                "lease_branch",
                "blocker",
                "blocker_id",
                "checkpoint",
                "run",
                "review",
                "note",
            )
            if getattr(self, name) is not None
        }
        allowed: dict[LabEventKind, tuple[set[str], set[str]]] = {
            LabEventKind.status: ({"status"}, {"note"}),
            LabEventKind.assign: ({"owner"}, {"note"}),
            LabEventKind.claim: (
                {
                    "owner",
                    "lease_id",
                    "lease_ttl_seconds",
                    "lease_base_commit",
                    "lease_branch",
                },
                {"note"},
            ),
            LabEventKind.renew: ({"lease_id", "lease_ttl_seconds"}, {"note"}),
            LabEventKind.release: ({"lease_id"}, {"note"}),
            LabEventKind.block: ({"blocker"}, {"note"}),
            LabEventKind.resolve_blocker: ({"blocker_id"}, {"note"}),
            LabEventKind.checkpoint: ({"checkpoint"}, {"note"}),
            LabEventKind.link_run: ({"run"}, set()),
            LabEventKind.review: ({"review"}, {"note"}),
            LabEventKind.note: ({"note"}, set()),
        }
        required, optional = allowed[self.kind]
        if not required.issubset(present) or not present.issubset(required | optional):
            raise ValueError(
                f"lab event fields do not match kind {self.kind.value}: "
                f"required={sorted(required)}, present={sorted(present)}"
            )
        return self

    def request_identity(self) -> dict[str, object]:
        return self.model_dump(
            mode="json",
            exclude={"format", "sequence", "previous_revision", "created_at"},
            exclude_none=True,
        )


class LabLease(LabModel):
    lease_id: str = Field(pattern=LAB_ID_PATTERN)
    owner: str = Field(pattern=ACTOR_PATTERN)
    acquired_at: datetime
    expires_at: datetime
    event_id: str = Field(pattern=LAB_ID_PATTERN)
    base_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    branch: str = Field(min_length=1, max_length=300)
    write_scope: LabWriteScope

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def expiry_follows_acquisition(self) -> "LabLease":
        if self.expires_at <= self.acquired_at:
            raise ValueError("lab lease must expire after acquisition")
        return self


class LabIssueState(LabModel):
    issue: LabIssue
    status: LabStatus
    owner: str | None = Field(default=None, pattern=ACTOR_PATTERN)
    revision: str = Field(pattern=SHA256_PATTERN)
    event_count: int = Field(ge=0, strict=True)
    updated_at: datetime
    lease: LabLease | None = None
    blockers: tuple[LabBlocker, ...] = ()
    latest_checkpoint: LabCheckpoint | None = None
    runs: tuple[LabRunLink, ...] = ()
    latest_review: LabReview | None = None

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    def active_lease(self, at: datetime) -> LabLease | None:
        normalized = _require_utc(at)
        if self.lease is not None and self.lease.expires_at > normalized:
            return self.lease
        return None


__all__ = [
    "ACTOR_PATTERN",
    "ENV_NAME_PATTERN",
    "GIT_COMMIT_PATTERN",
    "LAB_EVENT_FORMAT",
    "LAB_ID_PATTERN",
    "LAB_ISSUE_FORMAT",
    "SHA256_PATTERN",
    "LabArtifactRef",
    "LabBlocker",
    "LabBlockerCategory",
    "LabCheckpoint",
    "LabCriterion",
    "LabEvent",
    "LabEventKind",
    "LabIssue",
    "LabIssueState",
    "LabLease",
    "LabPriority",
    "LabReview",
    "LabReviewVerdict",
    "LabRunLink",
    "LabRunState",
    "LabStatus",
    "LabWriteScope",
]
