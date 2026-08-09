from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from MagentaBench.runner.adapter_registry import (
    AdapterRegistry,
    AdapterRegistryError,
)
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.backend.fake import FakeBackend
from MagentaBench.runner.gates import _receipt_binding_errors, evaluate_run_report
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.runner.scheduler import Scheduler, SchedulerError
from MagentaBench.schemas import (
    ComparisonKind,
    CustomCaseOrderSpec,
    GateName,
    ProtocolSpec,
    RunPurpose,
)
from MagentaBench.schemas.verification import (
    _active_schedule_digests,
    _schedule_substantiates_protocol,
    _verify_schedule_manifest_binding,
)
from MagentaBench.adapters.fake import FakeTask


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _custom_project(tmp_path: Path, ids: tuple[str, ...]) -> tuple[Path, Path]:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(ROOT / "MagentaBench/conformance", project / "MagentaBench/conformance")
    protocol = project / "registries/protocols/fake-deterministic.toml"
    order_bytes = (
        json.dumps(
            {
                "schema_version": "bmp.case-order.v1",
                "ordered_case_ids": list(ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    order_path = project / "case_orders/custom.json"
    order_path.parent.mkdir(parents=True)
    order_path.write_bytes(order_bytes)
    protocol_text = protocol.read_text(encoding="utf-8")
    protocol_text = protocol_text.replace('case_order = "fixed"', 'case_order = "custom"')
    protocol_text = protocol_text.replace(
        "\n[protocol.budget]",
        "\n[protocol.custom_order]\n"
        'adapter = "magentabench.case-order.json.v1"\n'
        'source = "case_orders/custom.json"\n'
        f'sha256 = "{hashlib.sha256(order_bytes).hexdigest()}"\n'
        f"size_bytes = {len(order_bytes)}\n\n"
        "[protocol.budget]",
    )
    protocol.write_text(protocol_text, encoding="utf-8")
    tasks = project / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    tasks.write_text(
        tasks.read_text(encoding="utf-8")
        + '\n[[tasks]]\nid = "case-002"\ninstruction = "Emit the BMP protocol sentinel twice."\nexpected = "BMP_OK"\noutput = "answer.txt"\n',
        encoding="utf-8",
    )
    return project, project / "MagentaBench/conformance/experiments/fake-sweep.toml"


def test_protocol_explicit_case_ids_are_nonempty_unique_and_identity_bearing() -> None:
    with pytest.raises(ValueError, match="custom_order is required"):
        ProtocolSpec(
            id="explicit",
            kind="mechanism_validation",
            adapter="magentabench.scheduler",
            case_order="custom",
            candidate_selection="single",
        )
    with pytest.raises(ValueError, match="unique"):
        ProtocolSpec(
            id="explicit",
            kind="mechanism_validation",
            adapter="magentabench.scheduler",
            case_order="explicit",
            explicit_case_ids=("case-1", "case-1"),
            candidate_selection="single",
        )
    with pytest.raises(ValueError, match="forbidden"):
        ProtocolSpec(
            id="explicit",
            kind="mechanism_validation",
            adapter="magentabench.scheduler",
            case_order="fixed",
            explicit_case_ids=("case-1",),
            candidate_selection="single",
        )
    first = ProtocolSpec(
        id="explicit",
        kind="mechanism_validation",
        adapter="magentabench.scheduler",
        case_order="custom",
        custom_order=CustomCaseOrderSpec(
            source="case_orders/first.json",
            sha256="a" * 64,
            size_bytes=10,
        ),
        candidate_selection="single",
    )
    second = first.model_copy(
        update={
            "custom_order": first.custom_order.model_copy(
                update={"sha256": "b" * 64}
            )
        }
    )
    assert first != second


def test_fake_loader_selects_exact_declared_order(tmp_path: Path) -> None:
    project, experiment = _custom_project(tmp_path, ("case-002", "case-001"))
    run = Compiler(project).compile(experiment)[0]
    loader = AdapterRegistry.production().benchmark_loader(run)
    resolved = loader.resolve(run, tmp_path / "case-set")
    assert resolved.artifact.selection_method == "custom_order_artifact"
    assert resolved.artifact.order_strategy_adapter == (
        "magentabench.case-order.json.v1"
    )
    assert resolved.artifact.order_strategy_ref is not None
    assert resolved.artifact.ordered_case_ids == ("case-002", "case-001")
    loaded = loader.load(run, resolved)
    assert tuple(case.task_id for case in loaded.cases) == ("case-002", "case-001")
    protocol = run.manifest.execution.protocol
    assert protocol is not None
    assert protocol.custom_order is not None
    reversed_manifest = run.manifest.model_copy(
        update={
            "execution": run.manifest.execution.model_copy(
                update={
                    "protocol": protocol.model_copy(
                        update={
                            "custom_order": protocol.custom_order.model_copy(
                                update={"sha256": "0" * 64}
                            )
                        }
                    )
                }
            )
        }
    )
    assert replace(run, manifest=reversed_manifest).manifest_digest != run.manifest_digest


def test_custom_order_source_is_rechecked_after_compilation(tmp_path: Path) -> None:
    project, experiment = _custom_project(tmp_path, ("case-002", "case-001"))
    run = Compiler(project).compile(experiment)[0]
    order_path = project / "case_orders/custom.json"
    order_path.write_text(
        '{"schema_version":"bmp.case-order.v1","ordered_case_ids":["case-001"]}\n',
        encoding="utf-8",
    )
    loader = AdapterRegistry.production().benchmark_loader(run)
    with pytest.raises(AdapterRegistryError, match="differs from declaration"):
        loader.resolve(run, tmp_path / "case-set")


def test_fake_loader_rejects_unknown_declared_id(tmp_path: Path) -> None:
    project, experiment = _custom_project(tmp_path, ("case-404",))
    run = Compiler(project).compile(experiment)[0]
    loader = AdapterRegistry.production().benchmark_loader(run)
    with pytest.raises(AdapterRegistryError, match="missing"):
        loader.resolve(run, tmp_path / "case-set")


def test_fake_loader_rejects_duplicate_source_case_id(tmp_path: Path) -> None:
    project, experiment = _custom_project(tmp_path, ("case-001",))
    tasks = project / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    tasks.write_text(
        tasks.read_text(encoding="utf-8")
        + '\n[[tasks]]\nid = "case-001"\ninstruction = "Duplicate."\nexpected = "BMP_OK"\noutput = "answer.txt"\n',
        encoding="utf-8",
    )
    run = Compiler(project).compile(experiment)[0]
    loader = AdapterRegistry.production().benchmark_loader(run)
    with pytest.raises(AdapterRegistryError, match="duplicate case ids"):
        loader.resolve(run, tmp_path / "case-set")


def test_scheduler_preserves_explicit_order_and_rejects_extra_case(tmp_path: Path) -> None:
    project, experiment = _custom_project(tmp_path, ("case-002", "case-001"))
    run = Compiler(project).compile(experiment)[0]
    protocol = run.manifest.execution.protocol
    assert protocol is not None
    backend = FakeBackend(tmp_path / "records", allow_test_task_override=True)
    task = FakeTask()
    cases = [replace(task, task_id="case-001"), replace(task, task_id="case-002")]

    def attempt_runner(attempt):
        case = next(item for item in cases if item.task_id == attempt.case_id)
        return backend.execute(
            run,
            case,
            activated_case_set_digest="a" * 64,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
        )

    result = Scheduler(record_root=tmp_path / "records").execute(
        run,
        cases,
        attempt_runner=attempt_runner,
        reset_state=backend.reset_state,
        receipt_path=tmp_path / "receipt.json",
    )
    assert result.receipt.observed_case_order == ("case-002", "case-001")
    with pytest.raises(SchedulerError, match="outside selected case ids"):
        Scheduler._ordered_cases(
            cases + [replace(task, task_id="case-003")],
            "custom",
            None,
            ("case-002", "case-001"),
        )
    with pytest.raises(SchedulerError, match="must be unique"):
        Scheduler._ordered_cases(
            cases,
            "explicit",
            None,
            ("case-001", "case-001"),
        )


def test_pipeline_and_gate_bind_explicit_case_order(tmp_path: Path) -> None:
    project, experiment = _custom_project(tmp_path, ("case-002", "case-001"))
    result = Pipeline(project, tmp_path / "records").run(experiment)

    assert len(result.runs) == 16
    assert all(
        item.case_set_receipt.ordered_case_ids == ("case-002", "case-001")
        and item.schedule_receipt.observed_case_order == ("case-002", "case-001")
        for item in result.runs
    )
    first = result.runs[0]
    assert _schedule_substantiates_protocol(
        first.schedule_receipt,
        first.plan.manifest,
        active_digests=_active_schedule_digests(),
        case_set_receipt=first.case_set_receipt,
    )
    assert not _schedule_substantiates_protocol(
        first.schedule_receipt,
        first.plan.manifest,
        active_digests=_active_schedule_digests(),
    )
    forged = result.runs[0].schedule_receipt.model_copy(
        update={"observed_case_order": ("case-001", "case-002")}
    )
    errors = _receipt_binding_errors(
        replace(result.runs[0], schedule_receipt=forged)
    )
    assert "observed_case_order does not match custom order artifact" in errors
    verification_errors: list[str] = []
    _verify_schedule_manifest_binding(
        forged,
        result.runs[0].plan.manifest,
        case_set_receipt=result.runs[0].case_set_receipt,
        label="schedule",
        mismatches=verification_errors,
    )
    assert any("activated custom order" in item for item in verification_errors)


def test_multicase_claim_evidence_refs_are_unique(tmp_path: Path) -> None:
    project, experiment = _custom_project(tmp_path, ("case-002", "case-001"))
    result = Pipeline(project, tmp_path / "records").run(experiment)
    registered_subject = Compiler(ROOT)._subject_artifact("fake.nonfake")
    claim_runs = []
    for item in result.runs:
        design = item.plan.manifest.claim_design.model_copy(
            update={
                "purpose": RunPurpose.claim,
                "comparison_kind": ComparisonKind.coding_agent,
            }
        )
        manifest = item.plan.manifest.model_copy(
            update={
                "claim_design": design,
                "subject": registered_subject,
            }
        )
        claim_runs.append(replace(item, plan=replace(item.plan, manifest=manifest)))
    expected = tuple(
        f"{item.plan.manifest.metadata.run_id}::{item.case.case_id}"
        for item in claim_runs
    )
    report = evaluate_run_report(completed=claim_runs, expected_run_ids=expected)
    assert report.purpose == RunPurpose.claim
    assert report.gates[GateName.protocol_valid].valid
    for gate in report.gates.values():
        assert len(gate.evidence_refs) == len(set(gate.evidence_refs))
