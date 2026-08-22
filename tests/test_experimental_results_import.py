from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest

from MagentaBench.collab import build_experiment_ledger
from MagentaBench.collab.experimental_results import (
    ExperimentalResultsError,
    H20ExperimentalResultsSnapshot,
    load_snapshot,
)
from MagentaBench.collab.import_models import HistoricalRun, HistoricalUnitResult
from MagentaBench.collab.imports import load_historical_imports
from MagentaBench.collab.paper_table import project_paper_table
from scripts.historical_imports import h20_experimental_results_v1 as projector


ROOT = Path(__file__).parents[1]
IMPORT_ID = "h20-experimental-results-20260822"
IMPORT_ROOT = ROOT / "imports" / IMPORT_ID
SOURCE_PATH = "imports/h20-experimental-results-20260822/source_snapshot.json"
EXPECTED_COMMIT = "4dd8c0bd7786899434d1d01c625df6a9f5205ba1"
EXPECTED_TREE = "0bcf574c47f50a1cb27296b6202ec601449f9610"
EXPECTED_BLOB = "89dd5d9e1250a289c3e5547bac911e7a3cc7198e"
EXPECTED_SNAPSHOT_SHA256 = (
    "39cf16dbd2337768c6c0c5e6f02b8aa32ec677782375b59db302eed3a580bfa0"
)
EXPECTED_SNAPSHOT_SIZE = 703_813


@pytest.fixture(scope="module")
def imported_records():
    snapshot = load_historical_imports(ROOT)
    return tuple(
        item.record for item in snapshot.records if item.record.source_id == IMPORT_ID
    )


@pytest.fixture(scope="module")
def snapshot_bytes() -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "blob", EXPECTED_BLOB],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout


