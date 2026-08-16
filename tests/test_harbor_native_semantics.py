from __future__ import annotations

import json
from pathlib import Path

from MagentaBench.runner.backend.harbor import parse_harbor_result, parse_harbor_results
from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas import RunStatus

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "harbor-shim-smoke.toml"


def _parse_test_result(*args, **kwargs):
    return parse_harbor_result(*args, allow_test_parse=True, **kwargs)


def _parse_test_results(*args, **kwargs):
    return parse_harbor_results(*args, allow_test_parse=True, **kwargs)


def _run():
    return Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]


def test_invented_top_level_outcome_cannot_create_verifier_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "invented-outcome"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"status": "pass", "state": "completed", "outcome": "success"}),
        encoding="utf-8",
    )
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.status == RunStatus.invalid_output
    assert case.bundle.verifier_evidence is None
    assert any(Path(ref.path).name == "result.json" for ref in case.bundle.log_refs)


def test_native_job_trial_uses_child_result_artifact_without_answer(tmp_path: Path) -> None:
    root = tmp_path / "job"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"trial_results": [{"trial_name": "trial-a", "verifier_result": {"rewards": {"score": 1.0}}}]}),
        encoding="utf-8",
    )
    child = root / "trial-a"
    child.mkdir()
    (child / "result.json").write_text("{}", encoding="utf-8")
    cases = _parse_test_results(
        _run(),
        result_root=root,
        authoritative_reward_key="score",
        reward_pass_value=1.0,
    )
    assert len(cases) == 1
    case = cases[0]
    assert case.bundle.status == RunStatus.pass_
    assert case.bundle.verifier_evidence is not None
    assert case.bundle.verifier_evidence.artifact_refs
    assert Path(case.bundle.verifier_evidence.artifact_refs[0].path).name == "result.json"
    assert case.bundle.output_refs


def test_harbor_020_disk_job_result_is_not_ingested_as_a_second_trial(
    tmp_path: Path,
) -> None:
    root = tmp_path / "harbor-020-job"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps(
            {
                "id": "job-id",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
            }
        ),
        encoding="utf-8",
    )
    child = root / "regex-log__GifA3FE"
    child.mkdir()
    (child / "result.json").write_text(
        json.dumps(
            {
                "task_name": "terminal-bench/regex-log",
                "trial_name": "regex-log__GifA3FE",
                "agent_result": {
                    "n_input_tokens": None,
                    "n_output_tokens": None,
                    "cost_usd": None,
                },
                "verifier_result": {"rewards": {"reward": 1.0}},
                "started_at": "2026-08-08T06:58:31.216790Z",
                "finished_at": "2026-08-08T06:58:43.716790Z",
            }
        ),
        encoding="utf-8",
    )

    cases = _parse_test_results(
        _run(),
        result_root=root,
        authoritative_reward_key="reward",
        reward_pass_value=1.0,
    )
    assert len(cases) == 1
    case = cases[0]
    assert case.case_id == "case-001__regex-log__GifA3FE"
    assert case.bundle.status == RunStatus.pass_
    assert case.bundle.usage is not None
    assert case.bundle.usage.wall_clock_seconds == 12.5
    copied_results = {
        Path(ref.path)
        for ref in (*case.bundle.log_refs, *case.bundle.output_refs)
        if Path(ref.path).name == "result.json"
    }
    assert len(copied_results) == 2
    assert all(path.is_relative_to(root / "bmp_cases") for path in copied_results)


def test_completed_native_verifier_timeout_is_verifier_error(tmp_path: Path) -> None:
    root = tmp_path / "completed-verifier-timeout"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "trial-a",
                "agent_result": {
                    "n_input_tokens": None,
                    "n_output_tokens": None,
                },
                "verifier_result": None,
                "exception_info": {
                    "exception_type": "VerifierTimeoutError",
                    "exception_message": "Verifier execution timed out",
                },
                "verifier": {
                    "started_at": "2026-08-08T00:00:01Z",
                    "finished_at": "2026-08-08T00:01:31Z",
                },
            }
        ),
        encoding="utf-8",
    )
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.status == RunStatus.verifier_error
    assert case.bundle.verifier_evidence is not None
    assert case.bundle.verifier_evidence.details["incomplete_phase"] is None


def test_native_named_rewards_require_adapter_semantics(tmp_path: Path) -> None:
    root = tmp_path / "multi"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"score": 0.5, "quality": 0.2}}}),
        encoding="utf-8",
    )
    (root / "answer.txt").write_text("answer", encoding="utf-8")
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.status == RunStatus.unsupported
    assert case.bundle.verifier_evidence is not None
    assert case.bundle.verifier_evidence.score is None
    assert case.bundle.verifier_evidence.metrics == {"score": 0.5, "quality": 0.2}
    assert case.bundle.verifier_evidence.details["scoring_semantics_declared"] is False


