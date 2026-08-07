"""End-to-end BMP execution, checkpoint, resume, aggregation and claims."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from MagentaBench.schemas import ClaimReport

from .backend.fake import CaseExecution, EvidenceDriftError, FakeBackend
from .compiler import CompiledRun, Compiler, canonical_json_bytes, sha256_bytes
from .evidence import atomic_write_json, sha256_file
from .gates import CompletedRun, evaluate_claim


class ResumeDriftError(RuntimeError):
    """Resume was requested but pinned execution identity has changed."""


class InjectedInterruption(RuntimeError):
    """Conformance-only interruption raised after a checkpoint boundary."""


@dataclass(frozen=True)
class PipelineResult:
    runs: tuple[CompletedRun, ...]
    aggregate_path: Path
    claim_report_path: Path
    claim_report: ClaimReport


class Pipeline:
    def __init__(
        self,
        project_root: str | Path,
        record_root: str | Path,
        *,
        backend: Any | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.record_root = Path(record_root).resolve()
        self.compiler = Compiler(self.project_root)
        self.backend = backend or FakeBackend(self.record_root)

    @staticmethod
    def _read_json(path: Path) -> Any:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _append_event(path: Path, event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        sequence = 1
        if path.is_file():
            with path.open("rb") as existing:
                sequence += sum(1 for line in existing if line.strip())
        payload = dict(event)
        payload["seq"] = sequence
        with path.open("ab") as handle:
            handle.write(canonical_json_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

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
        self, checkpoint_path: Path, plan_path: Path, run_count: int
    ) -> None:
        try:
            checkpoint = self._read_json(checkpoint_path)
            next_index = int(checkpoint["next_index"])
            completed = checkpoint["completed"]
            plan_digest = checkpoint["plan_sha256"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ResumeDriftError(
                f"resume checkpoint is missing or malformed: {exc}"
            ) from exc
        if plan_digest != sha256_file(plan_path):
            raise ResumeDriftError("resume checkpoint references a drifted plan")
        if not isinstance(completed, dict) or next_index != len(completed):
            raise ResumeDriftError("resume checkpoint completion ledger is inconsistent")
        if next_index < 0 or next_index > run_count:
            raise ResumeDriftError("resume checkpoint next_index is out of range")

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
        declaration = self.compiler._load_toml(Path(experiment_path).resolve())
        experiment = declaration["experiment"]
        control_id = str(experiment.get("control", compiled[0].manifest.subject.id))
        treatment_id = str(experiment.get("treatment", compiled[-1].manifest.subject.id))
        counterbalanced = bool(experiment.get("counterbalance", False))
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
        plan = self._plan(compiled)
        if resume:
            self._validate_resume(plan_path, plan)
            self._validate_checkpoint(
                experiment_dir / "checkpoint.json", plan_path, len(compiled)
            )
        else:
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
            task = self.backend._load_task(run)
            bundle_path = (
                self.backend.run_directory(run)
                / "cases"
                / task.task_id
                / "evidence_bundle.json"
            )
            case: CaseExecution | None = None
            if resume and bundle_path.is_file():
                try:
                    case = self.backend.load_completed(
                        run,
                        bundle_path,
                        expected_runner_digest=self.backend.runner_digest,
                    )
                except EvidenceDriftError as exc:
                    raise ResumeDriftError(str(exc)) from exc
            if case is None:
                case = self.backend.execute(run, task)
                executed_ids.append(run.manifest.metadata.run_id)
            else:
                reused_ids.append(run.manifest.metadata.run_id)
            completed.append(CompletedRun(plan=run, case=case))

            checkpoint = {
                "plan_sha256": sha256_file(plan_path),
                "completed": {
                    item.plan.manifest.metadata.run_id: item.case.bundle_digest
                    for item in completed
                },
                "next_index": len(completed),
                "budget_ledger": {
                    "tokens": 0,
                    "cost": 0.0,
                    "wall_clock_seconds": 0.0,
                },
            }
            atomic_write_json(experiment_dir / "checkpoint.json", checkpoint)
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
        claim = evaluate_claim(
            experiment_id=experiment_id,
            experiment_digest=experiment_digest,
            completed=completed,
            expected_run_count=len(compiled),
            control_id=control_id,
            treatment_id=treatment_id,
            deterministic_conformance=deterministic,
            counterbalanced=counterbalanced,
        )
        claim_path = experiment_dir / "claim_report.json"
        atomic_write_json(claim_path, claim)
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
            "claim_report_sha256": sha256_file(claim_path),
        }
        aggregate_path = experiment_dir / "aggregate.json"
        atomic_write_json(aggregate_path, aggregate)
        atomic_write_json(
            experiment_dir / "resume_receipt.json",
            {"resume": resume, "reused": reused_ids, "rerun": executed_ids},
        )
        self._append_event(
            events_path,
            {"seq": len(completed) + 2, "event": "complete", "eligible": claim.claim_eligible},
        )
        return PipelineResult(
            runs=tuple(completed),
            aggregate_path=aggregate_path,
            claim_report_path=claim_path,
            claim_report=claim,
        )


__all__ = [
    "InjectedInterruption",
    "Pipeline",
    "PipelineResult",
    "ResumeDriftError",
]
