"""Mutation tests proving the scoring/isolation gates are not vacuous.

A gate that reports success because it found nothing to check looks like
verification while providing none. These tests hold the code path fixed and
mutate only the evidence, so a regression that restores a vacuous pass fails
here rather than surviving as a plausible-looking reason string.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from MagentaBench.runner.evidence import artifact_ref, atomic_write_json, sha256_file
from MagentaBench.runner.gates import _evaluate_claim
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    GateName,
    NetworkBoundary,
    NetworkObservation,
    NetworkObservationMode,
    NetworkPolicySource,
    ResolvedNetworkPolicy,
    RunStatus,
    ScheduleActivationReceipt,
    canonical_digest,
)

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench" / "conformance" / "experiments" / "fake-sweep.toml"
EXPECTED_RUNS = 8


def _with_bundle(item, bundle):
    atomic_write_json(item.case.bundle_path, bundle)
    bundle_ref = artifact_ref(item.case.bundle_path)
    case = dataclasses.replace(
        item.case,
        bundle=bundle,
        bundle_digest=sha256_file(item.case.bundle_path),
    )
    metric = item.plan.manifest.benchmark.authoritative_reward_metric
    reward_value = (
        None
        if bundle.verifier_evidence is None
        else bundle.verifier_evidence.metrics.get(metric)
    )
    attempts = tuple(
        attempt.model_copy(
            update={
                "evidence_bundle_ref": bundle_ref,
                "status": bundle.status,
                "reward_metric": metric if reward_value is not None else None,
                "reward_value": reward_value,
            }
        )
        for attempt in item.schedule_receipt.attempts
    )
    receipt = ScheduleActivationReceipt.model_validate(
        item.schedule_receipt.model_copy(
            update={"attempts": attempts}
        ).model_dump(mode="json")
    )
    atomic_write_json(item.schedule_receipt_path, receipt)
    return dataclasses.replace(
        item,
        case=case,
        schedule_receipt=receipt,
        schedule_receipt_sha256=sha256_file(item.schedule_receipt_path),
    )


def _completed(tmp_path: Path):
    runs = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    completed = []
    for item in runs:
        policy = ResolvedNetworkPolicy(
            resolver_adapter="fake",
            execution_adapter="fake",
            case_id=item.case.case_id,
            boundary=NetworkBoundary.process,
            allow_internet=False,
            required_observation=NetworkObservationMode.active_probe,
            source=NetworkPolicySource.backend_artifact,
            source_artifact_digest=item.runner_digest,
        )
        probe_path = Path(item.case.bundle_path).with_name("network_probe.json")
        atomic_write_json(
            probe_path,
            {"egress_attempted": True, "egress_succeeded": False},
        )
        observation = NetworkObservation(
            policy_digest=canonical_digest(policy),
            declared_allow_internet=False,
            mode=NetworkObservationMode.active_probe,
            egress_attempted=True,
            egress_succeeded=False,
            evidence_refs=(artifact_ref(probe_path),),
        )
        bundle = item.case.bundle.model_copy(
            update={
                "network_policy": policy,
                "network_observation": observation,
            }
        )
        completed.append(_with_bundle(item, bundle))
    return tuple(completed)


def _with_policy(item, policy: ResolvedNetworkPolicy):
    observation = item.case.bundle.network_observation
    assert observation is not None
    bundle = item.case.bundle.model_copy(
        update={
            "network_policy": policy,
            "network_observation": observation.model_copy(
                update={"policy_digest": canonical_digest(policy)}
            ),
        }
    )
    return _with_bundle(item, bundle)


def _claim(items, *, expected_run_count: int = EXPECTED_RUNS):
    return _evaluate_claim(
        completed=items,
        expected_run_count=expected_run_count,
        control_id="fake.control",
        treatment_id="fake.treatment",
        deterministic_conformance=True,
        counterbalanced=True,
        record_index_ref=None,
    )


def _restatus(item, status: RunStatus):
    """Return a copy of a CompletedRun whose bundle carries a new status."""
    bundle = item.case.bundle.model_copy(update={"status": status})
    return _with_bundle(item, bundle)


def test_fake_pipeline_does_not_synthesize_network_isolation(
    tmp_path: Path,
) -> None:
    runs = Pipeline(ROOT, tmp_path).run(EXPERIMENT).runs
    assert all(item.case.bundle.network_policy is None for item in runs)
    assert all(item.case.bundle.network_observation is None for item in runs)


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
    items[0] = _with_bundle(item, bundle)
    report = _claim(items)
    isolation = report.gates[GateName.isolation_valid]
    assert report.gates[GateName.protocol_valid].valid is True
    assert isolation.valid is False
    assert "NetworkObservation missing" in isolation.reason


def test_missing_resolved_network_policy_invalidates_isolation(
    tmp_path: Path,
) -> None:
    items = list(_completed(tmp_path))
    item = items[0]
    bundle = item.case.bundle.model_copy(update={"network_policy": None})
    items[0] = _with_bundle(item, bundle)
    isolation = _claim(items).gates[GateName.isolation_valid]
    assert isolation.valid is False
    assert "ResolvedNetworkPolicy missing" in isolation.reason


def test_network_observation_must_bind_resolved_policy(tmp_path: Path) -> None:
    baseline = list(_completed(tmp_path))
    item = baseline[0]
    observation = item.case.bundle.network_observation
    assert observation is not None
    mutations = (
        ({"declared_allow_internet": True}, "disagrees with resolved policy"),
        ({"policy_digest": "0" * 64}, "policy digest drift"),
        ({"evidence_refs": ()}, "evidence reference missing"),
    )
    for update, expected in mutations:
        items = list(baseline)
        bundle = item.case.bundle.model_copy(
            update={"network_observation": observation.model_copy(update=update)}
        )
        items[0] = _with_bundle(item, bundle)
        isolation = _claim(items).gates[GateName.isolation_valid]
        assert isolation.valid is False
        assert expected in isolation.reason


def test_network_policy_identity_substitutions_are_rejected(tmp_path: Path) -> None:
    baseline = list(_completed(tmp_path))
    item = baseline[0]
    policy = item.case.bundle.network_policy
    assert policy is not None
    mutations = (
        ({"execution_adapter": "subprocess"}, "execution adapter mismatch"),
        ({"resolver_adapter": "subprocess"}, "resolver adapter mismatch"),
        ({"case_id": "other-case"}, "case binding mismatch"),
        ({"boundary": NetworkBoundary.task_container}, "boundary mismatch"),
        ({"source_artifact_digest": "0" * 64}, "source artifact digest drift"),
    )
    for update, expected in mutations:
        items = list(baseline)
        items[0] = _with_policy(item, policy.model_copy(update=update))
        isolation = _claim(items).gates[GateName.isolation_valid]
        assert isolation.valid is False
        assert expected in isolation.reason


def test_case_set_policy_fails_closed_without_activation_receipt(
    tmp_path: Path,
) -> None:
    items = list(_completed(tmp_path))
    item = items[0]
    policy = item.case.bundle.network_policy
    assert policy is not None
    case_policy = policy.model_copy(
        update={
            "source": NetworkPolicySource.case_set_artifact,
            "source_artifact_digest": item.case_set_digest,
        }
    )
    items[0] = dataclasses.replace(
        _with_policy(item, case_policy), case_set_digest=None
    )
    isolation = _claim(items).gates[GateName.isolation_valid]
    assert isolation.valid is False
    assert "CaseSetActivationReceipt" in isolation.reason


def test_denial_policy_requires_positive_failed_egress_probe(
    tmp_path: Path,
) -> None:
    baseline = list(_completed(tmp_path))
    item = baseline[0]
    policy = item.case.bundle.network_policy
    observation = item.case.bundle.network_observation
    assert policy is not None and observation is not None
    mutations = (
        (
            policy.model_copy(
                update={"required_observation": NetworkObservationMode.unobservable}
            ),
            observation.model_copy(
                update={
                    "mode": NetworkObservationMode.unobservable,
                    "egress_attempted": False,
                    "egress_succeeded": False,
                }
            ),
        ),
        (
            policy,
            observation.model_copy(update={"egress_succeeded": True}),
        ),
    )
    for mutated_policy, mutated_observation in mutations:
        items = list(baseline)
        mutated_observation = mutated_observation.model_copy(
            update={"policy_digest": canonical_digest(mutated_policy)}
        )
        bundle = item.case.bundle.model_copy(
            update={
                "network_policy": mutated_policy,
                "network_observation": mutated_observation,
            }
        )
        items[0] = _with_bundle(item, bundle)
        isolation = _claim(items).gates[GateName.isolation_valid]
        assert isolation.valid is False
        assert "cannot substantiate isolation" in isolation.reason


def test_network_mode_provenance_cannot_replace_observation(
    tmp_path: Path,
) -> None:
    items = list(_completed(tmp_path))
    item = items[0]
    provenance = item.case.bundle.provenance.model_copy(
        update={"network_mode": "none"}
    )
    bundle = item.case.bundle.model_copy(
        update={"provenance": provenance, "network_observation": None}
    )
    items[0] = _with_bundle(item, bundle)
    isolation = _claim(items).gates[GateName.isolation_valid]
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

    assert report.gates[GateName.protocol_valid].valid is True
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
        no_scores.append(_with_bundle(item, bundle))
    report = _claim(no_scores)
    statistics = report.gates[GateName.statistics_valid]
    assert report.gates[GateName.protocol_valid].valid is True
    assert statistics.valid is False
    assert "verifier scores" in statistics.reason


def test_scoring_counts_only_execution_valid_bundles(tmp_path: Path) -> None:
    """The success count must report bundles actually scored, not plan size."""
    items = _completed(tmp_path)
    mutated = [_restatus(items[0], RunStatus.no_output), *items[1:]]

    report = _claim(mutated)
    scoring = report.gates[GateName.scoring_valid]
    assert report.gates[GateName.protocol_valid].valid is True

    # The plan is complete, so the completeness branch does not fire, and at
    # least one bundle scored, so the empty-set branch does not fire either.
    # The count must nonetheless distinguish scored from present.
    assert f"/{EXPECTED_RUNS} scored" in scoring.reason
    assert f"{EXPECTED_RUNS}/{EXPECTED_RUNS} scored" not in scoring.reason