def test_native_continuous_reward_uses_declared_adapter_semantics(tmp_path: Path) -> None:
    root = tmp_path / "continuous"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"quality": 0.5}}}),
        encoding="utf-8",
    )
    (root / "answer.txt").write_text("answer", encoding="utf-8")
    case = _parse_test_result(
        _run(),
        result_root=root,
        authoritative_reward_key="quality",
        reward_pass_value=0.5,
    )
    assert case.bundle.status == RunStatus.pass_
    assert case.bundle.verifier_evidence is not None
    assert case.bundle.verifier_evidence.score == 0.5
    assert case.bundle.verifier_evidence.details["scoring_semantics_declared"] is True


def test_native_no_result_exception_families_are_distinct(tmp_path: Path) -> None:
    expected = {
        "VerifierOutputParseError": RunStatus.verifier_error,
        "RewardFileNotFoundError": RunStatus.verifier_error,
        "AgentAuthenticationError": RunStatus.agent_error,
    }
    for exception_type, status in expected.items():
        root = tmp_path / exception_type
        root.mkdir()
        (root / "result.json").write_text(
            json.dumps({"exception_info": {"exception_type": exception_type}}),
            encoding="utf-8",
        )
        case = _parse_test_result(_run(), result_root=root)
        assert case.bundle.status == status


def test_native_timing_info_is_primary_failure_phase_signal(tmp_path: Path) -> None:
    root = tmp_path / "timing"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps(
            {
                "exception_info": {"exception_type": "RuntimeError"},
                "environment_setup": {"started_at": "t", "finished_at": "done"},
                "agent_execution": {"started_at": "t", "finished_at": None},
            }
        ),
        encoding="utf-8",
    )
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.status == RunStatus.agent_error


def test_verifier_phase_timeout_stays_verifier_error(tmp_path: Path) -> None:
    root = tmp_path / "verifier-timeout"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps(
            {
                "exception_info": {"exception_type": "VerifierTimeoutError"},
                "verifier": {"started_at": "t", "finished_at": None},
            }
        ),
        encoding="utf-8",
    )
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.status == RunStatus.verifier_error
    assert case.bundle.verifier_evidence is not None
    assert case.bundle.verifier_evidence.details["incomplete_phase"] == "verifier"
    assert case.bundle.verifier_evidence.details["exception_info"]["exception_type"] == "VerifierTimeoutError"


def test_native_usage_is_read_from_nested_agent_result(tmp_path: Path) -> None:
    root = tmp_path / "usage"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps(
            {
                "agent_result": {
                    "n_input_tokens": 11,
                    "n_output_tokens": 7,
                    "cost_usd": 0.03,
                },
                "verifier_result": {"rewards": {"score": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.usage is not None
    assert case.bundle.usage.input_tokens == 11
    assert case.bundle.usage.output_tokens == 7
    assert case.bundle.usage.total_tokens == 18
    assert case.bundle.usage.cost == 0.03


def test_none_model_without_native_usage_records_observed_zero(tmp_path: Path) -> None:
    root = tmp_path / "none-model-usage"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"score": 0.0}}}),
        encoding="utf-8",
    )
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.usage is not None
    assert case.bundle.usage.input_tokens == 0
    assert case.bundle.usage.output_tokens == 0
    assert case.bundle.usage.total_tokens == 0
    assert case.bundle.usage.cost == 0.0


def test_malformed_result_is_preserved_as_invalid_output(tmp_path: Path) -> None:
    root = tmp_path / "malformed"
    root.mkdir()
    (root / "result.json").write_text("{not-json", encoding="utf-8")
    case = _parse_test_result(_run(), result_root=root)
    assert case.bundle.status == RunStatus.invalid_output
    assert case.bundle.log_refs
    assert any(Path(ref.path).name == "result.json" for ref in case.bundle.log_refs)


def test_missing_native_trial_child_is_infra_without_cross_trial_mix(tmp_path: Path) -> None:
    root = tmp_path / "missing-child"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"trial_results": [{"trial_name": "trial-missing", "verifier_result": {"rewards": {"score": 1}}}]}),
        encoding="utf-8",
    )
    (root / "unrelated-secret.log").write_text("not this trial", encoding="utf-8")
    cases = _parse_test_results(_run(), result_root=root)
    assert len(cases) == 1
    assert cases[0].bundle.status == RunStatus.infra_error
    assert all(Path(ref.path).name != "unrelated-secret.log" for ref in cases[0].bundle.log_refs)
