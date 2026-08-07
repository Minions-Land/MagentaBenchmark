from __future__ import annotations

import json
from pathlib import Path

import pytest

from MagentaBench.runner.backend.harbor import HarborConfigurationError, parse_harbor_result, parse_harbor_results
from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas import RunStatus

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "harbor-shim-smoke.toml"


def test_native_trial_results_are_separated_by_attempt(tmp_path: Path) -> None:
    run = Compiler(ROOT).compile(EXPERIMENT)[0]
    root = tmp_path / "harbor-results"
    root.mkdir()
    payload = {
        "trial_results": [
            {"trial_name": "trial-a", "verifier_result": {"rewards": {"score": 1.0}}},
            {"trial_name": "trial-b", "verifier_result": {"rewards": {"score": 0.0}}},
        ]
    }
    (root / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    for name, answer in (("trial-a", "ok"), ("trial-b", "bad")):
        trial = root / name
        trial.mkdir()
        (trial / "result.json").write_text("{}", encoding="utf-8")
        (trial / "answer.txt").write_text(answer, encoding="utf-8")
        (trial / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")

    cases = parse_harbor_results(
        run,
        result_root=root,
        case_id="case-1",
        authoritative_reward_key="score",
        reward_pass_value=1.0,
    )
    assert len(cases) == 2
    assert {case.case_id for case in cases} == {"case-1__trial-a", "case-1__trial-b"}
    assert {case.bundle.status for case in cases} == {
        RunStatus.pass_,
        RunStatus.verified_fail,
    }
    assert all(
        all(Path(ref.path).is_relative_to(root / "bmp_cases") for ref in case.bundle.output_refs)
        for case in cases
    )


def test_harbor_case_traversal_is_rejected(tmp_path: Path) -> None:
    run = Compiler(ROOT).compile(EXPERIMENT)[0]
    with pytest.raises(HarborConfigurationError, match="invalid case id"):
        parse_harbor_result(run, result_root=tmp_path, case_id="../escape")


def test_harbor_symlink_artifact_escape_is_rejected(tmp_path: Path) -> None:
    run = Compiler(ROOT).compile(EXPERIMENT)[0]
    root = tmp_path / "harbor-results"
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"trial_results": [{"trial_name": "trial-a", "verifier_result": {"rewards": {"score": 1.0}}}]}),
        encoding="utf-8",
    )
    trial = root / "trial-a"
    trial.mkdir()
    (trial / "result.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside-answer.txt"
    outside.write_text("secret", encoding="utf-8")
    (trial / "answer.txt").symlink_to(outside)
    with pytest.raises(HarborConfigurationError, match="symlink"):
        parse_harbor_result(run, result_root=root)
