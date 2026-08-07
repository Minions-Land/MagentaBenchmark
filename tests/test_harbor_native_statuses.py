from __future__ import annotations

import json
from pathlib import Path

from MagentaBench.runner.backend.harbor import parse_harbor_result
from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas import RunStatus

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "harbor-shim-smoke.toml"


def _parse_test_result(*args, **kwargs):
    return parse_harbor_result(*args, allow_test_parse=True, **kwargs)


def test_native_harbor_shapes_cover_current_runtime_statuses(tmp_path: Path) -> None:
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]
    fixtures = {
        "pass": {"verifier_result": {"rewards": {"score": 1.0}}},
        "verified_fail": {"verifier_result": {"rewards": {"score": 0.0}}},
        "no_output": {"agent_result": {}},
        "invalid_output": {"exception_info": {"exception_type": "OutputContractError"}},
        "timeout": {
            "exception_info": {"exception_type": "TimeoutExpired"},
            "agent_execution": {"started_at": "t", "finished_at": None},
        },
        "agent_error": {
            "exception_info": {"exception_type": "RuntimeError"},
            "agent_execution": {"started_at": "t", "finished_at": None},
        },
        "harness_fault": {
            "exception_info": {"exception_type": "RuntimeError"},
            "agent_setup": {"started_at": "t", "finished_at": None},
        },
        "verifier_error": {
            "exception_info": {"exception_type": "ValueError"},
            "verifier": {"started_at": "t", "finished_at": None},
        },
        "infra_error": {
            "exception_info": {"exception_type": "DockerError"},
            "environment_setup": {"started_at": "t", "finished_at": None},
        },
        "unsupported": {"verifier_result": {"rewards": {"quality": 0.2, "other": 0.3}}},
    }
    observed = set()
    for expected, payload in fixtures.items():
        root = tmp_path / expected
        root.mkdir()
        (root / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        if expected in {"pass", "verified_fail"}:
            (root / "answer.txt").write_text(expected, encoding="utf-8")
        case = _parse_test_result(
            run,
            result_root=root,
            case_id="case-001",
            authoritative_reward_key="score",
            reward_pass_value=1.0,
        )
        assert case.bundle.provenance.test_override is not None
        assert case.bundle.provenance.test_override.forced_scope == "conformance"
        observed.add(case.bundle.status.value)
    assert observed == {status.value for status in RunStatus if status != RunStatus.scored}
