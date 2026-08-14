from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import shutil

import pytest

from MagentaBench.collab import build_experiment_ledger, parse_path_maps, render_csv
from MagentaBench.collab import ledger as ledger_module
from MagentaBench.collab.cli import main as collab_main
from MagentaBench.collab.ledger import (
    ExperimentLedger,
    _expected_manifest_identities,
    _manifest_rows,
    _method_id,
    _run_rows,
)
from MagentaBench.collab.repository import ExperimentRepository
from MagentaBench.lab import LabArtifactRef, LabRunLink, LabRunState, LabStore
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas.verification import verify_run_report


ROOT = Path(__file__).parents[1]
FAKE_SPEC = "MagentaBench/conformance/experiments/fake-sweep.toml"
DUPLICATE_SPEC = "MagentaBench/conformance/experiments/subprocess-echo-smoke.toml"


@pytest.fixture(scope="module")
def fake_run(tmp_path_factory: pytest.TempPathFactory):
    record_root = tmp_path_factory.mktemp("experiment-ledger-records")
    result = Pipeline(ROOT, record_root).run(ROOT / FAKE_SPEC)
    return result, record_root


def _fake_bundle(*, project_root: Path = ROOT):
    source = ExperimentRepository(ROOT).load_bundle(
        "experiments/terminal-bench-magenta-smoke/bundle.json"
    )
    spec = project_root / FAKE_SPEC
    return source.model_copy(
        update={
            "bmp_spec": FAKE_SPEC,
            "bmp_spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
            "id": "fake-conformance-sweep",
            "protocol_id": "fake.deterministic.v1",
            "execution": source.execution.model_copy(
                update={"backend_id": "fake.local"}
            ),
        }
    )


def _finished_state(result, report_path: Path | None = None):
    linked_report = result.report_path if report_path is None else report_path
    content = linked_report.read_bytes()
    state = LabStore(ROOT).load("magenta-single-case-pilot")
    return state.model_copy(
        update={
            "runs": (
                LabRunLink(
                    run_id="verified-ledger-run",
                    state=LabRunState.finished,
                    record_root=str(linked_report.parent),
                    manifest_digest=result.report.manifest_digest,
                    report_ref=LabArtifactRef(
                        locator=str(linked_report),
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                    ),
                ),
            )
        }
    )


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
    assert row["run_count"] == 0
    assert row["run_ids"] == []
    assert row["run_states"] == {}
    assert "latest_run_id" not in row
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
    assert "run_count,run_ids,run_states" in header
    assert "latest_run" not in header
    assert "terminal-bench-magenta-smoke" in first
    assert render_csv(ledger, "runs").splitlines() == [
        "experiment_id,lab_issue,lab_run_id,run_state,record_root,report_ref,manifest_digest,purpose,standalone_verification,claim_eligible,protocol_valid,isolation_valid,validity_gates,failure_breakdown,metric_row_count,verified_manifest_refs"
    ]
    assert render_csv(ledger, "metrics").splitlines() == [
        "experiment_id,lab_run_id,parent_run_id,manifest_digest,method_id,factor_values,configuration_id,configuration_digest,configuration_profiles,subject_id,model,benchmark_id,dataset_id,dataset_commit,dataset_digest,dataset_split,backend_id,purpose,metric_id,metric_digest,metric_state,value,reason,planned_rollout_count,task_count,rollouts_per_task,observed_count,zero_filled_count,excluded_count,missing_count,invalid_count,uncertainty_method,uncertainty_confidence_level,uncertainty_lower,uncertainty_upper"
    ]


def test_path_map_requires_absolute_unique_prefixes() -> None:
    assert parse_path_maps(["/old=/new"]) == {"/old": "/new"}
    assert parse_path_maps(["/=/restored"]) == {"/": "/restored"}
    with pytest.raises(ValueError, match="absolute OLD=NEW"):
        parse_path_maps(["old=new"])
    with pytest.raises(ValueError, match="duplicate"):
        parse_path_maps(["/old=/new", "/old=/other"])
    for value in (
        "/old/../root=/new",
        "/old=/new/../target",
        "/old/=/new",
        "//old=/new",
    ):
        with pytest.raises(ValueError, match="normalized absolute"):
            parse_path_maps([value])


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
    assert (
        "must name claim_report.json or observation_report.json"
        in ledger.errors[0]["message"]
    )


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
    fake_run,
) -> None:
    result, _ = fake_run
    verified = verify_run_report(result.report_path)
    bundle = _fake_bundle()
    linked = _finished_state(result)
    runs, metrics, errors = _run_rows(ROOT, bundle, linked)

    assert not errors
    assert runs[0]["standalone_verification"] == "verified"
    assert len(metrics) == len(verified.report.metric_results)
    assert {row["metric_id"] for row in metrics} == {
        item.metric_id for item in verified.report.metric_results
    }
    assert all(
        row["method_id"] in {"fake.control", "fake.treatment"} for row in metrics
    )
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


