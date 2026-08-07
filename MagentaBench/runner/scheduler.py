"""Deterministic protocol scheduler with auditable allocation and selection."""

from __future__ import annotations

import hashlib
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

from MagentaBench.schemas import (
    AttemptAllocation,
    AttemptExecution,
    Budget,
    BudgetAllocation,
    BudgetDebit,
    BudgetLedger,
    CaseAllocation,
    EvidenceBundle,
    RunStatus,
    ScheduleActivationReceipt,
    UsageRecord,
    canonical_digest,
)

from .compiler import CompiledRun

if TYPE_CHECKING:
    from .backend.fake import CaseExecution
from .evidence import artifact_ref, atomic_write_json, sha256_file


class SchedulerError(RuntimeError):
    """A schedule cannot be activated or its receipt cannot be validated."""


@dataclass(frozen=True)
class ScheduledAttempt:
    case_id: str
    attempt_id: str
    attempt_index: int
    allocation: BudgetAllocation
    remaining_wall_seconds: float | None


@dataclass(frozen=True)
class ScheduleResult:
    receipt: ScheduleActivationReceipt
    receipt_path: Path
    selected: tuple[CaseExecution, ...]
    launched: tuple[CaseExecution, ...]


AttemptRunner = Callable[[ScheduledAttempt], Any]
StateReset = Callable[[str, str], Any | None]


def _divide_int(value: int | None, parts: int) -> list[int | None]:
    if value is None:
        return [None] * parts
    quotient, remainder = divmod(value, parts)
    return [quotient + (1 if index < remainder else 0) for index in range(parts)]


def _divide_float(value: float | None, parts: int) -> list[float | None]:
    if value is None:
        return [None] * parts
    result: list[float] = []
    spent = 0.0
    for index in range(parts):
        if index == parts - 1:
            share = value - spent
        else:
            share = value / parts
            spent += share
        result.append(share)
    return result


def _usage(bundle: EvidenceBundle, elapsed: float) -> UsageRecord:
    usage = bundle.usage
    if usage is None:
        return UsageRecord(
            total_tokens=None,
            cost=None,
            wall_clock_seconds=elapsed,
        )
    return usage.model_copy(update={"wall_clock_seconds": elapsed})


def _usage_total(values: Sequence[UsageRecord]) -> UsageRecord:
    def total(name: str) -> int | float | None:
        fields = [getattr(value, name) for value in values]
        if any(item is None for item in fields):
            return None
        return sum(fields)

    input_tokens = total("input_tokens")
    output_tokens = total("output_tokens")
    cache_read = total("cache_read_tokens")
    cache_write = total("cache_write_tokens")
    total_tokens = total("total_tokens")
    cost = total("cost")
    return UsageRecord(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=total_tokens,
        cost=cost,
        wall_clock_seconds=None,
    )


