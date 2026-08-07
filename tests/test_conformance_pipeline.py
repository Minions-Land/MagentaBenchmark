from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import MagentaBench.runner.pipeline as pipeline_module
from MagentaBench.adapters.fake import FakeTask
from MagentaBench.runner.backend.fake import FakeBackend
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.pipeline import (
    InjectedInterruption,
    Pipeline,
    ResumeDriftError,
)
from MagentaBench.schemas import ObservationReport, RunStatus, ScheduleActivationReceipt


ROOT = Path(__file__).parents[1]
EXPERIMENTS = ROOT / "MagentaBench" / "conformance" / "experiments"


def _replace_required(text: str, old: str, new: str) -> str:
    assert old in text, old
    replaced = text.replace(old, new)
    assert replaced != text
    return replaced


def test_subject_view_hides_verifier_gold() -> None:
    public = FakeTask().public_input()
    assert not hasattr(public, "expected")
    assert public.instruction == "Emit the BMP protocol sentinel."


def test_fake_backend_classifies_complete_failure_taxonomy(tmp_path: Path) -> None:
    compiler = Compiler(ROOT)
    fault_runs = compiler.compile(EXPERIMENTS / "fake-taxonomy.toml")
    sweep_runs = compiler.compile(EXPERIMENTS / "fake-sweep.toml")
    control = next(
        run
        for run in sweep_runs
        if run.manifest.subject.id == "fake.control"
        and run.factor_values["repetition"] == 0
    )
    backend = FakeBackend(tmp_path)

    statuses = {backend.execute(run).bundle.status for run in fault_runs}
    statuses.add(backend.execute(control).bundle.status)

    assert statuses == set(RunStatus) - {RunStatus.scored}
    assert RunStatus.pass_ in statuses
    assert RunStatus.verified_fail in statuses
    for bundle_path in tmp_path.rglob("evidence_bundle.json"):
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        assert payload["status"] in {status.value for status in RunStatus}
        assert payload["provenance"]["manifest_digest"]


def test_pipeline_rejects_declared_observed_backend_adapter_mismatch(
    tmp_path: Path,
) -> None:
    experiment = EXPERIMENTS / "subprocess-echo-smoke.toml"
    with pytest.raises(RuntimeError, match="backend adapter mismatch"):
        Pipeline(
            ROOT,
            tmp_path,
            backend=FakeBackend(tmp_path),
            allow_test_override=True,
        ).run(experiment)


def test_end_to_end_fake_sweep_writes_evidence_and_observation(tmp_path: Path) -> None:
    result = Pipeline(ROOT, tmp_path).run(EXPERIMENTS / "fake-sweep.toml")

    assert len(result.runs) == 8
    status_order = [item.case.bundle.status for item in result.runs]
    assert status_order.count(RunStatus.pass_) == 4
    assert status_order.count(RunStatus.verified_fail) == 4
    assert status_order[:4] == [
        RunStatus.verified_fail,
        RunStatus.pass_,
        RunStatus.pass_,
        RunStatus.verified_fail,
    ]
    assert isinstance(result.report, ObservationReport)
    assert result.report.observations[0].metric == "exact_match"
    assert result.report.observations[0].value == 0.5
    assert result.report.observations[0].n_runs == 8
    assert len(result.report.lineage) == 8
    assert {
        item.schedule_receipt_ref.sha256 for item in result.report.lineage
    } == {
        item.schedule_receipt_sha256 for item in result.runs
    }
    serialized = result.report.model_dump(mode="json")
    assert "claim_eligible" not in serialized
    assert "effect" not in serialized
    assert "gates" not in serialized

    experiment_dir = tmp_path / "fake-conformance-sweep"
    for name in (
        "plan.json",
        "events.jsonl",
        "aggregate.json",
        "observation_report.json",
    ):
        assert (experiment_dir / name).is_file()
    assert not (experiment_dir / "checkpoint.json").exists()
    assert not (experiment_dir / "resume_receipt.json").exists()
    case_dirs = list(experiment_dir.glob("*/cases/*__case-001__attempt-0000"))
    assert len(case_dirs) == 8
    assert len({case_dir.parents[1] for case_dir in case_dirs}) == 8
    for completed, case_dir in zip(result.runs, sorted(case_dirs)):
        assert (case_dir / "input.json").is_file()
        assert "expected" not in json.loads(
            (case_dir / "input.json").read_text(encoding="utf-8")
        )
        assert (case_dir / "subject_receipt.json").is_file()
        assert (case_dir / "status.json").is_file()
        assert (case_dir / "evidence_bundle.json").is_file()
        usage = completed.case.bundle.usage
        assert usage is not None
        assert usage.total_tokens == 0
        assert usage.cost == 0.0
        assert usage.wall_clock_seconds is not None
        assert usage.wall_clock_seconds >= 0.0
        attempt = completed.schedule_receipt.attempts[0]
        assert attempt.debit is not None
        assert attempt.debit.spent == usage


