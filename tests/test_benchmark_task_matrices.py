"""Focused contract tests for the historical task-matrix projection."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.historical_imports.benchmark_task_matrices_v1 import project_report
from scripts.historical_imports.validate_benchmark_task_matrices import validate_report

ROOT = Path(__file__).parents[1]
REPORT_PATH = ROOT / "reports" / "benchmark_task_matrices.json"


@pytest.fixture()
def report() -> dict[str, Any]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _errors(value: dict[str, Any]) -> list[str]:
    return validate_report(value)


def test_checked_in_matrix_is_valid(report: dict[str, Any]) -> None:
    assert _errors(report) == []


def test_row_claim_eligibility_is_required_and_false(report: dict[str, Any]) -> None:
    rows = report["benchmarks"][0]["rows"]  # type: ignore[index]
    rows[0].pop("claim_eligible")
    assert any("missing false claim_eligible" in error for error in _errors(report))

    rows[0]["claim_eligible"] = True
    assert any("missing false claim_eligible" in error for error in _errors(report))


def test_cell_claim_eligibility_cannot_be_true(report: dict[str, Any]) -> None:
    cell = report["benchmarks"][1]["rows"][0]["methods"]["Biomni · default"]  # type: ignore[index]
    cell["claim_eligible"] = True
    assert any("missing false claim_eligible" in error for error in _errors(report))


def test_projector_identity_drift_fails_closed(report: dict[str, Any]) -> None:
    report["projector"]["sha256"] = "0" * 64  # type: ignore[index]
    assert "projector digest mismatch" in _errors(report)


def test_source_projector_identity_drift_fails_closed(
    report: dict[str, Any],
) -> None:
    report["benchmarks"][0]["source"]["projector_sha256"] = "0" * 64  # type: ignore[index]
    assert any(
        "source projector identity mismatch" in error for error in _errors(report)
    )


def test_source_metadata_missing_fails_closed(report: dict[str, Any]) -> None:
    report["benchmarks"][0].pop("source")
    assert "benchmark cmtbench source metadata missing" in _errors(report)


def test_source_commit_drift_fails_closed(report: dict[str, Any]) -> None:
    report["benchmarks"][0]["source"]["commit_sha"] = "0" * 40
    assert any("source commit_sha mismatch" in error for error in _errors(report))


def test_declaration_numeric_injection_fails_closed(report: dict[str, Any]) -> None:
    cell = report["benchmarks"][2]["rows"][0]["methods"][
        "Magenta · Claude Opus 4.7 · medium"
    ]  # type: ignore[index]
    cell["score"] = 1.0
    assert any(
        "declaration benchmark has numeric value" in error for error in _errors(report)
    )


def test_aggregate_drift_fails_closed(report: dict[str, Any]) -> None:
    summary = report["benchmarks"][1]["method_summaries"][0]  # type: ignore[index]
    summary["verdict_counts"]["成功"] += 1
    assert any("verdict aggregate drift" in error for error in _errors(report))


def test_projector_requires_fixed_source_roots(report: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="source root is required"):
        project_report(report, {}, require_sources=True)


def test_unbound_projection_is_deterministic(report: dict[str, Any]) -> None:
    first = project_report(report, {}, require_sources=False)
    second = project_report(deepcopy(report), {}, require_sources=False)
    assert first == second
