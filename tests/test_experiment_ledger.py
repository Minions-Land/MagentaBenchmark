from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from MagentaBench.collab import build_experiment_ledger, parse_path_maps, render_csv
from MagentaBench.collab.ledger import _run_rows
from MagentaBench.collab.repository import ExperimentRepository
from MagentaBench.lab import LabArtifactRef, LabRunLink, LabRunState, LabStore
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas.verification import verify_run_report


ROOT = Path(__file__).parents[1]


def test_checked_in_ledger_is_derived_from_bundle_and_lab() -> None:
    ledger = build_experiment_ledger(ROOT)

    assert ledger.ok, ledger.errors
    assert len(ledger.experiments) == len(
        tuple((ROOT / "MagentaBench/conformance/experiments").glob("*.toml"))
    )
    row = next(
        item
        for item in ledger.experiments
        if item["experiment_id"] == "terminal-bench-magenta-smoke"
    )
    assert row["experiment_id"] == "terminal-bench-magenta-smoke"
    assert row["benchmark_id"] == "terminal-bench-2.1"
    assert row["dataset_id"] == "dataset.terminal-bench-2.1"
    assert row["subject_id"] == "terminal-bench.magenta"
    assert row["model"] == "openai/gpt-5.6"
    assert row["metric_ids"][0] == "reward.authoritative.v1"
    assert row["case_ids"] == ["fix-git"]
    assert row["lab_status"] == "blocked"
    assert ledger.runs == ()
    assert ledger.metrics == ()
    unmanaged = next(
        item
        for item in ledger.experiments
        if item["experiment_id"] == "fake-conformance-sweep"
    )
    assert unmanaged["lab_status"] == "unmanaged"
    assert unmanaged["bundle_id"] is None


def test_csv_is_machine_readable_and_deterministic() -> None:
    ledger = build_experiment_ledger(ROOT)

    first = render_csv(ledger, "experiments")
    second = render_csv(ledger, "experiments")
    assert first == second
    header = first.splitlines()[0]
    assert "experiment_id" in header
    assert "terminal-bench-magenta-smoke" in first
    assert render_csv(ledger, "metrics").splitlines() == [
        "experiment_id,lab_run_id,parent_run_id,manifest_digest,method_id,factor_values,configuration_id,configuration_digest,configuration_profiles,subject_id,model,benchmark_id,dataset_id,dataset_commit,dataset_digest,dataset_split,backend_id,purpose,metric_id,metric_digest,metric_state,value,reason,planned_rollout_count,task_count,rollouts_per_task,observed_count,zero_filled_count,excluded_count,missing_count,invalid_count,uncertainty_method,uncertainty_confidence_level,uncertainty_lower,uncertainty_upper"
    ]


def test_path_map_requires_absolute_unique_prefixes() -> None:
    assert parse_path_maps(["/old=/new"]) == {"/old": "/new"}
    with pytest.raises(ValueError, match="absolute OLD=NEW"):
        parse_path_maps(["old=new"])
    with pytest.raises(ValueError, match="duplicate"):
        parse_path_maps(["/old=/new", "/old=/other"])