def test_report_must_match_the_bundle_pinned_resolved_identity(
    tmp_path: Path,
    fake_run,
) -> None:
    result, _ = fake_run
    project = tmp_path / "project"
    shutil.copytree(ROOT / "MagentaBench", project / "MagentaBench")
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(ROOT / "plugins", project / "plugins")
    spec = project / FAKE_SPEC
    original = spec.read_text(encoding="utf-8")
    changed = original.replace("max_wall_seconds = 1.0", "max_wall_seconds = 3.0")
    assert changed != original
    spec.write_text(changed, encoding="utf-8")
    bundle = _fake_bundle(project_root=project)

    runs, metrics, errors = _run_rows(
        project,
        bundle,
        _finished_state(result),
    )

    assert runs[0]["standalone_verification"] == "failed"
    assert metrics == []
    assert errors[0]["code"] == "run-verification"
    assert "identities differ from the pinned BMP declaration" in errors[0]["message"]


def test_pinned_bmp_is_compiled_from_the_hashed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "MagentaBench", project / "MagentaBench")
    shutil.copytree(ROOT / "registries", project / "registries")
    spec = project / FAKE_SPEC
    bundle = _fake_bundle(project_root=project)
    expected = tuple(
        sorted(
            (run.manifest.metadata.run_id, run.manifest_digest)
            for run in Compiler(project).compile(spec)
        )
    )
    original_compile = ledger_module.Compiler.compile
    replaced = False

    def replace_before_compile(self, path, **kwargs):
        nonlocal replaced
        if not replaced:
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "max_wall_seconds = 1.0", "max_wall_seconds = 3.0"
                ),
                encoding="utf-8",
            )
            replaced = True
        return original_compile(self, path, **kwargs)

    monkeypatch.setattr(ledger_module.Compiler, "compile", replace_before_compile)

    assert _expected_manifest_identities(project, bundle) == expected
    assert replaced


def test_manifest_record_order_is_not_identity(
    fake_run,
) -> None:
    result, _ = fake_run
    verified = verify_run_report(result.report_path)
    reversed_index = verified.record_index.model_copy(
        update={"manifest_refs": tuple(reversed(verified.record_index.manifest_refs))}
    )
    reordered = verified.__class__(
        report=verified.report,
        report_path=verified.report_path,
        record_index=reversed_index,
    )

    snapshot = _manifest_rows(reordered, ROOT, {})

    assert snapshot.identities == _expected_manifest_identities(ROOT, _fake_bundle())


def test_manifest_replacement_after_standalone_verification_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_run,
) -> None:
    result, record_root = fake_run
    restored_root = tmp_path / "restored"
    shutil.copytree(record_root, restored_root)
    restored_report = restored_root / result.report_path.relative_to(record_root)
    path_map = {str(record_root): str(restored_root)}
    replaced = False

    def replace_after_verification(path: Path, *, path_map):
        nonlocal replaced
        verified = verify_run_report(path, path_map=path_map)
        if not replaced:
            manifest_ref = verified.record_index.manifest_refs[0]
            manifest_path = restored_root / Path(manifest_ref.path).relative_to(
                record_root
            )
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["created_at"] = "2099-01-01T00:00:00Z"
            manifest_path.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            replaced = True
        return verified

    monkeypatch.setattr(ledger_module, "verify_run_report", replace_after_verification)
    runs, metrics, errors = _run_rows(
        ROOT,
        _fake_bundle(),
        _finished_state(result, restored_report),
        path_map=path_map,
    )

    assert replaced
    assert runs[0]["standalone_verification"] == "failed"
    assert metrics == []
    assert "verified manifest digest or size" in errors[0]["message"]


def test_linked_report_is_verified_from_its_content_addressed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_run,
) -> None:
    result, record_root = fake_run
    restored_root = tmp_path / "restored"
    shutil.copytree(record_root, restored_root)
    restored_report = restored_root / result.report_path.relative_to(record_root)
    path_map = {str(record_root): str(restored_root)}
    observed_snapshot: Path | None = None

    def replace_link_after_snapshot(path: Path, *, path_map):
        nonlocal observed_snapshot
        observed_snapshot = path
        restored_report.write_text("{}\n", encoding="utf-8")
        return verify_run_report(path, path_map=path_map)

    monkeypatch.setattr(ledger_module, "verify_run_report", replace_link_after_snapshot)
    runs, metrics, errors = _run_rows(
        ROOT,
        _fake_bundle(),
        _finished_state(result, restored_report),
        path_map=path_map,
    )

    assert observed_snapshot is not None
    assert observed_snapshot != restored_report
    assert not errors
    assert runs[0]["standalone_verification"] == "verified"
    assert metrics


