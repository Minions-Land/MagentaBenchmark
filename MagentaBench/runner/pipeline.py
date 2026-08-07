"""End-to-end BMP execution, checkpoint, resume, aggregation and reports."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from math import isclose
from pathlib import Path
from typing import Any

from MagentaBench.schemas import (
    CheckpointSaveReceipt,
    ClaimReport,
    RunPurpose,
    RunReport,
    ScheduleActivationReceipt,
    canonical_digest,
)

from .backend.fake import CaseExecution, EvidenceDriftError, FakeBackend
from .compiler import CompiledRun, Compiler, canonical_json_bytes, sha256_bytes
from .evidence import atomic_write_bytes, atomic_write_json, sha256_file
from .gates import CompletedRun, _receipt_binding_errors, evaluate_run_report
from .scheduler import ScheduleResult, Scheduler


class ResumeDriftError(RuntimeError):
    """Resume was requested but pinned execution identity has changed."""


class InjectedInterruption(RuntimeError):
    """Conformance-only interruption raised after a checkpoint boundary."""


@dataclass(frozen=True)
class PipelineResult:
    runs: tuple[CompletedRun, ...]
    aggregate_path: Path
    report_path: Path
    report: RunReport


class Pipeline:
    def __init__(
        self,
        project_root: str | Path,
        record_root: str | Path,
        *,
        backend: Any | None = None,
        allow_test_override: bool = False,
    ) -> None:
        if backend is not None and not allow_test_override:
            raise ValueError(
                "backend injection requires allow_test_override=true"
            )
        self.project_root = Path(project_root).resolve()
        self.record_root = Path(record_root).resolve()
        self.compiler = Compiler(
            self.project_root, allow_test_override=allow_test_override
        )
        self.backend = backend
        self.allow_test_override = allow_test_override
        self.scheduler = Scheduler(record_root=self.record_root)

    @staticmethod
    def _read_json(path: Path) -> Any:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _append_event(path: Path, event: dict[str, Any]) -> None:
        existing = path.read_bytes() if path.is_file() else b""
        sequence = 1 + sum(1 for line in existing.splitlines() if line.strip())
        payload = dict(event)
        payload["seq"] = sequence
        atomic_write_bytes(path, existing + canonical_json_bytes(payload) + b"\n")

    @staticmethod
    def _task_manifest_digest(run: CompiledRun) -> str:
        benchmark = run.manifest.benchmark
        task_manifest = getattr(benchmark, "task_manifest", None)
        source = getattr(benchmark, "source", None)
        if task_manifest and source:
            path = Path(source) / str(task_manifest)
            if not path.is_file():
                raise FileNotFoundError(f"resolved task manifest is missing: {path}")
            return sha256_file(path)
        return benchmark.artifact_digest

    @staticmethod
    def _verifier_digest(run: CompiledRun) -> str:
        verifier_id = str(getattr(run.manifest.benchmark, "verifier", "unknown"))
        verifier_code = Path(__file__).parents[1] / "adapters" / "fake" / "verifier.py"
        return sha256_bytes(verifier_id.encode("utf-8") + b"\0" + verifier_code.read_bytes())

    def _plan(self, runs: list[CompiledRun]) -> dict[str, Any]:
        return {
            "runner_digest": self.backend.runner_digest,
            "scheduler_digest": self.scheduler.scheduler_digest,
            "runs": [
                {
                    "run_id": run.manifest.metadata.run_id,
                    "manifest_digest": run.manifest_digest,
                    "benchmark_digest": run.manifest.benchmark.artifact_digest,
                    "task_manifest_digest": self._task_manifest_digest(run),
                    "verifier_digest": self._verifier_digest(run),
                    "subject_digest": run.manifest.subject.artifact_digest,
                    "backend_digest": run.manifest.execution.backend.digest,
                    "factors": dict(run.factor_values),
                }
                for run in runs
            ],
        }

    @staticmethod
    def _counterbalanced_order(
        runs: list[CompiledRun],
        *,
        control_id: str,
        treatment_id: str,
        enabled: bool,
    ) -> list[CompiledRun]:
        if not enabled or control_id == treatment_id:
            return list(runs)
        grouped: dict[bytes, dict[str, CompiledRun]] = {}
        group_order: list[bytes] = []
        for run in runs:
            factors = {
                key: value
                for key, value in run.factor_values.items()
                if key not in {"subject", "experiment.subject"}
            }
            key = canonical_json_bytes(factors)
            if key not in grouped:
                grouped[key] = {}
                group_order.append(key)
            grouped[key][run.manifest.subject.id] = run
        ordered: list[CompiledRun] = []
        for key in group_order:
            pair = grouped[key]
            if set(pair) != {control_id, treatment_id}:
                ordered.extend(pair.values())
                continue
            repetition = pair[control_id].factor_values.get("repetition", 0)
            treatment_first = int(repetition) % 2 == 1
            ids = (
                (treatment_id, control_id)
                if treatment_first
                else (control_id, treatment_id)
            )
            ordered.extend(pair[subject_id] for subject_id in ids)
        return ordered

    def _validate_resume(self, plan_path: Path, expected: dict[str, Any]) -> None:
        try:
            actual = self._read_json(plan_path)
        except (OSError, ValueError) as exc:
            raise ResumeDriftError(f"resume plan is missing or unreadable: {exc}") from exc
        if canonical_json_bytes(actual) != canonical_json_bytes(expected):
            raise ResumeDriftError(
                "resume refused: manifest/backend/runner/task/verifier plan drift"
            )

    def _validate_checkpoint(
        self, checkpoint_path: Path, plan_path: Path, runs: list[CompiledRun]
    ) -> None:
        try:
            checkpoint = self._read_json(checkpoint_path)
            next_index = int(checkpoint["next_index"])
            completed = checkpoint["completed"]
            schedule_receipts = checkpoint["schedule_receipts"]
            plan_digest = checkpoint["plan_sha256"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ResumeDriftError(
                f"resume checkpoint is missing or malformed: {exc}"
            ) from exc
        if plan_digest != sha256_file(plan_path):
            raise ResumeDriftError("resume checkpoint references a drifted plan")
        if not isinstance(completed, dict) or next_index != len(completed):
            raise ResumeDriftError("resume checkpoint completion ledger is inconsistent")
        if next_index < 0 or next_index > len(runs):
            raise ResumeDriftError("resume checkpoint next_index is out of range")
        expected_ids = {
            run.manifest.metadata.run_id for run in runs[:next_index]
        }
        if set(completed) != expected_ids:
            raise ResumeDriftError("resume checkpoint completed run ids drift")
        if not isinstance(schedule_receipts, dict) or set(schedule_receipts) != expected_ids:
            raise ResumeDriftError("resume checkpoint schedule receipt lineage drift")
        for run in runs[:next_index]:
            run_id = run.manifest.metadata.run_id
            receipt_path = (
                self.backend.run_directory(run) / "schedule_activation_receipt.json"
            )
            if (
                not receipt_path.is_file()
                or schedule_receipts[run_id] != sha256_file(receipt_path)
            ):
                raise ResumeDriftError(
                    f"resume checkpoint schedule receipt digest drift: {run_id}"
                )
            try:
                receipt = ScheduleActivationReceipt.model_validate(
                    self._read_json(receipt_path)
                )
                selected_refs = [
                    item.evidence_bundle_ref
                    for item in receipt.attempts
                    if item.selected and item.evidence_bundle_ref is not None
                ]
            except (OSError, ValueError) as exc:
                raise ResumeDriftError(
                    f"resume checkpoint schedule receipt malformed: {run_id}"
                ) from exc
            if (
                len(selected_refs) != 1
                or completed[run_id] != selected_refs[0].sha256
            ):
                raise ResumeDriftError(
                    f"resume checkpoint completed bundle digest drift: {run_id}"
                )

    @staticmethod
    def _record_checkpoint_save(
        schedule: ScheduleResult,
        checkpoint_path: Path,
        completion_sequence: int,
    ) -> ScheduleResult:
        save_receipt = CheckpointSaveReceipt(
            written_digest=sha256_file(checkpoint_path),
            size_bytes=checkpoint_path.stat().st_size,
            write_completion_sequence=completion_sequence,
            path=str(checkpoint_path.resolve()),
        )
        receipt = schedule.receipt.model_copy(
            update={"checkpoint_save_ref": save_receipt}
        )
        receipt = ScheduleActivationReceipt.model_validate(
            receipt.model_dump(mode="json")
        )
        atomic_write_json(schedule.receipt_path, receipt)
        return replace(schedule, receipt=receipt)

    def _validate_receipt_identity(
        self, run: CompiledRun, receipt: ScheduleActivationReceipt
    ) -> None:
        if receipt.run_id != run.manifest.metadata.run_id:
            raise ResumeDriftError("resume schedule evidence run_id drift")
        if receipt.order_seed != run.manifest.execution.seed:
            raise ResumeDriftError("resume schedule evidence order_seed drift")
        if receipt.scheduler_digest != self.scheduler.scheduler_digest:
            raise ResumeDriftError("resume schedule evidence scheduler digest drift")
        if receipt.pipeline_digest != self.scheduler.pipeline_digest:
            raise ResumeDriftError("resume schedule evidence pipeline digest drift")
        case_ids = tuple(
            item.case_id for item in receipt.budget_ledger.case_allocations
        )
        if receipt.observed_case_order != case_ids:
            raise ResumeDriftError(
                "resume schedule observed_case_order allocation drift"
            )
        budget = run.manifest.execution.budget
        for field in ("max_tokens", "max_cost"):
            declared = getattr(budget, field)
            values = [
                getattr(item.allocated, field)
                for item in receipt.budget_ledger.case_allocations
            ]
            if declared is None:
                if any(value is not None for value in values):
                    raise ResumeDriftError(
                        f"resume schedule root {field} allocation drift"
                    )
            elif any(value is None for value in values):
                raise ResumeDriftError(
                    f"resume schedule root {field} allocation drift"
                )
            else:
                total = sum(values)
                matches = (
                    total == declared
                    if field == "max_tokens"
                    else isclose(total, declared, rel_tol=0.0, abs_tol=1e-12)
                )
                if not matches:
                    raise ResumeDriftError(
                        f"resume schedule root {field} allocation drift"
                    )

    def _load_schedule_execution(
        self, run: CompiledRun, receipt_path: Path
    ) -> ScheduleResult | None:
        try:
            receipt = ScheduleActivationReceipt.model_validate(
                self._read_json(receipt_path)
            )
        except (OSError, ValueError):
            return None
        self._validate_receipt_identity(run, receipt)
        protocol = run.manifest.execution.protocol
        if protocol is None:
            raise ResumeDriftError("schedule receipt exists for a run without protocol")
        if receipt.protocol_digest != canonical_digest(protocol):
            raise ResumeDriftError("resume schedule evidence protocol digest drift")
        launched: list[CaseExecution] = []
        selected: list[CaseExecution] = []
        for attempt in receipt.attempts:
            if attempt.evidence_bundle_ref is None:
                raise ResumeDriftError("launched attempt lacks evidence bundle reference")
            ref = attempt.evidence_bundle_ref
            bundle_path = Path(ref.path)
            if not bundle_path.is_file():
                return None
            case = self.backend.load_completed(
                run,
                bundle_path,
                expected_runner_digest=self.backend.runner_digest,
            )
            if case is None:
                return None
            if (
                bundle_path.stat().st_size != ref.size_bytes
                or sha256_file(bundle_path) != ref.sha256
                or case.bundle.run_id != attempt.attempt_id
            ):
                return None
            case = replace(case, case_id=attempt.case_id)
            launched.append(case)
            if attempt.selected:
                selected.append(case)
        if receipt.schedule_valid and not selected:
            return None
        representative = selected[0] if selected else launched[0]
        binding_errors = _receipt_binding_errors(
            CompletedRun(
                plan=run,
                case=representative,
                schedule_receipt=receipt,
                schedule_receipt_path=receipt_path,
                schedule_receipt_sha256=sha256_file(receipt_path),
                scheduler_digest=self.scheduler.scheduler_digest,
                pipeline_digest=self.scheduler.pipeline_digest,
                runner_digest=self.backend.runner_digest,
            )
        )
        if binding_errors:
            raise ResumeDriftError(
                "resume schedule receipt binding drift: "
                + "; ".join(binding_errors)
            )
        return ScheduleResult(
            receipt=receipt,
            receipt_path=receipt_path,
            selected=tuple(selected),
            launched=tuple(launched),
        )

    def run(
        self,
        experiment_path: str | Path,
        *,
        resume: bool = False,
        stop_after: int | None = None,
    ) -> PipelineResult:
        compiled = self.compiler.compile(experiment_path, record_root=self.record_root)
        if not compiled:
            raise ValueError("experiment expanded to zero runs")
        if self.backend is None:
            adapters = {
                run.manifest.execution.backend.adapter for run in compiled
            }
            if adapters != {"fake"}:
                raise RuntimeError(
                    "production Pipeline has no registered constructor for adapters "
                    f"{sorted(adapters)}; registered adapters: ['fake']"
                )
            self.backend = FakeBackend(self.record_root)
        contrast = compiled[0].manifest.contrast
        control_id = contrast.control_id or compiled[0].manifest.subject.id
        treatment_id = contrast.treatment_id or compiled[-1].manifest.subject.id
        counterbalanced = contrast.counterbalanced
        compiled = self._counterbalanced_order(
            compiled,
            control_id=control_id,
            treatment_id=treatment_id,
            enabled=counterbalanced,
        )
        experiment_id = compiled[0].manifest.metadata.experiment_id
        experiment_dir = self.record_root / experiment_id
        experiment_dir.mkdir(parents=True, exist_ok=True)
        plan_path = experiment_dir / "plan.json"
        checkpoint_path = experiment_dir / "checkpoint.json"
        plan = self._plan(compiled)
        checkpoint_loaded = False
        if resume:
            self._validate_resume(plan_path, plan)
            self._validate_checkpoint(checkpoint_path, plan_path, compiled)
            checkpoint_loaded = True
        else:
            protocol = compiled[0].manifest.execution.protocol
            if (
                protocol is not None
                and protocol.checkpoint_policy == "disabled"
                and checkpoint_path.exists()
            ):
                raise ResumeDriftError(
                    "checkpoint policy 'disabled' requires no checkpoint file"
                )
            atomic_write_json(plan_path, plan)

        events_path = experiment_dir / "events.jsonl"
        self._append_event(
            events_path,
            {"seq": 1, "event": "resume" if resume else "start", "runs": len(compiled)},
        )

        completed: list[CompletedRun] = []
        reused_ids: list[str] = []
        executed_ids: list[str] = []
        for run in compiled:
            observed_backend_adapter = getattr(self.backend, "adapter", None)
            declared_backend_adapter = run.manifest.execution.backend.adapter
            if observed_backend_adapter != declared_backend_adapter:
                raise RuntimeError(
                    "execution backend adapter mismatch: declared "
                    f"{declared_backend_adapter!r}, observed {observed_backend_adapter!r}"
                )
            protocol = run.manifest.execution.protocol
            if protocol is None:
                raise ValueError("resolved protocol missing")
            if resume and protocol.checkpoint_policy not in {"resume", "save_and_resume"}:
                raise ResumeDriftError(
                    f"checkpoint policy {protocol.checkpoint_policy!r} forbids resume"
                )
            if not resume and protocol.checkpoint_policy == "resume":
                raise ResumeDriftError("checkpoint policy 'resume' requires resume=true")
            task = self.backend._load_task(run)
            receipt_path = (
                self.backend.run_directory(run) / "schedule_activation_receipt.json"
            )
            schedule: ScheduleResult | None = None
            if resume and receipt_path.is_file():
                try:
                    schedule = self._load_schedule_execution(run, receipt_path)
                except EvidenceDriftError as exc:
                    raise ResumeDriftError(str(exc)) from exc
            if schedule is None:
                schedule = self.scheduler.execute(
                    run,
                    [task],
                    attempt_runner=lambda attempt, run=run, task=task: self.backend.execute(
                        run,
                        task,
                        case_id=attempt.attempt_id,
                        execution_run_id=attempt.attempt_id,
                        attempt_budget=attempt.allocation,
                        remaining_wall_seconds=attempt.remaining_wall_seconds,
                    ),
                    reset_state=getattr(self.backend, "reset_state", None),
                    receipt_path=receipt_path,
                )
                executed_ids.append(run.manifest.metadata.run_id)
            else:
                reused_ids.append(run.manifest.metadata.run_id)
            if len(schedule.selected) != 1:
                raise RuntimeError(
                    "schedule did not produce exactly one selected candidate per case"
                )
            case = schedule.selected[0]
            completed.append(
                CompletedRun(
                    plan=run,
                    case=case,
                    schedule_receipt=schedule.receipt,
                    schedule_receipt_path=schedule.receipt_path,
                    schedule_receipt_sha256=sha256_file(schedule.receipt_path),
                    scheduler_digest=self.scheduler.scheduler_digest,
                    pipeline_digest=self.scheduler.pipeline_digest,
                    runner_digest=self.backend.runner_digest,
                )
            )

            token_values = [
                item.schedule_receipt.budget_ledger.total_usage.total_tokens
                for item in completed
                if item.schedule_receipt is not None
            ]
            cost_values = [
                item.schedule_receipt.budget_ledger.total_usage.cost
                for item in completed
                if item.schedule_receipt is not None
            ]
            wall_values = [
                item.schedule_receipt.budget_ledger.global_elapsed_wall_seconds
                for item in completed
                if item.schedule_receipt is not None
            ]
            checkpoint = {
                "plan_sha256": sha256_file(plan_path),
                "completed": {
                    item.plan.manifest.metadata.run_id: item.case.bundle_digest
                    for item in completed
                },
                "next_index": len(completed),
                "budget_ledger": {
                    "tokens": (
                        sum(token_values)
                        if token_values and all(value is not None for value in token_values)
                        else None
                    ),
                    "cost": (
                        sum(cost_values)
                        if cost_values and all(value is not None for value in cost_values)
                        else None
                    ),
                    "wall_clock_seconds": sum(wall_values),
                },
                "schedule_receipts": {
                    item.plan.manifest.metadata.run_id: item.schedule_receipt_sha256
                    for item in completed
                },
            }
            if protocol.checkpoint_policy in {"save", "save_and_resume"}:
                atomic_write_json(checkpoint_path, checkpoint)
                self._validate_checkpoint(checkpoint_path, plan_path, compiled)
                schedule = self._record_checkpoint_save(
                    schedule, checkpoint_path, len(completed)
                )
                completed[-1] = replace(
                    completed[-1],
                    schedule_receipt=schedule.receipt,
                    schedule_receipt_sha256=sha256_file(schedule.receipt_path),
                )
                checkpoint["schedule_receipts"][
                    run.manifest.metadata.run_id
                ] = completed[-1].schedule_receipt_sha256
                atomic_write_json(checkpoint_path, checkpoint)
                self._validate_checkpoint(checkpoint_path, plan_path, compiled)
            if stop_after is not None and len(completed) >= stop_after:
                self._append_event(
                    events_path,
                    {"seq": len(completed) + 1, "event": "interrupted"},
                )
                raise InjectedInterruption(
                    f"injected interruption after {len(completed)} run(s)"
                )

        protocol = compiled[0].manifest.execution.protocol
        deterministic = bool(getattr(protocol, "deterministic_conformance", False))
        experiment_digest = sha256_bytes(
            canonical_json_bytes([run.manifest_digest for run in compiled])
        )
        report = evaluate_run_report(
            experiment_id=experiment_id,
            experiment_digest=experiment_digest,
            completed=completed,
            expected_run_count=len(compiled),
            control_id=control_id,
            treatment_id=treatment_id,
            deterministic_conformance=deterministic,
            counterbalanced=counterbalanced,
        )
        report_name = (
            "claim_report.json"
            if report.purpose == RunPurpose.claim
            else "observation_report.json"
        )
        report_path = experiment_dir / report_name
        atomic_write_json(report_path, report)
        aggregate = {
            "experiment_id": experiment_id,
            "experiment_digest": experiment_digest,
            "run_count": len(completed),
            "statuses": [item.case.bundle.status.value for item in completed],
            "scores": [
                None
                if item.case.bundle.verifier_evidence is None
                else item.case.bundle.verifier_evidence.score
                for item in completed
            ],
            "schedule_receipts": {
                item.plan.manifest.metadata.run_id: item.schedule_receipt_sha256
                for item in completed
            },
            "run_report_sha256": sha256_file(report_path),
        }
        aggregate_path = experiment_dir / "aggregate.json"
        atomic_write_json(aggregate_path, aggregate)
        if resume:
            atomic_write_json(
                experiment_dir / "resume_receipt.json",
                {"resume": True, "reused": reused_ids, "rerun": executed_ids},
            )
        complete_event: dict[str, Any] = {
            "seq": len(completed) + 2,
            "event": "complete",
            "purpose": report.purpose.value,
        }
        if isinstance(report, ClaimReport):
            complete_event["eligible"] = report.claim_eligible
        self._append_event(events_path, complete_event)
        return PipelineResult(
            runs=tuple(completed),
            aggregate_path=aggregate_path,
            report_path=report_path,
            report=report,
        )


__all__ = [
    "InjectedInterruption",
    "Pipeline",
    "PipelineResult",
    "ResumeDriftError",
]
