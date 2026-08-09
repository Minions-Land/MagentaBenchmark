from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.runner.backend.fake import FakeBackend
from MagentaBench.runner.compiler import Compiler, canonical_manifest_json, sha256_bytes
from MagentaBench.runner.evidence import atomic_write_json, sha256_file
from MagentaBench.runner.scheduler import Scheduler
from MagentaBench.schemas import Budget, ScheduleActivationReceipt, UsageRecord

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _scheduled_run(*, seed: int = 11, **protocol_updates):
    source = Compiler(ROOT).compile(EXPERIMENT)[0]
    protocol = source.manifest.execution.protocol
    assert protocol is not None
    protocol = protocol.model_copy(
        update={"checkpoint_policy": "disabled", **protocol_updates}
    )
    execution = source.manifest.execution.model_copy(
        update={"protocol": protocol, "seed": seed}
    )
    manifest = source.manifest.model_copy(update={"execution": execution})
    canonical = canonical_manifest_json(manifest)
    return replace(source, manifest=manifest)


def _execute(tmp_path: Path, run, cases, attempt_runner, backend):
    return Scheduler(record_root=tmp_path / "records").execute(
        run,
        cases,
        attempt_runner=attempt_runner,
        reset_state=backend.reset_state,
        receipt_path=tmp_path / "schedule_activation_receipt.json",
    )


def test_scheduler_retains_three_rollouts_and_ordered_reservations(tmp_path: Path) -> None:
    run = _scheduled_run(
        rollouts_per_case=3,
        parallelism=2,
        candidate_selection="best_of_n",
    )
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)

    def attempt_runner(attempt):
        return backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )

    result = _execute(tmp_path, run, [task], attempt_runner, backend)
    assert result.receipt_path.is_file()
    restored = ScheduleActivationReceipt.model_validate_json(
        result.receipt_path.read_bytes()
    )
    assert restored == result.receipt
    assert len(result.receipt.budget_ledger.attempt_allocations) == 3
    assert len(result.receipt.attempts) == 3
    reservations = result.receipt.budget_ledger.attempt_allocations
    assert [item.reservation_sequence for item in reservations] == [0, 1, 2]
    assert all(item.launched for item in reservations)
    assert all(
        item.reservation_sequence < (item.launch_sequence or 0)
        for item in reservations
    )
    assert result.receipt.observed_state_reset_count == 0
    assert result.receipt.observed_selection_policy == "best_of_n"
    assert sum(attempt.selected for attempt in result.receipt.attempts) == 1
    assert next(
        attempt.attempt_index
        for attempt in result.receipt.attempts
        if attempt.selected
    ) == 0
    assert result.receipt.schedule_valid is True
    assert len(result.selected) == 1


def test_best_of_n_all_failures_keeps_a_reportable_lineage(tmp_path: Path) -> None:
    run = _scheduled_run(
        rollouts_per_case=2,
        parallelism=2,
        candidate_selection="best_of_n",
    )
    manifest = run.manifest.model_copy(
        update={
            "subject": run.manifest.subject.model_copy(
                update={"fault_mode": "infra_error"}
            )
        }
    )
    run = replace(run, manifest=manifest)
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)

    def attempt_runner(attempt):
        return backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )

    result = _execute(tmp_path, run, [task], attempt_runner, backend)

    assert len(result.receipt.attempts) == 2
    assert all(item.status.value == "infra_error" for item in result.receipt.attempts)
    assert sum(item.selected for item in result.receipt.attempts) == 1
    assert result.receipt.attempts[0].selection_reason == (
        "best_of_n_unscored_fallback"
    )
    assert result.receipt.schedule_valid is False
    assert "best_of_n requires benchmark reward evidence" in result.receipt.mismatch_reasons
    assert len(result.selected) == 1


