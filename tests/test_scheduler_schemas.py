"""Focused scheduler receipt contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import (
    ArtifactRef,
    AttemptAllocation,
    AttemptContext,
    AttemptExecution,
    BudgetAllocation,
    BudgetDebit,
    BudgetLedger,
    CaseAllocation,
    CheckpointLoadReceipt,
    CheckpointSaveReceipt,
    RunStatus,
    ScheduleActivationReceipt,
    UsageRecord,
)

SHA = "a" * 64


def usage(tokens: int, cost: float, wall: float | None = None) -> UsageRecord:
    return UsageRecord(total_tokens=tokens, cost=cost, wall_clock_seconds=wall)


def debit(index: int) -> BudgetDebit:
    return BudgetDebit(
        attempt_id=f"attempt-{index}",
        child_run_id=f"child-{index}",
        completion_sequence=4 + index,
        spent=usage(10, 1.0, 2.0),
        released=BudgetAllocation(max_tokens=40, max_cost=4.0),
    )


def valid_payload() -> dict[str, object]:
    debits = (debit(0), debit(1))
    ledger = BudgetLedger(
        case_allocations=(
            CaseAllocation(
                case_id="case-1",
                allocation_id="case-allocation-1",
                allocated=BudgetAllocation(max_tokens=100, max_cost=10.0),
                attempt_count=2,
            ),
        ),
        attempt_allocations=(
            AttemptAllocation(
                attempt_id="attempt-0",
                case_id="case-1",
                case_allocation_id="case-allocation-1",
                attempt_index=0,
                allocated=BudgetAllocation(max_tokens=50, max_cost=5.0),
                reservation_sequence=0,
                launched=True,
                launch_sequence=1,
            ),
            AttemptAllocation(
                attempt_id="attempt-1",
                case_id="case-1",
                case_allocation_id="case-allocation-1",
                attempt_index=1,
                allocated=BudgetAllocation(max_tokens=50, max_cost=5.0),
                reservation_sequence=2,
                launched=True,
                launch_sequence=3,
            ),
        ),
        aborted_at_exhaustion=False,
        aborted_children=(),
        total_usage=usage(20, 2.0),
        parent_overhead=usage(0, 0.0, 0.1),
        global_elapsed_wall_seconds=2.1,
        reconciles_exactly=True,
    )
    attempts = tuple(
        AttemptExecution(
            attempt_id=f"attempt-{index}",
            case_id="case-1",
            attempt_index=index,
            status=RunStatus.pass_,
            evidence_bundle_ref=ArtifactRef(
                path=f"/tmp/evidence-{index}.json", sha256=SHA, size_bytes=10
            ),
            debit=debits[index],
            selected=index == 1,
            selection_reason="highest reward" if index == 1 else "lower reward",
            reward_value=float(index),
            reward_metric="reward",
        )
        for index in range(2)
    )
    return {
        "run_id": "schedule-1",
        "protocol_digest": SHA,
        "scheduler_digest": SHA,
        "pipeline_digest": SHA,
        "reservation_policy": "equal_division_per_case",
        "global_deadline_at": "2026-08-07T04:00:00Z",
        "declared_rollouts_per_case": 2,
        "observed_attempt_count": 2,
        "declared_parallelism": 2,
        "observed_max_concurrency": 2,
        "declared_case_order": "fixed",
        "observed_case_order": ("case-1",),
        "declared_state_reset": "per_rollout",
        "observed_state_reset_count": 2,
        "declared_candidate_selection": "best_reward",
        "observed_selection_policy": "best_reward",
        "declared_checkpoint_policy": "disabled",
        "order_seed": None,
        "attempts": attempts,
        "budget_ledger": ledger,
        "schedule_valid": True,
        "mismatch_reasons": (),
    }


def test_best_of_n_preserves_all_attempts_and_selects_exactly_one() -> None:
    receipt = ScheduleActivationReceipt.model_validate(valid_payload())
    assert len(receipt.attempts) == 2
    assert sum(item.selected for item in receipt.attempts) == 1


def test_deadline_must_be_timezone_aware_utc() -> None:
    payload = valid_payload()
    payload["global_deadline_at"] = "2026-08-07T04:00:00"
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        ScheduleActivationReceipt.model_validate(payload)
    payload["global_deadline_at"] = "2026-08-07T04:00:00+01:00"
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        ScheduleActivationReceipt.model_validate(payload)


def test_attempt_allocations_must_exactly_divide_case_cap() -> None:
    payload = valid_payload()
    ledger = payload["budget_ledger"].model_dump(mode="python")  # type: ignore[union-attr]
    ledger["attempt_allocations"][1]["allocated"]["max_tokens"] = 49
    with pytest.raises(ValidationError, match="exactly divide"):
        BudgetLedger.model_validate(ledger)


def test_unlaunched_slots_have_allocations_but_no_executions() -> None:
    payload = valid_payload()
    ledger = payload["budget_ledger"].model_dump(mode="python")  # type: ignore[union-attr]
    for allocation in ledger["attempt_allocations"]:
        allocation["launched"] = False
        allocation["launch_sequence"] = None
    ledger["aborted_at_exhaustion"] = True
    ledger["aborted_children"] = ("attempt-0", "attempt-1")
    ledger["total_usage"] = usage(0, 0.0).model_dump(mode="python")
    payload["budget_ledger"] = ledger
    payload["attempts"] = ()
    payload["observed_attempt_count"] = 0
    payload["observed_max_concurrency"] = 0
    payload["observed_state_reset_count"] = 0
    payload["schedule_valid"] = False
    payload["mismatch_reasons"] = ("budget exhausted before launch",)
    receipt = ScheduleActivationReceipt.model_validate(payload)
    assert receipt.attempts == ()
    assert len(receipt.budget_ledger.attempt_allocations) == 2


def test_duplicate_or_missing_lineage_is_rejected() -> None:
    payload = valid_payload()
    attempts = list(payload["attempts"])  # type: ignore[arg-type]
    attempts[1] = attempts[1].model_copy(update={"attempt_id": "attempt-0"})
    payload["attempts"] = tuple(attempts)
    with pytest.raises(ValidationError, match="attempt"):
        ScheduleActivationReceipt.model_validate(payload)


def test_planned_attempt_indices_are_ordered_and_contiguous() -> None:
    payload = valid_payload()
    ledger = payload["budget_ledger"].model_dump(mode="python")  # type: ignore[union-attr]
    ledger["attempt_allocations"][0]["attempt_index"] = 1
    ledger["attempt_allocations"][1]["attempt_index"] = 0

    with pytest.raises(ValidationError, match="ordered, contiguous planned indices"):
        BudgetLedger.model_validate(ledger)


def test_execution_attempt_index_swap_is_rejected() -> None:
    payload = valid_payload()
    attempts = list(payload["attempts"])  # type: ignore[arg-type]
    attempts[0] = attempts[0].model_copy(update={"attempt_index": 1})
    attempts[1] = attempts[1].model_copy(update={"attempt_index": 0})
    payload["attempts"] = tuple(attempts)

    with pytest.raises(ValidationError, match="index must match its allocation"):
        ScheduleActivationReceipt.model_validate(payload)


def test_allocation_and_execution_index_relabel_is_rejected() -> None:
    payload = valid_payload()
    ledger = payload["budget_ledger"].model_dump(mode="python")  # type: ignore[union-attr]
    ledger["attempt_allocations"][0]["attempt_index"] = 1
    ledger["attempt_allocations"][1]["attempt_index"] = 0
    attempts = [item.model_dump(mode="python") for item in payload["attempts"]]  # type: ignore[union-attr]
    attempts[0]["attempt_index"] = 1
    attempts[1]["attempt_index"] = 0
    payload["budget_ledger"] = ledger
    payload["attempts"] = attempts

    with pytest.raises(ValidationError, match="ordered, contiguous planned indices"):
        ScheduleActivationReceipt.model_validate(payload)


def test_measured_mismatch_cannot_be_marked_valid() -> None:
    payload = valid_payload()
    payload["declared_parallelism"] = 1
    with pytest.raises(ValidationError, match="schedule_valid=true"):
        ScheduleActivationReceipt.model_validate(payload)


def test_attempt_budget_overrun_is_distinct_from_launch_exhaustion() -> None:
    payload = valid_payload()
    attempts = list(payload["attempts"])  # type: ignore[arg-type]
    exceeded_debit = BudgetDebit(
        attempt_id="attempt-0",
        child_run_id="child-0",
        completion_sequence=4,
        spent=usage(60, 6.0, 2.0),
        released=BudgetAllocation(),
        budget_exceeded=True,
    )
    attempts[0] = attempts[0].model_copy(
        update={"status": RunStatus.agent_error, "debit": exceeded_debit}
    )
    payload["attempts"] = tuple(attempts)
    ledger = payload["budget_ledger"].model_dump(mode="python")  # type: ignore[union-attr]
    ledger["total_usage"] = usage(70, 7.0).model_dump(mode="python")
    payload["budget_ledger"] = ledger
    payload["schedule_valid"] = False
    payload["mismatch_reasons"] = ("attempt exceeded its budget allocation",)
    receipt = ScheduleActivationReceipt.model_validate(payload)
    assert receipt.attempts[0].status == RunStatus.agent_error
    assert receipt.attempts[0].debit is not None
    assert receipt.attempts[0].debit.budget_exceeded is True


def test_attempt_context_cannot_exceed_remaining_global_budget() -> None:
    context = AttemptContext(
        case_id="case-1",
        execution_run_id="attempt-1",
        attempt_index=0,
        attempt_budget=BudgetAllocation(max_tokens=5, max_cost=1.0),
        remaining_global_budget=BudgetAllocation(max_tokens=5, max_cost=1.0),
        remaining_wall_seconds=10.0,
    )
    assert context.attempt_budget.max_tokens == 5
    with pytest.raises(ValidationError, match="remaining global budget"):
        AttemptContext(
            case_id="case-1",
            execution_run_id="attempt-2",
            attempt_index=1,
            attempt_budget=BudgetAllocation(max_tokens=6, max_cost=1.0),
            remaining_global_budget=BudgetAllocation(max_tokens=5, max_cost=1.0),
        )
    with pytest.raises(ValidationError, match="remaining global budget"):
        AttemptContext(
            case_id="case-1",
            execution_run_id="attempt-3",
            attempt_index=2,
            attempt_budget=BudgetAllocation(max_tokens=None, max_cost=1.0),
            remaining_global_budget=BudgetAllocation(max_tokens=5, max_cost=1.0),
        )
    unlimited = AttemptContext(
        case_id="case-1",
        execution_run_id="attempt-4",
        attempt_index=3,
        attempt_budget=BudgetAllocation(max_tokens=5, max_cost=1.0),
        remaining_global_budget=BudgetAllocation(max_tokens=None, max_cost=None),
    )
    assert unlimited.attempt_budget.max_tokens == 5


def test_checkpoint_policy_requires_decomposed_observed_receipts() -> None:
    payload = valid_payload()
    payload["declared_checkpoint_policy"] = "save_and_resume"
    payload["checkpoint_save_ref"] = CheckpointSaveReceipt(
        written_digest=SHA,
        size_bytes=10,
        write_completion_sequence=6,
        path="/tmp/checkpoint.json",
    )
    payload["checkpoint_load_ref"] = CheckpointLoadReceipt(
        loaded_checkpoint_digest=SHA,
        resolved_plan_digest=SHA,
        schedule_receipt_digest=SHA,
        selected_bundle_digests=(SHA,),
    )
    with pytest.raises(ValidationError, match="ancestor schedule lineage"):
        ScheduleActivationReceipt.model_validate(payload)

    payload["schedule_valid"] = False
    payload["mismatch_reasons"] = ("checkpoint save not yet observed",)
    payload["checkpoint_save_ref"] = None
    payload["checkpoint_load_ref"] = None
    provisional = ScheduleActivationReceipt.model_validate(payload)
    assert provisional.schedule_valid is False