def test_interrupted_resume_is_semantically_equivalent_and_reuses_completed(
    tmp_path: Path,
) -> None:
    experiment = EXPERIMENTS / "fake-sweep.toml"
    pipeline = Pipeline(ROOT, tmp_path)
    clean = pipeline.run(experiment)
    experiment_dir = tmp_path / "fake-conformance-sweep"
    clean_aggregate = json.loads(clean.aggregate_path.read_text(encoding="utf-8"))
    clean_report = clean.report_path.read_bytes()
    clean_bundles = {
        path.relative_to(experiment_dir): path.read_bytes()
        for path in experiment_dir.rglob("evidence_bundle.json")
    }

    shutil.rmtree(experiment_dir)
    with pytest.raises(InjectedInterruption):
        Pipeline(ROOT, tmp_path).run(experiment, stop_after=3)
    resumed = Pipeline(ROOT, tmp_path).run(experiment, resume=True)

    resumed_aggregate = json.loads(
        resumed.aggregate_path.read_text(encoding="utf-8")
    )
    for key in (
        "experiment_id",
        "experiment_digest",
        "run_count",
        "statuses",
        "scores",
        "run_report_sha256",
    ):
        assert resumed_aggregate[key] == clean_aggregate[key]
    assert resumed.report_path.read_bytes() == clean_report
    assert {
        path.relative_to(experiment_dir): path.read_bytes()
        for path in experiment_dir.rglob("evidence_bundle.json")
    } == clean_bundles
    receipt = json.loads(
        (experiment_dir / "resume_receipt.json").read_text(encoding="utf-8")
    )
    assert len(receipt["reused"]) == 3
    assert len(receipt["rerun"]) == 5
    sequences = [
        json.loads(line)["seq"]
        for line in (experiment_dir / "events.jsonl").read_text().splitlines()
    ]
    assert sequences == list(range(1, len(sequences) + 1))


def test_schedule_receipt_identity_mutations_are_rejected(tmp_path: Path) -> None:
    pipeline = Pipeline(ROOT, tmp_path)
    result = pipeline.run(EXPERIMENTS / "fake-sweep.toml")
    completed = result.runs[0]
    receipt = completed.schedule_receipt
    assert receipt is not None

    changed_run = receipt.model_copy(update={"run_id": "forged-run"})
    changed_seed = receipt.model_copy(update={"order_seed": 1})
    changed_order = receipt.model_copy(
        update={"observed_case_order": ("fabricated-case",)}
    )
    changed_scheduler = receipt.model_copy(
        update={"scheduler_digest": "0" * 64}
    )

    inflated = receipt.model_dump(mode="json")
    inflated["budget_ledger"]["case_allocations"][0]["allocated"] = {
        "max_tokens": 100,
        "max_cost": 100.0,
    }
    inflated["budget_ledger"]["attempt_allocations"][0]["allocated"] = {
        "max_tokens": 100,
        "max_cost": 100.0,
    }
    inflated["attempts"][0]["debit"]["released"] = {
        "max_tokens": 100,
        "max_cost": 100.0,
    }
    inflated_budget = ScheduleActivationReceipt.model_validate(inflated)

    for mutation, reason in (
        (changed_run, "run_id"),
        (changed_seed, "order_seed"),
        (changed_order, "observed_case_order"),
        (changed_scheduler, "scheduler digest"),
        (inflated_budget, "root max_tokens allocation"),
    ):
        with pytest.raises(ResumeDriftError, match=reason):
            pipeline._validate_receipt_identity(completed.plan, mutation)


