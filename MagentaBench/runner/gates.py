"""Claim gate evaluation for completed BMP runs."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Iterable, Mapping

from MagentaBench.schemas import (
    ClaimReport,
    EffectEstimate,
    Observation,
    ObservationReport,
    GateName,
    GateResult,
    LineageRef,
    RunPurpose,
    RunReport,
    RunStatus,
)

from .backend.fake import CaseExecution
from .compiler import CompiledRun


@dataclass(frozen=True)
class CompletedRun:
    plan: CompiledRun
    case: CaseExecution


_EXECUTION_VALID = frozenset({RunStatus.pass_, RunStatus.verified_fail})


def _valid(reason: str | None = None) -> GateResult:
    return GateResult(valid=True, reason=reason)


def _invalid(reason: str) -> GateResult:
    return GateResult(valid=False, reason=reason)


def _subject_pairs(
    completed: list[CompletedRun], control_id: str, treatment_id: str
) -> tuple[list[tuple[CompletedRun, CompletedRun]], str | None]:
    grouped: dict[bytes, dict[str, CompletedRun]] = defaultdict(dict)
    from .compiler import canonical_json_bytes

    for item in completed:
        factors = {
            key: value
            for key, value in item.plan.factor_values.items()
            if key not in {"subject", "experiment.subject", "order_position"}
        }
        grouped[canonical_json_bytes(factors)][item.plan.manifest.subject.id] = item
    pairs: list[tuple[CompletedRun, CompletedRun]] = []
    for pair in grouped.values():
        if set(pair) != {control_id, treatment_id}:
            return [], "paired control/treatment structure is incomplete"
        pairs.append((pair[control_id], pair[treatment_id]))
    if not pairs:
        return [], "no paired control/treatment runs"
    return pairs, None


def _counterbalance_is_valid(
    completed: list[CompletedRun], control_id: str, treatment_id: str
) -> bool:
    from .compiler import canonical_json_bytes

    positions = {
        item.plan.manifest.metadata.run_id: index for index, item in enumerate(completed)
    }
    pair_groups: dict[bytes, dict[str, CompletedRun]] = defaultdict(dict)
    outer_keys: dict[bytes, bytes] = {}
    for item in completed:
        factors = dict(item.plan.factor_values)
        pair_factors = {
            key: value
            for key, value in factors.items()
            if key not in {"subject", "experiment.subject", "order_position"}
        }
        outer_factors = {
            key: value for key, value in pair_factors.items() if key != "repetition"
        }
        pair_key = canonical_json_bytes(pair_factors)
        pair_groups[pair_key][item.plan.manifest.subject.id] = item
        outer_keys[pair_key] = canonical_json_bytes(outer_factors)

    directions: dict[bytes, set[bool]] = defaultdict(set)
    counts: Counter[bytes] = Counter()
    for pair_key, pair in pair_groups.items():
        if set(pair) != {control_id, treatment_id}:
            return False
        control_first = (
            positions[pair[control_id].plan.manifest.metadata.run_id]
            < positions[pair[treatment_id].plan.manifest.metadata.run_id]
        )
        outer = outer_keys[pair_key]
        directions[outer].add(control_first)
        counts[outer] += 1
    return bool(directions) and all(
        counts[key] >= 2 and values == {False, True}
        for key, values in directions.items()
    )


def _score(item: CompletedRun) -> float | None:
    evidence = item.case.bundle.verifier_evidence
    return None if evidence is None else evidence.score


def _evaluate_claim(
    *,
    experiment_id: str,
    experiment_digest: str,
    completed: Iterable[CompletedRun],
    expected_run_count: int,
    control_id: str,
    treatment_id: str,
    deterministic_conformance: bool,
    counterbalanced: bool,
) -> ClaimReport:
    """Evaluate independent gates and derive claim eligibility."""

    items = list(completed)
    statuses = [item.case.bundle.status for item in items]
    execution_errors = [status.value for status in statuses if status not in _EXECUTION_VALID]
    execution_gate = (
        _valid("all cases produced contractual, verifiable outputs")
        if not execution_errors and len(items) == expected_run_count
        else _invalid(
            "execution-invalid statuses or missing cases: "
            + ", ".join(execution_errors or [f"{len(items)}/{expected_run_count} complete"])
        )
    )

    protocol_reasons: list[str] = []
    if len(items) != expected_run_count:
        protocol_reasons.append("not all planned runs terminated")
    for item in items:
        protocol = item.plan.manifest.execution.protocol
        if protocol is None:
            protocol_reasons.append("resolved protocol missing")
        elif protocol.state_reset != "per_case":
            protocol_reasons.append("fake conformance requires state_reset=per_case")
    protocol_gate = (
        _invalid("; ".join(sorted(set(protocol_reasons))))
        if protocol_reasons
        else _valid("resolved schedule and reset policy match the plan")
    )

    # Compiler enforcement is a precondition for reaching execution. Recheck
    # immutable provenance digests so bypassed/corrupt evidence cannot pass.
    isolation_errors: list[str] = []
    for item in items:
        provenance = item.case.bundle.provenance
        if provenance.manifest_digest != item.plan.manifest_digest:
            isolation_errors.append(f"{item.case.case_id}: manifest digest drift")
        if provenance.backend_digest != item.plan.manifest.execution.backend.digest:
            isolation_errors.append(f"{item.case.case_id}: backend digest drift")
        if provenance.benchmark_digest != item.plan.manifest.benchmark.artifact_digest:
            isolation_errors.append(f"{item.case.case_id}: benchmark digest drift")
        if provenance.subject_digest != item.plan.manifest.subject.artifact_digest:
            isolation_errors.append(f"{item.case.case_id}: subject digest drift")
    isolation_gate = (
        _invalid("; ".join(isolation_errors))
        if isolation_errors
        else _valid("allowed-diff compile check and evidence provenance agree")
    )

    scoring_errors: list[str] = []
    for item in items:
        bundle = item.case.bundle
        if bundle.status == RunStatus.verifier_error:
            scoring_errors.append(f"{bundle.run_id}: verifier failed")
        elif bundle.status == RunStatus.unsupported:
            scoring_errors.append(f"{bundle.run_id}: unsupported scoring semantics")
        elif bundle.status in _EXECUTION_VALID:
            if bundle.verifier_evidence is None or bundle.verifier_evidence.score is None:
                scoring_errors.append(f"{bundle.run_id}: verifier evidence missing")
            elif bundle.status == RunStatus.pass_ and not bundle.verifier_evidence.passed:
                scoring_errors.append(f"{bundle.run_id}: pass status contradicts verifier")
            elif (
                bundle.status == RunStatus.verified_fail
                and bundle.verifier_evidence.passed
            ):
                scoring_errors.append(
                    f"{bundle.run_id}: verified_fail status contradicts verifier"
                )
            elif bundle.verifier_evidence.details.get("scoring_semantics_declared") is False:
                scoring_errors.append(
                    f"{bundle.run_id}: authoritative verifier semantics are undeclared"
                )
    scoring_gate = (
        _invalid("; ".join(scoring_errors))
        if scoring_errors
        else _valid("every verifiable output has exact-verifier evidence")
    )

    pairs, pair_error = _subject_pairs(items, control_id, treatment_id)
    statistics_reasons: list[str] = []
    if pair_error:
        statistics_reasons.append(pair_error)
    if deterministic_conformance:
        if not counterbalanced:
            statistics_reasons.append("counterbalance metadata is false")
        elif not _counterbalance_is_valid(items, control_id, treatment_id):
            statistics_reasons.append("observed control/treatment order is not counterbalanced")
        if len(items) != expected_run_count:
            statistics_reasons.append("not all repetitions are terminal")
    else:
        statistics_reasons.append(
            "full real-experiment statistics are not implemented by the fake gate"
        )
    statistics_gate = (
        _invalid("; ".join(statistics_reasons))
        if statistics_reasons
        else _valid("deterministic conformance pairing and counterbalance are complete")
    )

    gates = {
        GateName.execution_valid: execution_gate,
        GateName.protocol_valid: protocol_gate,
        GateName.isolation_valid: isolation_gate,
        GateName.scoring_valid: scoring_gate,
        GateName.statistics_valid: statistics_gate,
    }
    effect = None
    differences: list[float] = []
    for control, treatment in pairs:
        control_score = _score(control)
        treatment_score = _score(treatment)
        if control_score is None or treatment_score is None:
            differences = []
            break
        differences.append(treatment_score - control_score)
    if differences:
        point = mean(differences)
        effect = EffectEstimate(
            metric="exact_match",
            point_estimate=point,
            confidence_interval=(min(differences), max(differences)),
            n_runs=len(items),
            n_pairs=len(pairs),
        )

    counts = Counter(statuses)
    lineage = tuple(
        LineageRef(
            run_id=item.case.bundle.run_id,
            case_id=item.case.case_id,
            evidence_bundle_sha256=item.case.bundle_digest,
        )
        for item in items
    )
    return ClaimReport(
        purpose=RunPurpose.claim,
        experiment_id=experiment_id,
        manifest_digest=experiment_digest,
        gates=gates,
        effect=effect,
        failure_breakdown=dict(counts),
        lineage=lineage,
    )


def evaluate_run_report(
    *,
    experiment_id: str,
    experiment_digest: str,
    completed: Iterable[CompletedRun],
    expected_run_count: int,
    control_id: str,
    treatment_id: str,
    deterministic_conformance: bool,
    counterbalanced: bool,
) -> RunReport:
    """Build the report type structurally required by the declared purpose."""

    items = list(completed)
    if not items:
        raise ValueError("cannot report an experiment with no completed runs")
    purposes = {item.plan.manifest.claim_design.purpose for item in items}
    if len(purposes) != 1:
        raise ValueError("run purpose must be invariant across an experiment")
    purpose = next(iter(purposes))
    if purpose == RunPurpose.claim:
        return _evaluate_claim(
            experiment_id=experiment_id,
            experiment_digest=experiment_digest,
            completed=items,
            expected_run_count=expected_run_count,
            control_id=control_id,
            treatment_id=treatment_id,
            deterministic_conformance=deterministic_conformance,
            counterbalanced=counterbalanced,
        )

    statuses = [item.case.bundle.status for item in items]
    scores = [score for item in items if (score := _score(item)) is not None]
    observations = (
        Observation(metric="exact_match", value=mean(scores), n_runs=len(scores)),
    ) if scores else ()
    lineage = tuple(
        LineageRef(
            run_id=item.case.bundle.run_id,
            case_id=item.case.case_id,
            evidence_bundle_sha256=item.case.bundle_digest,
        )
        for item in items
    )
    return ObservationReport(
        purpose=RunPurpose.exploratory,
        experiment_id=experiment_id,
        manifest_digest=experiment_digest,
        observations=observations,
        failure_breakdown=dict(Counter(statuses)),
        lineage=lineage,
    )


__all__ = ["CompletedRun", "evaluate_run_report"]