def test_relocated_projection_does_not_expose_materialization_paths(
    tmp_path: Path,
    fake_run,
) -> None:
    result, record_root = fake_run

    def project_at(restored_root: Path):
        shutil.copytree(record_root, restored_root)
        restored_report = restored_root / result.report_path.relative_to(record_root)
        return _run_rows(
            ROOT,
            _fake_bundle(),
            _finished_state(result, restored_report),
            path_map={str(record_root): str(restored_root)},
        )

    first = project_at(tmp_path / "host-a")
    second = project_at(tmp_path / "host-b")

    assert first == second
    runs, metrics, errors = first
    assert not errors
    assert runs[0]["record_root"] == "<external>"
    assert runs[0]["report_ref"].startswith("sha256:")
    assert all(path.startswith("sha256:") for path in runs[0]["verified_manifest_refs"])
    serialized = json.dumps({"runs": runs, "metrics": metrics}, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert str(record_root) not in serialized


def test_nonempty_run_and_metric_csv_round_trip(fake_run) -> None:
    result, _ = fake_run
    runs, metrics, errors = _run_rows(
        ROOT,
        _fake_bundle(),
        _finished_state(result),
    )
    assert not errors
    escaped_run = dict(runs[0])
    escaped_run["report_ref"] = 'artifact,with "quotes"\nand a newline'
    ledger = ExperimentLedger(
        experiments=(),
        runs=(escaped_run,),
        metrics=tuple(metrics),
    )

    run_rows = list(csv.DictReader(io.StringIO(render_csv(ledger, "runs"))))
    metric_rows = list(csv.DictReader(io.StringIO(render_csv(ledger, "metrics"))))

    assert run_rows[0]["report_ref"] == escaped_run["report_ref"]
    assert len(metric_rows) == len(metrics)
    assert json.loads(metric_rows[0]["factor_values"]) == metrics[0]["factor_values"]
    assert json.loads(metric_rows[0]["configuration_profiles"]) == []


def test_evolution_method_identity_is_separate_from_configuration() -> None:
    evolver = (
        Compiler(ROOT)
        .compile(
            ROOT
            / "MagentaBench/conformance/experiments/deterministic-evolution-smoke.toml"
        )[0]
        .manifest
    )
    meta_evolver = (
        Compiler(ROOT)
        .compile(
            ROOT
            / "MagentaBench/conformance/experiments/deterministic-meta-evolution-smoke.toml"
        )[0]
        .manifest
    )

    assert evolver.metadata.configuration is not None
    assert _method_id(evolver) == "evolver.deterministic.v1"
    assert meta_evolver.metadata.configuration is not None
    assert _method_id(meta_evolver) == "meta-evolver.deterministic.v1"


def test_duplicate_declaration_ids_fail_without_duplicate_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "experiments", project / "experiments")
    shutil.copytree(
        ROOT / "MagentaBench/conformance/experiments",
        project / "MagentaBench/conformance/experiments",
    )
    shutil.copytree(ROOT / "lab", project / "lab")
    source = project / DUPLICATE_SPEC
    shutil.copyfile(source, source.with_name("duplicate-subprocess-echo-smoke.toml"))
    validation = ExperimentRepository(ROOT).validate()
    monkeypatch.setattr(
        ExperimentRepository,
        "validate",
        lambda self, **kwargs: validation,
    )

    ledger = build_experiment_ledger(project)

    assert not ledger.ok
    assert any(item["code"] == "duplicate-experiment-id" for item in ledger.errors)
    assert [row["experiment_id"] for row in ledger.experiments].count(
        "subprocess-echo-smoke"
    ) == 0


def test_ledger_uses_one_lab_state_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = ExperimentRepository(ROOT).validate()
    monkeypatch.setattr(
        ExperimentRepository,
        "validate",
        lambda self, **kwargs: validation,
    )

    original_list = LabStore.list
    list_calls = 0

    def counted_list(self: LabStore):
        nonlocal list_calls
        list_calls += 1
        return original_list(self)

    monkeypatch.setattr(LabStore, "list", counted_list)

    ledger = build_experiment_ledger(ROOT)

    assert ledger.ok, ledger.errors
    assert list_calls == 1


def test_cli_rejects_noncanonical_map_without_machine_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    return_code = collab_main(
        (
            "--project-root",
            str(ROOT),
            "ledger",
            "--format",
            "json",
            "--map",
            "/old/../root=/new",
        )
    )

    captured = capsys.readouterr()
    assert return_code == 2
    assert captured.out == ""
    assert "normalized absolute OLD=NEW" in captured.err
