from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from MagentaBench.collab.import_models import (
    HistoricalRecord,
    HistoricalSource,
    canonical_json_bytes,
    canonical_repository_name,
    compute_record_id,
    logical_key_digest,
    source_snapshot_identity,
)
from MagentaBench.collab.imports import (
    HistoricalImportError,
    LoadedHistoricalRecord,
    _validate_supersession_graph,
    load_historical_imports,
    validate_historical_imports,
)
from MagentaBench.collab import imports as imports_module
from MagentaBench.collab.cli import main as collab_main
from MagentaBench.collab.ledger import build_experiment_ledger, render_csv
from MagentaBench.collab.repository import CollaborationError

_RECORD_ADAPTER = TypeAdapter(HistoricalRecord)
ROOT = Path(__file__).parents[1]


def _source(
    source_id: str = "sample-source",
    *,
    repository: str = "Minions-Land/SampleBench",
    commit_digit: str = "1",
) -> dict[str, object]:
    return {
        "commit_sha": commit_digit * 40,
        "format": "magentabench-historical-source-v1",
        "license_id": "Apache-2.0",
        "license_status": "declared",
        "normalizer_id": "sample-normalizer.v1",
        "normalizer_sha256": "3" * 64,
        "ref_hint": "main",
        "repository": repository,
        "root_tree": {"algorithm": "sha1", "digest": "2" * 40},
        "source_id": source_id,
        "visibility": "public",
    }


def _publication_approval() -> dict[str, object]:
    return {
        "approval_id": "github-issue-85",
        "approved_at": "2026-08-16",
        "approved_by": "PoorOtterBob",
        "decision_ref": "Minions-Land/MagentaBenchmark#85",
        "decision_sha256": "a" * 64,
        "destination_repository": "Minions-Land/MagentaBenchmark",
        "scope": "typed-results-only",
    }


def _experiment() -> dict[str, object]:
    return {
        "benchmark": {"id": "sample-bench", "name": "Sample Bench", "version": "1"},
        "comparability": {
            "case_set_sha256": "a" * 64,
            "comparison_group": "sample-bench-v1",
            "evaluator_sha256": "b" * 64,
            "protocol_sha256": "9" * 64,
            "status": "exact",
        },
        "dataset": {
            "commit_sha": "4" * 40,
            "content_sha256": "5" * 64,
            "id": "sample-dataset",
            "name": "Sample Dataset",
            "split": "test",
            "version": "1",
        },
        "evaluator": {
            "id": "sample-evaluator",
            "independent": True,
            "kind": "deterministic",
            "name": "Sample Evaluator",
            "version": "1",
        },
        "execution": {
            "backend_id": "sample-docker",
            "budget": {
                "max_cases": 10,
                "max_cost_usd": 2.0,
                "max_tokens": 1000,
                "max_wall_seconds": 120.0,
            },
            "case_count": 10,
            "configuration_id": "sample-config",
            "configuration_profiles": ["default"],
            "configuration_sha256": "8" * 64,
            "factors": [{"id": "effort", "unit": None, "value": "high"}],
            "hardware": {
                "accelerator": None,
                "accelerator_count": 0,
                "architecture": "x86_64",
                "cpu_count": 4,
                "memory_bytes": 4096,
            },
            "image_sha256": "7" * 64,
            "isolation": "container",
            "mode": "docker",
            "network_policy": "disabled",
            "order_policy": "fixed",
            "repetitions_per_case": 1,
            "seeds": [0],
        },
        "experiment_id": "sample-experiment",
        "harness": {
            "configuration_sha256": "6" * 64,
            "id": "sample-harness",
            "name": "Sample Harness",
            "protocol_id": "sample-protocol.v1",
            "version": "1",
        },
        "limitations": [],
        "method": {
            "id": "sample-method",
            "name": "Sample Method",
            "subject_id": "sample-subject",
            "version": "1",
        },
        "model": {
            "id": "sample-model",
            "name": "Sample Model",
            "revision": "r1",
            "version": "1",
        },
        "provider": {
            "id": "sample-provider",
            "name": "Sample Provider",
            "region": "global",
            "version": "1",
        },
        "purpose": "benchmark",
    }


def _snapshot_digest(source: dict[str, object]) -> str:
    model = HistoricalSource.model_validate_json(json.dumps(source), strict=True)
    return source_snapshot_identity(model)


def _provenance(
    role: str = "declaration",
    *,
    path: str = "evidence/declaration.json",
    content_digit: str = "d",
    size_bytes: int = 123,
) -> dict[str, object]:
    return {
        "content_sha256": content_digit * 64,
        "git_blob_oid": {"algorithm": "sha1", "digest": "c" * 40},
        "path": path,
        "role": role,
        "size_bytes": size_bytes,
    }


def _declaration(
    *,
    source_id: str = "sample-source",
    source_snapshot_sha256: str | None = None,
    logical_key: str = "sample-experiment",
    supersedes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "claim_eligible": False,
        "evidence_tier": "declaration-only",
        "experiment": _experiment(),
        "format": "magentabench-historical-record-v1",
        "kind": "declaration",
        "logical_key": logical_key,
        "metric_ids": ["accuracy"],
        "provenance": [_provenance()],
        "source_id": source_id,
        "source_snapshot_sha256": (
            _snapshot_digest(_source(source_id))
            if source_snapshot_sha256 is None
            else source_snapshot_sha256
        ),
        "supersedes": [] if supersedes is None else supersedes,
    }


