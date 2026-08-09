from __future__ import annotations

import json
from pathlib import Path

import pytest

from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    MetricComputationState,
    ReportVerificationError,
    RolloutTrajectory,
    TrajectoryEventKind,
    verify_run_report,
)


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def test_registered_metrics_and_trajectories_are_replayable(tmp_path: Path) -> None:
    result = Pipeline(ROOT, tmp_path).run(EXPERIMENT)

    pass_results = [
        item
        for item in result.report.metric_results
        if item.metric_id == "pass-at-1.infra-zero.v1"
    ]
    assert len(pass_results) == 8
    assert sorted(item.value for item in pass_results) == [
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        1.0,
        1.0,
        1.0,
    ]
    assert all(
        item.state == MetricComputationState.complete
        and item.planned_rollout_count == 1
        and item.task_count == 1
        and item.rollouts_per_task == 1
        and item.observed_count == 1
        for item in pass_results
    )

    token_results = [
        item
        for item in result.report.metric_results
        if item.metric_id == "tokens.mean-completed.v1"
    ]
    assert len(token_results) == 8
    assert all(item.value == 0.0 for item in token_results)
    efficiency_results = [
        item
        for item in result.report.metric_results
        if item.metric_id == "successes-per-million-tokens.v1"
    ]
    assert len(efficiency_results) == 8
    assert all(
        item.state == MetricComputationState.unavailable
        and item.reason == "registered metric denominator is zero"
        for item in efficiency_results
    )

    for completed in result.runs:
        trajectory_ref = completed.case.bundle.trajectory_ref
        assert trajectory_ref is not None
        trajectory = RolloutTrajectory.model_validate_json(
            Path(trajectory_ref.path).read_bytes()
        )
        assert trajectory.attempt_id == completed.case.bundle.run_id
        assert trajectory.terminal_status == completed.case.bundle.status
        assert trajectory.usage == completed.case.bundle.usage
        assert trajectory.events[0].kind == TrajectoryEventKind.rollout_started
        assert trajectory.events[-1].kind == TrajectoryEventKind.rollout_finished
        assert tuple(event.sequence for event in trajectory.events) == tuple(
            range(1, len(trajectory.events) + 1)
        )

    verify_run_report(result.report_path)
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    target = next(
        item
        for item in payload["metric_results"]
        if item["metric_id"] == "pass-at-1.infra-zero.v1"
        and item["value"] == 1.0
    )
    target["value"] = 0.5
    result.report_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReportVerificationError, match="metric_results"):
        verify_run_report(result.report_path)
