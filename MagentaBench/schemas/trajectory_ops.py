"""Typed receipts for context, trace conversion, and candidate validity gates."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .models import (
    ArtifactRef,
    ID_PATTERN,
    SHA256_PATTERN,
    StrictModel,
    UsageRecord,
)


def _utc(value: str, *, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC")
    return value


class ContextCompactionStatus(str, Enum):
    complete = "complete"
    failed = "failed"


class ContextCompactionReceipt(StrictModel):
    """One budgeted, replayable context transformation.

    The uncompacted history remains independently authoritative; this receipt
    only explains how one runtime context view was derived from it.
    """

    format: Literal["bmp-context-compaction-receipt-v1"] = (
        "bmp-context-compaction-receipt-v1"
    )
    attempt_id: str = Field(pattern=ID_PATTERN)
    operation_id: str = Field(pattern=ID_PATTERN)
    trigger: Literal["threshold", "emergency", "manual", "provider_limit"]
    phase: str = Field(pattern=ID_PATTERN)
    mode: Literal["truncate", "summarize", "hybrid"]
    strategy_id: str = Field(pattern=ID_PATTERN)
    strategy_digest: str = Field(pattern=SHA256_PATTERN)
    prompt_ref: ArtifactRef | None = None
    summary_model_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    raw_history_ref: ArtifactRef
    pre_context_digest: str = Field(pattern=SHA256_PATTERN)
    post_context_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    pre_message_ids: tuple[str, ...]
    retained_message_ids: tuple[str, ...]
    dropped_message_ids: tuple[str, ...]
    summarized_message_ids: tuple[str, ...]
    pre_message_count: int = Field(ge=0, strict=True)
    post_message_count: int | None = Field(default=None, ge=0, strict=True)
    pre_token_count: int = Field(ge=0, strict=True)
    post_token_count: int | None = Field(default=None, ge=0, strict=True)
    truncated: bool
    summary_input_ref: ArtifactRef | None = None
    summary_output_ref: ArtifactRef | None = None
    usage: UsageRecord
    retries: int = Field(ge=0, strict=True)
    status: ContextCompactionStatus
    error_type: str | None = Field(default=None, pattern=ID_PATTERN)
    error_ref: ArtifactRef | None = None
    budget_event_ref: ArtifactRef
    started_at: str
    finished_at: str

    @field_validator("started_at", "finished_at")
    @classmethod
    def timestamps_are_utc(cls, value: str, info: Any) -> str:
        return _utc(value, field_name=info.field_name)

    @field_validator(
        "pre_message_ids",
        "retained_message_ids",
        "dropped_message_ids",
        "summarized_message_ids",
    )
    @classmethod
    def message_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("context compaction message ids must be unique")
        if any(not value.strip() for value in values):
            raise ValueError("context compaction message ids must be non-empty")
        return values

    @model_validator(mode="after")
    def compaction_lineage_is_closed(self) -> "ContextCompactionReceipt":
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
        if finished < started:
            raise ValueError("context compaction cannot finish before it starts")
        if self.pre_message_count != len(self.pre_message_ids):
            raise ValueError("pre_message_count must equal pre_message_ids")
        retained = set(self.retained_message_ids)
        dropped = set(self.dropped_message_ids)
        summarized = set(self.summarized_message_ids)
        if retained & dropped or retained & summarized or dropped & summarized:
            raise ValueError("context compaction message dispositions must be disjoint")
        if retained | dropped | summarized != set(self.pre_message_ids):
            raise ValueError("every pre-context message requires one disposition")
        if self.truncated != bool(self.dropped_message_ids):
            raise ValueError("truncated must exactly reflect dropped messages")
        uses_summary = bool(self.summarized_message_ids)
        summary_refs = self.summary_input_ref is not None and self.summary_output_ref is not None
        if uses_summary != summary_refs:
            raise ValueError("summarized messages require input and output artifact refs")
        if self.mode == "truncate" and uses_summary:
            raise ValueError("truncate mode forbids summarized messages")
        if self.mode == "summarize" and self.dropped_message_ids:
            raise ValueError("summarize mode forbids unrepresented dropped messages")
        if uses_summary and (
            self.prompt_ref is None or self.summary_model_digest is None
        ):
            raise ValueError("model summarization requires prompt and model identity")
        if self.status == ContextCompactionStatus.complete:
            if (
                self.post_context_digest is None
                or self.post_message_count is None
                or self.post_token_count is None
                or self.error_type is not None
                or self.error_ref is not None
            ):
                raise ValueError("complete compaction requires output counts and no error")
            if self.post_token_count > self.pre_token_count:
                raise ValueError("compaction cannot increase token count")
        else:
            if self.error_type is None or self.error_ref is None:
                raise ValueError("failed compaction requires typed error evidence")
            if any(
                value is not None
                for value in (
                    self.post_context_digest,
                    self.post_message_count,
                    self.post_token_count,
                )
            ):
                raise ValueError("failed compaction cannot claim a post context")
        return self


class TraceConversionStatus(str, Enum):
    complete = "complete"
    partial = "partial"
    failed = "failed"


class TraceConversionReceipt(StrictModel):
    """Raw provider trace to normalized trajectory conversion lineage."""

    format: Literal["bmp-trace-conversion-receipt-v1"] = (
        "bmp-trace-conversion-receipt-v1"
    )
    attempt_id: str = Field(pattern=ID_PATTERN)
    converter_id: str = Field(pattern=ID_PATTERN)
    converter_version: str = Field(min_length=1)
    converter_implementation_ref: ArtifactRef
    converter_closure_digest: str = Field(pattern=SHA256_PATTERN)
    provider_mode: str = Field(pattern=ID_PATTERN)
    provider_schema_digest: str = Field(pattern=SHA256_PATTERN)
    raw_trace_ref: ArtifactRef
    normalized_trajectory_ref: ArtifactRef | None = None
    raw_record_count: int = Field(ge=0, strict=True)
    mapped_record_count: int = Field(ge=0, strict=True)
    dropped_record_count: int = Field(ge=0, strict=True)
    unclassified_record_count: int = Field(ge=0, strict=True)
    lossy: bool
    status: TraceConversionStatus
    mapping_ledger_ref: ArtifactRef
    error_type: str | None = Field(default=None, pattern=ID_PATTERN)
    error_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def conversion_counts_and_status_reconcile(self) -> "TraceConversionReceipt":
        if (
            self.mapped_record_count
            + self.dropped_record_count
            + self.unclassified_record_count
            != self.raw_record_count
        ):
            raise ValueError("trace conversion counts must cover every raw record")
        if self.lossy != bool(
            self.dropped_record_count or self.unclassified_record_count
        ):
            raise ValueError("trace conversion lossy flag must match mapping counts")
        if self.status == TraceConversionStatus.complete:
            if (
                self.normalized_trajectory_ref is None
                or self.lossy
                or self.error_type is not None
                or self.error_ref is not None
            ):
                raise ValueError("complete trace conversion must be lossless and error-free")
        elif self.status == TraceConversionStatus.partial:
            if (
                self.normalized_trajectory_ref is None
                or not self.lossy
                or self.error_type is None
                or self.error_ref is None
            ):
                raise ValueError("partial trace conversion requires output and typed loss")
        else:
            if (
                self.normalized_trajectory_ref is not None
                or self.error_type is None
                or self.error_ref is None
            ):
                raise ValueError("failed trace conversion requires only typed error evidence")
        return self


class CandidateGateCommandKind(str, Enum):
    import_check = "import_check"
    compile = "compile"
    schema = "schema"
    smoke = "smoke"
    custom = "custom"


class CandidateGateCommandSpec(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    kind: CandidateGateCommandKind
    argv: tuple[str, ...]
    timeout_seconds: float = Field(gt=0, strict=True)
    expected_exit_codes: tuple[int, ...] = (0,)

    @model_validator(mode="after")
    def command_is_closed(self) -> "CandidateGateCommandSpec":
        if not self.argv or any(not value for value in self.argv):
            raise ValueError("candidate gate argv must be non-empty")
        if not self.expected_exit_codes or len(set(self.expected_exit_codes)) != len(
            self.expected_exit_codes
        ):
            raise ValueError("candidate gate expected exit codes must be unique")
        return self


class CandidateGateCommandReceipt(StrictModel):
    command_id: str = Field(pattern=ID_PATTERN)
    started_at: str
    finished_at: str
    status: Literal["passed", "failed", "timeout", "infra_error"]
    exit_code: int | None = None
    stdout_ref: ArtifactRef
    stderr_ref: ArtifactRef
    error_ref: ArtifactRef | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def command_timestamps_are_utc(cls, value: str, info: Any) -> str:
        return _utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def command_terminal_evidence_is_coherent(self) -> "CandidateGateCommandReceipt":
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
        if finished < started:
            raise ValueError("candidate gate command finished before it started")
        if self.status in {"passed", "failed"}:
            if self.exit_code is None or self.error_ref is not None:
                raise ValueError("process completion requires exit code and no typed error")
        else:
            if self.exit_code is not None or self.error_ref is None:
                raise ValueError("timeout/infra error requires typed error and no exit code")
        return self


class CandidateValidityGateReceipt(StrictModel):
    """Pre-evaluation source/build validity gate for one generated candidate."""

    format: Literal["bmp-candidate-validity-gate-v1"] = (
        "bmp-candidate-validity-gate-v1"
    )
    candidate_id: str = Field(pattern=ID_PATTERN)
    candidate_manifest_digest: str = Field(pattern=SHA256_PATTERN)
    source_snapshot_ref: ArtifactRef
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    environment_digest: str = Field(pattern=SHA256_PATTERN)
    tracked_patch_ref: ArtifactRef
    untracked_patch_ref: ArtifactRef
    gate_policy_digest: str = Field(pattern=SHA256_PATTERN)
    commands: tuple[CandidateGateCommandSpec, ...]
    command_receipts: tuple[CandidateGateCommandReceipt, ...]
    valid: bool
    invalid_reasons: tuple[str, ...] = ()

    @field_validator("invalid_reasons")
    @classmethod
    def invalid_reasons_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(not value.strip() for value in values):
            raise ValueError("candidate invalid reasons must be unique and non-empty")
        return values

    @model_validator(mode="after")
    def validity_follows_every_registered_gate(self) -> "CandidateValidityGateReceipt":
        if not self.commands:
            raise ValueError("candidate validity gate requires registered commands")
        command_ids = [command.id for command in self.commands]
        receipt_ids = [receipt.command_id for receipt in self.command_receipts]
        if len(set(command_ids)) != len(command_ids):
            raise ValueError("candidate gate command ids must be unique")
        if receipt_ids != command_ids:
            raise ValueError("candidate gate receipts must follow every command in order")
        by_id = {command.id: command for command in self.commands}
        passed = True
        for receipt in self.command_receipts:
            command = by_id[receipt.command_id]
            if (
                receipt.status != "passed"
                or receipt.exit_code not in command.expected_exit_codes
            ):
                passed = False
        if self.valid != passed:
            raise ValueError("candidate validity must exactly follow command receipts")
        if self.valid and self.invalid_reasons:
            raise ValueError("valid candidate forbids invalid reasons")
        if not self.valid and not self.invalid_reasons:
            raise ValueError("invalid candidate requires reasons")
        return self


__all__ = [
    "CandidateGateCommandKind",
    "CandidateGateCommandReceipt",
    "CandidateGateCommandSpec",
    "CandidateValidityGateReceipt",
    "ContextCompactionReceipt",
    "ContextCompactionStatus",
    "TraceConversionReceipt",
    "TraceConversionStatus",
]
