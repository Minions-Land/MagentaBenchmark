from __future__ import annotations

import csv
import io

from MagentaBench.collab.ledger import ExperimentLedger
from MagentaBench.collab.paper_table import (
    PAPER_COLUMNS,
    PAPER_TABLE_FORMAT,
    project_paper_table,
    render_csv,
    render_markdown,
    select_paper_table,
)


def _ledger() -> ExperimentLedger:
    experiment = {
        "experiment_id": "exp-a",
        "benchmark_id": "bench-a",
        "dataset_id": "data-a",
        "subject_id": "method-a",
        "model": "model-a",
        "code_commit": "a" * 40,
        "provider_id": "provider-a",
        "protocol_id": "protocol-a",
        "purpose": "comparison",
        "case_ids": ["case-1"],
        "question": "Does the method pass?",
    }
    runs = (
        {
            "experiment_id": "exp-a",
            "lab_run_id": "run-failed",
            "run_state": "failed",
            "standalone_verification": "failed",
            "record_root": "records/run-failed",
            "lab_issue": "paper-run-a",
            "validity_gates": {"protocol": False},
            "failure_breakdown": {"timeout": 1},
            "configuration_profiles": {"temperature": 0},
            "factor_values": {"split": "test"},
        },
    )
    observations = (
        {
            "observation_id": "obs-zero",
            "record_origin": "bmp",
            "experiment_id": "exp-a",
            "run_id": "run-zero",
            "benchmark_id": "bench-a",
            "dataset_id": "data-a",
            "dataset_split": "test",
            "method_id": "method-a",
            "model": "model-a",
            "metric_id": "accuracy",
            "metric_state": "observed",
            "value": 0,
            "denominator": {"planned_count": 2, "observed_count": 2},
            "evidence_tier": "bmp-standalone",
            "claim_eligible": False,
            "verification_status": "verified",
            "provider_id": "provider-a",
            "protocol_id": "protocol-a",
            "conditions": {"note": "a|b\nc"},
        },
        {
            "observation_id": "obs-missing",
            "record_origin": "legacy-import",
            "experiment_id": "exp-a",
            "run_id": "legacy-run",
            "benchmark_id": "bench-a",
            "dataset_id": "data-a",
            "metric_id": "accuracy",
            "metric_state": "missing",
            "value": None,
            "denominator": {"planned_count": 2, "missing_count": 2},
            "evidence_tier": "legacy-typed",
            "claim_eligible": False,
            "limitations": ["source-only"],
        },
        {
            "observation_id": "obs-invalid",
            "record_origin": "legacy-import",
            "experiment_id": "exp-a",
            "run_id": "legacy-invalid",
            "benchmark_id": "bench-b",
            "dataset_id": "data-b",
            "metric_id": "accuracy",
            "metric_state": "invalid",
            "value": 0,
            "evidence_tier": "legacy-typed",
            "claim_eligible": False,
        },
        {
            "observation_id": "obs-claim",
            "record_origin": "bmp",
            "experiment_id": "exp-a",
            "run_id": "run-claim",
            "benchmark_id": "bench-a",
            "dataset_id": "data-a",
            "metric_id": "accuracy",
            "metric_state": "observed",
            "value": 1,
            "claim_eligible": True,
        },
    )
    return ExperimentLedger(
        experiments=(experiment,),
        runs=runs,
        metrics=(),
        observations=observations,
    )


def test_projection_is_deterministic_and_preserves_states() -> None:
    table = project_paper_table(_ledger())
    again = project_paper_table(_ledger())

    assert table.as_dict() == again.as_dict()
    assert table.as_dict()["format"] == PAPER_TABLE_FORMAT
    assert {row["row_id"] for row in table.rows} >= {
        "obs-zero",
        "obs-invalid",
        "obs-missing",
        "obs-claim",
    }
    assert sum(row["row_kind"] == "run" for row in table.rows) == 1
    zero = next(row for row in table.rows if row["row_id"] == "obs-zero")
    missing = next(row for row in table.rows if row["row_id"] == "obs-missing")
    invalid = next(row for row in table.rows if row["row_id"] == "obs-invalid")
    failed = next(row for row in table.rows if row["row_kind"] == "run")
    assert zero["value"] == 0
    assert zero["denominator_planned"] == 2
    assert missing["result_status"] == "missing"
    assert missing["claim_status"] == "ineligible"
    assert invalid["result_status"] == "invalid"
    assert failed["terminal_state"] == "failed"
    assert failed["code_commit"] == "a" * 40
    assert failed["lab_issue"] == "paper-run-a"
    assert failed["purpose"] == "comparison"
    assert failed["provider_id"] == "provider-a"
    assert failed["protocol_id"] == "protocol-a"
    assert failed["standalone_verification"] == "failed"
    assert failed["validity_gates"] == {"protocol": False}
    assert failed["configuration_profiles"] == {"temperature": 0}
    assert failed["factor_values"] == {"split": "test"}
    claim = next(row for row in table.rows if row["row_id"] == "obs-claim")
    assert claim["claim_eligible"] is True
    assert claim["claim_status"] == "eligible"


def test_renderers_have_fixed_columns_and_escape_structured_cells() -> None:
    table = project_paper_table(_ledger())
    csv_rows = list(csv.DictReader(io.StringIO(render_csv(table))))
    assert tuple(csv_rows[0]) == PAPER_COLUMNS
    zero = next(row for row in csv_rows if row["row_id"] == "obs-zero")
    assert zero["value"] == "0"
    assert '"planned_count":2' in zero["denominator"]
    markdown = render_markdown(table)
    assert markdown.splitlines()[0] == "| " + " | ".join(PAPER_COLUMNS) + " |"
    assert "obs-zero" in markdown
    assert "source-only" in markdown
    assert "a\\|b\\nc" in markdown

    reversed_ledger = _ledger()
    reordered = ExperimentLedger(
        experiments=reversed_ledger.experiments,
        runs=reversed_ledger.runs,
        metrics=reversed_ledger.metrics,
        observations=tuple(reversed(reversed_ledger.observations)),
    )
    assert project_paper_table(reordered).as_dict() == table.as_dict()


def test_selection_keeps_errors_and_fixed_schema() -> None:
    table = project_paper_table(_ledger())
    selected = select_paper_table(table, benchmark_ids=("bench-b",))

    assert [row["row_id"] for row in selected.rows] == ["obs-invalid"]
    assert tuple(selected.rows[0]) == PAPER_COLUMNS
    assert select_paper_table(table, metric_ids=("not-present",)).rows == ()


def test_empty_projection_keeps_machine_readable_schema() -> None:
    table = project_paper_table(
        ExperimentLedger(experiments=(), runs=(), metrics=(), observations=())
    )

    assert table.ok
    assert table.rows == ()
    assert list(csv.DictReader(io.StringIO(render_csv(table)))) == []
    assert render_csv(table).splitlines()[0].split(",") == list(PAPER_COLUMNS)
