from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import (
    ArtifactRef,
    CandidateGateCommandReceipt,
    CandidateGateCommandSpec,
    CandidateValidityGateReceipt,
    ContextCompactionReceipt,
    TraceConversionReceipt,
    UsageRecord,
)


def _ref(tmp_path: Path, name: str, marker: str) -> ArtifactRef:
    path = (tmp_path / name).resolve()
    return ArtifactRef(path=str(path), sha256=marker * 64, size_bytes=1)


def test_context_compaction_preserves_raw_history_and_message_lineage(
    tmp_path: Path,
) -> None:
    receipt = ContextCompactionReceipt(
        attempt_id="attempt-1",
        operation_id="compact-1",
        trigger="threshold",
        phase="agent-turn",
        mode="hybrid",
        strategy_id="summary-v1",
        strategy_digest="a" * 64,
        prompt_ref=_ref(tmp_path, "prompt", "b"),
        summary_model_digest="c" * 64,
        raw_history_ref=_ref(tmp_path, "raw", "d"),
        pre_context_digest="e" * 64,
        post_context_digest="f" * 64,
        pre_message_ids=("m1", "m2", "m3"),
        retained_message_ids=("m3",),
        dropped_message_ids=("m1",),
        summarized_message_ids=("m2",),
        pre_message_count=3,
        post_message_count=2,
        pre_token_count=1000,
        post_token_count=300,
        truncated=True,
        summary_input_ref=_ref(tmp_path, "summary-in", "1"),
        summary_output_ref=_ref(tmp_path, "summary-out", "2"),
        usage=UsageRecord(total_tokens=50),
        retries=1,
        status="complete",
        budget_event_ref=_ref(tmp_path, "budget", "3"),
        started_at="2026-08-10T00:00:00Z",
        finished_at="2026-08-10T00:00:01Z",
    )
    assert receipt.raw_history_ref.sha256 == "d" * 64

    payload = receipt.model_dump(mode="json")
    payload["dropped_message_ids"] = []
    with pytest.raises(ValidationError, match="one disposition"):
        ContextCompactionReceipt.model_validate(payload)


def test_trace_conversion_cannot_hide_dropped_provider_records(tmp_path: Path) -> None:
    partial = TraceConversionReceipt(
        attempt_id="attempt-1",
        converter_id="openai-responses-v1",
        converter_version="1.2.0",
        converter_implementation_ref=_ref(tmp_path, "converter", "a"),
        converter_closure_digest="b" * 64,
        provider_mode="responses",
        provider_schema_digest="c" * 64,
        raw_trace_ref=_ref(tmp_path, "raw", "d"),
        normalized_trajectory_ref=_ref(tmp_path, "normalized", "e"),
        raw_record_count=10,
        mapped_record_count=8,
        dropped_record_count=1,
        unclassified_record_count=1,
        lossy=True,
        status="partial",
        mapping_ledger_ref=_ref(tmp_path, "mapping", "f"),
        error_type="unmapped-records",
        error_ref=_ref(tmp_path, "error", "1"),
    )
    assert partial.lossy

    payload = partial.model_dump(mode="json")
    payload["lossy"] = False
    with pytest.raises(ValidationError, match="lossy flag"):
        TraceConversionReceipt.model_validate(payload)


def test_candidate_validity_retains_failed_commands_and_patch_bytes(
    tmp_path: Path,
) -> None:
    commands = (
        CandidateGateCommandSpec(
            id="compile", kind="compile", argv=("python", "-m", "compileall"), timeout_seconds=30.0
        ),
        CandidateGateCommandSpec(
            id="smoke", kind="smoke", argv=("pytest", "-q"), timeout_seconds=60.0
        ),
    )
    receipts = (
        CandidateGateCommandReceipt(
            command_id="compile",
            started_at="2026-08-10T00:00:00Z",
            finished_at="2026-08-10T00:00:01Z",
            status="passed",
            exit_code=0,
            stdout_ref=_ref(tmp_path, "compile-out", "a"),
            stderr_ref=_ref(tmp_path, "compile-err", "b"),
        ),
        CandidateGateCommandReceipt(
            command_id="smoke",
            started_at="2026-08-10T00:00:01Z",
            finished_at="2026-08-10T00:01:01Z",
            status="timeout",
            stdout_ref=_ref(tmp_path, "smoke-out", "c"),
            stderr_ref=_ref(tmp_path, "smoke-err", "d"),
            error_ref=_ref(tmp_path, "smoke-timeout", "e"),
        ),
    )
    gate = CandidateValidityGateReceipt(
        candidate_id="candidate-1",
        candidate_manifest_digest="f" * 64,
        source_snapshot_ref=_ref(tmp_path, "source", "1"),
        source_commit="2" * 40,
        environment_digest="3" * 64,
        tracked_patch_ref=_ref(tmp_path, "tracked", "4"),
        untracked_patch_ref=_ref(tmp_path, "untracked", "5"),
        gate_policy_digest="6" * 64,
        commands=commands,
        command_receipts=receipts,
        valid=False,
        invalid_reasons=("smoke command timed out",),
    )
    assert not gate.valid

    payload = gate.model_dump(mode="json")
    payload["valid"] = True
    with pytest.raises(ValidationError, match="exactly follow"):
        CandidateValidityGateReceipt.model_validate(payload)
