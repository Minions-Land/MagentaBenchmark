"""Standard rollout trajectory finalization over adapter-native evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from MagentaBench.schemas import (
    ArtifactRef,
    CaseArtifact,
    RolloutTrajectory,
    RunStatus,
    TrajectoryCapture,
    TrajectoryCaptureState,
    TrajectoryEvent,
    TrajectoryEventKind,
    UsageRecord,
)

from .backend.fake import CaseExecution
from .compiler import CompiledRun
from .evidence import artifact_ref, atomic_write_json, sha256_file


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unique_refs(refs: Iterable[ArtifactRef | None]) -> tuple[ArtifactRef, ...]:
    result: list[ArtifactRef] = []
    seen: set[tuple[str, str, int]] = set()
    for ref in refs:
        if ref is None:
            continue
        identity = (ref.path, ref.sha256, ref.size_bytes)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(ref)
    return tuple(result)


def _capture(
    run: CompiledRun,
    execution: CaseExecution,
    usage: UsageRecord,
    native_trace_refs: tuple[ArtifactRef, ...],
) -> TrajectoryCapture:
    bundle = execution.bundle
    no_model = run.manifest.execution.model in {
        "none",
        "none/deterministic",
        "none/echo",
    }
    reasons: dict[str, str] = {}
    if no_model:
        model_io = TrajectoryCaptureState.not_applicable
        tool_io = TrajectoryCaptureState.not_applicable
    elif native_trace_refs:
        model_io = TrajectoryCaptureState.partial
        tool_io = TrajectoryCaptureState.partial
        reasons["model_io"] = "adapter-native trace retained without a complete typed event projection"
        reasons["tool_io"] = "adapter-native trace retained without a complete typed event projection"
    else:
        model_io = TrajectoryCaptureState.unavailable
        tool_io = TrajectoryCaptureState.unavailable
        reasons["model_io"] = "adapter emitted no model input-output trace"
        reasons["tool_io"] = "adapter emitted no tool input-output trace"

    if bundle.log_refs:
        process_io = TrajectoryCaptureState.complete
    elif run.manifest.execution.backend.adapter == "fake":
        process_io = TrajectoryCaptureState.not_applicable
    else:
        process_io = TrajectoryCaptureState.unavailable
        reasons["process_io"] = "adapter emitted no process log artifacts"

    if bundle.verifier_evidence is None:
        evaluator_io = TrajectoryCaptureState.unavailable
        reasons["evaluator_io"] = "rollout terminated without evaluator evidence"
    else:
        evaluator_io = TrajectoryCaptureState.complete

    provenance = bundle.provenance
    if provenance.environment_receipt is not None:
        environment = TrajectoryCaptureState.complete
    else:
        environment = TrajectoryCaptureState.partial
        reasons["environment"] = (
            "resolved backend identity retained but runtime environment receipt unavailable"
        )

    required_usage = (usage.total_tokens, usage.cost, usage.wall_clock_seconds)
    if all(value is not None for value in required_usage):
        resource_usage = TrajectoryCaptureState.complete
    else:
        resource_usage = TrajectoryCaptureState.partial
        reasons["resource_usage"] = "one or more token, cost, or wall-clock counters are unavailable"

    return TrajectoryCapture(
        model_io=model_io,
        tool_io=tool_io,
        process_io=process_io,
        evaluator_io=evaluator_io,
        environment=environment,
        resource_usage=resource_usage,
        reasons=reasons,
    )


def finalize_rollout_trajectory(
    run: CompiledRun,
    case: CaseArtifact,
    execution: CaseExecution,
    *,
    attempt_index: int,
    usage: UsageRecord,
    started_at: datetime,
    finished_at: datetime,
    elapsed_seconds: float,
) -> CaseExecution:
    """Write and bind one complete standardized trajectory index."""

    bundle = execution.bundle
    runtime_trace_ref = (
        None
        if bundle.provenance.runtime_manifest_receipt is None
        else bundle.provenance.runtime_manifest_receipt.trace_ref
    )
    native_trace_refs = _unique_refs((bundle.trace_ref, runtime_trace_ref))
    evaluator_refs = (
        ()
        if bundle.verifier_evidence is None
        else _unique_refs(bundle.verifier_evidence.artifact_refs)
    )
    input_refs = _unique_refs(
        (
            case.public_input_ref,
            *case.task_contract_refs,
            *case.verifier_contract_refs,
        )
    )
    capture = _capture(run, execution, usage, native_trace_refs)
    start_text = _utc(started_at)
    finish_text = _utc(finished_at)
    events: list[TrajectoryEvent] = []

    def append_event(
        kind: TrajectoryEventKind,
        *,
        occurred_at: str,
        input_refs: tuple[ArtifactRef, ...] = (),
        output_refs: tuple[ArtifactRef, ...] = (),
        status: RunStatus | None = None,
        event_usage: UsageRecord | None = None,
        details: dict[str, object] | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        sequence = len(events) + 1
        event_id = f"{bundle.run_id}.event-{sequence:04d}"
        events.append(
            TrajectoryEvent(
                event_id=event_id,
                parent_event_id=(None if not events else events[-1].event_id),
                sequence=sequence,
                kind=kind,
                occurred_at=occurred_at,
                duration_seconds=duration_seconds,
                input_refs=input_refs,
                output_refs=output_refs,
                usage=event_usage,
                status=status,
                details={} if details is None else details,
            )
        )

    append_event(
        TrajectoryEventKind.rollout_started,
        occurred_at=start_text,
        input_refs=input_refs,
        details={
            "parent_run_id": run.manifest.metadata.run_id,
            "attempt_index": attempt_index,
            "backend_adapter": run.manifest.execution.backend.adapter,
            "subject_id": run.manifest.subject.id,
        },
    )
    append_event(
        TrajectoryEventKind.environment_snapshot,
        occurred_at=start_text,
        output_refs=_unique_refs((bundle.provenance.container_receipt_ref,)),
        details={
            "backend_kind": bundle.provenance.backend_kind,
            "network_mode": bundle.provenance.network_mode,
            "workspace_namespace": bundle.provenance.workspace_namespace,
        },
    )
    if native_trace_refs:
        append_event(
            TrajectoryEventKind.native_trace_attached,
            occurred_at=finish_text,
            output_refs=native_trace_refs,
        )
    if bundle.verifier_evidence is not None:
        append_event(
            TrajectoryEventKind.evaluator_response,
            occurred_at=finish_text,
            input_refs=bundle.output_refs,
            output_refs=evaluator_refs,
            details={
                "verifier": bundle.verifier_evidence.verifier,
                "passed": bundle.verifier_evidence.passed,
                "score": bundle.verifier_evidence.score,
                "metrics": dict(bundle.verifier_evidence.metrics),
            },
        )
    if bundle.status not in {
        RunStatus.pass_,
        RunStatus.verified_fail,
        RunStatus.scored,
    }:
        append_event(
            TrajectoryEventKind.exception,
            occurred_at=finish_text,
            status=bundle.status,
            output_refs=bundle.log_refs,
            details={"terminal_status": bundle.status.value},
        )
    append_event(
        TrajectoryEventKind.rollout_finished,
        occurred_at=finish_text,
        output_refs=_unique_refs((*bundle.output_refs, *bundle.log_refs, *evaluator_refs)),
        status=bundle.status,
        event_usage=usage,
        duration_seconds=elapsed_seconds,
    )

    configuration = run.manifest.metadata.configuration
    trajectory = RolloutTrajectory(
        parent_run_id=run.manifest.metadata.run_id,
        attempt_id=bundle.run_id,
        case_id=case.case_id,
        attempt_index=attempt_index,
        manifest_digest=run.manifest_digest,
        evaluator_digest=run.manifest.evaluator.artifact_digest,
        metric_digests=tuple(
            artifact.artifact_digest for artifact in run.manifest.metrics
        ),
        configuration_digest=(
            None if configuration is None else configuration.artifact_digest
        ),
        started_at=start_text,
        finished_at=finish_text,
        elapsed_seconds=elapsed_seconds,
        terminal_status=bundle.status,
        input_refs=input_refs,
        output_refs=bundle.output_refs,
        log_refs=bundle.log_refs,
        native_trace_refs=native_trace_refs,
        evaluator_refs=evaluator_refs,
        provenance=bundle.provenance,
        verifier_evidence=bundle.verifier_evidence,
        usage=usage,
        capture=capture,
        events=tuple(events),
    )
    trajectory_path = Path(execution.bundle_path).parent / "rollout_trajectory.json"
    atomic_write_json(trajectory_path, trajectory)
    updated_bundle = bundle.model_copy(
        update={"trajectory_ref": artifact_ref(trajectory_path), "usage": usage}
    )
    atomic_write_json(execution.bundle_path, updated_bundle)
    return replace(
        execution,
        bundle=updated_bundle,
        bundle_digest=sha256_file(execution.bundle_path),
    )


__all__ = ["finalize_rollout_trajectory"]
