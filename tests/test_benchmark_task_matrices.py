"""Focused contract tests for the historical task-matrix projection."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.historical_imports import benchmark_task_matrices_v1 as projector
from scripts.historical_imports import validate_benchmark_task_matrices as validator
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


def test_required_source_projection_drift_fails_closed(
    report: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = deepcopy(report)
    report["benchmarks"][0]["rows"][0]["task_id"] = "tampered-task"
    monkeypatch.setattr(validator, "_validate_sources", lambda *args: None)
    monkeypatch.setattr(
        validator,
        "project_report",
        lambda *_args, **_kwargs: canonical,
    )
    errors = validate_report(report, roots={}, require_sources=True)
    assert "report does not match canonical source projection" in errors


def test_malformed_report_fails_closed_without_traceback(
    report: dict[str, Any],
) -> None:
    report["benchmarks"][0]["method_summaries"] = None
    assert any("method summaries malformed" in error for error in _errors(report))

    report["benchmarks"][0]["source"]["paths"] = None
    assert any("source paths malformed" in error for error in _errors(report))


def test_declaration_numeric_injection_fails_closed(report: dict[str, Any]) -> None:
    cell = report["benchmarks"][2]["rows"][0]["methods"][
        "Magenta · Claude Opus 4.7 · medium"
    ]  # type: ignore[index]
    cell["score"] = 1.0
    assert any(
        "declaration benchmark has numeric value" in error for error in _errors(report)
    )

    cell["score"] = "0.5"
    assert any(
        "declaration benchmark has numeric value" in error for error in _errors(report)
    )


def test_forbidden_authenticated_locator_fails_closed(report: dict[str, Any]) -> None:
    cell = report["benchmarks"][2]["rows"][0]["methods"][
        "Magenta · Claude Opus 4.7 · medium"
    ]
    cell["reason"] = "https://user:password@example.test/x?access_token=secret#token=x"
    assert any(
        "machine-private or authenticated locator" in error for error in _errors(report)
    )


def test_unknown_safe_field_fails_closed(report: dict[str, Any]) -> None:
    cell = report["benchmarks"][0]["rows"][0]["methods"]["purellm_gpt54 · gpt-5.4"]
    cell["evaluator_output"] = "not allowed"
    assert any("unknown field" in error for error in _errors(report))


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


def test_git_checkout_does_not_use_basename_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "per_answer_regrade.csv").write_bytes(b"pinned bytes")
    monkeypatch.setattr(
        projector,
        "_git_checkout_identity",
        lambda _root: ("head", "tree"),
    )
    with pytest.raises(ValueError, match="repository path"):
        projector._candidate(tmp_path, projector.CMT_PATH, git_checkout=True)