def test_resume_validates_every_retained_rollout_bundle(tmp_path: Path) -> None:
    project = tmp_path / "rollout-resume-project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance",
        project / "MagentaBench/conformance",
    )
    protocol_path = project / "registries/protocols/fake-deterministic.toml"
    protocol_text = protocol_path.read_text(encoding="utf-8")
    protocol_text = _replace_required(
        protocol_text, "rollouts_per_case = 1", "rollouts_per_case = 3"
    )
    protocol_text = _replace_required(
        protocol_text, "parallelism = 1", "parallelism = 2"
    )
    protocol_text = _replace_required(
        protocol_text, 'candidate_selection = "single"', 'candidate_selection = "best_of_n"'
    )
    protocol_path.write_text(protocol_text, encoding="utf-8")
    experiment = project / "MagentaBench/conformance/experiments/fake-sweep.toml"
    record_root = tmp_path / "rollout-resume-records"
    with pytest.raises(InjectedInterruption):
        Pipeline(project, record_root).run(experiment, stop_after=1)

    experiment_root = record_root / "fake-conformance-sweep"
    receipt = json.loads(
        next(experiment_root.rglob("schedule_activation_receipt.json")).read_text(
            encoding="utf-8"
        )
    )
    nonselected = next(item for item in receipt["attempts"] if not item["selected"])
    bundle_path = experiment_root / nonselected["evidence_bundle_ref"]["path"]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["provenance"]["runner_digest"] = "0" * 64
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    with pytest.raises(ResumeDriftError, match="runner_digest"):
        Pipeline(project, record_root).run(experiment, resume=True)


def _project_with_checkpoint_policy(tmp_path: Path, policy: str) -> tuple[Path, Path]:
    project = tmp_path / policy
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench" / "conformance",
        project / "MagentaBench" / "conformance",
    )
    protocol_path = project / "registries/protocols/fake-deterministic.toml"
    protocol_text = protocol_path.read_text(encoding="utf-8")
    if policy != "disabled":
        protocol_text = _replace_required(
            protocol_text,
            'checkpoint_policy = "disabled"',
            f'checkpoint_policy = "{policy}"',
        )
    protocol_path.write_text(protocol_text, encoding="utf-8")
    experiment = project / "MagentaBench/conformance/experiments/fake-sweep.toml"
    return project, experiment


def test_checkpoint_policy_controls_actual_pipeline_writes(tmp_path: Path) -> None:
    disabled_project, disabled_experiment = _project_with_checkpoint_policy(
        tmp_path, "disabled"
    )
    disabled_records = tmp_path / "disabled-records"
    disabled = Pipeline(disabled_project, disabled_records).run(disabled_experiment)
    assert not (disabled_records / "fake-conformance-sweep/checkpoint.json").exists()
    assert all(
        item.schedule_receipt is not None
        and item.schedule_receipt.declared_checkpoint_policy == "disabled"
        and item.schedule_receipt.checkpoint_save_ref is None
        and item.schedule_receipt.checkpoint_load_ref is None
        for item in disabled.runs
    )

    save_project, save_experiment = _project_with_checkpoint_policy(tmp_path, "save")
    save_records = tmp_path / "save-records"
    saved = Pipeline(save_project, save_records).run(save_experiment)
    assert (save_records / "fake-conformance-sweep/checkpoint.json").is_file()
    assert all(
        item.schedule_receipt is not None
        and item.schedule_receipt.declared_checkpoint_policy == "save"
        and item.schedule_receipt.checkpoint_save_ref is not None
        and item.schedule_receipt.checkpoint_load_ref is None
        for item in saved.runs
    )


def test_resume_drift_aborts_nonzero_path(tmp_path: Path) -> None:
    experiment = EXPERIMENTS / "fake-sweep.toml"
    with pytest.raises(InjectedInterruption):
        Pipeline(ROOT, tmp_path).run(experiment, stop_after=1)
    plan_path = tmp_path / "fake-conformance-sweep" / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["runs"][0]["backend_digest"] = "drifted"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ResumeDriftError, match="drift"):
        Pipeline(ROOT, tmp_path).run(experiment, resume=True)


