from __future__ import annotations

import json
from pathlib import Path

from MagentaBench.runner.cli import compile_main, run_main
from MagentaBench.schemas import verify_run_report


ROOT = Path(__file__).parents[1]
EXPERIMENT = (
    ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
)


def test_compile_cli_emits_resolved_identity(capsys) -> None:
    assert compile_main(
        (str(EXPERIMENT), "--project-root", str(ROOT))
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 8
    assert payload[0]["run_id"] == "fake-conformance-sweep__run0000"
    assert len(payload[0]["manifest_digest"]) == 64
    assert payload[0]["manifest"]["claim_design"] == {
        "comparison_kind": None,
        "intervention_factor_id": "conformance.fake-subject",
        "purpose": "exploratory",
        "statistical_analysis": None,
    }


def test_run_cli_executes_and_standalone_verifies(
    tmp_path: Path, capsys
) -> None:
    records = tmp_path / "records"
    assert run_main(
        (
            str(EXPERIMENT),
            "--project-root",
            str(ROOT),
            "--record-root",
            str(records),
        )
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experiment_id"] == "fake-conformance-sweep"
    assert payload["purpose"] == "exploratory"
    assert payload["run_count"] == 8
    assert payload["verified"] is True
    assert verify_run_report(payload["report"]).report.experiment_id == (
        "fake-conformance-sweep"
    )