def test_ledger_rejects_non_repository_report_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "experiments", project / "experiments")
    shutil.copytree(
        ROOT / "MagentaBench/conformance/experiments",
        project / "MagentaBench/conformance/experiments",
    )
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(ROOT / "execution-profiles", project / "execution-profiles")
    shutil.copytree(ROOT / "lab", project / "lab")
    report = project / "artifacts/untrusted.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}\n", encoding="utf-8")
    content = report.read_bytes()
    ref = LabArtifactRef(
        locator="artifacts/untrusted.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    store = LabStore(project)
    state = store.load("magenta-single-case-pilot")
    linked = state.model_copy(
        update={
            "runs": (
                LabRunLink(
                    run_id="unsafe-ledger-run",
                    state=LabRunState.finished,
                    record_root="artifacts/run",
                    report_ref=ref,
                ),
            )
        }
    )
    original_load = LabStore.load
    original_list = LabStore.list

    monkeypatch.setattr(
        LabStore,
        "load",
        lambda self, issue_id: (
            linked
            if self.root == project / "lab" and issue_id == "magenta-single-case-pilot"
            else original_load(self, issue_id)
        ),
    )
    monkeypatch.setattr(
        LabStore,
        "list",
        lambda self: (
            tuple(
                linked if item.issue.issue_id == "magenta-single-case-pilot" else item
                for item in original_list(self)
            )
            if self.root == project / "lab"
            else original_list(self)
        ),
    )
    monkeypatch.setattr(
        LabStore,
        "doctor",
        lambda self, **kwargs: {
            "errors": [],
            "format": "magentabench-lab-doctor-v1",
            "issue_count": len(original_list(self)),
            "ok": True,
            "warnings": [],
        },
    )

    ledger = build_experiment_ledger(project)

    assert not ledger.ok
    assert "must name claim_report.json or observation_report.json" in ledger.errors[0][
        "message"
    ]


def test_finished_report_must_pass_standalone_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "experiments", project / "experiments")
    shutil.copytree(
        ROOT / "MagentaBench/conformance/experiments",
        project / "MagentaBench/conformance/experiments",
    )
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(ROOT / "execution-profiles", project / "execution-profiles")
    shutil.copytree(ROOT / "lab", project / "lab")
    report = project / "artifacts/observation_report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}\n", encoding="utf-8")
    content = report.read_bytes()
    ref = LabArtifactRef(
        locator="artifacts/observation_report.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    store = LabStore(project)
    state = store.load("magenta-single-case-pilot")
    linked = state.model_copy(
        update={
            "runs": (
                LabRunLink(
                    run_id="ledger-test-run",
                    state=LabRunState.finished,
                    record_root="artifacts/run",
                    report_ref=ref,
                ),
            )
        }
    )
    original_load = LabStore.load
    original_list = LabStore.list

    def patched_load(self: LabStore, issue_id: str):
        if self.root == project / "lab" and issue_id == "magenta-single-case-pilot":
            return linked
        return original_load(self, issue_id)

    monkeypatch.setattr(LabStore, "load", patched_load)

    def patched_list(self: LabStore):
        states = original_list(self)
        if self.root != project / "lab":
            return states
        return tuple(
            linked if item.issue.issue_id == "magenta-single-case-pilot" else item
            for item in states
        )

    monkeypatch.setattr(LabStore, "list", patched_list)

    monkeypatch.setattr(
        LabStore,
        "doctor",
        lambda self, **kwargs: {
            "errors": [],
            "format": "magentabench-lab-doctor-v1",
            "issue_count": len(original_list(self)),
            "ok": True,
            "warnings": [],
        },
    )

    ledger = build_experiment_ledger(project)

    assert not ledger.ok
    assert ledger.runs[0]["standalone_verification"] == "failed"
    assert ledger.metrics == ()
    assert ledger.errors[0]["code"] == "run-verification"
    assert "report verification failed" in ledger.errors[0]["message"]


def test_json_projection_contains_all_normalized_tables() -> None:
    payload = build_experiment_ledger(ROOT).as_dict()

    assert payload["format"] == "magentabench-experiment-ledger-v1"
    assert payload["experiment_count"] == len(payload["experiments"])
    assert payload["run_count"] == len(payload["runs"])
    assert payload["metric_row_count"] == len(payload["metrics"])
    json.dumps(payload, allow_nan=False, sort_keys=True)


def test_verified_report_expands_into_long_form_metric_rows(
    tmp_path: Path,
) -> None:
    result = Pipeline(ROOT, tmp_path / "records").run(
        ROOT / "MagentaBench/conformance/experiments/subprocess-echo-smoke.toml"
    )
    verified = verify_run_report(result.report_path)
    bundle = ExperimentRepository(ROOT).load_bundle(
        "experiments/terminal-bench-magenta-smoke/bundle.json"
    ).model_copy(update={"id": "subprocess-echo-smoke"})
    state = LabStore(ROOT).load("magenta-single-case-pilot")
    linked = state.model_copy(
        update={
            "runs": (
                LabRunLink(
                    run_id="verified-ledger-run",
                    state=LabRunState.finished,
                    record_root=str(result.report_path.parent),
                    manifest_digest=result.report.manifest_digest,
                    report_ref=LabArtifactRef(
                        locator=str(result.report_path),
                        sha256=hashlib.sha256(result.report_path.read_bytes()).hexdigest(),
                        size_bytes=result.report_path.stat().st_size,
                    ),
                ),
            )
        }
    )
    runs, metrics, errors = _run_rows(ROOT, bundle, linked)

    assert not errors
    assert runs[0]["standalone_verification"] == "verified"
    assert len(metrics) == len(verified.report.metric_results)
    assert {row["metric_id"] for row in metrics} == {
        item.metric_id for item in verified.report.metric_results
    }
    assert all(row["method_id"] in {"fake.control", "fake.treatment"} for row in metrics)
    assert all(row["dataset_id"] == "dataset.fake.exact.v1" for row in metrics)
    assert {tuple(sorted(row["factor_values"])) for row in metrics} == {
        ("repetition", "subject")
    }
    assert {row["factor_values"]["subject"] for row in metrics} == {
        "fake.control",
        "fake.treatment",
    }
    assert all(row["configuration_id"] is None for row in metrics)
    assert all(row["configuration_digest"] is None for row in metrics)
    assert all(row["configuration_profiles"] == [] for row in metrics)