def test_resume_refuses_benchmark_scoring_semantics_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench" / "conformance",
        project / "MagentaBench" / "conformance",
    )
    shutil.copytree(
        ROOT / "MagentaBench" / "adapters" / "fake",
        project / "MagentaBench" / "adapters" / "fake",
    )
    benchmark_path = project / "registries" / "benchmarks" / "fake-exact.toml"
    original_declaration = benchmark_path.read_text(encoding="utf-8")
    changed_declaration = _replace_required(
        original_declaration,
        'reward_pass_value = 1.0',
        'reward_pass_value = 0.0',
    )
    assert changed_declaration != original_declaration

    experiment = project / "MagentaBench" / "conformance" / "experiments" / "fake-sweep.toml"
    record_root = tmp_path / "records"
    with pytest.raises(InjectedInterruption):
        Pipeline(project, record_root).run(experiment, stop_after=1)

    benchmark_path.write_text(changed_declaration, encoding="utf-8")
    with pytest.raises(ResumeDriftError, match="drift"):
        Pipeline(project, record_root).run(experiment, resume=True)


def test_resume_refuses_post_checkpoint_schedule_receipt_tamper(
    tmp_path: Path,
) -> None:
    experiment = EXPERIMENTS / "fake-sweep.toml"
    with pytest.raises(InjectedInterruption):
        Pipeline(ROOT, tmp_path).run(experiment, stop_after=1)
    receipt_path = next(
        (tmp_path / "fake-conformance-sweep").rglob(
            "schedule_activation_receipt.json"
        )
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["run_id"] = "forged-run"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ResumeDriftError, match="schedule receipt digest drift"):
        Pipeline(ROOT, tmp_path).run(experiment, resume=True)


@pytest.mark.parametrize("mutation", ["delete", "extra", "missing"])
def test_resume_enforces_checkpoint_schedule_receipt_lineage(
    tmp_path: Path, mutation: str
) -> None:
    experiment = EXPERIMENTS / "fake-sweep.toml"
    with pytest.raises(InjectedInterruption):
        Pipeline(ROOT, tmp_path).run(experiment, stop_after=1)
    experiment_root = tmp_path / "fake-conformance-sweep"
    checkpoint_path = experiment_root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if mutation == "delete":
        next(experiment_root.rglob("schedule_activation_receipt.json")).unlink()
    elif mutation == "extra":
        checkpoint["schedule_receipts"]["extra-run"] = "0" * 64
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    else:
        checkpoint["schedule_receipts"].pop(next(iter(checkpoint["schedule_receipts"])))
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(ResumeDriftError, match="schedule receipt"):
        Pipeline(ROOT, tmp_path).run(experiment, resume=True)


def test_resume_refuses_complete_bundle_provenance_drift(tmp_path: Path) -> None:
    experiment = EXPERIMENTS / "fake-sweep.toml"
    with pytest.raises(InjectedInterruption):
        Pipeline(ROOT, tmp_path).run(experiment, stop_after=1)
    experiment_dir = tmp_path / "fake-conformance-sweep"
    bundle_path = next(experiment_dir.rglob("evidence_bundle.json"))
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["provenance"]["runner_digest"] = "0" * 64
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    with pytest.raises(ResumeDriftError, match="runner_digest"):
        Pipeline(ROOT, tmp_path).run(experiment, resume=True)


def test_corrupt_partial_bundle_is_rerun_not_reused(tmp_path: Path) -> None:
    experiment = EXPERIMENTS / "fake-sweep.toml"
    with pytest.raises(InjectedInterruption):
        Pipeline(ROOT, tmp_path).run(experiment, stop_after=2)
    experiment_dir = tmp_path / "fake-conformance-sweep"
    completed_bundles = sorted(experiment_dir.rglob("evidence_bundle.json"))
    assert len(completed_bundles) == 2
    completed_bundles[0].write_text("{corrupt", encoding="utf-8")
    stale_cache = completed_bundles[0].parent / "stale-cache.txt"
    stale_cache.write_text("must not leak into rerun", encoding="utf-8")

    Pipeline(ROOT, tmp_path).run(experiment, resume=True)
    receipt = json.loads(
        (experiment_dir / "resume_receipt.json").read_text(encoding="utf-8")
    )
    assert len(receipt["reused"]) == 1
    assert len(receipt["rerun"]) == 7
    assert not stale_cache.exists()