class Scheduler:
    """Execute one resolved protocol while retaining every attempt."""

    reservation_policy = "equal_division_per_case"

    def __init__(self, *, record_root: str | Path | None = None) -> None:
        self.record_root = Path(record_root).resolve() if record_root else None
        self.scheduler_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        # Enumerated receipt orchestration surface: Pipeline finalizes checkpoint
        # observation, and gates consume the finalized receipt for attribution.
        pipeline_paths = (
            Path(__file__).with_name("pipeline.py"),
            Path(__file__).with_name("gates.py"),
        )
        pipeline_digest = hashlib.sha256()
        for path in pipeline_paths:
            pipeline_digest.update(path.name.encode("utf-8"))
            pipeline_digest.update(b"\0")
            pipeline_digest.update(path.read_bytes())
            pipeline_digest.update(b"\0")
        self.pipeline_digest = pipeline_digest.hexdigest()

    @staticmethod
    def _ordered_cases(cases: Sequence[Any], order: str, seed: int) -> list[Any]:
        ordered = list(cases)
        if order == "fixed":
            return ordered
        if order == "seeded_random":
            random.Random(seed).shuffle(ordered)
            return ordered
        random.SystemRandom().shuffle(ordered)
        return ordered

    @staticmethod
    def _case_id(case: Any) -> str:
        value = getattr(case, "task_id", case if isinstance(case, str) else None)
        if not isinstance(value, str) or not value:
            raise SchedulerError("scheduled cases require non-empty task_id values")
        return value

    @staticmethod
    def _allocation(cap: Budget, *, parts: int, index: int) -> BudgetAllocation:
        tokens = _divide_int(cap.max_tokens, parts)[index]
        cost = _divide_float(cap.max_cost, parts)[index]
        return BudgetAllocation(max_tokens=tokens, max_cost=cost)

    def execute(
        self,
        run: CompiledRun,
        cases: Iterable[Any],
        *,
        attempt_runner: AttemptRunner,
        reset_state: StateReset | None = None,
        receipt_path: str | Path | None = None,
    ) -> ScheduleResult:
        protocol = run.manifest.execution.protocol
        if protocol is None:
            raise SchedulerError("resolved protocol is required for scheduling")
        ordered_cases = self._ordered_cases(
            list(cases), protocol.case_order, run.manifest.execution.seed
        )
        if not ordered_cases:
            raise SchedulerError("scheduler requires at least one case")
        case_ids = [self._case_id(case) for case in ordered_cases]
        if len(set(case_ids)) != len(case_ids):
            raise SchedulerError("scheduled case ids must be unique")
        rollouts = protocol.rollouts_per_case
        selection = protocol.candidate_selection
        if selection not in {"single", "exact", "best_of_n"}:
            raise SchedulerError(f"unsupported candidate selection policy: {selection!r}")
        if selection in {"single", "exact"} and rollouts != 1:
            raise SchedulerError(
                f"{selection} selection requires rollouts_per_case=1"
            )
        if protocol.state_reset != "never" and reset_state is None:
            raise SchedulerError(
                "state_reset requires a backend reset_state activation hook"
            )

        budget = run.manifest.execution.budget
        case_count = len(ordered_cases)
        case_allocations: list[CaseAllocation] = []
        attempt_allocations: list[AttemptAllocation] = []
        planned: list[tuple[Any, ScheduledAttempt, AttemptAllocation]] = []
        reservation_sequence = 0
        for case_index, case in enumerate(ordered_cases):
            case_id = case_ids[case_index]
            case_cap = self._allocation(budget, parts=case_count, index=case_index)
            case_allocation_id = f"{run.manifest.metadata.run_id}__case-{case_index:04d}"
            case_allocations.append(
                CaseAllocation(
                    case_id=case_id,
                    allocation_id=case_allocation_id,
                    allocated=case_cap,
                    attempt_count=rollouts,
                )
            )
            token_parts = _divide_int(case_cap.max_tokens, rollouts)
            cost_parts = _divide_float(case_cap.max_cost, rollouts)
            for attempt_index in range(rollouts):
                attempt_id = (
                    f"{run.manifest.metadata.run_id}__{case_id}__attempt-{attempt_index:04d}"
                )
                allocation = BudgetAllocation(
                    max_tokens=token_parts[attempt_index],
                    max_cost=cost_parts[attempt_index],
                )
                attempt_allocation = AttemptAllocation(
                    attempt_id=attempt_id,
                    case_allocation_id=case_allocation_id,
                    case_id=case_id,
                    allocated=allocation,
                    reservation_sequence=reservation_sequence,
                    launched=False,
                    launch_sequence=None,
                )
                reservation_sequence += 1
                attempt_allocations.append(attempt_allocation)
                planned.append(
                    (
                        case,
                        ScheduledAttempt(
                            case_id=case_id,
                            attempt_id=attempt_id,
                            attempt_index=attempt_index,
                            allocation=allocation,
                            remaining_wall_seconds=None,
                        ),
                        attempt_allocation,
                    )
                )
        case_order_index = {
            case_id: index for index, case_id in enumerate(case_ids)
        }
        planned.sort(
            key=lambda item: (
                item[1].attempt_index,
                case_order_index[item[1].case_id],
            )
        )

        wall_limit = budget.max_wall_seconds
        started = time.monotonic()
        deadline = (
            started + wall_limit if wall_limit is not None else None
        )
        global_deadline_at = (
            datetime.now(timezone.utc) + timedelta(seconds=wall_limit)
            if wall_limit is not None
            else None
        )
        remaining_tokens = budget.max_tokens
        remaining_cost = budget.max_cost
        launch_sequence = len(attempt_allocations)
        completion_sequence = len(planned) * 2
        active = 0
        observed_max_concurrency = 0
        observed_reset_count = 0
        reset_cases: set[str] = set()
        active_cases: set[str] = set()
        lock = threading.Lock()
        future_data: dict[
            Future[tuple[CaseExecution, float]], ScheduledAttempt
        ] = {}
        launched_records: dict[str, tuple[CaseExecution, float, AttemptAllocation]] = {}
        completion_by_attempt: dict[str, int] = {}
        aborted_ids: list[str] = []
        hard_abort_reason: str | None = None
        next_plan = 0

        def launch(item: tuple[Any, ScheduledAttempt, AttemptAllocation]) -> None:
            nonlocal launch_sequence, observed_reset_count
            nonlocal remaining_tokens, remaining_cost
            _, attempt, allocation = item
            if deadline is not None and time.monotonic() >= deadline:
                raise SchedulerError("global_wall_deadline_exhausted")
            cap = allocation.allocated
            if cap.max_tokens is not None and remaining_tokens is not None and cap.max_tokens > remaining_tokens:
                raise SchedulerError("budget_exhausted_at_launch")
            if cap.max_cost is not None and remaining_cost is not None and cap.max_cost > remaining_cost + 1e-15:
                raise SchedulerError("budget_exhausted_at_launch")
            if cap.max_tokens is not None and remaining_tokens is not None:
                remaining_tokens -= cap.max_tokens
            if cap.max_cost is not None and remaining_cost is not None:
                remaining_cost -= cap.max_cost
            launch_sequence += 1
            launched = allocation.model_copy(
                update={"launched": True, "launch_sequence": launch_sequence}
            )
            allocation_index = next(
                index
                for index, item in enumerate(attempt_allocations)
                if item.attempt_id == attempt.attempt_id
            )
            attempt_allocations[allocation_index] = launched
            if protocol.state_reset in {"per_case", "per_rollout"}:
                if protocol.state_reset == "per_rollout" or attempt.case_id not in reset_cases:
                    reset_receipt = (
                        None
                        if reset_state is None
                        else reset_state(attempt.case_id, protocol.state_reset)
                    )
                    reset_cases.add(attempt.case_id)
                    if reset_receipt is not None:
                        observed_reset_count += 1
            remaining_wall = None if deadline is None else max(0.0, deadline - time.monotonic())
            scheduled = ScheduledAttempt(
                case_id=attempt.case_id,
                attempt_id=attempt.attempt_id,
                attempt_index=attempt.attempt_index,
                allocation=attempt.allocation,
                remaining_wall_seconds=remaining_wall,
            )
            def run_one() -> tuple[CaseExecution, float]:
                nonlocal active, observed_max_concurrency
                child_started = time.monotonic()
                with lock:
                    active += 1
                    observed_max_concurrency = max(observed_max_concurrency, active)
                try:
                    return attempt_runner(scheduled), time.monotonic() - child_started
                finally:
                    with lock:
                        active -= 1
            active_cases.add(attempt.case_id)
            future = pool.submit(run_one)
            future_data[future] = scheduled

        with ThreadPoolExecutor(max_workers=protocol.parallelism) as pool:
            while next_plan < len(planned) or future_data:
                while next_plan < len(planned) and len(future_data) < protocol.parallelism and hard_abort_reason is None:
                    item = planned[next_plan]
                    if (
                        protocol.state_reset == "per_rollout"
                        and item[1].case_id in active_cases
                    ):
                        break
                    try:
                        launch(item)
                    except SchedulerError as exc:
                        hard_abort_reason = str(exc)
                        break
                    next_plan += 1
                if not future_data:
                    if hard_abort_reason is not None:
                        break
                    continue
                done, _ = wait(tuple(future_data), return_when=FIRST_COMPLETED)
                for future in done:
                    scheduled = future_data.pop(future)
                    active_cases.discard(scheduled.case_id)
                    completion_sequence += 1
                    execution, elapsed = future.result()
                    usage = _usage(execution.bundle, elapsed)
                    if execution.bundle.usage != usage:
                        bundle = execution.bundle.model_copy(update={"usage": usage})
                        atomic_write_json(execution.bundle_path, bundle)
                        execution = replace(
                            execution,
                            bundle=bundle,
                            bundle_digest=sha256_file(execution.bundle_path),
                        )
                    completion_by_attempt[scheduled.attempt_id] = completion_sequence
                    launched_record = next(a for a in attempt_allocations if a.attempt_id == scheduled.attempt_id)
                    launched_records[scheduled.attempt_id] = (execution, elapsed, launched_record)
                    over = (
                        (scheduled.allocation.max_tokens is not None and usage.total_tokens is not None and usage.total_tokens > scheduled.allocation.max_tokens)
                        or (scheduled.allocation.max_cost is not None and usage.cost is not None and usage.cost > scheduled.allocation.max_cost)
                    )
                    if over:
                        hard_abort_reason = hard_abort_reason or "budget_exceeded"
                    if deadline is not None and time.monotonic() >= deadline:
                        hard_abort_reason = (
                            hard_abort_reason or "global_wall_deadline_exhausted"
                        )
                    if scheduled.allocation.max_tokens is not None and usage.total_tokens is not None:
                        remaining_tokens = (remaining_tokens or 0) + max(0, scheduled.allocation.max_tokens - usage.total_tokens)
                    if scheduled.allocation.max_cost is not None and usage.cost is not None:
                        remaining_cost = (remaining_cost or 0.0) + max(0.0, scheduled.allocation.max_cost - usage.cost)

        if hard_abort_reason is not None:
            for _, attempt, allocation in planned[next_plan:]:
                aborted_ids.append(attempt.attempt_id)
                index = next(a for a, item in enumerate(attempt_allocations) if item.attempt_id == attempt.attempt_id)
                attempt_allocations[index] = allocation.model_copy(update={"launched": False, "launch_sequence": None})

        # Build candidate execution records and deterministic best-of-n selection.
        launched_ids_in_plan_order = [
            item[1].attempt_id
            for item in planned
            if item[1].attempt_id in launched_records
        ]
        candidate_executions = tuple(
            launched_records[attempt_id][0]
            for attempt_id in launched_ids_in_plan_order
        )
        selected: list[CaseExecution] = []
        attempts: list[AttemptExecution] = []
        for attempt_id in launched_ids_in_plan_order:
            execution, elapsed, allocation = launched_records[attempt_id]
            usage = _usage(execution.bundle, elapsed)
            over = (
                (allocation.allocated.max_tokens is not None and usage.total_tokens is not None and usage.total_tokens > allocation.allocated.max_tokens)
                or (allocation.allocated.max_cost is not None and usage.cost is not None and usage.cost > allocation.allocated.max_cost)
            )
            released = BudgetAllocation(
                max_tokens=(None if allocation.allocated.max_tokens is None or usage.total_tokens is None else max(0, allocation.allocated.max_tokens - usage.total_tokens)),
                max_cost=(None if allocation.allocated.max_cost is None or usage.cost is None else max(0.0, allocation.allocated.max_cost - usage.cost)),
            )
            debit = BudgetDebit(
                attempt_id=attempt_id,
                child_run_id=attempt_id,
                completion_sequence=completion_by_attempt[attempt_id],
                spent=usage,
                released=released,
                budget_exceeded=over,
            )
            reward_metric_name = (
                run.manifest.benchmark.authoritative_reward_metric
            )
            verifier = execution.bundle.verifier_evidence
            reward_value = (
                None
                if verifier is None
                else verifier.metrics.get(reward_metric_name)
            )
            reward_metric = (
                reward_metric_name if reward_value is not None else None
            )
            attempts.append(AttemptExecution(
                attempt_id=attempt_id,
                case_id=execution.case_id,
                attempt_index=next(item[1].attempt_index for item in planned if item[1].attempt_id == attempt_id),
                status=(RunStatus.agent_error if over else execution.bundle.status),
                evidence_bundle_ref=artifact_ref(execution.bundle_path),
                debit=debit,
                selected=False,
                selection_reason=None,
                reward_value=reward_value,
                reward_metric=reward_metric,
            ))
        # Select only launched, scored candidates; preserve all rejected candidates.
        for case_id in case_ids:
            candidates = [item for item in attempts if item.case_id == case_id]
            if not candidates:
                continue
            if selection == "best_of_n":
                scored = [item for item in candidates if item.reward_value is not None]
                if not scored:
                    continue
                winner = max(scored, key=lambda item: (item.reward_value, -item.attempt_index))
            else:
                winner = min(candidates, key=lambda item: item.attempt_index)
            for item in candidates:
                updated = item.model_copy(
                    update={
                        "selected": item.attempt_id == winner.attempt_id,
                        "selection_reason": selection,
                    }
                )
                attempts[attempts.index(item)] = updated
            selected.append(launched_records[winner.attempt_id][0])

        total_spent = _usage_total([_usage(execution.bundle, elapsed) for execution, elapsed, _ in launched_records.values()])
        parent_overhead = UsageRecord(total_tokens=0, cost=0.0)
        reconciles = total_spent.total_tokens is not None and total_spent.cost is not None
        ledger = BudgetLedger(
            case_allocations=tuple(case_allocations),
            attempt_allocations=tuple(attempt_allocations),
            aborted_at_exhaustion=bool(aborted_ids),
            aborted_children=tuple(aborted_ids),
            total_usage=total_spent,
            parent_overhead=parent_overhead,
            global_elapsed_wall_seconds=time.monotonic() - started,
            reconciles_exactly=reconciles,
        )
        mismatch_reasons = []
        if hard_abort_reason:
            mismatch_reasons.append(hard_abort_reason)
        if len(attempts) != len(planned):
            mismatch_reasons.append("not all planned attempts launched")
        if selection == "best_of_n" and any(
            not any(
                item.case_id == case_id and item.reward_value is not None
                for item in attempts
            )
            for case_id in case_ids
            if any(item.case_id == case_id for item in attempts)
        ):
            mismatch_reasons.append("best_of_n requires benchmark reward evidence")
        if not selected:
            mismatch_reasons.append("no selected candidate")
        schedule_valid = (
            not mismatch_reasons
            and observed_max_concurrency <= protocol.parallelism
            and reconciles
        )
        receipt = ScheduleActivationReceipt(
            run_id=run.manifest.metadata.run_id,
            protocol_digest=canonical_digest(protocol),
            scheduler_digest=self.scheduler_digest,
            pipeline_digest=self.pipeline_digest,
            reservation_policy=self.reservation_policy,
            global_deadline_at=(None if global_deadline_at is None else global_deadline_at.isoformat().replace("+00:00", "Z")),
            declared_rollouts_per_case=rollouts,
            observed_attempt_count=len(attempts),
            declared_parallelism=protocol.parallelism,
            observed_max_concurrency=observed_max_concurrency,
            declared_case_order=protocol.case_order,
            observed_case_order=tuple(case_ids),
            declared_state_reset=protocol.state_reset,
            observed_state_reset_count=observed_reset_count,
            declared_candidate_selection=selection,
            observed_selection_policy=selection,
            declared_checkpoint_policy=protocol.checkpoint_policy,
            checkpoint_save_ref=None,
            checkpoint_load_ref=None,
            ancestor_schedule_receipt_ref=None,
            order_seed=run.manifest.execution.seed,
            attempts=tuple(attempts),
            budget_ledger=ledger,
            schedule_valid=schedule_valid,
            mismatch_reasons=tuple(mismatch_reasons),
        )
        if receipt_path is None:
            if self.record_root is None:
                raise SchedulerError("receipt_path or record_root is required")
            receipt_path = self.record_root / run.manifest.metadata.experiment_id / run.manifest_digest / "schedule_activation_receipt.json"
        receipt_path = Path(receipt_path).resolve()
        atomic_write_json(receipt_path, receipt)
        return ScheduleResult(receipt=receipt, receipt_path=receipt_path, selected=tuple(selected), launched=candidate_executions)


__all__ = ["ScheduleResult", "ScheduledAttempt", "Scheduler", "SchedulerError"]
