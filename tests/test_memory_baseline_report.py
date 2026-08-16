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
    assert len(matrix["methods"]) == 37
    assert len(capabilities) == 37 * 5
    assert len(paper_native) == len(matrix["paper_native_paths"]) * 5
    assert all(
        row["status"] == "blocked"
        for row in capabilities
        if row["availability"] == "blocked"
    )
    assert all(
        row["status"] != "blocked" or "0" not in row["reason"]
        for row in capabilities
    )


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
    }
    assert expected_native <= set(document["metric_columns"])
    assert expected_usage <= set(document["metric_columns"])
    assert any(name.startswith("bmp:") for name in document["metric_columns"])
    assert document["results"][0]["score"] == 0.75
    assert document["results"][0]["passed"] is None
    assert document["results"][0]["model_activation"] is None

    encoded = json.loads(output_json.read_text(encoding="utf-8"))
    rendered = output_html.read_text(encoding="utf-8")
    assert encoded == document
    for metric in (*expected_native, *expected_usage):
        assert metric in rendered
    for metric in document["metric_columns"]:
        assert metric in rendered
    assert "Verifier score" in rendered
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
