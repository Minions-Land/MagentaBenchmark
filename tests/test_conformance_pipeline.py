from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from MagentaBench.adapters.fake import FakeTask
from MagentaBench.runner.backend.fake import FakeBackend
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.pipeline import (
    InjectedInterruption,
    Pipeline,
    ResumeDriftError,
)
from MagentaBench.schemas import GateName, RunStatus


ROOT = Path(__file__).parents[1]
EXPERIMENTS = ROOT / "MagentaBench" / "conformance" / "experiments"


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
        and run.factor_values["execution.seed"] == 11
        and run.factor_values["repetition"] == 0
    )
    backend = FakeBackend(tmp_path)

    statuses = {backend.execute(run).bundle.status for run in fault_runs}
    statuses.add(backend.execute(control).bundle.status)

    assert statuses == set(RunStatus)
    assert RunStatus.pass_ in statuses
    assert RunStatus.verified_fail in statuses
    for bundle_path in tmp_path.rglob("evidence_bundle.json"):
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        assert payload["status"] in {status.value for status in RunStatus}
        assert payload["provenance"]["manifest_digest"]


def test_end_to_end_fake_sweep_writes_evidence_and_eligible_claim(tmp_path: Path) -> None:
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
    assert result.claim_report.claim_eligible is True
    assert result.claim_report.effect is not None
    assert result.claim_report.effect.point_estimate == 1.0
    assert result.claim_report.effect.n_pairs == 4
    assert len(result.claim_report.lineage) == 8
    assert all(
        result.claim_report.gates[name].valid
        for name in (
            GateName.execution_valid,
            GateName.protocol_valid,
            GateName.isolation_valid,
            GateName.scoring_valid,
            GateName.statistics_valid,
        )
    )

    experiment_dir = tmp_path / "fake-conformance-sweep"
    for name in (
        "plan.json",
        "events.jsonl",
        "checkpoint.json",
        "aggregate.json",
        "claim_report.json",
        "resume_receipt.json",
    ):
        assert (experiment_dir / name).is_file()
    case_dirs = list(experiment_dir.glob("*/cases/case-001"))
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
        assert usage.wall_clock_seconds == 0.0


def test_interrupted_resume_is_semantically_equivalent_and_reuses_completed(
    tmp_path: Path,
) -> None:
    experiment = EXPERIMENTS / "fake-sweep.toml"
    pipeline = Pipeline(ROOT, tmp_path)
    clean = pipeline.run(experiment)
    experiment_dir = tmp_path / "fake-conformance-sweep"
    clean_aggregate = clean.aggregate_path.read_bytes()
    clean_claim = clean.claim_report_path.read_bytes()
    clean_bundles = {
        path.relative_to(experiment_dir): path.read_bytes()
        for path in experiment_dir.rglob("evidence_bundle.json")
    }

    shutil.rmtree(experiment_dir)
    with pytest.raises(InjectedInterruption):
        Pipeline(ROOT, tmp_path).run(experiment, stop_after=3)
    resumed = Pipeline(ROOT, tmp_path).run(experiment, resume=True)

    assert resumed.aggregate_path.read_bytes() == clean_aggregate
    assert resumed.claim_report_path.read_bytes() == clean_claim
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
    scored_declaration = benchmark_path.read_text(encoding="utf-8")
    unscored_declaration = scored_declaration.replace(
        'authoritative_reward_metric = "score"\nreward_pass_value = 1.0\n',
        "",
    )
    assert unscored_declaration != scored_declaration
    benchmark_path.write_text(unscored_declaration, encoding="utf-8")

    experiment = project / "MagentaBench" / "conformance" / "experiments" / "fake-sweep.toml"
    record_root = tmp_path / "records"
    with pytest.raises(InjectedInterruption):
        Pipeline(project, record_root).run(experiment, stop_after=1)

    benchmark_path.write_text(scored_declaration, encoding="utf-8")
    with pytest.raises(ResumeDriftError, match="drift"):
        Pipeline(project, record_root).run(experiment, resume=True)


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
