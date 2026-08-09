"""Standalone report byte and lineage verification tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from MagentaBench.schemas import (
    ArtifactRef,
    GateName,
    ObservationReport,
    RecordIndex,
    ReportVerificationError,
    ResolvedNetworkPolicy,
    RunPurpose,
    RolloutTrajectory,
    ScheduleActivationReceipt,
    StatisticalAnalysisPlan,
    TrajectoryCapture,
    TrajectoryCaptureState,
    VerifiedObservationReport,
    canonical_digest,
    verify_claim_report,
    verify_observation_report,
    write_registry_lock,
)
from MagentaBench.runner.evidence import atomic_write_json
from MagentaBench.runner.gates import evaluate_run_report
from MagentaBench.schemas.models import SubjectKind
from MagentaBench.runner.pipeline import InjectedInterruption, Pipeline


ROOT = Path(__file__).parents[1]


def replace_required(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


def checkpoint_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "checkpoint-project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance",
        project / "MagentaBench/conformance",
    )
    experiment = project / "MagentaBench/conformance/experiments/fake-sweep.toml"
    experiment.write_text(
        replace_required(
            experiment.read_text(encoding="utf-8"),
            'protocol = "fake.deterministic.v1"',
            'protocol = "fake.checkpoint-lineage.v1"',
        ),
        encoding="utf-8",
    )
    return project, experiment


def compact_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def ref(path: Path) -> ArtifactRef:
    content = path.read_bytes()
    return ArtifactRef(
        path=str(path.resolve()),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def rewrite_report_and_aggregate(report_path: Path, payload: object) -> None:
    report_bytes = compact_bytes(payload)
    report_path.write_bytes(report_bytes)
    aggregate_path = report_path.parent / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["run_report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    aggregate_path.write_bytes(compact_bytes(aggregate))


def rewrite_schedule_lineage(result: object, index: int, schedule_path: Path) -> None:
    lineage = result.report.lineage[index]
    schedule_ref = ref(schedule_path)
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    payload["lineage"][index]["schedule_receipt_ref"] = schedule_ref.model_dump(
        mode="json"
    )
    aggregate = json.loads(result.aggregate_path.read_text(encoding="utf-8"))
    aggregate["schedule_receipts"][lineage.run_id] = schedule_ref.sha256
    result.aggregate_path.write_bytes(compact_bytes(aggregate))
    rewrite_report_and_aggregate(result.report_path, payload)


def registered_claim_project(
    root: Path,
    statistical_analysis: StatisticalAnalysisPlan,
) -> Path:
    """Create a self-contained, TOML-registered coding-agent claim fixture."""

    project = root / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance/fixtures",
        project / "MagentaBench/conformance/fixtures",
    )

    adapter_path = project / "report_claim_adapter.py"
    adapter_path.write_text(
        '''"""Content-addressed fake execution adapter for report verification."""

import hashlib
from pathlib import Path


_MODULE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class RegisteredFakeOpaqueExecution:
    benchmark_adapter = "fake"
    backend_adapter = "fake"
    subject_interface = "task_to_output"
    digest = _MODULE_DIGEST

    def execute(self, backend, run, case, attempt):
        return backend.execute(
            run,
            case.task,
            activated_case_set_digest=case.case_set_digest,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
            attempt_budget=attempt.allocation,
            remaining_wall_seconds=attempt.remaining_wall_seconds,
        )

    def reset_state(self, backend, case_id, policy):
        return backend.reset_state(case_id, policy)
''',
        encoding="utf-8",
    )
    adapter_digest = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    (project / "registries/adapters/report-claim-execution.toml").write_text(
        f'''[adapter]
id = "report.claim-execution"
kind = "adapter"
adapter = "fake"
bmp_version = "0.1"
adapter_kind = "execution"
source = "."
entrypoint = "report_claim_adapter.py:RegisteredFakeOpaqueExecution"
digest = "{adapter_digest}"
supported_benchmark_kinds = ["task_suite"]
supported_subject_kinds = ["opaque_agent"]
supported_backend_kinds = ["local"]
supported_backend_adapters = ["fake"]
supported_subject_adapters = ["fake"]
supported_subject_interfaces = ["task_to_output"]
none_model_sentinels = ["none/deterministic"]
supported_state_reset_policies = ["never"]
''',
        encoding="utf-8",
    )

    subject_template = '''[subject]
id = "{subject_id}"
kind = "opaque_agent"
comparison_kind = "coding_agent"
adapter = "fake"
bmp_version = "0.1"
source = "../../MagentaBench/conformance/fixtures/fake_benchmark"
commit = "report-claim-subject-v1"
entrypoint = "fixed:{answer}"
interface = "task_to_output"
emits_trace = false
'''
    subjects = project / "registries/subjects"
    (subjects / "report-claim-control.toml").write_text(
        subject_template.format(
            subject_id="report.claim-control",
            answer="BMP_BAD",
        ),
        encoding="utf-8",
    )
    (subjects / "report-claim-treatment.toml").write_text(
        subject_template.format(
            subject_id="report.claim-treatment",
            answer="BMP_OK",
        ),
        encoding="utf-8",
    )
    (project / "registries/factors/report-claim-subject.toml").write_text(
        '''[factor]
id = "report.claim-subject"
kind = "factor"
adapter = "magentabench.factor"
bmp_version = "0.1"
category = "subject_identity"
selector_path = "subject"
applies_to = ["coding_agent"]
resolved_diff_paths = [
  "subject.artifact_digest",
  "subject.entrypoint",
  "subject.id",
]
activation_evidence = "subject_activation"
value_registry = "subject"
metadata_only = false

[[factor.levels]]
id = "control"
value = "report.claim-control"

[[factor.levels]]
id = "treatment"
value = "report.claim-treatment"
''',
        encoding="utf-8",
    )

    plan_lines = "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=True, separators=(',', ':'))}"
        for key, value in statistical_analysis.model_dump(
            mode="json", exclude_none=True
        ).items()
    )
    experiment = (
        ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    ).read_text(encoding="utf-8")
    replacements = (
        ('id = "fake-conformance-sweep"', 'id = "report-verification-claim"'),
        ('subject = "fake.control"', 'subject = "report.claim-control"'),
        (
            'protocol = "fake.deterministic.v1"',
            'protocol = "deterministic-evolution.v1"',
        ),
        (
            'factors = ["conformance.fake-subject", "repetition.four"]',
            'factors = ["report.claim-subject", "repetition.four"]',
        ),
        (
            'factor_id = "conformance.fake-subject"',
            'factor_id = "report.claim-subject"',
        ),
        (
            '[experiment.design]\npurpose = "exploratory"',
            '[experiment.design]\n'
            'comparison_kind = "coding_agent"\n'
            'purpose = "claim"\n\n'
            '[experiment.design.statistical_analysis]\n'
            + plan_lines,
        ),
    )
    for old, new in replacements:
        experiment = replace_required(experiment, old, new)
    experiment_path = project / "report-claim.toml"
    experiment_path.write_text(experiment, encoding="utf-8")
    write_registry_lock(project / "registries")
    return experiment_path


def write_verified_claim(
    root: Path,
    *,
    statistical_analysis: StatisticalAnalysisPlan | None = None,
) -> tuple[Path, tuple[object, ...]]:
    analysis_plan = statistical_analysis or StatisticalAnalysisPlan(
        holdout_required=True,
        holdout_split="test",
    )
    experiment = registered_claim_project(root.parent, analysis_plan)
    result = Pipeline(experiment.parent, root).run(experiment)
    completed = []
    for item in result.runs:
        bundle = item.case.bundle
        if bundle.trajectory_ref is not None:
            trajectory_path = Path(bundle.trajectory_ref.path)
            trajectory = RolloutTrajectory.model_validate_json(
                trajectory_path.read_bytes()
            ).model_copy(
                update={
                    "capture": TrajectoryCapture(
                        model_io=TrajectoryCaptureState.not_applicable,
                        tool_io=TrajectoryCaptureState.not_applicable,
                        process_io=TrajectoryCaptureState.complete,
                        evaluator_io=TrajectoryCaptureState.complete,
                        environment=TrajectoryCaptureState.complete,
                        resource_usage=TrajectoryCaptureState.complete,
                    ),
                }
            )
            atomic_write_json(trajectory_path, trajectory)
            bundle = bundle.model_copy(
                update={"trajectory_ref": ref(trajectory_path)}
            )
        atomic_write_json(item.case.bundle_path, bundle)
        bundle_ref = ref(item.case.bundle_path)
        case = replace(
            item.case,
            bundle=bundle,
            bundle_digest=bundle_ref.sha256,
        )
        attempts = tuple(
            attempt.model_copy(update={"evidence_bundle_ref": bundle_ref})
            for attempt in item.schedule_receipt.attempts
        )
        schedule = ScheduleActivationReceipt.model_validate(
            item.schedule_receipt.model_copy(
                update={"attempts": attempts}
            ).model_dump(mode="json")
        )
        atomic_write_json(item.schedule_receipt_path, schedule)
        schedule_ref = ref(item.schedule_receipt_path)
        completed.append(
            replace(
                item,
                case=case,
                schedule_receipt=schedule,
                schedule_receipt_sha256=schedule_ref.sha256,
            )
        )

    report = evaluate_run_report(
        completed=completed,
        expected_run_ids=tuple(
            item.plan.manifest.metadata.run_id for item in completed
        ),
        record_index_ref=result.report.record_index_ref,
    )
    report_path = result.report_path
    atomic_write_json(report_path, report)
    aggregate = {
        "experiment_id": report.experiment_id,
        "experiment_digest": report.manifest_digest,
        "run_count": len(completed),
        "statuses": [item.case.bundle.status.value for item in completed],
        "scores": [
            item.case.bundle.verifier_evidence.score
            for item in completed
        ],
        "schedule_receipts": {
            item.plan.manifest.metadata.run_id: item.schedule_receipt_sha256
            for item in completed
        },
        "run_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }
    result.aggregate_path.write_bytes(compact_bytes(aggregate))
    verify_claim_report(report_path)
    return report_path, tuple(completed)


def write_verified_observation(root: Path) -> Path:
    root.mkdir()
    aggregate_path = root / "aggregate.json"
    index_path = root / "record_index.json"
    report_path = root / "observation_report.json"
    index = RecordIndex(
        format="bmp-record-index-v1",
        experiment_id="verified-observation",
        manifest_refs=(),
        aggregate_path=str(aggregate_path.resolve()),
    )
    index_path.write_bytes(compact_bytes(index.model_dump(mode="json")))
    empty_experiment_digest = hashlib.sha256(compact_bytes([])).hexdigest()
    report = ObservationReport(
        purpose=RunPurpose.exploratory,
        subject_kinds=(SubjectKind.fake,),
        experiment_id="verified-observation",
        manifest_digest=empty_experiment_digest,
        isolation_valid=False,
        isolation_reasons=("no executed lineage",),
        record_index_ref=ref(index_path),
    )
    report_bytes = compact_bytes(report.model_dump(mode="json"))
    report_path.write_bytes(report_bytes)
    aggregate_path.write_bytes(
        compact_bytes(
            {
                "experiment_id": report.experiment_id,
                "experiment_digest": report.manifest_digest,
                "run_count": 0,
                "statuses": [],
                "scores": [],
                "schedule_receipts": {},
                "run_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            }
        )
    )
    return report_path


def test_verified_observation_report_is_a_distinct_verified_type(tmp_path: Path) -> None:
    report_path = write_verified_observation(tmp_path / "record")
    verified = verify_observation_report(report_path)
    assert isinstance(verified, VerifiedObservationReport)
    assert verified.report.experiment_id == "verified-observation"
    assert verified.record_index.manifest_refs == ()


def test_report_verifier_aggregates_integrity_mismatches(tmp_path: Path) -> None:
    report_path = write_verified_observation(tmp_path / "record")
    aggregate_path = report_path.parent / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["run_report_sha256"] = "0" * 64
    aggregate["experiment_digest"] = "1" * 64
    aggregate_path.write_bytes(compact_bytes(aggregate))
    with pytest.raises(ReportVerificationError) as captured:
        verify_observation_report(report_path)
    assert len(captured.value.mismatches) == 2
    assert any("run_report_sha256 mismatch" in item for item in captured.value.mismatches)
    assert any("experiment_digest" in item for item in captured.value.mismatches)


def test_report_verifier_rejects_missing_record_index(tmp_path: Path) -> None:
    report = ObservationReport(
        purpose=RunPurpose.exploratory,
        subject_kinds=(SubjectKind.fake,),
        experiment_id="unindexed",
        manifest_digest="a" * 64,
        isolation_valid=False,
        isolation_reasons=("record index unavailable",),
    )
    path = tmp_path / "observation_report.json"
    path.write_bytes(compact_bytes(report.model_dump(mode="json")))
    with pytest.raises(ReportVerificationError, match="record_index_ref is missing"):
        verify_observation_report(path)


def test_fresh_pipeline_observation_report_round_trips_through_standalone_verifier(
    tmp_path: Path,
) -> None:
    experiment = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    result = Pipeline(ROOT, tmp_path).run(experiment)

    verified = verify_observation_report(result.report_path)

    assert verified.report.experiment_id == "fake-conformance-sweep"
    assert verified.report.record_index_ref is not None
    assert len(verified.report.lineage) == len(result.runs) == 8
    assert (result.report_path.parent / "record_index.json").is_file()
    assert (result.report_path.parent / "manifests").is_dir()


def test_standalone_verifier_requires_nonbuiltin_adapter_capabilities(
    tmp_path: Path,
) -> None:
    experiment = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    result = Pipeline(ROOT, tmp_path / "records").run(experiment)
    assert result.report.record_index_ref is not None
    index = json.loads(
        Path(result.report.record_index_ref.path).read_text(encoding="utf-8")
    )
    manifest_path = Path(index["manifest_refs"][0]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["benchmark"]["adapter"] = "external.demo"
    manifest["execution"]["backend"]["adapter"] = "external.backend"
    manifest_path.write_bytes(compact_bytes(manifest))

    with pytest.raises(ReportVerificationError) as captured:
        verify_observation_report(result.report_path)
    assert any(
        "missing required adapter capability ('external.demo', 'benchmark_loader')"
        in mismatch
        for mismatch in captured.value.mismatches
    )
    assert any(
        "missing required adapter capability ('external.backend', 'backend_factory')"
        in mismatch
        for mismatch in captured.value.mismatches
    )
    assert any(
        "missing required adapter capability ('external.demo', 'execution')"
        in mismatch
        for mismatch in captured.value.mismatches
    )


def test_standalone_verifier_checks_case_set_receipt_refs(tmp_path: Path) -> None:
    experiment = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    result = Pipeline(ROOT, tmp_path).run(experiment)
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    payload["lineage"][0]["case_set_receipt_ref"]["sha256"] = "0" * 64
    result.report_path.write_bytes(compact_bytes(payload))

    with pytest.raises(ReportVerificationError, match="case_set_receipt"):
        verify_observation_report(result.report_path)


def test_standalone_verifier_checks_nested_bundle_artifacts(tmp_path: Path) -> None:
    experiment = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    result = Pipeline(ROOT, tmp_path).run(experiment)
    lineage = result.report.lineage[0]
    bundle = json.loads(
        Path(lineage.evidence_bundle_ref.path).read_text(encoding="utf-8")
    )
    output_path = Path(bundle["output_refs"][0]["path"])
    output_path.write_bytes(output_path.read_bytes() + b"tampered")

    with pytest.raises(ReportVerificationError, match="output_refs"):
        verify_observation_report(result.report_path)


def test_standalone_verifier_binds_nonselected_attempt_provenance(
    tmp_path: Path,
) -> None:
    project, experiment = checkpoint_project(tmp_path)
    protocol_path = project / "registries/protocols/fake-checkpoint-lineage.toml"
    protocol = protocol_path.read_text(encoding="utf-8")
    protocol = replace_required(protocol, "rollouts_per_case = 1", "rollouts_per_case = 3")
    protocol = replace_required(
        protocol,
        'candidate_selection = "single"',
        'candidate_selection = "best_of_n"',
    )
    protocol_path.write_text(protocol, encoding="utf-8")
    result = Pipeline(project, tmp_path / "rollout-records").run(experiment)
    lineage = result.report.lineage[0]
    schedule_path = Path(lineage.schedule_receipt_ref.path)
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    nonselected = next(item for item in schedule["attempts"] if not item["selected"])
    bundle_path = Path(nonselected["evidence_bundle_ref"]["path"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["provenance"]["benchmark_digest"] = "0" * 64
    bundle_path.write_bytes(compact_bytes(bundle))
    nonselected["evidence_bundle_ref"] = ref(bundle_path).model_dump(mode="json")
    schedule_path.write_bytes(compact_bytes(schedule))
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    payload["lineage"][0]["schedule_receipt_ref"] = ref(schedule_path).model_dump(
        mode="json"
    )
    aggregate = json.loads(result.aggregate_path.read_text(encoding="utf-8"))
    aggregate["schedule_receipts"][lineage.run_id] = ref(schedule_path).sha256
    result.aggregate_path.write_bytes(compact_bytes(aggregate))
    rewrite_report_and_aggregate(result.report_path, payload)

    with pytest.raises(ReportVerificationError, match="provenance benchmark_digest drift"):
        verify_observation_report(result.report_path)


def test_standalone_verifier_binds_nonselected_attempt_reward(
    tmp_path: Path,
) -> None:
    project, experiment = checkpoint_project(tmp_path)
    protocol_path = project / "registries/protocols/fake-checkpoint-lineage.toml"
    protocol = protocol_path.read_text(encoding="utf-8")
    protocol = replace_required(protocol, "rollouts_per_case = 1", "rollouts_per_case = 2")
    protocol = replace_required(
        protocol,
        'candidate_selection = "single"',
        'candidate_selection = "best_of_n"',
    )
    protocol_path.write_text(protocol, encoding="utf-8")
    result = Pipeline(project, tmp_path / "reward-records").run(experiment)
    schedule_path = Path(result.report.lineage[0].schedule_receipt_ref.path)
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    nonselected = next(item for item in schedule["attempts"] if not item["selected"])
    nonselected["reward_value"] = 123.0
    schedule_path.write_bytes(compact_bytes(schedule))
    rewrite_schedule_lineage(result, 0, schedule_path)

    with pytest.raises(ReportVerificationError, match="authoritative reward binding drift"):
        verify_observation_report(result.report_path)


def test_standalone_verifier_binds_nonselected_attempt_network_case(
    tmp_path: Path,
) -> None:
    project, experiment = checkpoint_project(tmp_path)
    protocol_path = project / "registries/protocols/fake-checkpoint-lineage.toml"
    protocol = protocol_path.read_text(encoding="utf-8")
    protocol = replace_required(protocol, "rollouts_per_case = 1", "rollouts_per_case = 2")
    protocol = replace_required(
        protocol,
        'candidate_selection = "single"',
        'candidate_selection = "best_of_n"',
    )
    protocol_path.write_text(protocol, encoding="utf-8")
    result = Pipeline(project, tmp_path / "network-records").run(experiment)
    schedule_path = Path(result.report.lineage[0].schedule_receipt_ref.path)
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    nonselected = next(item for item in schedule["attempts"] if not item["selected"])
    bundle_path = Path(nonselected["evidence_bundle_ref"]["path"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    policy = ResolvedNetworkPolicy.model_validate(
        {
            "resolver_adapter": "fake",
            "execution_adapter": "fake",
            "case_id": "forged-case",
            "boundary": "process",
            "allow_internet": False,
            "required_observation": "active_probe",
            "source": "backend_artifact",
            "source_artifact_digest": bundle["provenance"]["runner_digest"],
        }
    )
    bundle["network_policy"] = policy.model_dump(mode="json")
    bundle["network_observation"] = {
        "policy_digest": canonical_digest(policy),
        "declared_allow_internet": False,
        "mode": "active_probe",
        "egress_attempted": True,
        "egress_succeeded": False,
        "reached_endpoints": [],
        "evidence_refs": [bundle["output_refs"][0]],
    }
    bundle_path.write_bytes(compact_bytes(bundle))
    nonselected["evidence_bundle_ref"] = ref(bundle_path).model_dump(mode="json")
    schedule_path.write_bytes(compact_bytes(schedule))
    rewrite_schedule_lineage(result, 0, schedule_path)

    with pytest.raises(ReportVerificationError, match="network policy case binding"):
        verify_observation_report(result.report_path)


def test_standalone_verifier_binds_observed_case_order_to_allocations(
    tmp_path: Path,
) -> None:
    experiment = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    result = Pipeline(ROOT, tmp_path / "case-order-records").run(experiment)
    schedule_path = Path(result.report.lineage[0].schedule_receipt_ref.path)
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["observed_case_order"] = ["forged-case"]
    schedule_path.write_bytes(compact_bytes(schedule))
    rewrite_schedule_lineage(result, 0, schedule_path)

    with pytest.raises(ReportVerificationError, match="observed_case_order"):
        verify_observation_report(result.report_path)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("next_index", 99, "next_index"),
        ("plan_sha256", "0" * 64, "plan_sha256"),
        ("completed", {}, "completed"),
        ("schedule_receipts", {}, "schedule"),
    ],
)
def test_standalone_verifier_rejects_checkpoint_ledger_mutations(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    project, experiment = checkpoint_project(tmp_path)
    result = Pipeline(project, tmp_path / "checkpoint-records").run(experiment)
    lineage = result.report.lineage[0]
    schedule_path = Path(lineage.schedule_receipt_ref.path)
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    checkpoint_path = Path(schedule["checkpoint_save_ref"]["path"])
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint[field] = value
    checkpoint_path.write_bytes(compact_bytes(checkpoint))
    checkpoint_ref = ref(checkpoint_path)
    schedule["checkpoint_save_ref"]["written_digest"] = checkpoint_ref.sha256
    schedule["checkpoint_save_ref"]["size_bytes"] = checkpoint_ref.size_bytes
    schedule_path.write_bytes(compact_bytes(schedule))
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    payload["lineage"][0]["schedule_receipt_ref"] = ref(schedule_path).model_dump(
        mode="json"
    )
    aggregate = json.loads(result.aggregate_path.read_text(encoding="utf-8"))
    aggregate["schedule_receipts"][lineage.run_id] = ref(schedule_path).sha256
    result.aggregate_path.write_bytes(compact_bytes(aggregate))
    rewrite_report_and_aggregate(result.report_path, payload)

    with pytest.raises(ReportVerificationError, match=reason):
        verify_observation_report(result.report_path)


def test_standalone_verifier_binds_resume_load_to_complete_ancestor_ledger(
    tmp_path: Path,
) -> None:
    project, experiment = checkpoint_project(tmp_path)
    records = tmp_path / "resume-records"
    with pytest.raises(InjectedInterruption):
        Pipeline(project, records).run(experiment, stop_after=2)
    protocol_path = project / "registries/protocols/fake-checkpoint-lineage.toml"
    protocol_path.write_text(
        replace_required(
            protocol_path.read_text(encoding="utf-8"),
            'checkpoint_policy = "save"',
            'checkpoint_policy = "save_and_resume"',
        ),
        encoding="utf-8",
    )
    result = Pipeline(project, records).run(experiment, resume=True)
    lineage = result.report.lineage[2]
    schedule_path = Path(lineage.schedule_receipt_ref.path)
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["checkpoint_load_ref"]["selected_bundle_digests"] = (
        schedule["checkpoint_load_ref"]["selected_bundle_digests"][:-1]
    )
    schedule_path.write_bytes(compact_bytes(schedule))
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    payload["lineage"][2]["schedule_receipt_ref"] = ref(schedule_path).model_dump(
        mode="json"
    )
    aggregate = json.loads(result.aggregate_path.read_text(encoding="utf-8"))
    aggregate["schedule_receipts"][lineage.run_id] = ref(schedule_path).sha256
    result.aggregate_path.write_bytes(compact_bytes(aggregate))
    rewrite_report_and_aggregate(result.report_path, payload)

    with pytest.raises(ReportVerificationError, match="ancestor completed ledger"):
        verify_observation_report(result.report_path)


def test_standalone_verifier_checks_complete_aggregate_summary(tmp_path: Path) -> None:
    experiment = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    result = Pipeline(ROOT, tmp_path).run(experiment)
    aggregate_path = result.aggregate_path
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["run_count"] = 999
    aggregate_path.write_bytes(compact_bytes(aggregate))

    with pytest.raises(ReportVerificationError, match="run_count"):
        verify_observation_report(result.report_path)


def test_empty_observation_lineage_cannot_claim_positive_isolation(
    tmp_path: Path,
) -> None:
    report_path = write_verified_observation(tmp_path / "record")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["isolation_valid"] = True
    payload["isolation_reasons"] = []
    rewrite_report_and_aggregate(report_path, payload)

    with pytest.raises(ReportVerificationError, match="without executed lineage"):
        verify_observation_report(report_path)


def test_path_map_rejects_parent_traversal_in_recorded_suffix(
    tmp_path: Path,
) -> None:
    report_path = write_verified_observation(tmp_path / "record")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    index_path = report_path.parent / "record_index.json"
    mapped_root = tmp_path / "mapped" / "record"
    escaped_index = mapped_root.parent / "outside" / "record_index.json"
    escaped_index.parent.mkdir(parents=True)
    escaped_index.write_bytes(index_path.read_bytes())
    recorded_prefix = "/recorded/root"
    payload["record_index_ref"]["path"] = (
        recorded_prefix + "/../outside/record_index.json"
    )
    rewrite_report_and_aggregate(report_path, payload)

    with pytest.raises(ReportVerificationError, match="may traverse"):
        verify_observation_report(
            report_path,
            path_map={recorded_prefix: str(mapped_root)},
        )


def test_claim_protocol_gate_is_recomputed_from_schedule_validity(
    tmp_path: Path,
) -> None:
    report_path, _ = write_verified_claim(tmp_path / "records")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    lineage = payload["lineage"][0]
    schedule_path = Path(lineage["schedule_receipt_ref"]["path"])
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["schedule_valid"] = False
    schedule["mismatch_reasons"] = ["adversarial schedule mismatch"]
    schedule_path.write_bytes(compact_bytes(schedule))
    lineage["schedule_receipt_ref"] = ref(schedule_path).model_dump(mode="json")

    aggregate_path = report_path.parent / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["schedule_receipts"][lineage["run_id"]] = ref(schedule_path).sha256
    report_bytes = compact_bytes(payload)
    report_path.write_bytes(report_bytes)
    aggregate["run_report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    aggregate_path.write_bytes(compact_bytes(aggregate))

    with pytest.raises(ReportVerificationError) as captured:
        verify_claim_report(report_path)
    assert any(
        "claim protocol_valid does not match verified schedule" in mismatch
        for mismatch in captured.value.mismatches
    )


def test_claim_report_rejects_exploratory_indexed_manifests(tmp_path: Path) -> None:
    report_path, _ = write_verified_claim(tmp_path / "records")
    index = json.loads(
        (report_path.parent / "record_index.json").read_text(encoding="utf-8")
    )
    manifest_path = Path(index["manifest_refs"][0]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["claim_design"]["purpose"] = RunPurpose.exploratory.value
    manifest_path.write_bytes(compact_bytes(manifest))

    with pytest.raises(ReportVerificationError, match="purpose does not match"):
        verify_claim_report(report_path)


def test_claim_statistics_gate_is_recomputed_from_observed_order(
    tmp_path: Path,
) -> None:
    report_path, completed = write_verified_claim(tmp_path / "records")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    original_lineage = payload["lineage"]
    controls = [
        index
        for index, item in enumerate(completed)
        if item.plan.manifest.subject.id == "report.claim-control"
    ]
    treatments = [
        index
        for index, item in enumerate(completed)
        if item.plan.manifest.subject.id == "report.claim-treatment"
    ]
    order = controls + treatments
    payload["lineage"] = [original_lineage[index] for index in order]
    bundle_paths = [item["evidence_bundle_ref"]["path"] for item in payload["lineage"]]
    schedule_paths = [item["schedule_receipt_ref"]["path"] for item in payload["lineage"]]
    verifier_paths = [
        artifact["path"]
        for item in payload["lineage"]
        for artifact in json.loads(
            Path(item["evidence_bundle_ref"]["path"]).read_text(encoding="utf-8")
        )["verifier_evidence"]["artifact_refs"]
    ]
    payload["gates"][GateName.execution_valid.value]["evidence_refs"] = bundle_paths
    payload["gates"][GateName.protocol_valid.value]["evidence_refs"] = schedule_paths
    payload["gates"][GateName.statistics_valid.value]["evidence_refs"] = schedule_paths
    payload["gates"][GateName.scoring_valid.value]["evidence_refs"] = [
        *bundle_paths,
        *verifier_paths,
    ]

    aggregate_path = report_path.parent / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    bundles = [
        json.loads(Path(path).read_text(encoding="utf-8")) for path in bundle_paths
    ]
    aggregate["statuses"] = [bundle["status"] for bundle in bundles]
    aggregate["scores"] = [
        bundle["verifier_evidence"]["score"] for bundle in bundles
    ]
    report_bytes = compact_bytes(payload)
    report_path.write_bytes(report_bytes)
    aggregate["run_report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    aggregate_path.write_bytes(compact_bytes(aggregate))

    with pytest.raises(ReportVerificationError) as captured:
        verify_claim_report(report_path)
    assert any(
        "claim statistics_valid does not match verified pairing and scores" in mismatch
        for mismatch in captured.value.mismatches
    )


def test_statistical_analysis_receipt_is_replayed_by_standalone_verifier(
    tmp_path: Path,
) -> None:
    plan = StatisticalAnalysisPlan(
        holdout_required=True,
        holdout_split="test",
    )
    report_path, _ = write_verified_claim(
        tmp_path / "records",
        statistical_analysis=plan,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    receipt = report["statistics_receipt"]
    assert receipt["plan_digest"] == plan.canonical_digest()
    assert receipt["observed_pair_count"] == 4
    assert receipt["observed_min_repetitions"] == 4
    assert receipt["confidence_interval"] is not None
    verify_claim_report(report_path)


def test_statistical_analysis_receipt_tampering_is_rejected(
    tmp_path: Path,
) -> None:
    report_path, _ = write_verified_claim(
        tmp_path / "records",
        statistical_analysis=StatisticalAnalysisPlan(
            holdout_required=True,
            holdout_split="test",
        ),
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["statistics_receipt"]["point_estimate"] += 0.125
    rewrite_report_and_aggregate(report_path, payload)

    with pytest.raises(ReportVerificationError) as captured:
        verify_claim_report(report_path)
    assert any(
        "statistics_receipt does not match the replayed" in mismatch
        for mismatch in captured.value.mismatches
    )


def test_valid_gate_evidence_refs_must_match_runner_contract(tmp_path: Path) -> None:
    report_path, _ = write_verified_claim(tmp_path / "records")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["gates"][GateName.protocol_valid.value]["evidence_refs"] = [
        str((report_path.parent / "aggregate.json").resolve())
    ]
    rewrite_report_and_aggregate(report_path, payload)

    with pytest.raises(ReportVerificationError, match="evidence_refs do not match"):
        verify_claim_report(report_path)


def test_indexed_test_override_lineage_fails_closed(tmp_path: Path) -> None:
    report_path, _ = write_verified_claim(tmp_path / "records")
    index = json.loads(
        (report_path.parent / "record_index.json").read_text(encoding="utf-8")
    )
    manifest_path = Path(index["manifest_refs"][0]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"]["test_override"] = {
        "reason": "adversarial override",
        "forced_purpose": "exploratory",
    }
    manifest_path.write_bytes(compact_bytes(manifest))

    with pytest.raises(ReportVerificationError) as captured:
        verify_claim_report(report_path)
    assert any(
        "test_override lineage cannot produce a verified report" in mismatch
        for mismatch in captured.value.mismatches
    )