def test_observed_concurrency_measures_real_overlap(tmp_path: Path) -> None:
    run = _scheduled_run(
        rollouts_per_case=4,
        parallelism=4,
        candidate_selection="best_of_n",
    )
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)
    overlap = threading.Barrier(4)

    def attempt_runner(attempt):
        overlap.wait(timeout=2)
        time.sleep(0.05)
        return backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )

    result = _execute(tmp_path, run, [task], attempt_runner, backend)
    restored = ScheduleActivationReceipt.model_validate_json(
        result.receipt_path.read_bytes()
    )
    assert restored.observed_max_concurrency == 4
    assert result.receipt.observed_max_concurrency == 4
    mutated = restored.model_dump(mode="json")
    mutated["declared_parallelism"] = 3
    with pytest.raises(ValidationError, match="schedule_valid=true"):
        ScheduleActivationReceipt.model_validate(mutated)


def test_seeded_case_order_is_repeatable_and_seed_sensitive(tmp_path: Path) -> None:
    base = _scheduled_run(
        rollouts_per_case=1,
        parallelism=1,
        candidate_selection="exact",
        case_order="seeded_random",
    )
    task = FakeBackend._load_task(base)
    cases = [replace(task, task_id=f"case-{index:02d}") for index in range(26)]

    def one(seed: int, name: str):
        run = _scheduled_run(
            seed=seed,
            rollouts_per_case=1,
            parallelism=1,
            candidate_selection="exact",
            case_order="seeded_random",
        )
        backend = FakeBackend(
            tmp_path / name, allow_test_task_override=True
        )
        by_id = {case.task_id: case for case in cases}

        def attempt_runner(attempt):
            case = by_id[attempt.case_id]
            return backend.execute(
                run,
                case,
                case_id=attempt.attempt_id,
                execution_run_id=attempt.attempt_id,
            )

        receipt_path = tmp_path / f"{name}.json"
        result = Scheduler(record_root=tmp_path / name).execute(
            run,
            cases,
            attempt_runner=attempt_runner,
            reset_state=backend.reset_state,
            receipt_path=receipt_path,
        )
        restored = ScheduleActivationReceipt.model_validate_json(
            receipt_path.read_bytes()
        )
        assert restored == result.receipt
        return restored.observed_case_order

    first = one(1, "first")
    same = one(1, "same")
    different = one(99, "different")
    assert first == same
    assert first != different


def test_per_rollout_state_reset_is_observed_for_every_attempt(
    tmp_path: Path,
) -> None:
    run = _scheduled_run(
        rollouts_per_case=3,
        parallelism=1,
        candidate_selection="best_of_n",
        state_reset="per_rollout",
    )
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)
    reset_calls: list[tuple[str, str]] = []

    def attempt_runner(attempt):
        return backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )

    def reset_state(case_id: str, policy: str):
        reset_calls.append((case_id, policy))
        return {"case_id": case_id, "policy": policy}

    result = Scheduler(record_root=tmp_path / "records").execute(
        run,
        [task],
        attempt_runner=attempt_runner,
        reset_state=reset_state,
        receipt_path=tmp_path / "schedule_activation_receipt.json",
    )
    assert reset_calls == [(task.task_id, "per_rollout")] * 3
    assert result.receipt.observed_state_reset_count == 3


def test_disabled_checkpoint_policy_has_no_checkpoint_receipts(tmp_path: Path) -> None:
    run = _scheduled_run(
        rollouts_per_case=1,
        parallelism=1,
        candidate_selection="single",
    )
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)

    def attempt_runner(attempt):
        return backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )

    result = _execute(tmp_path, run, [task], attempt_runner, backend)
    assert result.receipt.declared_checkpoint_policy == "disabled"
    assert result.receipt.checkpoint_save_ref is None
    assert result.receipt.checkpoint_load_ref is None
    assert result.receipt.schedule_valid is True


