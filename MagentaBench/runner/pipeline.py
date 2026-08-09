"""End-to-end BMP execution, checkpoint, resume, aggregation and reports."""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, replace
from math import isclose
from pathlib import Path
from typing import Any, Mapping

from MagentaBench.schemas import (
    ArtifactRef,
    CaseSetActivationReceipt,
    CheckpointLoadReceipt,
    CheckpointSaveReceipt,
    ClaimReport,
    EvidenceBundle,
    ProvenanceRecord,
    RecordIndex,
    RunPurpose,
    RunReport,
    RunStatus,
    ScheduleActivationReceipt,
    UsageRecord,
    canonical_digest,
)

from .adapter_registry import (
    AdapterRegistry,
    LoadedCaseSet,
    ResolvedCaseSet,
    verify_resolved_case_set,
    write_immutable_json,
)
from .backend.fake import CaseExecution, EvidenceDriftError
from .compiler import CompiledRun, Compiler, canonical_json_bytes, sha256_bytes
from .case_order import CaseOrderError, selected_case_ids
from .evidence import artifact_ref, atomic_write_bytes, atomic_write_json, sha256_file
from .gates import CompletedRun, _receipt_binding_errors, evaluate_run_report
from .model_activation import ensure_model_activation_receipt
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
        adapter_registry: AdapterRegistry | None = None,
        allow_test_override: bool = False,
    ) -> None:
        if (backend is not None or adapter_registry is not None) and not allow_test_override:
            raise ValueError(
                "backend or adapter-registry injection requires "
                "allow_test_override=true"
            )
        self.project_root = Path(project_root).resolve()
        self.record_root = Path(record_root).resolve()
        self.compiler = Compiler(
            self.project_root, allow_test_override=allow_test_override
        )
        self.backend = backend
        self._load_project_adapters = adapter_registry is None
        self.adapter_registry = adapter_registry or AdapterRegistry.production()
        self.allow_test_override = allow_test_override
        self.scheduler = Scheduler(record_root=self.record_root)

    def _verify_adapter_activation(self, run: CompiledRun) -> None:
        """Bind resolved capability artifacts to the active runtime registry."""

        for artifact in run.manifest.metadata.adapter_capabilities:
            if artifact.capability.adapter_kind == "execution":
                observed = self.adapter_registry.execution_capability(run)
            else:
                observed = self.adapter_registry.capability(
                    artifact.capability.adapter,
                    artifact.capability.adapter_kind,
                )
            if observed != artifact.capability:
                raise ResumeDriftError(
                    f"active adapter capability drift: {artifact.capability.adapter!r}"
                )
            if artifact.source_closure_digest is not None:
                observed_closure = self.adapter_registry.source_closure_digest(
                    artifact.capability, run
                )
                if observed_closure != artifact.source_closure_digest:
                    raise ResumeDriftError(
                        "active adapter source closure drift: "
                        f"{artifact.capability.adapter!r}"
                    )

    def _record_attempt_exception(
        self,
        run: CompiledRun,
        case_id: str,
        attempt_id: str,
        exc: Exception,
    ) -> CaseExecution:
        """Turn an uncaught worker failure into retained rollout evidence."""

        if isinstance(exc, TimeoutError):
            status = RunStatus.timeout
        elif isinstance(exc, (ConnectionError, OSError)):
            status = RunStatus.infra_error
        else:
            status = RunStatus.harness_fault
        directory = (
            self.backend.run_directory(run)
            / "attempt_failures"
            / attempt_id
        )
        directory.mkdir(parents=True, exist_ok=True)
        exception_path = directory / "exception.txt"
        atomic_write_bytes(
            exception_path,
            "".join(traceback.format_exception(exc)).encode("utf-8", errors="replace"),
        )
        runner_digest = self.backend.runner_digest
        provenance = ProvenanceRecord(
            manifest_digest=run.manifest_digest,
            runner_digest=runner_digest,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            subject_digest=run.manifest.subject.artifact_digest,
            backend_digest=(
                run.manifest.execution.backend.digest or runner_digest
            ),
            trace_emission_claimed=False,
            backend_kind=run.manifest.execution.backend.kind,
            network_mode=str(
                run.manifest.execution.backend.defaults.get(
                    "network_mode",
                    run.manifest.execution.backend.defaults.get("network", "unobserved"),
                )
            ),
        )
        bundle = EvidenceBundle(
            run_id=attempt_id,
            status=status,
            log_refs=(artifact_ref(exception_path),),
            usage=UsageRecord(total_tokens=None, cost=None),
            provenance=provenance,
        )
        bundle_path = directory / "evidence_bundle.json"
        atomic_write_json(bundle_path, bundle)
        return CaseExecution(
            case_id=case_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=sha256_file(bundle_path),
        )

    def _execute_attempt(
        self,
        run: CompiledRun,
        adapter: Any,
        case: Any,
        attempt: Any,
    ) -> CaseExecution:
        try:
            return ensure_model_activation_receipt(
                run,
                adapter.execute(self.backend, run, case, attempt),
            )
        except Exception as exc:
            return self._record_attempt_exception(
                run,
                attempt.case_id,
                attempt.attempt_id,
                exc,
            )

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

    def _plan(
        self,
        runs: list[CompiledRun],
        case_sets: Mapping[str, LoadedCaseSet],
    ) -> dict[str, Any]:
        return {
            "runner_digest": self.backend.runner_digest,
            "scheduler_digest": self.scheduler.scheduler_digest,
            "runs": [
                {
                    "run_id": run.manifest.metadata.run_id,
                    "manifest_digest": run.manifest_digest,
                    "benchmark_digest": run.manifest.benchmark.artifact_digest,
                    "dataset_digest": run.manifest.dataset.artifact_digest,
                    "subject_digest": run.manifest.subject.artifact_digest,
                    "backend_digest": run.manifest.execution.backend.digest,
                    "benchmark_loader_digest": (
                        self.adapter_registry.benchmark_loader(run).digest
                    ),
                    "execution_adapter_digest": (
                        self.adapter_registry.execution_adapter(run).digest
                    ),
                    "case_set_digest": case_sets[
                        run.manifest.metadata.run_id
                    ].artifact.canonical_digest(),
                    "ordered_case_ids": list(
                        case_sets[
                            run.manifest.metadata.run_id
                        ].artifact.ordered_case_ids
                    ),
                    "checkpoint_policy": run.manifest.execution.protocol.checkpoint_policy,
                    "manifest_identity": run.manifest.identity_data(),
                    "factors": dict(run.factor_values),
                }
                for run in runs
            ],
        }

    def _activate_case_set(
        self,
        run: CompiledRun,
        experiment_dir: Path,
    ) -> tuple[LoadedCaseSet, CaseSetActivationReceipt, Path]:
        self._verify_adapter_activation(run)
        loader = self.adapter_registry.benchmark_loader(run)
        artifact_root = experiment_dir / "case_sets" / run.manifest_digest
        resolved = loader.resolve(run, artifact_root)
        verify_resolved_case_set(
            run,
            resolved,
            expected_loader_adapter=loader.adapter,
            expected_loader_digest=loader.digest,
        )
        loaded = loader.load(run, resolved)
        verify_resolved_case_set(
            run,
            ResolvedCaseSet(
                artifact=loaded.artifact,
                artifact_path=loaded.artifact_path,
                artifact_sha256=loaded.artifact_sha256,
            ),
            expected_loader_adapter=loader.adapter,
            expected_loader_digest=loader.digest,
        )
        receipt = CaseSetActivationReceipt(
            case_set_ref=artifact_ref(loaded.artifact_path),
            case_set_digest=loaded.artifact.canonical_digest(),
            loader_adapter=loader.adapter,
            loader_digest=loader.digest,
            dataset_id=loaded.artifact.dataset_id,
            dataset_digest=loaded.artifact.dataset_digest,
            ordered_case_ids=loaded.artifact.ordered_case_ids,
        )
        receipt_path = (
            loaded.artifact_path.parent
            / "case_set_activation_receipt.json"
        )
        write_immutable_json(
            receipt_path,
            receipt,
            label="case-set activation receipt",
        )
        return loaded, receipt, receipt_path

    @staticmethod
    def _counterbalanced_order(
        runs: list[CompiledRun],
        *,
        control_value: Any,
        treatment_value: Any,
        factor_path: str | None = None,
        enabled: bool,
    ) -> list[CompiledRun]:
        control_key = canonical_json_bytes(control_value)
        treatment_key = canonical_json_bytes(treatment_value)
        if not enabled or control_key == treatment_key:
            return list(runs)
        grouped: dict[bytes, dict[bytes, CompiledRun]] = {}
        group_order: list[bytes] = []
        for run in runs:
            factors = {
                key: value
                for key, value in run.factor_values.items()
                if key not in {"subject", "experiment.subject", factor_path}
            }
            key = canonical_json_bytes(factors)
            if key not in grouped:
                grouped[key] = {}
                group_order.append(key)
            if factor_path is not None and factor_path not in run.factor_values:
                raise RuntimeError(
                    f"counterbalance factor {factor_path!r} is absent from a run"
                )
            arm = canonical_json_bytes(
                run.manifest.subject.id
                if factor_path is None
                else run.factor_values[factor_path]
            )
            grouped[key][arm] = run
        ordered: list[CompiledRun] = []
        for key in group_order:
            pair = grouped[key]
            if set(pair) != {control_key, treatment_key}:
                ordered.extend(pair.values())
                continue
            repetition = pair[control_key].factor_values.get("repetition", 0)
            treatment_first = int(repetition) % 2 == 1
            arms = (
                (treatment_key, control_key)
                if treatment_first
                else (control_key, treatment_key)
            )
            ordered.extend(pair[arm] for arm in arms)
        return ordered

    @staticmethod
    def _is_checkpoint_transition(
        previous: dict[str, Any], expected: dict[str, Any]
    ) -> bool:
        previous_runs = previous.get("runs")
        expected_runs = expected.get("runs")
        if not isinstance(previous_runs, list) or not isinstance(expected_runs, list):
            return False
        if len(previous_runs) != len(expected_runs):
            return False
        previous_copy = json.loads(json.dumps(previous))
        expected_copy = json.loads(json.dumps(expected))
        for old_run, new_run in zip(
            previous_copy["runs"], expected_copy["runs"], strict=True
        ):
            if old_run.get("checkpoint_policy") != "save":
                return False
            if new_run.get("checkpoint_policy") != "save_and_resume":
                return False
            old_run["checkpoint_policy"] = "save_and_resume"
            old_manifest = old_run.get("manifest_identity", {})
            new_manifest = new_run.get("manifest_identity", {})
            try:
                old_manifest["execution"]["protocol"]["checkpoint_policy"] = (
                    "save_and_resume"
                )
                old_run["manifest_digest"] = new_run["manifest_digest"]
            except (KeyError, TypeError):
                return False
            if old_manifest != new_manifest:
                return False
        return canonical_json_bytes(previous_copy) == canonical_json_bytes(expected_copy)

    def _validate_resume(self, plan_path: Path, expected: dict[str, Any]) -> dict[str, Any]:
        try:
            actual = self._read_json(plan_path)
        except (OSError, ValueError) as exc:
            raise ResumeDriftError(f"resume plan is missing or unreadable: {exc}") from exc
        if canonical_json_bytes(actual) != canonical_json_bytes(expected) and not self._is_checkpoint_transition(actual, expected):
            raise ResumeDriftError(
                "resume refused: manifest/backend/runner/task/verifier plan drift"
            )
        return actual

    def _validate_checkpoint(
        self,
        checkpoint_path: Path,
        plan_path: Path,
        runs: list[CompiledRun],
        *,
        accepted_save_plan_digests: set[str] | None = None,
    ) -> None:
        try:
            checkpoint = self._read_json(checkpoint_path)
            next_index = int(checkpoint["next_index"])
            completed = checkpoint["completed"]
            schedule_receipts = checkpoint["schedule_receipts"]
            schedule_receipt_paths = checkpoint["schedule_receipt_paths"]
            plan_digest = checkpoint["plan_sha256"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ResumeDriftError(
                f"resume checkpoint is missing or malformed: {exc}"
            ) from exc
        if plan_digest != sha256_file(plan_path):
            raise ResumeDriftError("resume checkpoint references a drifted plan")
        if accepted_save_plan_digests is None:
            accepted_save_plan_digests = {plan_digest}
        else:
            # The checkpoint root remains bound to the active plan.  The
            # additional digests are only for immutable ancestor snapshots
            # retained across an explicit save -> save_and_resume transition.
            accepted_save_plan_digests = set(accepted_save_plan_digests)
            accepted_save_plan_digests.add(plan_digest)
        retained_plan_digests = checkpoint.get("retained_plan_sha256", ())
        if isinstance(retained_plan_digests, str):
            retained_plan_digests = (retained_plan_digests,)
        if not isinstance(retained_plan_digests, (list, tuple)):
            raise ResumeDriftError(
                "resume checkpoint retained plan digest metadata is malformed"
            )
        for retained_digest in retained_plan_digests:
            if (
                not isinstance(retained_digest, str)
                or len(retained_digest) != 64
                or any(char not in "0123456789abcdef" for char in retained_digest)
            ):
                raise ResumeDriftError(
                    "resume checkpoint retained plan digest metadata is malformed"
                )
            accepted_save_plan_digests.add(retained_digest)
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
        if (
            not isinstance(schedule_receipt_paths, dict)
            or set(schedule_receipt_paths) != expected_ids
        ):
            raise ResumeDriftError("resume checkpoint schedule receipt path lineage drift")
        save_root = (checkpoint_path.parent / "checkpoint_saves").resolve()
        for position, run in enumerate(runs[:next_index], start=1):
            run_id = run.manifest.metadata.run_id
            receipt_path = Path(schedule_receipt_paths[run_id])
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
                bundle_refs = [
                    item.evidence_bundle_ref
                    for item in receipt.attempts
                    if item.evidence_bundle_ref is not None
                ]
                selected_refs = [
                    item.evidence_bundle_ref
                    for item in receipt.attempts
                    if item.selected and item.evidence_bundle_ref is not None
                ]
            except (OSError, ValueError) as exc:
                raise ResumeDriftError(
                    f"resume checkpoint schedule receipt malformed: {run_id}"
                ) from exc
            save_receipt = receipt.checkpoint_save_ref
            if save_receipt is None:
                raise ResumeDriftError(
                    f"resume checkpoint save receipt missing: {run_id}"
                )
            expected_save_path = (
                save_root / f"{position:04d}-{run_id}.json"
            ).resolve()
            observed_save_path = Path(save_receipt.path).resolve()
            if observed_save_path != expected_save_path:
                raise ResumeDriftError(
                    f"resume checkpoint save path drift: {run_id}"
                )
            if save_receipt.write_completion_sequence != position:
                raise ResumeDriftError(
                    f"resume checkpoint save sequence drift: {run_id}"
                )
            if (
                not observed_save_path.is_file()
                or observed_save_path.stat().st_size != save_receipt.size_bytes
                or sha256_file(observed_save_path) != save_receipt.written_digest
            ):
                raise ResumeDriftError(
                    f"resume checkpoint save artifact byte drift: {run_id}"
                )
            try:
                saved_checkpoint = self._read_json(observed_save_path)
            except (OSError, ValueError) as exc:
                raise ResumeDriftError(
                    f"resume checkpoint save artifact malformed: {run_id}"
                ) from exc
            prefix_ids = {
                prefix.manifest.metadata.run_id for prefix in runs[:position]
            }
            if (
                saved_checkpoint.get("plan_sha256")
                not in accepted_save_plan_digests
                or saved_checkpoint.get("next_index") != position
                or not isinstance(saved_checkpoint.get("completed"), dict)
                or set(saved_checkpoint["completed"]) != prefix_ids
                or saved_checkpoint["completed"]
                != {key: completed[key] for key in prefix_ids}
            ):
                raise ResumeDriftError(
                    f"resume checkpoint save artifact lineage drift: {run_id}"
                )
            for ref in bundle_refs:
                bundle_path = Path(ref.path)
                if (
                    not bundle_path.is_file()
                    or bundle_path.stat().st_size != ref.size_bytes
                    or sha256_file(bundle_path) != ref.sha256
                ):
                    raise ResumeDriftError(
                        f"resume checkpoint retained bundle byte drift: {run_id}"
                    )
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
        save_artifact_path: Path,
        completion_sequence: int,
        *,
        checkpoint_load_ref: CheckpointLoadReceipt | None = None,
        ancestor_schedule_receipt_ref: ArtifactRef | None = None,
    ) -> ScheduleResult:
        save_receipt = CheckpointSaveReceipt(
            written_digest=sha256_file(save_artifact_path),
            size_bytes=save_artifact_path.stat().st_size,
            write_completion_sequence=completion_sequence,
            path=str(save_artifact_path.resolve()),
        )
        remaining_mismatches = tuple(
            reason
            for reason in schedule.receipt.mismatch_reasons
            if reason != "checkpoint receipt finalization pending"
        )
        receipt = schedule.receipt.model_copy(
            update={
                "checkpoint_save_ref": save_receipt,
                "checkpoint_load_ref": checkpoint_load_ref,
                "ancestor_schedule_receipt_ref": ancestor_schedule_receipt_ref,
                "schedule_valid": not remaining_mismatches,
                "mismatch_reasons": remaining_mismatches,
            }
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
        expected_seed = (
            run.manifest.execution.seed
            if run.manifest.execution.protocol is not None
            and run.manifest.execution.protocol.case_order == "seeded_random"
            else None
        )
        if receipt.order_seed != expected_seed:
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
        protocol = run.manifest.execution.protocol
        if protocol is not None and protocol.case_order in {"custom", "explicit"}:
            try:
                expected_case_ids = selected_case_ids(protocol)
            except CaseOrderError as exc:
                raise ResumeDriftError(str(exc)) from exc
            if receipt.observed_case_order != expected_case_ids:
                raise ResumeDriftError(
                    "resume schedule selected case order drift"
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
        config_files: tuple[str | Path, ...] | list[str | Path] = (),
        raw_config_files: tuple[str | Path, ...] | list[str | Path] = (),
        config_profiles: tuple[str, ...] | list[str] = (),
        config_overrides: Mapping[str, Any] | None = None,
    ) -> PipelineResult:
        compiled = self.compiler.compile(
            experiment_path,
            record_root=self.record_root,
            config_files=config_files,
            raw_config_files=raw_config_files,
            config_profiles=config_profiles,
            config_overrides=config_overrides,
        )
        if not compiled:
            raise ValueError("experiment expanded to zero runs")
        if self._load_project_adapters:
            self.adapter_registry = AdapterRegistry.from_project(
                self.project_root,
                required_capabilities=AdapterRegistry.required_capability_keys(compiled),
            )
        factories = {
            run.manifest.execution.backend.adapter: (
                self.adapter_registry.backend_factory(run)
            )
            for run in compiled
        }
        if len(factories) != 1:
            raise RuntimeError(
                f"one Pipeline experiment cannot mix backend adapters: "
                f"{sorted(factories)}"
            )
        contrast = compiled[0].manifest.contrast
        if contrast.mode == "one_factor":
            factor_path = next(
                artifact.factor.selector_path
                for artifact in compiled[0].manifest.metadata.factor_artifacts
                if artifact.factor.id == contrast.factor_id
            )
            control_value = next(
                artifact.factor.level(contrast.control_level).value
                for artifact in compiled[0].manifest.metadata.factor_artifacts
                if artifact.factor.id == contrast.factor_id
            )
            treatment_value = next(
                artifact.factor.level(contrast.treatment_level).value
                for artifact in compiled[0].manifest.metadata.factor_artifacts
                if artifact.factor.id == contrast.factor_id
            )
            arm_path = factor_path
        else:
            control_value = compiled[0].manifest.subject.id
            treatment_value = compiled[-1].manifest.subject.id
            arm_path = None
        counterbalanced = contrast.counterbalanced
        compiled = self._counterbalanced_order(
            compiled,
            control_value=control_value,
            treatment_value=treatment_value,
            factor_path=arm_path,
            enabled=counterbalanced,
        )
        experiment_id = compiled[0].manifest.metadata.experiment_id
        experiment_dir = self.record_root / experiment_id
        if not resume and experiment_dir.exists() and any(experiment_dir.iterdir()):
            raise ResumeDriftError(
                "record root already contains an execution instance for "
                f"experiment {experiment_id!r}; use resume=true or choose a new record_root"
            )
        experiment_dir.mkdir(parents=True, exist_ok=True)
        activated_case_sets = {
            run.manifest.metadata.run_id: self._activate_case_set(
                run, experiment_dir
            )
            for run in compiled
        }
        loaded_case_sets = {
            run_id: activation[0]
            for run_id, activation in activated_case_sets.items()
        }
        # A schedule receipt can carry one selected attempt per case, and the
        # report will materialize each selected case as its own lineage entry.
        # Checkpoint ledgers still key their completion map by parent run_id;
        # keep that feature fail-closed for multi-case runs until its on-disk
        # schema is widened to a parent/case key.
        for run_id, loaded_case_set in loaded_case_sets.items():
            protocol = next(
                run.manifest.execution.protocol
                for run in compiled
                if run.manifest.metadata.run_id == run_id
            )
            if len(loaded_case_set.cases) > 1 and protocol is not None and protocol.checkpoint_policy != "disabled":
                raise RuntimeError(
                    "multi-case checkpoint identity is not implemented: "
                    f"compiled arm {run_id!r} resolved "
                    f"{len(loaded_case_set.cases)} selected cases; "
                    "use checkpoint_policy=disabled"
                )
        if self.backend is None:
            factory = next(iter(factories.values()))
            self.backend = factory.build(
                compiled[0],
                record_root=self.record_root,
                workspace_root=self.record_root / "workspaces",
            )
        plan_path = experiment_dir / "plan.json"
        checkpoint_path = experiment_dir / "checkpoint.json"
        plan = self._plan(compiled, loaded_case_sets)
        checkpoint_load_ref: CheckpointLoadReceipt | None = None
        ancestor_schedule_receipt_ref: ArtifactRef | None = None
        accepted_save_plan_digests: set[str] | None = None
        retained_plan_digests: tuple[str, ...] = ()
        resume_prefix_count = 0
        resume_schedule_paths: dict[str, str] = {}
        resume_old_runs: list[CompiledRun] = []
        if resume:
            self._validate_resume(plan_path, plan)
            checkpoint = self._read_json(checkpoint_path)
            resume_prefix_count = int(checkpoint["next_index"])
            resume_schedule_paths = dict(checkpoint["schedule_receipt_paths"])
            resume_old_runs = [
                replace(
                    run,
                    manifest=run.manifest.model_copy(
                        update={
                            "execution": run.manifest.execution.model_copy(
                                update={
                                    "protocol": run.manifest.execution.protocol.model_copy(
                                        update={"checkpoint_policy": "save"}
                                    )
                                }
                            )
                        }
                    ),
                )
                for run in compiled[:resume_prefix_count]
            ]
            self._validate_checkpoint(
                checkpoint_path, plan_path, resume_old_runs
            )
            checkpoint = self._read_json(checkpoint_path)
            previous_plan_digest = sha256_file(plan_path)
            next_index = int(checkpoint["next_index"])
            if next_index < 1:
                raise ResumeDriftError("resume requires at least one saved ancestor run")
            ancestor_run = resume_old_runs[next_index - 1]
            ancestor_run_id = ancestor_run.manifest.metadata.run_id
            ancestor_receipt_path = Path(
                checkpoint["schedule_receipt_paths"][ancestor_run_id]
            )
            ancestor_schedule_receipt_ref = artifact_ref(ancestor_receipt_path)
            ancestor_receipt = ScheduleActivationReceipt.model_validate(
                self._read_json(ancestor_receipt_path)
            )
            ancestor_save = ancestor_receipt.checkpoint_save_ref
            if ancestor_save is None:
                raise ResumeDriftError(
                    "ancestor schedule receipt lacks checkpoint save evidence"
                )
            ancestor_checkpoint_digest = ancestor_save.written_digest
            previous_checkpoint_plan_digest = checkpoint.get("plan_sha256")
            if previous_plan_digest != previous_checkpoint_plan_digest:
                raise ResumeDriftError("loaded checkpoint plan digest drift")
            raw_retained_plan_digests = checkpoint.get("retained_plan_sha256", ())
            if isinstance(raw_retained_plan_digests, str):
                raw_retained_plan_digests = (raw_retained_plan_digests,)
            if not isinstance(raw_retained_plan_digests, (list, tuple)):
                raise ResumeDriftError(
                    "resume checkpoint retained plan digest metadata is malformed"
                )
            retained_plan_digests = tuple(raw_retained_plan_digests)
            atomic_write_json(plan_path, plan)
            new_plan_digest = sha256_file(plan_path)
            if previous_plan_digest != new_plan_digest:
                retained_plan_digests = tuple(
                    dict.fromkeys((*retained_plan_digests, previous_plan_digest))
                )
            checkpoint["plan_sha256"] = new_plan_digest
            checkpoint["retained_plan_sha256"] = list(retained_plan_digests)
            atomic_write_json(checkpoint_path, checkpoint)
            checkpoint_load_ref = CheckpointLoadReceipt(
                loaded_checkpoint_digest=ancestor_checkpoint_digest,
                resolved_plan_digest=sha256_file(plan_path),
                schedule_receipt_digest=ancestor_schedule_receipt_ref.sha256,
                selected_bundle_digests=tuple(checkpoint["completed"].values()),
            )
            # Retained ancestor save artifacts were written under the old
            # checkpoint plan. New saves after the transition use the active
            # plan, so the final ledger must accept both immutable identities.
            accepted_save_plan_digests = {previous_plan_digest}
            resume_root = (
                experiment_dir
                / f"resume-execution-{ancestor_checkpoint_digest[:12]}"
            )
            self.backend = self.adapter_registry.backend_factory(compiled[0]).build(
                compiled[0],
                record_root=resume_root,
                workspace_root=resume_root / "workspaces",
            )
        else:
            protocol = compiled[0].manifest.execution.protocol
            if (
                protocol is not None
                and protocol.checkpoint_policy == "save_and_resume"
            ):
                raise ResumeDriftError(
                    "checkpoint policy 'save_and_resume' requires resume=true "
                    "and an ancestor schedule receipt"
                )
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
        for run_index, run in enumerate(compiled):
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
            (
                loaded_case_set,
                case_set_receipt,
                case_set_receipt_path,
            ) = activated_case_sets[run.manifest.metadata.run_id]
            execution_adapter = self.adapter_registry.execution_adapter(run)
            runtime_cases = {
                case_id: case
                for case_id, case in zip(
                    loaded_case_set.artifact.ordered_case_ids,
                    loaded_case_set.cases,
                    strict=True,
                )
            }
            execution_run = run
            schedule: ScheduleResult | None = None
            if resume and run_index < resume_prefix_count:
                execution_run = resume_old_runs[run_index]
                receipt_path = Path(
                    resume_schedule_paths[run.manifest.metadata.run_id]
                )
                try:
                    schedule = self._load_schedule_execution(
                        execution_run, receipt_path
                    )
                except EvidenceDriftError as exc:
                    raise ResumeDriftError(str(exc)) from exc
                if schedule is None:
                    raise ResumeDriftError(
                        f"resume retained schedule evidence is unavailable: "
                        f"{run.manifest.metadata.run_id}"
                    )
                reused_ids.append(run.manifest.metadata.run_id)
            else:
                receipt_path = (
                    self.backend.run_directory(run)
                    / "schedule_activation_receipt.json"
                )
                schedule = self.scheduler.execute(
                    run,
                    loaded_case_set.cases,
                    case_artifacts={
                        case.case_id: case
                        for case in loaded_case_set.artifact.cases
                    },
                    attempt_runner=(
                        lambda attempt,
                        run=run,
                        adapter=execution_adapter,
                        cases=runtime_cases: self._execute_attempt(
                            run,
                            adapter,
                            cases[attempt.case_id],
                            attempt,
                        )
                    ),
                    reset_state=(
                        lambda case_id,
                        policy,
                        adapter=execution_adapter: adapter.reset_state(
                            self.backend, case_id, policy
                        )
                    ),
                    receipt_path=receipt_path,
                )
                executed_ids.append(run.manifest.metadata.run_id)
            if not schedule.selected:
                raise RuntimeError(
                    "schedule did not produce a selected candidate for any case"
                )
            receipt_sha256 = sha256_file(schedule.receipt_path)
            case_set_receipt_sha256 = sha256_file(case_set_receipt_path)
            for case in schedule.selected:
                completed.append(
                    CompletedRun(
                        plan=execution_run,
                        case=case,
                        schedule_receipt=schedule.receipt,
                        schedule_receipt_path=schedule.receipt_path,
                        schedule_receipt_sha256=receipt_sha256,
                        scheduler_digest=self.scheduler.scheduler_digest,
                        pipeline_digest=self.scheduler.pipeline_digest,
                        runner_digest=self.backend.runner_digest,
                        case_set_receipt=case_set_receipt,
                        case_set_receipt_path=case_set_receipt_path,
                        case_set_receipt_sha256=case_set_receipt_sha256,
                        case_set_digest=case_set_receipt.case_set_digest,
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
                "schedule_receipt_paths": {
                    item.plan.manifest.metadata.run_id: str(
                        item.schedule_receipt_path.resolve()
                    )
                    for item in completed
                },
            }
            if retained_plan_digests:
                checkpoint["retained_plan_sha256"] = list(retained_plan_digests)
            if (
                protocol.checkpoint_policy in {"save", "save_and_resume"}
                and run_index >= resume_prefix_count
            ):
                save_artifact_path = (
                    experiment_dir
                    / "checkpoint_saves"
                    / f"{len(completed):04d}-{run.manifest.metadata.run_id}.json"
                )
                atomic_write_json(save_artifact_path, checkpoint)
                schedule = self._record_checkpoint_save(
                    schedule,
                    save_artifact_path,
                    len(completed),
                    checkpoint_load_ref=checkpoint_load_ref,
                    ancestor_schedule_receipt_ref=ancestor_schedule_receipt_ref,
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
                self._validate_checkpoint(
                    checkpoint_path,
                    plan_path,
                    compiled,
                    accepted_save_plan_digests=accepted_save_plan_digests,
                )
            if stop_after is not None and run_index + 1 >= stop_after:
                self._append_event(
                    events_path,
                    {"seq": len(completed) + 1, "event": "interrupted"},
                )
                raise InjectedInterruption(
                    f"injected interruption after {len(completed)} run(s)"
                )

        # Materialize the exact manifest bytes before creating the index. The
        # index is deliberately written before the report so the report can
        # carry a stable content reference without a report/index hash cycle.
        manifest_dir = experiment_dir / "manifests"
        manifest_refs: list[ArtifactRef] = []
        # The record index contains one manifest per parent run.  Multiple
        # selected cases share that immutable manifest and are represented by
        # separate lineage entries below.
        report_plans: list[CompiledRun] = []
        seen_manifest_digests: set[str] = set()
        for item in completed:
            if item.plan.manifest_digest in seen_manifest_digests:
                continue
            seen_manifest_digests.add(item.plan.manifest_digest)
            report_plans.append(item.plan)
        for run in report_plans:
            manifest_path = manifest_dir / f"{run.manifest_digest}.json"
            write_immutable_json(
                manifest_path,
                run.manifest,
                label="resolved manifest",
            )
            manifest_refs.append(artifact_ref(manifest_path))
        experiment_digest = sha256_bytes(
            canonical_json_bytes([run.manifest_digest for run in report_plans])
        )
        aggregate_path = experiment_dir / "aggregate.json"
        index_path = experiment_dir / "record_index.json"
        record_index = RecordIndex(
            format="bmp-record-index-v1",
            experiment_id=experiment_id,
            manifest_refs=tuple(manifest_refs),
            aggregate_path=str(aggregate_path.resolve()),
        )
        write_immutable_json(index_path, record_index, label="record index")
        record_index_ref = artifact_ref(index_path)
        report = evaluate_run_report(
            completed=completed,
            expected_run_ids=tuple(
                (
                    f"{run.manifest.metadata.run_id}::{case_id}"
                    if len(loaded_case_sets[run.manifest.metadata.run_id].cases) > 1
                    else run.manifest.metadata.run_id
                )
                for run in compiled
                for case_id in loaded_case_sets[run.manifest.metadata.run_id].artifact.ordered_case_ids
            ),
            record_index_ref=record_index_ref,
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
                (
                    f"{item.plan.manifest.metadata.run_id}::{item.case.case_id}"
                    if sum(
                        other.plan.manifest.metadata.run_id
                        == item.plan.manifest.metadata.run_id
                        for other in completed
                    ) > 1
                    else item.plan.manifest.metadata.run_id
                ): item.schedule_receipt_sha256
                for item in completed
            },
            "run_report_sha256": sha256_file(report_path),
        }
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