def test_git_history_retains_the_sanitized_source_snapshot(
    snapshot_bytes: bytes,
) -> None:
    source = json.loads((IMPORT_ROOT / "source.json").read_bytes())
    assert source["commit_sha"] == EXPECTED_COMMIT
    assert source["root_tree"] == {"algorithm": "sha1", "digest": EXPECTED_TREE}
    assert source["publication_approval"]["approved_by"] == "PoorOtterBob"
    assert source["publication_approval"]["decision_ref"] == (
        "Minions-Land/MagentaBenchmark#159"
    )
    assert not (ROOT / SOURCE_PATH).exists()

    tree = subprocess.run(
        ["git", "rev-parse", f"{EXPECTED_COMMIT}^{{tree}}"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    entry = subprocess.run(
        ["git", "ls-tree", EXPECTED_COMMIT, SOURCE_PATH],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    assert tree == EXPECTED_TREE
    assert f"blob {EXPECTED_BLOB}\t{SOURCE_PATH}\n" in entry
    assert len(snapshot_bytes) == EXPECTED_SNAPSHOT_SIZE
    assert hashlib.sha256(snapshot_bytes).hexdigest() == EXPECTED_SNAPSHOT_SHA256
    snapshot = H20ExperimentalResultsSnapshot.model_validate_json(
        snapshot_bytes, strict=True
    )
    assert snapshot.owner_count == 30
    assert snapshot.unit_count == 2_360


def test_import_has_exact_owner_unit_and_outcome_counts(imported_records) -> None:
    owners = [
        record for record in imported_records if isinstance(record, HistoricalRun)
    ]
    units = [
        record
        for record in imported_records
        if isinstance(record, HistoricalUnitResult)
    ]
    assert len(imported_records) == 2_390
    assert len(owners) == 30
    assert len(units) == 2_360
    assert sum(len(owner.metrics) for owner in owners) == 58
    assert Counter(unit.experiment.benchmark.id for unit in units) == {
        "biomnibench-da": 1_500,
        "cmtbench": 800,
        "swebench-verified": 60,
    }
    assert Counter(unit.result_status for unit in units) == {
        "success": 1_380,
        "verified_fail": 966,
        "invalid_output": 12,
        "no_output": 2,
    }
    assert all(record.claim_eligible is False for record in imported_records)
    assert len({unit.source_run_record_id for unit in units}) == 30
    assert (
        len(
            {
                unit.aggregate_run_record_id
                for unit in units
                if unit.aggregate_run_record_id is not None
            }
        )
        == 18
    )
    assert Counter(unit.aggregate_reconciliation_status for unit in units) == {
        "matched": 1_700,
        "not-compared": 600,
        None: 60,
    }


def test_unit_rows_preserve_denominator_and_negative_states(imported_records) -> None:
    units = [
        record
        for record in imported_records
        if isinstance(record, HistoricalUnitResult)
    ]
    for unit in units:
        metric = unit.metric
        assert metric.denominator.planned_count == 1
        assert metric.denominator.excluded_count == 0
        assert metric.aggregation == "none"
        assert metric.zero_filled_count == 0
        assert (
            metric.denominator.observed_count
            + metric.missing_count
            + metric.invalid_count
            == 1
        )
        if unit.result_status in {"success", "verified_fail"}:
            assert metric.state == "observed"
            assert metric.value is not None
        elif unit.result_status == "no_output":
            assert metric.state == "missing"
            assert metric.value is None
        else:
            assert metric.state == "invalid"
            assert metric.value is None
    swe = [
        unit for unit in units if unit.experiment.benchmark.id == "swebench-verified"
    ]
    assert {unit.code_commit for unit in swe} == {
        "174590db9b51b61ace9270dbf1f24d4364c6c640"
    }
    assert all(
        unit.code_commit is None
        for unit in units
        if unit.experiment.benchmark.id != "swebench-verified"
    )


def test_snapshot_fails_closed_on_identity_drift_and_aliases(
    tmp_path: Path, snapshot_bytes: bytes
) -> None:
    document = json.loads(snapshot_bytes)
    document["inventory"]["catalog_sha256"] = "0" * 64
    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ExperimentalResultsError, match="inventory identity"):
        load_snapshot(drifted)

    source = tmp_path / "source.json"
    source.write_bytes(snapshot_bytes)
    alias = tmp_path / "alias.json"
    alias.symlink_to(source)
    with pytest.raises(ExperimentalResultsError, match="cannot open"):
        load_snapshot(alias)


def test_projection_is_deterministic(
    tmp_path: Path, snapshot_bytes: bytes, imported_records
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_bytes(snapshot_bytes)
    snapshot = load_snapshot(snapshot_path)
    output = tmp_path / "import"
    summary = projector.project_snapshot(
        snapshot,
        snapshot_bytes=snapshot_bytes,
        output_root=output,
        project_root=ROOT,
        source_commit=EXPECTED_COMMIT,
        source_tree=EXPECTED_TREE,
        source_blob=EXPECTED_BLOB,
    )
    assert summary["record_count"] == 2_390
    assert summary["claim_eligible"] is False
    assert (output / "source.json").read_bytes() == (
        IMPORT_ROOT / "source.json"
    ).read_bytes()
    expected_ids = {record.record_id for record in imported_records}
    assert {path.stem for path in (output / "records").glob("*.json")} == expected_ids
    for record_id in expected_ids:
        assert (output / "records" / f"{record_id}.json").read_bytes() == (
            IMPORT_ROOT / "records" / f"{record_id}.json"
        ).read_bytes()


def test_import_tree_contains_no_private_runtime_material() -> None:
    forbidden_keys = {
        "answer",
        "argv",
        "command",
        "commands",
        "environment",
        "host",
        "metadata",
        "notes",
        "prompt",
        "raw",
        "raw_metadata",
        "record_root",
        "script",
        "shell",
        "stderr",
        "stdout",
        "trace",
        "gold",
    }
    private_value = re.compile(
        r"(?:^/|^~/|^[A-Za-z]:\\|(?:[0-9]{1,3}\.){3}[0-9]{1,3}|"
        r"https?://|ssh://|\bsk-[A-Za-z0-9_-]{20,}\b|github_pat_)"
    )

    def walk(value):
        if isinstance(value, dict):
            assert not (set(value) & forbidden_keys)
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, str):
            yield value

    for path in sorted(IMPORT_ROOT.rglob("*.json")):
        document = json.loads(path.read_bytes())
        assert all(private_value.search(value) is None for value in walk(document))


def test_ledger_and_paper_projection_reconcile_exactly() -> None:
    ledger = build_experiment_ledger(ROOT)
    assert ledger.ok, ledger.errors
    legacy_catalog = [
        row for row in ledger.catalog if row["record_origin"] == "legacy-import"
    ]
    legacy_observations = [
        row for row in ledger.observations if row["record_origin"] == "legacy-import"
    ]
    unit_observations = [
        row
        for row in legacy_observations
        if row.get("source_run_record_id") is not None
    ]
    assert len(legacy_catalog) == 60
    assert len(legacy_observations) == 2_837
    assert len(unit_observations) == 2_360
    assert Counter(row["benchmark_id"] for row in legacy_observations) == {
        "biomnibench-da": 1_593,
        "cmtbench": 1_160,
        "naturebench": 12,
        "swebench-verified": 72,
    }
    assert all(row["claim_eligible"] is False for row in legacy_observations)
    paper = project_paper_table(ledger)
    assert (
        len(
            [
                row
                for row in paper.rows
                if row["record_origin"] == "legacy-import"
                and row["result_granularity"] == "unit"
            ]
        )
        == 2_360
    )