def _run(
    *,
    source_id: str = "sample-source",
    source_snapshot_sha256: str | None = None,
    logical_key: str = "sample-run",
    supersedes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "claim_eligible": False,
        "evidence_tier": "legacy-evaluated",
        "experiment": _experiment(),
        "format": "magentabench-historical-record-v1",
        "kind": "run",
        "logical_key": logical_key,
        "metrics": [
            {
                "aggregation": "mean",
                "definition_sha256": "e" * 64,
                "denominator": {
                    "excluded_count": 0,
                    "observed_count": 44,
                    "planned_count": 50,
                    "unit": "cases",
                },
                "direction": "higher-is-better",
                "invalid_count": 0,
                "metric_id": "accuracy",
                "missing_count": 6,
                "state": "observed",
                "uncertainty": {
                    "confidence_level": 0.95,
                    "lower": 0.2,
                    "method": "confidence-interval",
                    "sample_size": 50,
                    "upper": 0.5,
                    "value": None,
                },
                "unit": "fraction",
                "value": 0.34,
                "zero_filled_count": 0,
            }
        ],
        "parent_run_id": None,
        "provenance": [
            _provenance(
                "result",
                path="results/sample-result.json",
                content_digit="e",
            )
        ],
        "run_id": "sample-run-001",
        "source_id": source_id,
        "source_snapshot_sha256": (
            _snapshot_digest(_source(source_id))
            if source_snapshot_sha256 is None
            else source_snapshot_sha256
        ),
        "supersedes": [] if supersedes is None else supersedes,
        "terminal_state": "completed",
    }


def _asset(
    *,
    source_id: str = "sample-source",
    source_snapshot_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "asset": {
            "asset_id": "sample-report",
            "content_sha256": "f" * 64,
            "materialization_state": "metadata-only",
            "media_type": "application/json",
            "role": "report",
            "size_bytes": 321,
            "status": "available",
        },
        "claim_eligible": False,
        "evidence_tier": "candidate",
        "experiment_id": "sample-experiment",
        "format": "magentabench-historical-record-v1",
        "kind": "asset",
        "logical_key": "sample-report",
        "provenance": [
            _provenance(
                "asset",
                path="results/sample-report.json",
                content_digit="f",
                size_bytes=321,
            )
        ],
        "run_id": "sample-run-001",
        "source_id": source_id,
        "source_snapshot_sha256": (
            _snapshot_digest(_source(source_id))
            if source_snapshot_sha256 is None
            else source_snapshot_sha256
        ),
        "supersedes": [],
    }


def _bind_record(payload: dict[str, object]) -> dict[str, object]:
    bound = deepcopy(payload)
    bound["record_id"] = compute_record_id(bound)
    return bound


