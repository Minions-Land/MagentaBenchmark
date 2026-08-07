"""Mutation tests proving the scoring/isolation gates are not vacuous.

A gate that reports success because it found nothing to check looks like
verification while providing none. These tests hold the code path fixed and
mutate only the evidence, so a regression that restores a vacuous pass fails
here rather than surviving as a plausible-looking reason string.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from MagentaBench.runner.gates import _evaluate_claim
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    GateName,
    NetworkObservation,
    NetworkObservationMode,
    RunStatus,
)

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "fake-sweep.toml"
EXPECTED_RUNS = 8


def _completed(tmp_path: Path):
    runs = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    isolation_observation = NetworkObservation(
        declared_allow_internet=False,
        mode=NetworkObservationMode.active_probe,
        egress_attempted=True,
        egress_succeeded=False,
    )
    return tuple(
        dataclasses.replace(
            item,
            case=dataclasses.replace(
                item.case,
                bundle=item.case.bundle.model_copy(
                    update={"network_observation": isolation_observation}
                ),
            ),
        )
        for item in runs
    )


def _claim(items, *, expected_run_count: int = EXPECTED_RUNS):
    return _evaluate_claim(
        experiment_id="fake-conformance-sweep",
        experiment_digest="0" * 64,
        completed=items,
        expected_run_count=expected_run_count,
        control_id="fake.control",
        treatment_id="fake.treatment",
        deterministic_conformance=True,
        counterbalanced=True,
    )


def _restatus(item, status: RunStatus):
    """Return a copy of a CompletedRun whose bundle carries a new status."""
    bundle = item.case.bundle.model_copy(update={"status": status})
    case = dataclasses.replace(item.case, bundle=bundle)
    return dataclasses.replace(item, case=case)


def test_complete_plan_scores_every_run(tmp_path: Path) -> None:
    """Baseline: a complete plan reports both gates valid with positive counts."""
    report = _claim(_completed(tmp_path))
    scoring = report.gates[GateName.scoring_valid]
    isolation = report.gates[GateName.isolation_valid]
    assert scoring.valid is True
    assert f"{EXPECTED_RUNS}/{EXPECTED_RUNS} scored" in scoring.reason
    assert isolation.valid is True
    assert f"{EXPECTED_RUNS}/{EXPECTED_RUNS} verified" in isolation.reason


def test_missing_positive_isolation_observation_invalidates_isolation(
    tmp_path: Path,
) -> None:
    items = list(_completed(tmp_path))
    item = items[0]
    bundle = item.case.bundle.model_copy(update={"network_observation": None})
    items[0] = dataclasses.replace(
        item,
        case=dataclasses.replace(item.case, bundle=bundle),
    )
    report = _claim(items)
    isolation = report.gates[GateName.isolation_valid]
    assert isolation.valid is False
    assert "NetworkObservation missing" in isolation.reason


def test_partial_plan_invalidates_scoring_and_isolation(tmp_path: Path) -> None:
    """Dropping a planned run must invalidate both gates.

    Before the fix, seven scored bundles in an eight-run plan still reported
    agreement, because neither gate compared its evidence against the plan.
    """
    items = _completed(tmp_path)
    partial = items[:-1]
    assert len(partial) == EXPECTED_RUNS - 1

    report = _claim(partial)
    scoring = report.gates[GateName.scoring_valid]
    isolation = report.gates[GateName.isolation_valid]

    assert scoring.valid is False
    assert "incomplete" in scoring.reason
    assert f"{EXPECTED_RUNS - 1} of {EXPECTED_RUNS}" in scoring.reason
    assert isolation.valid is False
    assert "incomplete" in isolation.reason
    assert f"{EXPECTED_RUNS - 1} of {EXPECTED_RUNS}" in isolation.reason
    assert report.claim_eligible is False


def test_all_no_output_reports_scoring_unverified(tmp_path: Path) -> None:
    """A plan where nothing reached execution-valid status scores nothing.

    This is the exact shape of the recorded AOSE dryrun bundle
    (status=no_output, verifier_evidence=null, output_refs=[]), whose historical
    claim_report asserted 'every verifiable output has exact-verifier evidence'
    over an empty set.
    """
    items = [_restatus(item, RunStatus.no_output) for item in _completed(tmp_path)]

    report = _claim(items)
    scoring = report.gates[GateName.scoring_valid]

    assert scoring.valid is False
    assert "no execution-valid bundle was scored" in scoring.reason
    assert report.claim_eligible is False


def test_deterministic_statistics_require_scores(tmp_path: Path) -> None:
    """Pairing metadata cannot make statistics valid without observations."""
    items = _completed(tmp_path)
    no_scores = []
    for item in items:
        bundle = item.case.bundle.model_copy(
            update={
                "status": RunStatus.no_output,
                "output_refs": (),
                "verifier_evidence": None,
            }
        )
        no_scores.append(
            dataclasses.replace(item, case=dataclasses.replace(item.case, bundle=bundle))
        )
    report = _claim(no_scores)
    statistics = report.gates[GateName.statistics_valid]
    assert statistics.valid is False
    assert "verifier scores" in statistics.reason


def test_scoring_counts_only_execution_valid_bundles(tmp_path: Path) -> None:
    """The success count must report bundles actually scored, not plan size."""
    items = _completed(tmp_path)
    mutated = [_restatus(items[0], RunStatus.no_output), *items[1:]]

    report = _claim(mutated)
    scoring = report.gates[GateName.scoring_valid]

    # The plan is complete, so the completeness branch does not fire, and at
    # least one bundle scored, so the empty-set branch does not fire either.
    # The count must nonetheless distinguish scored from present.
    assert f"/{EXPECTED_RUNS} scored" in scoring.reason
    assert f"{EXPECTED_RUNS}/{EXPECTED_RUNS} scored" not in scoring.reason
