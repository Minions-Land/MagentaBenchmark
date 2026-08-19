from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    ClaimReport,
    ComparisonKind,
    GateName,
    GateResult,
    RunPurpose,
    SubjectKind,
)


ROOT = Path(__file__).parents[1]
EXPERIMENT = (
    ROOT
    / "MagentaBench/conformance/experiments/native-benchmark-continuous-smoke.toml"
)
PROGRAM = ROOT / "tools/memory_baseline_report"


def _load_renderer():
    path = PROGRAM / "render_report.py"
    spec = importlib.util.spec_from_file_location("memory_baseline_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capability_matrix_retains_every_registered_cell() -> None:
    renderer = _load_renderer()
    matrix = renderer.load_matrix(PROGRAM / "capability-matrix.json")
    capabilities = renderer.expand_capabilities(matrix)
    paper_native = renderer.expand_paper_native(matrix)

    assert len(matrix["benchmarks"]) == 5
    assert len(matrix["methods"]) == 48
    assert len(capabilities) == 48 * 5
    assert len(paper_native) == len(matrix["paper_native_paths"]) * 5
    assert {
        "frozen-demos",
        "recency-window",
        "random-retrieval",
        "dense-rag",
        "hybrid-rag",
        "raw-trajectory-dense-rag",
        "rolling-summary",
        "structured-event-memory",
        "structmem",
        "memzero",
        "memzero-graph",
    } <= {item["id"] for item in matrix["methods"]}
    assert all(
        row["status"] == "blocked"
        for row in capabilities
        if row["availability"] == "blocked"
    )
    assert all(
        row["status"] != "blocked" or "0" not in row["reason"]
        for row in capabilities
    )


def test_reviewable_matrix_covers_every_declared_family() -> None:
    renderer = _load_renderer()
    matrix = renderer.load_matrix(PROGRAM / "capability-matrix.json")
    document = (PROGRAM / "BASELINE_MATRIX.md").read_text(encoding="utf-8")
    entries = (*matrix["methods"], *matrix["paper_native_paths"])
    rows = {
        cells[0]: cells
        for line in document.splitlines()
        if line.startswith("| `")
        for cells in [[cell.strip().strip("`") for cell in line.split("|")[1:-1]]]
    }

    assert len(rows) == len(entries)
    for entry in entries:
        assert entry["id"] in rows
        assert rows[entry["id"]][2] == entry["equivalence"]
    for method in matrix["methods"]:
        expected = [
            method["support"]["overrides"].get(
                benchmark["id"], method["support"]["default"]
            )["status"]
            for benchmark in matrix["benchmarks"]
        ]
        assert rows[method["id"]][3:] == expected
    for method in matrix["paper_native_paths"]:
        expected = [
            method["support"][benchmark["id"]]["status"]
            for benchmark in matrix["benchmarks"]
        ]
        assert rows[method["id"]][3:] == expected


def test_claim_report_validity_comes_from_gates() -> None:
    renderer = _load_renderer()
    gates = {
        name: GateResult(valid=False, reason=f"{name.value} failed")
        for name in (
            GateName.execution_valid,
            GateName.protocol_valid,
            GateName.isolation_valid,
            GateName.scoring_valid,
            GateName.statistics_valid,
        )
    }
    gates[GateName.protocol_valid] = GateResult(
        valid=True, evidence_refs=("protocol.json",)
    )
    report = ClaimReport(
        purpose=RunPurpose.claim,
        comparison_kind=ComparisonKind.agent,
        subject_kinds=(SubjectKind.opaque_agent,),
        experiment_id="memory-report-claim-test",
        manifest_digest="0" * 64,
        gates=gates,
    )

    assert renderer._report_validity(report) == (True, False)


def test_completion_eligibility_fails_closed_on_tool_error_telemetry() -> None:
    renderer = _load_renderer()

    assert renderer._completion_eligibility(
        renderer.RunStatus.scored, 0, protocol_valid=True
    ) == (True, [])
    assert renderer._completion_eligibility(
        renderer.RunStatus.verified_fail, 0, protocol_valid=True
    ) == (True, [])
    assert renderer._completion_eligibility(
        renderer.RunStatus.scored, 3, protocol_valid=True
    ) == (False, ["tool_errors_nonzero:3"])
    assert renderer._completion_eligibility(
        renderer.RunStatus.scored, None, protocol_valid=True
    ) == (False, ["tool_errors_unobserved"])
    assert renderer._completion_eligibility(
        renderer.RunStatus.agent_error, 0, protocol_valid=False
    ) == (
        False,
        ["protocol_invalid", "non_scoring_status:agent_error"],
    )


def test_verified_native_report_preserves_all_metrics_and_usage(tmp_path: Path) -> None:
    renderer = _load_renderer()
    pipeline_result = Pipeline(ROOT, tmp_path / "records").run(EXPERIMENT)
    output_json = tmp_path / "report/memory-baselines.json"
    output_html = tmp_path / "report/memory-baselines.html"

    document = renderer.generate(
        matrix_path=PROGRAM / "capability-matrix.json",
        report_paths=[pipeline_result.report_path],
        output_json=output_json,
        output_html=output_html,
    )

    expected_native = {
        "native:native_score",
        "native:secondary_exact",
        "native:secondary_error",
    }
    expected_usage = {
        "usage:input_tokens",
        "usage:output_tokens",
        "usage:total_tokens",
        "usage:cost",
        "usage:model_calls",
        "usage:tool_calls",
        "usage:tool_errors",
    }
    assert expected_native <= set(document["metric_columns"])
    assert expected_usage <= set(document["metric_columns"])
    assert any(name.startswith("bmp:") for name in document["metric_columns"])
    assert document["results"][0]["score"] == 0.75
    assert document["results"][0]["passed"] is None
    assert document["results"][0]["model_activation"] is None
    assert document["results"][0]["tool_error_count"] == 0
    assert document["results"][0]["completion_eligible"] is True
    assert document["results"][0]["completion_exclusions"] == []
    assert document["summary"]["completion_eligible_result_row_count"] == 1
    assert document["summary"]["completion_ineligible_result_row_count"] == 0

    ineligible = json.loads(json.dumps(document))
    ineligible["results"][0]["tool_error_count"] = 2
    ineligible["results"][0]["completion_eligible"] = False
    ineligible["results"][0]["completion_exclusions"] = ["tool_errors_nonzero:2"]
    ineligible["results"][0]["values"]["usage:tool_errors"] = 2
    ineligible["summary"]["completion_eligible_result_row_count"] = 0
    ineligible["summary"]["completion_ineligible_result_row_count"] = 1
    ineligible_html = renderer.render_html(ineligible)
    assert "tool_errors_nonzero:2" in ineligible_html
    assert "usage:tool_errors" in ineligible_html
    assert "<td>0.75</td>" in ineligible_html

    encoded = json.loads(output_json.read_text(encoding="utf-8"))
    rendered = output_html.read_text(encoding="utf-8")
    assert encoded == document
    for metric in (*expected_native, *expected_usage):
        assert metric in rendered
    for metric in document["metric_columns"]:
        assert metric in rendered
    assert "Verifier score" in rendered
    assert "Completion eligible" in rendered
    assert "Completion exclusions" in rendered
    assert "Activation" in rendered
    assert "missing" in rendered
    assert "Protocol valid" in rendered
    assert document["source_reports"][0]["sha256"] in rendered
    assert "<td>0.75</td>" in rendered
    assert "state-blocked" in rendered
    assert ">blocked</div>" in rendered
    assert ">0</td>" not in renderer._matrix_table(
        document["capability_rows"], document["benchmarks"]
    )