def _record_model(payload: dict[str, object]):
    return _RECORD_ADAPTER.validate_json(
        json.dumps(_bind_record(payload), allow_nan=False), strict=True
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _install_source(
    project: Path,
    source_id: str = "sample-source",
    *,
    source: dict[str, object] | None = None,
) -> Path:
    path = project / "imports" / source_id / "source.json"
    _write_json(path, _source(source_id) if source is None else source)
    return path


def _install_record(
    project: Path,
    payload: dict[str, object],
    *,
    filename: str | None = None,
) -> tuple[Path, dict[str, object]]:
    bound = _bind_record(payload)
    source_id = str(bound["source_id"])
    name = filename or f"{bound['record_id']}.json"
    path = project / "imports" / source_id / "records" / name
    _write_json(path, bound)
    return path, bound


def _error_codes(project: Path) -> set[str]:
    return {item.code for item in validate_historical_imports(project).errors}


def test_missing_import_directory_is_a_valid_empty_snapshot(tmp_path: Path) -> None:
    report = validate_historical_imports(tmp_path)

    assert report.ok
    assert report.snapshot.sources == ()
    assert report.snapshot.records == ()
    assert report.as_dict()["format"] == "magentabench-historical-import-validation-v1"


def test_checked_in_import_readme_is_not_treated_as_a_source() -> None:
    report = validate_historical_imports(ROOT)

    assert report.ok, report.errors
    assert all(item.source.source_id != "README.md" for item in report.snapshot.sources)
    assert all("/README.md/" not in item.path for item in report.snapshot.records)


def test_valid_declaration_run_and_asset_load_deterministically(tmp_path: Path) -> None:
    _install_source(tmp_path)
    for payload in (_run(), _declaration(), _asset()):
        _install_record(tmp_path, payload)

    first = load_historical_imports(tmp_path)
    second = load_historical_imports(tmp_path)

    assert first == second
    assert [item.record.kind for item in first.records] == [
        "asset",
        "declaration",
        "run",
    ]
    assert len(first.records_of_kind("run")) == 1
    run = first.records_of_kind("run")[0].record
    assert run.claim_eligible is False
    assert run.evidence_tier == "legacy-evaluated"
    assert run.metrics[0].value == 0.34
    assert run.metrics[0].denominator.planned_count == 50
    assert run.metrics[0].missing_count == 6
    assert run.metrics[0].uncertainty.confidence_level == 0.95
    assert not hasattr(first, "latest")
    with pytest.raises(ValidationError, match="frozen"):
        first.sources[0].source.source_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.sources[0].path = "changed"  # type: ignore[misc]


def test_source_requires_coherent_license_status() -> None:
    declared = _source()

    model = HistoricalSource.model_validate_json(json.dumps(declared), strict=True)
    assert model.license_id == "Apache-2.0"

    missing = _source()
    missing["license_id"] = None
    with pytest.raises(ValidationError, match="license_id is required"):
        HistoricalSource.model_validate_json(json.dumps(missing), strict=True)

    inconsistent = _source()
    inconsistent["license_status"] = "unknown"
    with pytest.raises(ValidationError, match="license_id is required"):
        HistoricalSource.model_validate_json(json.dumps(inconsistent), strict=True)


def test_publication_approval_is_narrow_content_addressed_and_private_only() -> None:
    private = _source()
    private["visibility"] = "private"
    private["license_status"] = "not-detected"
    private["license_id"] = None
    private["publication_approval"] = _publication_approval()

    model = HistoricalSource.model_validate_json(json.dumps(private), strict=True)
    assert model.publication_approval is not None
    assert model.publication_approval.scope == "typed-results-only"

    public = _source()
    public["publication_approval"] = _publication_approval()
    with pytest.raises(ValidationError, match="only for a private source"):
        HistoricalSource.model_validate_json(json.dumps(public), strict=True)

    bad_digest = deepcopy(private)
    bad_digest["publication_approval"]["decision_sha256"] = "not-a-digest"  # type: ignore[index]
    with pytest.raises(ValidationError, match="decision_sha256"):
        HistoricalSource.model_validate_json(json.dumps(bad_digest), strict=True)


def test_ledger_projects_legacy_conditions_metrics_and_assets_without_changing_bmp_tables(
    tmp_path: Path,
) -> None:
    _install_source(tmp_path)
    _, declaration = _install_record(tmp_path, _declaration())
    _, run = _install_record(tmp_path, _run())
    candidate = _run(logical_key="candidate-run")
    candidate["evidence_tier"] = "candidate"
    candidate["metrics"] = []
    candidate["run_id"] = "candidate-run-001"
    _install_record(tmp_path, candidate)
    asset = _asset()
    second_ref = deepcopy(asset["provenance"][0])  # type: ignore[index]
    second_ref["path"] = "results/sample-report-copy.json"
    second_ref["git_blob_oid"] = {"algorithm": "sha1", "digest": "b" * 40}
    asset["provenance"].append(second_ref)  # type: ignore[union-attr]
    _, asset_record = _install_record(tmp_path, asset)

    baseline = build_experiment_ledger(ROOT)
    ledger = build_experiment_ledger(ROOT, imports_dir=tmp_path / "imports")

    assert baseline.ok, baseline.errors
    assert ledger.ok, ledger.errors
    assert ledger.experiments == baseline.experiments
    assert ledger.runs == baseline.runs
    assert ledger.metrics == baseline.metrics

    source = next(
        item for item in ledger.sources if item["record_origin"] == "legacy-import"
    )
    assert source["record_count"] == 4
    assert source["license_status"] == "declared"
    assert source["tree_oid"] == {"algorithm": "sha1", "digest": "2" * 40}
    assert str(tmp_path) not in source["snapshot_path"]

    legacy_catalog = [
        item for item in ledger.catalog if item["record_origin"] == "legacy-import"
    ]
    assert len(legacy_catalog) == 3
    projected_run = next(
        item for item in legacy_catalog if item["record_id"] == run["record_id"]
    )
    assert projected_run["terminal_state"] == "completed"
    assert projected_run["claim_eligible"] is False
    assert (
        projected_run["conditions"]["format"] == "magentabench-catalog-condition-set-v1"
    )
    run_conditions = projected_run["conditions"]["variants"][0]["conditions"]
    assert run_conditions["execution"]["budget"] == {
        "max_cases": 10,
        "max_cost_usd": 2.0,
        "max_tokens": 1000,
        "max_wall_seconds": 120.0,
    }
    assert run_conditions["execution"]["image_sha256"] == "7" * 64
    assert projected_run["image_digest"] == "7" * 64
    assert projected_run["budget"]["max_cost_usd"] == 2.0
    projected_declaration = next(
        item for item in legacy_catalog if item["record_id"] == declaration["record_id"]
    )
    assert projected_declaration["terminal_state"] is None

    legacy_observations = [
        item for item in ledger.observations if item["record_origin"] == "legacy-import"
    ]
    assert len(legacy_observations) == 1
    observation = legacy_observations[0]
    assert observation["record_id"] == run["record_id"]
    assert observation["metric_id"] == "accuracy"
    assert observation["value"] == 0.34
    assert observation["denominator"] == {
        "excluded_count": 0,
        "observed_count": 44,
        "planned_count": 50,
        "unit": "cases",
    }
    assert observation["planned_rollout_count"] is None
    assert observation["image_digest"] == "7" * 64
    assert observation["budget"]["max_tokens"] == 1000
    assert observation["uncertainty"]["confidence_level"] == 0.95
    assert observation["claim_eligible"] is False
    assert observation["provenance_refs"][0]["path"] == "results/sample-result.json"

    legacy_assets = [
        item for item in ledger.assets if item["record_origin"] == "legacy-import"
    ]
    assert len(legacy_assets) == 2
    assert {item["source_asset_id"] for item in legacy_assets} == {"sample-report"}
    assert {item["record_id"] for item in legacy_assets} == {asset_record["record_id"]}
    assert len({item["asset_id"] for item in legacy_assets}) == 2
    json.dumps(ledger.as_dict(), allow_nan=False, sort_keys=True)
    for table in ("sources", "catalog", "observations", "assets"):
        assert render_csv(ledger, table).splitlines()


def test_invalid_imports_fail_closed_without_partial_legacy_projection(
    tmp_path: Path,
) -> None:
    _install_source(tmp_path)
    _install_record(tmp_path, _run())
    invalid = tmp_path / "imports/sample-source/records/not-a-record-id.json"
    invalid.write_text("{}\n", encoding="utf-8")

    ledger = build_experiment_ledger(ROOT, imports_dir=tmp_path / "imports")

    assert not ledger.ok
    assert ledger.experiments
    assert all(item["record_origin"] == "bmp" for item in ledger.sources)
    assert all(item["record_origin"] == "bmp" for item in ledger.catalog)
    assert all(item["record_origin"] == "bmp" for item in ledger.observations)
    assert all(item["record_origin"] == "bmp" for item in ledger.assets)
    assert any(
        item["code"] == "historical-import-record-layout" for item in ledger.errors
    )


def test_validate_imports_cli_supports_an_external_companion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_source(tmp_path)
    _install_record(tmp_path, _declaration())

    return_code = collab_main(
        (
            "--project-root",
            str(ROOT),
            "validate-imports",
            "--imports-dir",
            str(tmp_path / "imports"),
            "--format",
            "json",
        )
    )

    captured = capsys.readouterr()
    assert return_code == 0
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["source_count"] == 1
    assert payload["record_count"] == 1
    assert str(tmp_path) not in captured.out
    assert captured.err == ""


def test_source_snapshot_identity_normalizes_github_name_and_url() -> None:
    name = HistoricalSource.model_validate_json(
        json.dumps(_source(repository="Minions-Land/SampleBench")), strict=True
    )
    url = HistoricalSource.model_validate_json(
        json.dumps(_source(repository="https://github.com/minions-land/samplebench")),
        strict=True,
    )

    assert canonical_repository_name(name.repository) == "minions-land/samplebench"
    assert source_snapshot_identity(name) == source_snapshot_identity(url)
    assert canonical_json_bytes(name) == canonical_json_bytes(name)


@pytest.mark.parametrize(
    "repository",
    (
        "http://github.com/Minions-Land/SampleBench",
        "https://user:password@github.com/Minions-Land/SampleBench",
        "https://github.com/Minions-Land/SampleBench?token=value",
        "https://example.com/Minions-Land/SampleBench",
        "git@github.com:Minions-Land/SampleBench.git",
        "Minions-Land/SampleBench.git",
        "https://github.com/Minions-Land/SampleBench/",
    ),
)
def test_source_rejects_noncanonical_or_authenticated_repositories(
    repository: str,
) -> None:
    with pytest.raises(ValidationError):
        HistoricalSource.model_validate_json(
            json.dumps(_source(repository=repository)), strict=True
        )


def test_duplicate_snapshot_identity_fails_even_with_different_source_ids(
    tmp_path: Path,
) -> None:
    _install_source(tmp_path, "source-one", source=_source("source-one"))
    duplicate = _source(
        "source-two",
        repository="https://github.com/minions-land/samplebench",
    )
    _install_source(tmp_path, "source-two", source=duplicate)

    report = validate_historical_imports(tmp_path)

    assert "duplicate-snapshot-identity" in {item.code for item in report.errors}
    with pytest.raises(HistoricalImportError) as raised:
        load_historical_imports(tmp_path)
    assert isinstance(raised.value, CollaborationError)
    assert all("/mnt/" not in item.message for item in raised.value.errors)


def test_source_and_record_must_match_their_layout_identity(tmp_path: Path) -> None:
    _install_source(tmp_path, "directory-id", source=_source("different-id"))
    assert "source-id-mismatch" in _error_codes(tmp_path)

    project = tmp_path / "record-project"
    _install_source(project)
    mismatched = _bind_record(_declaration(source_id="other-source"))
    _write_json(
        project
        / "imports"
        / "sample-source"
        / "records"
        / f"{mismatched['record_id']}.json",
        mismatched,
    )
    assert "record-source-mismatch" in _error_codes(project)


def test_record_filename_must_equal_canonical_record_identity(tmp_path: Path) -> None:
    _install_source(tmp_path)
    _, bound = _install_record(tmp_path, _declaration(), filename=f"{'0' * 64}.json")

    assert bound["record_id"] != "0" * 64
    assert "record-filename-mismatch" in _error_codes(tmp_path)


def test_record_content_drift_breaks_its_embedded_identity(tmp_path: Path) -> None:
    _install_source(tmp_path)
    path, bound = _install_record(tmp_path, _declaration())
    bound["metric_ids"] = ["different-metric"]
    _write_json(path, bound)

    report = validate_historical_imports(tmp_path)

    assert "record-json" in {item.code for item in report.errors}
    assert any("record_id differs" in item.message for item in report.errors)


def test_record_binds_the_exact_source_snapshot_identity(tmp_path: Path) -> None:
    _install_source(tmp_path)
    _install_record(tmp_path, _declaration())
    changed = _source(commit_digit="4")
    _install_source(tmp_path, source=changed)

    assert "record-snapshot-mismatch" in _error_codes(tmp_path)


def test_record_id_excludes_only_record_id() -> None:
    payload = _declaration()
    first = _bind_record(payload)
    second = deepcopy(first)
    second["record_id"] = "0" * 64
    changed = deepcopy(first)
    changed["logical_key"] = "different-logical-key"

    assert compute_record_id(first) == compute_record_id(second)
    assert compute_record_id(first) != compute_record_id(changed)
    assert logical_key_digest("run", "key") != logical_key_digest("asset", "key")


def test_duplicate_json_keys_are_rejected_before_model_validation(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "imports" / "sample-source"
    source_dir.mkdir(parents=True)
    source_dir.joinpath("source.json").write_text(
        '{"source_id":"sample-source","source_id":"shadow"}\n', encoding="utf-8"
    )

    report = validate_historical_imports(tmp_path)

    assert report.errors[0].code == "source-json"
    assert "duplicate JSON key" in report.errors[0].message


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("token", "not-even-a-real-token"),
        ("metadata", {"anything": "value"}),
        ("command", ["sh", "-c", "unsafe"]),
        ("notes", "password=not-a-secret-placeholder"),
    ),
)
def test_loader_rejects_credentials_commands_raw_metadata_and_secret_patterns(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    document = _source()
    document[field] = value
    _install_source(tmp_path, source=document)

    report = validate_historical_imports(tmp_path)

    assert not report.ok
    assert report.errors[0].code == "source-json"
    assert "not-a-secret-placeholder" not in report.errors[0].message


@pytest.mark.parametrize(
    "path",
    ("/etc/passwd", "../escape.json", "evidence//result.json", "C:\\host\\file.json"),
)
def test_provenance_rejects_absolute_host_and_non_normalized_paths(path: str) -> None:
    payload = _declaration()
    payload["provenance"][0]["path"] = path  # type: ignore[index]

    with pytest.raises(ValidationError):
        _record_model(payload)


def test_symlinks_and_unknown_layout_entries_fail_closed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    _write_json(outside, _source())
    source_dir = tmp_path / "imports" / "sample-source"
    source_dir.mkdir(parents=True)
    source_dir.joinpath("source.json").symlink_to(outside)
    source_dir.joinpath("index.json").write_text("{}\n", encoding="utf-8")

    codes = _error_codes(tmp_path)

    assert "source-json" in codes
    assert "source-layout" in codes


def test_symlinked_import_root_is_not_treated_as_missing(tmp_path: Path) -> None:
    actual = tmp_path / "actual-imports"
    actual.mkdir()
    (tmp_path / "imports").symlink_to(actual, target_is_directory=True)

    assert _error_codes(tmp_path) == {"imports-root"}


@pytest.mark.parametrize("bad_value", (True, float("nan"), float("inf"), -float("inf")))
def test_metric_rejects_boolean_and_nonfinite_values(bad_value: object) -> None:
    bound = _bind_record(_run())
    bound["metrics"][0]["value"] = bad_value  # type: ignore[index]

    with pytest.raises(ValidationError):
        _RECORD_ADAPTER.validate_json(json.dumps(bound, allow_nan=True), strict=True)


@pytest.mark.parametrize(
    "mutator",
    (
        lambda payload: payload["experiment"]["execution"].__setitem__(
            "case_count", True
        ),
        lambda payload: payload["experiment"]["execution"]["budget"].__setitem__(
            "max_tokens", True
        ),
        lambda payload: payload["metrics"][0]["denominator"].__setitem__(
            "planned_count", True
        ),
        lambda payload: payload["provenance"][0].__setitem__("size_bytes", True),
    ),
)
def test_boolean_is_never_coerced_into_a_numeric_field(mutator) -> None:
    payload = _run()
    mutator(payload)

    with pytest.raises(ValidationError):
        _record_model(payload)


def test_loader_rejects_nonfinite_json_constants_explicitly(tmp_path: Path) -> None:
    _install_source(tmp_path)
    bound = _bind_record(_run())
    bound["metrics"][0]["value"] = float("nan")  # type: ignore[index]
    path = (
        tmp_path
        / "imports"
        / "sample-source"
        / "records"
        / f"{bound['record_id']}.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bound, allow_nan=True), encoding="utf-8")

    report = validate_historical_imports(tmp_path)

    assert any("non-finite JSON number" in item.message for item in report.errors)


@pytest.mark.parametrize(
    ("field", "value"),
    (("claim_eligible", True), ("evidence_tier", "bmp-standalone")),
)
def test_legacy_records_cannot_claim_bmp_evidence(field: str, value: object) -> None:
    payload = _run()
    payload[field] = value

    with pytest.raises(ValidationError):
        _record_model(payload)


def test_candidate_run_and_declaration_cannot_emit_metrics() -> None:
    candidate = _run()
    candidate["evidence_tier"] = "candidate"
    with pytest.raises(ValidationError, match="candidate runs cannot emit metrics"):
        _record_model(candidate)

    declaration = _declaration()
    declaration["metrics"] = _run()["metrics"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _record_model(declaration)


def test_asset_requires_matching_content_addressed_provenance() -> None:
    payload = _asset()
    payload["asset"]["size_bytes"] = 999  # type: ignore[index]

    with pytest.raises(ValidationError, match="matching content-addressed"):
        _record_model(payload)


def test_metric_preserves_terminal_state_counts_and_uncertainty() -> None:
    model = _record_model(_run())

    assert model.terminal_state == "completed"
    metric = model.metrics[0]
    assert metric.state == "observed"
    assert metric.unit == "fraction"
    assert metric.direction == "higher-is-better"
    assert metric.aggregation == "mean"
    assert metric.denominator.observed_count == 44
    assert metric.missing_count == 6
    assert metric.invalid_count == 0
    assert metric.zero_filled_count == 0
    assert metric.uncertainty.lower == 0.2
    assert metric.uncertainty.upper == 0.5


def test_metric_counts_cannot_exceed_denominator() -> None:
    payload = _run()
    payload["metrics"][0]["invalid_count"] = 7  # type: ignore[index]

    with pytest.raises(ValidationError, match="counts exceed"):
        _record_model(payload)


def test_parallel_records_with_same_logical_key_require_explicit_supersession(
    tmp_path: Path,
) -> None:
    _install_source(tmp_path)
    _install_record(tmp_path, _declaration())
    conflict = _declaration()
    conflict["metric_ids"] = ["accuracy", "cost"]
    _install_record(tmp_path, conflict)

    assert "logical-conflict" in _error_codes(tmp_path)


def test_explicit_supersession_chain_is_valid_and_not_collapsed(tmp_path: Path) -> None:
    _install_source(tmp_path)
    _, old = _install_record(tmp_path, _declaration())
    replacement = _declaration(supersedes=[str(old["record_id"])])
    replacement["metric_ids"] = ["accuracy", "cost"]
    _install_record(tmp_path, replacement)

    snapshot = load_historical_imports(tmp_path)

    assert len(snapshot.records) == 2
    assert {item.record.record_id for item in snapshot.records} == {
        old["record_id"],
        compute_record_id(replacement),
    }


def test_supersedes_rejects_missing_and_incompatible_targets(tmp_path: Path) -> None:
    _install_source(tmp_path)
    _, old = _install_record(tmp_path, _declaration(logical_key="old-key"))
    _install_record(
        tmp_path,
        _declaration(logical_key="new-key", supersedes=[str(old["record_id"])]),
    )
    _install_record(
        tmp_path,
        _run(logical_key="dangling", supersedes=["0" * 64]),
    )

    codes = _error_codes(tmp_path)

    assert "supersedes-incompatible" in codes
    assert "supersedes-missing" in codes


def test_cycle_detection_is_defensive_even_for_content_addressed_nodes() -> None:
    base = _record_model(_declaration())
    first = base.model_copy(update={"record_id": "1" * 64, "supersedes": ("2" * 64,)})
    second = base.model_copy(update={"record_id": "2" * 64, "supersedes": ("1" * 64,)})
    digest = logical_key_digest(first.kind, first.logical_key)
    loaded = [
        LoadedHistoricalRecord(first, "imports/source/records/1.json", digest),
        LoadedHistoricalRecord(second, "imports/source/records/2.json", digest),
    ]
    errors = []

    _validate_supersession_graph(loaded, errors)

    assert "supersession-cycle" in {item.code for item in errors}
    assert "logical-conflict" in {item.code for item in errors}


def test_loader_scans_every_source_without_a_hand_index(tmp_path: Path) -> None:
    for source_id, digit in (("z-source", "4"), ("a-source", "5")):
        source = _source(source_id, commit_digit=digit)
        _install_source(
            tmp_path,
            source_id,
            source=source,
        )
        asset = _asset(
            source_id=source_id,
            source_snapshot_sha256=_snapshot_digest(source),
        )
        asset["logical_key"] = f"{source_id}-report"
        asset["experiment_id"] = None
        asset["run_id"] = None
        _install_record(tmp_path, asset)

    snapshot = load_historical_imports(tmp_path)

    assert [item.source.source_id for item in snapshot.sources] == [
        "a-source",
        "z-source",
    ]
    assert [item.record.source_id for item in snapshot.records] == [
        "a-source",
        "z-source",
    ]


def test_extra_raw_fields_are_forbidden_by_models() -> None:
    source = _source()
    source["raw_metadata"] = {}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        HistoricalSource.model_validate_json(json.dumps(source), strict=True)

    payload = _declaration()
    payload["command"] = "python -c value"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _record_model(payload)


def test_exact_comparability_requires_all_content_bindings() -> None:
    payload = _declaration()
    payload["experiment"]["comparability"]["case_set_sha256"] = None  # type: ignore[index]

    with pytest.raises(ValidationError, match="exact comparability requires"):
        _record_model(payload)


def test_provenance_accepts_typed_sha256_git_blob_oid() -> None:
    payload = _declaration()
    payload["provenance"][0]["git_blob_oid"] = {  # type: ignore[index]
        "algorithm": "sha256",
        "digest": "a" * 64,
    }

    model = _record_model(payload)

    assert model.provenance[0].git_blob_oid.algorithm == "sha256"


def test_source_root_tree_must_be_sha1() -> None:
    source = _source()
    source["root_tree"] = {"algorithm": "sha256", "digest": "a" * 64}

    with pytest.raises(ValidationError, match="root_tree must use"):
        HistoricalSource.model_validate_json(json.dumps(source), strict=True)


def test_secret_like_json_keys_and_paths_never_enter_diagnostics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "sk-" + "A" * 24
    source_dir = tmp_path / "imports" / "sample-source"
    source_dir.mkdir(parents=True)
    source_dir.joinpath("source.json").write_text(
        f'{{"{marker}":1,"{marker}":2}}\n', encoding="utf-8"
    )
    leaked_path = tmp_path / "imports" / marker
    leaked_path.mkdir()

    report = validate_historical_imports(tmp_path)

    assert not report.ok
    assert marker not in json.dumps(report.as_dict(), sort_keys=True)
    assert any("duplicate JSON key" in item.message for item in report.errors)
    assert any("<redacted-field>" in (item.path or "") for item in report.errors)
    with pytest.raises(HistoricalImportError) as raised:
        load_historical_imports(tmp_path)
    assert marker not in str(raised.value)

    return_code = collab_main(
        (
            "--project-root",
            str(tmp_path),
            "validate-imports",
            "--format",
            "json",
        )
    )
    captured = capsys.readouterr()
    assert return_code == 1
    assert marker not in captured.out
    assert marker not in captured.err


def test_secret_like_extra_key_is_redacted_before_model_validation(
    tmp_path: Path,
) -> None:
    marker = "ghp_" + "B" * 24
    source = _source()
    source[marker] = "ordinary-value"
    _install_source(tmp_path, source=source)

    report = validate_historical_imports(tmp_path)

    assert not report.ok
    serialized = json.dumps(report.as_dict(), sort_keys=True)
    assert marker not in serialized
    assert "<redacted-field>" in serialized


@pytest.mark.parametrize(
    "secret_key",
    ("OPENAI_API_KEY", "client_secret", "db_password"),
)
def test_common_secret_key_names_are_redacted_from_diagnostics(
    tmp_path: Path,
    secret_key: str,
) -> None:
    source = _source()
    source[secret_key] = "ordinary-value"
    _install_source(tmp_path, source=source)
    (tmp_path / "imports" / secret_key).mkdir()

    report = validate_historical_imports(tmp_path)

    serialized = json.dumps(report.as_dict(), sort_keys=True)
    assert not report.ok
    assert secret_key not in serialized
    assert "<redacted-field>" in serialized


def test_logical_key_is_not_treated_as_secret_material(tmp_path: Path) -> None:
    _install_source(tmp_path)
    _install_record(tmp_path, _run())

    report = validate_historical_imports(tmp_path)

    assert report.ok, report.as_dict()


def test_explicit_missing_import_root_fails_without_exposing_host_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "private" / "missing-imports"

    report = validate_historical_imports(ROOT, imports_dir=missing)

    assert not report.ok
    assert {item.code for item in report.errors} == {"imports-root"}
    assert report.errors[0].path == "<external-imports>"
    assert str(tmp_path) not in json.dumps(report.as_dict(), sort_keys=True)

    return_code = collab_main(
        (
            "--project-root",
            str(ROOT),
            "validate-imports",
            "--imports-dir",
            str(missing),
            "--format",
            "json",
        )
    )
    captured = capsys.readouterr()
    assert return_code == 1
    assert str(tmp_path) not in captured.out


def test_external_import_paths_are_independent_of_relative_spelling(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    companion_parent = tmp_path / "private"
    _install_source(companion_parent)
    _install_record(companion_parent, _declaration())
    absolute = companion_parent / "imports"

    absolute_report = validate_historical_imports(project, imports_dir=absolute)
    relative_report = validate_historical_imports(
        project, imports_dir=Path("../private/imports")
    )

    assert absolute_report.ok, absolute_report.errors
    assert relative_report.ok, relative_report.errors
    assert absolute_report.snapshot == relative_report.snapshot
    assert absolute_report.snapshot.sources[0].path == (
        "<external-imports>/sample-source/source.json"
    )
    assert absolute_report.snapshot.records[0].path.startswith(
        "<external-imports>/sample-source/records/"
    )


def test_checked_in_private_or_unlicensed_source_requires_publication_approval(
    tmp_path: Path,
) -> None:
    source = _source()
    source["visibility"] = "private"
    source["license_status"] = "not-detected"
    source["license_id"] = None
    _install_source(tmp_path, source=source)

    checked_in = validate_historical_imports(tmp_path)
    external = validate_historical_imports(ROOT, imports_dir=tmp_path / "imports")

    assert "publication-approval" in {item.code for item in checked_in.errors}
    assert external.ok, external.errors


def test_checked_in_approved_private_typed_results_projection_is_accepted(
    tmp_path: Path,
) -> None:
    source = _source()
    source["visibility"] = "private"
    source["license_status"] = "not-detected"
    source["license_id"] = None
    source["publication_approval"] = _publication_approval()
    snapshot_digest = _snapshot_digest(source)
    _install_source(tmp_path, source=source)
    _install_record(
        tmp_path,
        _run(source_snapshot_sha256=snapshot_digest),
    )

    report = validate_historical_imports(tmp_path)

    assert report.ok, report.errors
    assert len(report.snapshot.records_of_kind("run")) == 1


def test_private_projection_approval_is_destination_bound(tmp_path: Path) -> None:
    source = _source()
    source["visibility"] = "private"
    source["license_status"] = "not-detected"
    source["license_id"] = None
    approval = _publication_approval()
    approval["destination_repository"] = "Minions-Land/AnotherRepository"
    source["publication_approval"] = approval
    _install_source(tmp_path, source=source)

    report = validate_historical_imports(tmp_path)

    assert "publication-approval" in {item.code for item in report.errors}


def test_private_typed_results_projection_rejects_asset_records(
    tmp_path: Path,
) -> None:
    source = _source()
    source["visibility"] = "private"
    source["license_status"] = "not-detected"
    source["license_id"] = None
    source["publication_approval"] = _publication_approval()
    snapshot_digest = _snapshot_digest(source)
    _install_source(tmp_path, source=source)
    _install_record(
        tmp_path,
        _run(source_snapshot_sha256=snapshot_digest),
    )
    _install_record(
        tmp_path,
        _asset(source_snapshot_sha256=snapshot_digest),
    )

    checked_in = validate_historical_imports(tmp_path)
    external = validate_historical_imports(ROOT, imports_dir=tmp_path / "imports")

    assert "publication-scope" in {item.code for item in checked_in.errors}
    assert external.ok, external.errors


def test_checked_in_publication_policy_survives_intermediate_symlink_alias(
    tmp_path: Path,
) -> None:
    source = _source()
    source["visibility"] = "private"
    source["license_status"] = "not-detected"
    source["license_id"] = None
    _install_source(tmp_path, source=source)
    (tmp_path / "alias").symlink_to(".", target_is_directory=True)

    report = validate_historical_imports(
        tmp_path,
        imports_dir=tmp_path / "alias" / "imports",
    )

    assert "publication-approval" in {item.code for item in report.errors}


def test_intermediate_symlink_loop_fails_as_an_imports_root_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "loop").symlink_to("loop", target_is_directory=True)

    report = validate_historical_imports(
        tmp_path,
        imports_dir=tmp_path / "loop" / "imports",
    )

    assert {item.code for item in report.errors} == {"imports-root"}
    assert str(tmp_path) not in json.dumps(report.as_dict(), sort_keys=True)


def test_natural_run_identity_cannot_be_bypassed_with_logical_key(
    tmp_path: Path,
) -> None:
    _install_source(tmp_path)
    _install_record(tmp_path, _run(logical_key="first-logical-key"))
    changed = _run(logical_key="second-logical-key")
    changed["metrics"][0]["value"] = 0.5  # type: ignore[index]
    _install_record(tmp_path, changed)

    assert "natural-identity-conflict" in _error_codes(tmp_path)


def test_parent_run_and_asset_references_must_resolve_in_the_same_source(
    tmp_path: Path,
) -> None:
    _install_source(tmp_path)
    child = _run()
    child["parent_run_id"] = "missing-parent"
    _install_record(tmp_path, child)
    orphan = _asset()
    orphan["run_id"] = "missing-run"
    orphan["experiment_id"] = "missing-experiment"
    _install_record(tmp_path, orphan)

    codes = _error_codes(tmp_path)

    assert "parent-run-missing" in codes
    assert "asset-run-missing" in codes
    assert "asset-experiment-missing" in codes


def test_parent_run_reference_is_scoped_to_its_experiment(tmp_path: Path) -> None:
    _install_source(tmp_path)
    parent = _run(logical_key="parent-a")
    parent["experiment"]["experiment_id"] = "experiment-a"  # type: ignore[index]
    parent["run_id"] = "shared-parent"
    _install_record(tmp_path, parent)
    other = _run(logical_key="parent-b")
    other["experiment"]["experiment_id"] = "experiment-b"  # type: ignore[index]
    other["run_id"] = "shared-parent"
    _install_record(tmp_path, other)
    child = _run(logical_key="child-c")
    child["experiment"]["experiment_id"] = "experiment-c"  # type: ignore[index]
    child["run_id"] = "child"
    child["parent_run_id"] = "shared-parent"
    _install_record(tmp_path, child)

    assert "parent-run-missing" in _error_codes(tmp_path)


def test_asset_run_reference_without_experiment_must_be_unambiguous(
    tmp_path: Path,
) -> None:
    _install_source(tmp_path)
    for suffix in ("a", "b"):
        run = _run(logical_key=f"run-{suffix}")
        run["experiment"]["experiment_id"] = f"experiment-{suffix}"  # type: ignore[index]
        run["run_id"] = "shared-run"
        _install_record(tmp_path, run)
    asset = _asset()
    asset["experiment_id"] = None
    asset["run_id"] = "shared-run"
    _install_record(tmp_path, asset)

    assert "asset-run-ambiguous" in _error_codes(tmp_path)


def test_legacy_evaluated_run_requires_result_or_metric_provenance() -> None:
    run = _run()
    run["provenance"] = [_provenance("declaration")]

    with pytest.raises(ValidationError, match="result or metric provenance"):
        _record_model(run)


def test_metric_state_and_completed_run_counts_fail_closed() -> None:
    no_observations = _run()
    no_observations["metrics"][0]["denominator"]["observed_count"] = 0  # type: ignore[index]
    no_observations["metrics"][0]["missing_count"] = 50  # type: ignore[index]
    with pytest.raises(ValidationError, match="observed_count"):
        _record_model(no_observations)

    incomplete = _run()
    incomplete["metrics"][0]["missing_count"] = 5  # type: ignore[index]
    with pytest.raises(ValidationError, match="must equal"):
        _record_model(incomplete)

    incomplete["terminal_state"] = "partial"
    assert _record_model(incomplete).terminal_state == "partial"


def test_set_like_record_fields_are_canonical_before_identity() -> None:
    first = _declaration()
    first["metric_ids"] = ["cost", "accuracy"]
    first["experiment"]["limitations"] = ["source-gap", "identity-gap"]  # type: ignore[index]
    first["experiment"]["execution"]["configuration_profiles"] = [  # type: ignore[index]
        "secondary",
        "default",
    ]
    first["experiment"]["execution"]["seeds"] = [2, 0]  # type: ignore[index]
    first["experiment"]["execution"]["factors"] = [  # type: ignore[index]
        {"id": "temperature", "unit": None, "value": 0.0},
        {"id": "effort", "unit": None, "value": "high"},
    ]
    first["provenance"].append(  # type: ignore[union-attr]
        _provenance(path="evidence/second.json", content_digit="a")
    )
    second = deepcopy(first)
    second["metric_ids"].reverse()  # type: ignore[union-attr]
    second["experiment"]["limitations"].reverse()  # type: ignore[index,union-attr]
    second["experiment"]["execution"]["configuration_profiles"].reverse()  # type: ignore[index,union-attr]
    second["experiment"]["execution"]["seeds"].reverse()  # type: ignore[index,union-attr]
    second["experiment"]["execution"]["factors"].reverse()  # type: ignore[index,union-attr]
    second["provenance"].reverse()  # type: ignore[union-attr]

    assert compute_record_id(first) == compute_record_id(second)
    model = _record_model(second)
    assert model.metric_ids == ("accuracy", "cost")
    assert model.experiment.limitations == ("identity-gap", "source-gap")

    omitted_defaults = deepcopy(first)
    omitted_defaults.pop("claim_eligible")
    omitted_defaults.pop("format")
    omitted_defaults.pop("supersedes")
    assert compute_record_id(first) == compute_record_id(omitted_defaults)


def test_deep_supersession_chain_is_iterative() -> None:
    base = _record_model(_declaration())
    record_ids = [f"{index:064x}" for index in range(2000)]
    loaded = [
        LoadedHistoricalRecord(
            base.model_copy(
                update={
                    "record_id": record_id,
                    "supersedes": (
                        (record_ids[index + 1],) if index + 1 < len(record_ids) else ()
                    ),
                }
            ),
            f"imports/source/records/{record_id}.json",
            logical_key_digest(base.kind, base.logical_key),
        )
        for index, record_id in enumerate(record_ids)
    ]
    errors = []

    _validate_supersession_graph(loaded, errors)

    assert errors == []


def test_supersession_limits_fail_closed_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _record_model(_declaration())
    first_id = "1" * 64
    second_id = "2" * 64
    digest = logical_key_digest(base.kind, base.logical_key)
    loaded = [
        LoadedHistoricalRecord(
            base.model_copy(update={"record_id": first_id, "supersedes": (second_id,)}),
            "imports/source/records/1.json",
            digest,
        ),
        LoadedHistoricalRecord(
            base.model_copy(update={"record_id": second_id, "supersedes": ()}),
            "imports/source/records/2.json",
            digest,
        ),
    ]
    monkeypatch.setattr(imports_module, "_MAX_SUPERSESSION_RECORDS", 1)

    first_errors = []
    second_errors = []
    _validate_supersession_graph(loaded, first_errors)
    _validate_supersession_graph(list(reversed(loaded)), second_errors)

    assert first_errors == second_errors
    assert [item.code for item in first_errors] == ["supersession-limit"]