def test_global_wall_deadline_stops_unlaunched_attempts(tmp_path: Path) -> None:
    run = _scheduled_run(
        rollouts_per_case=3,
        parallelism=1,
        candidate_selection="best_of_n",
    )
    budget = Budget(max_tokens=0, max_wall_seconds=0.03, max_cost=0.0)
    execution = run.manifest.execution.model_copy(update={"budget": budget})
    manifest = run.manifest.model_copy(update={"execution": execution})
    run = replace(run, manifest=manifest)
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)

    def attempt_runner(attempt):
        time.sleep(0.05)
        return backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )

    result = _execute(tmp_path, run, [task], attempt_runner, backend)
    assert result.receipt.schedule_valid is False
    assert result.receipt.mismatch_reasons[0] == "global_wall_deadline_exhausted"
    assert len(result.receipt.attempts) == 1
    assert len(result.receipt.budget_ledger.aborted_children) == 2
    assert result.receipt.budget_ledger.global_elapsed_wall_seconds >= 0.03


def test_attempt_overrun_is_agent_error_and_stops_later_launch(tmp_path: Path) -> None:
    run = _scheduled_run(
        rollouts_per_case=3,
        parallelism=2,
        candidate_selection="best_of_n",
    )
    budget = Budget(max_tokens=3, max_wall_seconds=3.0, max_cost=0.0)
    execution = run.manifest.execution.model_copy(update={"budget": budget})
    manifest = run.manifest.model_copy(update={"execution": execution})
    run = replace(run, manifest=manifest)
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)

    def attempt_runner(attempt):
        case = backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )
        usage = UsageRecord(
            input_tokens=2,
            output_tokens=0,
            total_tokens=2,
            cost=0.0,
            wall_clock_seconds=0.0,
        )
        bundle = case.bundle.model_copy(update={"usage": usage})
        atomic_write_json(case.bundle_path, bundle)
        return replace(
            case,
            bundle=bundle,
            bundle_digest=sha256_file(case.bundle_path),
        )

    result = _execute(tmp_path, run, [task], attempt_runner, backend)
    assert result.receipt.schedule_valid is False
    assert len(result.receipt.attempts) == 2
    assert len(result.receipt.budget_ledger.attempt_allocations) == 3
    assert len(result.receipt.budget_ledger.aborted_children) == 1
    assert all(attempt.status.value == "agent_error" for attempt in result.receipt.attempts)
    assert all(attempt.debit and attempt.debit.budget_exceeded for attempt in result.receipt.attempts)
    assert "budget_exceeded" in result.receipt.mismatch_reasons


def test_unobservable_usage_materializes_invalid_receipt_without_fake_zeroes(
    tmp_path: Path,
) -> None:
    """A native backend may omit token/cost counters without losing its evidence."""

    run = _scheduled_run(
        rollouts_per_case=1,
        parallelism=1,
        candidate_selection="single",
    )
    budget = Budget(max_tokens=100, max_wall_seconds=3.0, max_cost=2.0)
    run = replace(
        run,
        manifest=run.manifest.model_copy(
            update={
                "execution": run.manifest.execution.model_copy(update={"budget": budget})
            }
        ),
    )
    backend = FakeBackend(tmp_path / "records")
    task = backend._load_task(run)

    def attempt_runner(attempt):
        case = backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )
        # Simulate Harbor/NOP, which has no provider token or cost counters.
        unknown = case.bundle.model_copy(update={"usage": UsageRecord()})
        atomic_write_json(case.bundle_path, unknown)
        return replace(case, bundle=unknown, bundle_digest=sha256_file(case.bundle_path))

    result = _execute(tmp_path, run, [task], attempt_runner, backend)
    assert result.receipt.schedule_valid is False
    assert "budget usage is unobservable" in result.receipt.mismatch_reasons
    debit = result.receipt.attempts[0].debit
    assert debit is not None
    assert debit.usage_observable is False
    assert debit.released.max_tokens is None
    assert debit.released.max_cost is None
    restored = ScheduleActivationReceipt.model_validate_json(
        result.receipt_path.read_bytes()
    )
    assert restored == result.receipt
