"""Claim gate evaluation for completed BMP runs."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from math import isclose
from pathlib import Path
from statistics import mean
from typing import Iterable, Mapping

from MagentaBench.schemas import (
    ClaimReport,
    EffectEstimate,
    EvidenceBundle,
    Observation,
    ObservationReport,
    GateName,
    GateResult,
    LineageRef,
    RunPurpose,
    RunReport,
    RunStatus,
    ScheduleActivationReceipt,
    canonical_digest,
)

from .backend.fake import CaseExecution
from .compiler import CompiledRun, canonical_json_bytes, sha256_bytes
from .evidence import artifact_ref, sha256_file


@dataclass(frozen=True)
class CompletedRun:
    plan: CompiledRun
    case: CaseExecution
    schedule_receipt: ScheduleActivationReceipt | None = None
    schedule_receipt_path: Path | None = None
    schedule_receipt_sha256: str | None = None
    scheduler_digest: str | None = None
    pipeline_digest: str | None = None
    runner_digest: str | None = None


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


def _exploratory_metric_scores(
    items: Iterable[CompletedRun],
) -> tuple[str, tuple[float, ...]]:
    runs = tuple(items)
    metrics = {
        item.plan.manifest.benchmark.authoritative_reward_metric for item in runs
    }
    if len(metrics) != 1:
        raise ValueError(
            "exploratory authoritative reward metric differs across included runs"
        )
    metric = next(iter(metrics))
    scores: list[float] = []
    for item in runs:
        bundle = item.case.bundle
        evidence = bundle.verifier_evidence
        if evidence is None:
            if bundle.status in _EXECUTION_VALID:
                raise ValueError(
                    f"{item.case.case_id}: execution-valid bundle lacks verifier evidence"
                )
            continue
        if evidence.score is None:
            if bundle.status in _EXECUTION_VALID:
                raise ValueError(
                    f"{item.case.case_id}: execution-valid bundle lacks verifier score"
                )
            if evidence.metrics:
                raise ValueError(
                    f"{item.case.case_id}: named verifier metrics exist without score"
                )
            continue
        if not evidence.metrics:
            raise ValueError(
                f"{item.case.case_id}: verifier metrics are empty for scored evidence"
            )
        if metric not in evidence.metrics:
            raise ValueError(
                f"{item.case.case_id}: authoritative reward metric {metric!r} "
                "is missing from verifier evidence"
            )
        named_score = evidence.metrics[metric]
        if named_score != evidence.score:
            raise ValueError(
                f"{item.case.case_id}: authoritative reward metric {metric!r} "
                "disagrees with verifier score"
            )
        scores.append(named_score)
    return metric, tuple(scores)


def _receipt_binding_errors(item: CompletedRun) -> list[str]:
    receipt = item.schedule_receipt
    if receipt is None:
        return ["ScheduleActivationReceipt missing"]
    manifest = item.plan.manifest
    errors: list[str] = []
    protocol = manifest.execution.protocol
    if protocol is None:
        return ["resolved protocol missing"]
    effective_selection = protocol.candidate_selection
    if receipt.run_id != manifest.metadata.run_id:
        errors.append("schedule run_id does not match manifest")
    if receipt.protocol_digest != canonical_digest(protocol):
        errors.append("schedule protocol_digest does not match resolved protocol")
    if not receipt.schedule_valid:
        reasons = "; ".join(receipt.mismatch_reasons) or "unspecified mismatch"
        errors.append(f"schedule receipt is invalid: {reasons}")
    if receipt.declared_rollouts_per_case != protocol.rollouts_per_case:
        errors.append("declared rollouts do not match resolved protocol")
    if receipt.declared_parallelism != protocol.parallelism:
        errors.append("declared parallelism does not match resolved protocol")
    if receipt.declared_case_order != protocol.case_order:
        errors.append("declared case_order does not match resolved protocol")
    if receipt.declared_state_reset != protocol.state_reset:
        errors.append("declared state_reset does not match resolved protocol")
    if receipt.declared_candidate_selection != effective_selection:
        errors.append("declared candidate_selection does not match resolved protocol")
    if receipt.declared_checkpoint_policy != protocol.checkpoint_policy:
        errors.append("declared checkpoint_policy does not match resolved protocol")
    if receipt.order_seed != manifest.execution.seed:
        errors.append("schedule order_seed does not match execution seed")
    if receipt.scheduler_digest != item.scheduler_digest:
        errors.append("schedule scheduler_digest does not match active scheduler")
    if receipt.pipeline_digest != item.pipeline_digest:
        errors.append("schedule pipeline_digest does not match active pipeline")
    allocated_case_ids = tuple(
        allocation.case_id
        for allocation in receipt.budget_ledger.case_allocations
    )
    if receipt.observed_attempt_count != len(receipt.attempts):
        errors.append("observed attempt count does not match attempt executions")
    launched_case_ids = {attempt.case_id for attempt in receipt.attempts}
    expected_resets = {
        "never": 0,
        "per_case": len(launched_case_ids),
        "per_rollout": len(receipt.attempts),
    }[protocol.state_reset]
    if receipt.observed_state_reset_count != expected_resets:
        errors.append("observed state reset count does not match launched attempts")
    if receipt.observed_selection_policy != effective_selection:
        errors.append("observed candidate selection does not match resolved protocol")
    if receipt.observed_case_order != allocated_case_ids:
        errors.append("observed_case_order does not match case allocations")

    budget = manifest.execution.budget
    for field in ("max_tokens", "max_cost"):
        declared = getattr(budget, field)
        allocated = [
            getattr(allocation.allocated, field)
            for allocation in receipt.budget_ledger.case_allocations
        ]
        if declared is None:
            if any(value is not None for value in allocated):
                errors.append(f"root {field} allocation exceeds unbounded declaration")
            continue
        if any(value is None for value in allocated):
            errors.append(f"root {field} allocation is incomplete")
            continue
        observed = sum(allocated)
        equal = (
            observed == declared
            if field == "max_tokens"
            else isclose(observed, declared, rel_tol=0.0, abs_tol=1e-12)
        )
        if not equal:
            errors.append(f"root {field} allocations do not partition execution budget")

    loaded_attempts: dict[str, EvidenceBundle] = {}
    for attempt in receipt.attempts:
        ref = attempt.evidence_bundle_ref
        if ref is None:
            errors.append(f"{attempt.attempt_id}: evidence bundle reference missing")
            continue
        path = Path(ref.path)
        if (
            not path.is_file()
            or path.stat().st_size != ref.size_bytes
            or sha256_file(path) != ref.sha256
        ):
            errors.append(f"{attempt.attempt_id}: evidence bundle reference drift")
            continue
        try:
            bundle = EvidenceBundle.model_validate_json(path.read_bytes())
        except ValueError:
            errors.append(f"{attempt.attempt_id}: evidence bundle malformed")
            continue
        loaded_attempts[attempt.attempt_id] = bundle
        if bundle.run_id != attempt.attempt_id:
            errors.append(f"{attempt.attempt_id}: child_run_id binding drift")
        if attempt.debit is None or attempt.debit.child_run_id != bundle.run_id:
            errors.append(f"{attempt.attempt_id}: budget debit child_run_id drift")
        elif attempt.debit.spent != bundle.usage:
            errors.append(f"{attempt.attempt_id}: budget debit usage drift")
        expected_status = (
            RunStatus.agent_error
            if attempt.debit is not None and attempt.debit.budget_exceeded
            else bundle.status
        )
        if attempt.status != expected_status:
            errors.append(f"{attempt.attempt_id}: attempt status drift")
        reward_metric = manifest.benchmark.authoritative_reward_metric
        score = (
            None
            if bundle.verifier_evidence is None
            else bundle.verifier_evidence.metrics.get(reward_metric)
        )
        if (
            attempt.reward_value != score
            or attempt.reward_metric != (reward_metric if score is not None else None)
        ):
            errors.append(f"{attempt.attempt_id}: authoritative reward drift")

    for case_id in allocated_case_ids:
        candidates = [item for item in receipt.attempts if item.case_id == case_id]
        if not candidates:
            continue
        if effective_selection == "best_of_n":
            scored = [item for item in candidates if item.reward_value is not None]
            expected_winner = (
                None
                if not scored
                else max(
                    scored,
                    key=lambda item: (item.reward_value, -item.attempt_index),
                ).attempt_id
            )
        else:
            expected_winner = min(
                candidates, key=lambda item: item.attempt_index
            ).attempt_id
        selected_ids = [item.attempt_id for item in candidates if item.selected]
        if selected_ids != ([expected_winner] if expected_winner is not None else []):
            errors.append(f"{case_id}: candidate selection lineage drift")

    if item.schedule_receipt_path is None:
        errors.append("schedule receipt path is missing")
        receipt_path = None
    else:
        receipt_path = Path(item.schedule_receipt_path)
    if receipt_path is not None and not receipt_path.is_file():
        errors.append("schedule receipt path is missing")
    elif receipt_path is not None:
        persisted_digest = sha256_file(receipt_path)
        if persisted_digest != item.schedule_receipt_sha256:
            errors.append("persisted schedule receipt digest drift")
        try:
            persisted_receipt = ScheduleActivationReceipt.model_validate_json(
                receipt_path.read_bytes()
            )
        except ValueError:
            errors.append("persisted schedule receipt is malformed")
        else:
            if persisted_receipt != receipt:
                errors.append("persisted schedule receipt differs from in-memory receipt")
    expected_receipt_sha = sha256_bytes(
        canonical_json_bytes(receipt) + b"\n"
    )
    if item.schedule_receipt_sha256 != expected_receipt_sha:
        errors.append("schedule receipt digest does not match receipt content")
    return errors


def _evidence_integrity_errors(item: CompletedRun) -> list[str]:
    errors: list[str] = []
    manifest = item.plan.manifest
    provenance = item.case.bundle.provenance
    if provenance.runner_digest != item.runner_digest:
        errors.append("runner_digest does not match active backend")
    if provenance.manifest_digest != item.plan.manifest_digest:
        errors.append("manifest digest drift")
    expected_backend_digest = manifest.execution.backend.digest or item.runner_digest
    if provenance.backend_digest != expected_backend_digest:
        errors.append("backend digest drift")
    if provenance.benchmark_digest != manifest.benchmark.artifact_digest:
        errors.append("benchmark digest drift")
    if provenance.subject_digest != manifest.subject.artifact_digest:
        errors.append("subject digest drift")
    environment_spec = manifest.execution.backend.environment
    environment_receipt = provenance.environment_receipt
    if environment_spec is not None:
        if environment_receipt is None:
            errors.append("EnvironmentReceipt missing")
        elif environment_receipt.spec_digest != canonical_digest(environment_spec):
            errors.append("environment spec_digest drift")
    elif environment_receipt is not None:
        errors.append("undeclared EnvironmentReceipt")
    bundle_path = Path(item.case.bundle_path)
    if not bundle_path.is_file():
        errors.append("evidence bundle path is missing")
    else:
        if sha256_file(bundle_path) != item.case.bundle_digest:
            errors.append("evidence bundle digest does not match bundle bytes")
        try:
            persisted_bundle = EvidenceBundle.model_validate_json(
                bundle_path.read_bytes()
            )
        except ValueError:
            errors.append("persisted evidence bundle is malformed")
        else:
            if persisted_bundle != item.case.bundle:
                errors.append("persisted evidence bundle differs from in-memory bundle")
    bundle = item.case.bundle
    refs = [
        *bundle.output_refs,
        *bundle.log_refs,
    ]
    if bundle.trace_ref is not None:
        refs.append(bundle.trace_ref)
    if bundle.checkpoint_ref is not None:
        refs.append(bundle.checkpoint_ref)
    network_observation = bundle.network_observation
    if network_observation is not None:
        refs.extend(network_observation.evidence_refs)
    provenance = bundle.provenance
    if provenance.container_receipt_ref is not None:
        refs.append(provenance.container_receipt_ref)
    verifier = bundle.verifier_evidence
    if verifier is not None:
        refs.extend(verifier.artifact_refs)
    for ref in refs:
        path = Path(ref.path)
        if (
            not path.is_file()
            or path.stat().st_size != ref.size_bytes
            or sha256_file(path) != ref.sha256
        ):
            errors.append(f"artifact reference digest drift: {ref.path}")

    backend = item.plan.manifest.execution.backend
    if backend.adapter == "subprocess":
        if backend.executable is None or provenance.executable_digest is None:
            errors.append("subprocess executable digest missing")
        else:
            executable = Path(backend.executable)
            if (
                not executable.is_file()
                or sha256_file(executable) != provenance.executable_digest
                or provenance.executable_digest != backend.digest
            ):
                errors.append("subprocess executable digest drift")
    if provenance.backend_kind == "docker":
        ref = provenance.container_receipt_ref
        if ref is None:
            errors.append("container receipt reference missing")
        else:
            try:
                receipt = json.loads(Path(ref.path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                errors.append("container receipt is unreadable")
            else:
                if receipt.get("image_id") != provenance.image_digest:
                    errors.append("container image digest cross-link drift")
                if (
                    receipt.get("agent_executable_sha256")
                    != provenance.executable_digest
                ):
                    errors.append("container executable digest cross-link drift")
    return errors


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
        receipt = item.schedule_receipt
        binding_errors = _receipt_binding_errors(item)
        protocol_reasons.extend(
            f"{item.plan.manifest.metadata.run_id}: {reason}"
            for reason in binding_errors
        )
    protocol_gate = (
        _invalid("; ".join(sorted(set(protocol_reasons))))
        if protocol_reasons
        else _valid("resolved schedule and reset policy match the plan")
    )

    # Compiler enforcement is a precondition for reaching execution. Recheck
    # immutable provenance digests so bypassed/corrupt evidence cannot pass.
    isolation_errors: list[str] = []
    positive_isolation_observations = 0
    for item in items:
        bundle = item.case.bundle
        isolation_errors.extend(_evidence_integrity_errors(item))
        network_observation = bundle.network_observation
        if network_observation is None:
            isolation_errors.append(
                f"{item.case.case_id}: NetworkObservation missing"
            )
        elif not network_observation.claim_isolation_valid:
            isolation_errors.append(
                f"{item.case.case_id}: NetworkObservation cannot substantiate isolation"
            )
        else:
            positive_isolation_observations += 1
    if isolation_errors:
        isolation_gate = _invalid("; ".join(isolation_errors))
    elif len(items) != expected_run_count:
        # Isolation over a partial plan proves nothing about the missing runs.
        isolation_gate = _invalid(
            f"isolation is incomplete: {len(items)} of {expected_run_count} "
            "planned runs are present"
        )
    else:
        isolation_gate = _valid(
            "positive isolation observations and evidence provenance agree "
            f"({positive_isolation_observations}/{expected_run_count} verified)"
        )

    scoring_errors: list[str] = []
    scored_bundles = 0
    for item in items:
        bundle = item.case.bundle
        if bundle.status == RunStatus.verifier_error:
            scoring_errors.append(f"{bundle.run_id}: verifier failed")
        elif bundle.status == RunStatus.unsupported:
            scoring_errors.append(f"{bundle.run_id}: unsupported scoring semantics")
        elif bundle.status in _EXECUTION_VALID:
            scored_bundles += 1
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
    if scoring_errors:
        scoring_gate = _invalid("; ".join(scoring_errors))
    elif len(items) != expected_run_count:
        # Scoring cannot be complete over a partial plan: a single scored bundle
        # in a plan that is missing runs would otherwise report agreement.
        scoring_gate = _invalid(
            f"scoring is incomplete: {len(items)} of {expected_run_count} "
            "planned runs are present"
        )
    elif scored_bundles == 0:
        # No bundle reached an execution-valid status, so every scoring branch
        # above was skipped. Reporting agreement here would assert verification
        # over an empty set; state that nothing was scored instead.
        scoring_gate = _invalid(
            "no execution-valid bundle was scored; scoring is unverified"
        )
    else:
        scoring_gate = _valid(
            f"every verifiable output has exact-verifier evidence "
            f"({scored_bundles}/{len(items)} scored)"
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
        missing_scores = sum(1 for item in items if _score(item) is None)
        if missing_scores:
            statistics_reasons.append(
                f"statistics require verifier scores for every run; "
                f"{missing_scores} of {len(items)} are missing"
            )
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
            evidence_bundle_ref=artifact_ref(item.case.bundle_path),
            schedule_receipt_ref=artifact_ref(item.schedule_receipt_path),
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
    expected_run_ids: tuple[str, ...],
    control_id: str,
    treatment_id: str,
    deterministic_conformance: bool,
    counterbalanced: bool,
) -> RunReport:
    """Build the report type structurally required by the declared purpose."""

    items = list(completed)
    if not items:
        raise ValueError("cannot report an experiment with no completed runs")
    expected_duplicates = sorted(
        run_id for run_id, count in Counter(expected_run_ids).items() if count > 1
    )
    if expected_duplicates:
        raise ValueError(
            f"expected plan contains duplicate run ids: {expected_duplicates}"
        )
    observed_run_ids = tuple(
        item.plan.manifest.metadata.run_id for item in items
    )
    observed_duplicates = sorted(
        run_id for run_id, count in Counter(observed_run_ids).items() if count > 1
    )
    expected_set = set(expected_run_ids)
    observed_set = set(observed_run_ids)
    missing = sorted(expected_set - observed_set)
    unexpected = sorted(observed_set - expected_set)
    if observed_duplicates or missing or unexpected:
        raise ValueError(
            "report plan coverage mismatch: "
            f"{len(items)} of {len(expected_run_ids)} planned runs present; "
            f"missing={missing}; unexpected={unexpected}; "
            f"duplicates={observed_duplicates}"
        )
    expected_run_count = len(expected_run_ids)
    if any(
        item.plan.manifest.metadata.test_override is not None
        or item.case.bundle.provenance.test_override is not None
        for item in items
    ):
        raise ValueError("test override evidence cannot produce a run report")
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

    integrity_errors = [
        error
        for item in items
        for error in (
            *_evidence_integrity_errors(item),
            *_receipt_binding_errors(item),
        )
    ]
    if integrity_errors:
        raise ValueError(
            "exploratory evidence integrity failed: "
            + "; ".join(integrity_errors)
        )
    statuses = [item.case.bundle.status for item in items]
    metric, scores = _exploratory_metric_scores(items)
    observations = (
        Observation(metric=metric, value=mean(scores), n_runs=len(scores)),
    ) if scores else ()
    lineage = tuple(
        LineageRef(
            run_id=item.case.bundle.run_id,
            case_id=item.case.case_id,
            evidence_bundle_ref=artifact_ref(item.case.bundle_path),
            schedule_receipt_ref=artifact_ref(item.schedule_receipt_path),
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
