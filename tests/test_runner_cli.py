from __future__ import annotations

import json
from pathlib import Path

from MagentaBench.runner.cli import compile_main, run_main
from MagentaBench.schemas import verify_run_report


ROOT = Path(__file__).parents[1]
EXPERIMENT = (
    ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
)
FAULT_EXPERIMENT = (
    ROOT / "MagentaBench/conformance/experiments/fake-taxonomy.toml"
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
    assert payload["failed_attempts"] == []
    assert payload["verified"] is True
    assert verify_run_report(payload["report"]).report.experiment_id == (
        "fake-conformance-sweep"
    )


def test_run_cli_exposes_failed_attempt_evidence_without_log_contents(
    tmp_path: Path, capsys
) -> None:
    records = tmp_path / "fault-records"
    assert run_main(
        (
            str(FAULT_EXPERIMENT),
            "--project-root",
            str(ROOT),
            "--record-root",
            str(records),
        )
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    failures = payload["failed_attempts"]
    assert len(failures) == 8
    assert {item["status"] for item in failures} == {
        "agent_error",
        "harness_fault",
        "infra_error",
        "invalid_output",
        "no_output",
        "timeout",
        "unsupported",
        "verifier_error",
    }

    agent_error = next(
        item for item in failures if item["status"] == "agent_error"
    )
    assert set(agent_error) == {
        "attempt_id",
        "case_id",
        "evidence_bundle",
        "log_artifacts",
        "run_id",
        "status",
    }
    assert Path(agent_error["evidence_bundle"]).is_file()
    assert "stderr.log" in {
        Path(path).name for path in agent_error["log_artifacts"]
    }
