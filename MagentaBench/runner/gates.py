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
    ArtifactRef,
    CaseSetActivationReceipt,
    CaseSetArtifact,
    ClaimReport,
    EffectEstimate,
    EvidenceBundle,
    EvolutionRunEvidence,
    Observation,
    ObservationReport,
    GateName,
    GateResult,
    LineageRef,
    MetricResult,
    NetworkBoundary,
    NetworkPolicySource,
    RunPurpose,
    RunReport,
    RunStatus,
    RolloutTrajectory,
    ScheduleActivationReceipt,
    StatisticalAnalysisReceipt,
    canonical_digest,
)
from MagentaBench.schemas.models import ComparisonKind, SubjectKind
from MagentaBench.schemas.model_activation import replay_model_activation_receipt
from MagentaBench.schemas.statistics import (
    PairedScore,
    analyze_paired_scores,
    benchmark_evaluation_split,
)

from .backend.fake import CaseExecution
from .compiler import CompiledRun, canonical_json_bytes, sha256_bytes
from .case_order import (
    CaseOrderError,
    custom_order_binding,
    selected_case_ids as resolve_selected_case_ids,
)
from .evidence import artifact_ref, sha256_file, source_closure_digest
from MagentaBench.schemas.metrics import compute_metric_results


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
    case_set_receipt: CaseSetActivationReceipt | None = None
    case_set_receipt_path: Path | None = None
    case_set_receipt_sha256: str | None = None
    case_set_digest: str | None = None


_EXECUTION_VALID = frozenset({RunStatus.pass_, RunStatus.verified_fail})


def _valid(
    reason: str | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> GateResult:
    return GateResult(
        valid=True,
        reason=reason,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
    )


def _invalid(reason: str) -> GateResult:
    return GateResult(valid=False, reason=reason)


def _contrast_arm_key(
    item: CompletedRun,
    *,
    factor_path: str | None,
) -> str | bytes:
    if factor_path is None:
        return item.plan.manifest.subject.id
    if factor_path not in item.plan.factor_values:
        return b"__missing_factor__"
    return canonical_json_bytes(item.plan.factor_values[factor_path])


def _contrast_expected_keys(
    control_id: str,
    treatment_id: str,
    *,
    factor_path: str | None,
    control_value: object | None,
    treatment_value: object | None,
) -> tuple[str | bytes, str | bytes]:
    if factor_path is None:
        return control_id, treatment_id
    return canonical_json_bytes(control_value), canonical_json_bytes(treatment_value)


def _subject_pairs(
    completed: list[CompletedRun],
    control_id: str,
    treatment_id: str,
    *,
    factor_path: str | None = None,
    control_value: object | None = None,
    treatment_value: object | None = None,
) -> tuple[list[tuple[CompletedRun, CompletedRun]], str | None]:
    control_key, treatment_key = _contrast_expected_keys(
        control_id,
        treatment_id,
        factor_path=factor_path,
        control_value=control_value,
        treatment_value=treatment_value,
    )
    grouped: dict[bytes, dict[str | bytes, CompletedRun]] = defaultdict(dict)
    from .compiler import canonical_json_bytes

    for item in completed:
        factors = {
            key: value
            for key, value in item.plan.factor_values.items()
            if key
            not in {
                "subject",
                "experiment.subject",
                "order_position",
                factor_path,
            }
        }
        # A parent run can contain several selected benchmark cases.  Include
        # the case identity in pairing so control/treatment scores never cross
        # case boundaries.
        factors["__case_id"] = item.case.case_id
        arm = _contrast_arm_key(item, factor_path=factor_path)
        grouped[canonical_json_bytes(factors)][arm] = item
    pairs: list[tuple[CompletedRun, CompletedRun]] = []
    for pair in grouped.values():
        if set(pair) != {control_key, treatment_key}:
            return [], "paired control/treatment structure is incomplete"
        pairs.append((pair[control_key], pair[treatment_key]))
    if not pairs:
        return [], "no paired control/treatment runs"
    return pairs, None


def _counterbalance_is_valid(
    completed: list[CompletedRun],
    control_id: str,
    treatment_id: str,
    *,
    factor_path: str | None = None,
    control_value: object | None = None,
    treatment_value: object | None = None,
) -> bool:
    from .compiler import canonical_json_bytes

    positions = {
        (item.plan.manifest.metadata.run_id, item.case.case_id): index
        for index, item in enumerate(completed)
    }
    control_key, treatment_key = _contrast_expected_keys(
        control_id,
        treatment_id,
        factor_path=factor_path,
        control_value=control_value,
        treatment_value=treatment_value,
    )
    pair_groups: dict[bytes, dict[str | bytes, CompletedRun]] = defaultdict(dict)
    outer_keys: dict[bytes, bytes] = {}
    for item in completed:
        factors = dict(item.plan.factor_values)
        pair_factors = {
            key: value
            for key, value in factors.items()
            if key
            not in {
                "subject",
                "experiment.subject",
                "order_position",
                factor_path,
            }
        }
        outer_factors = {
            key: value
            for key, value in pair_factors.items()
            if key not in {"repetition", "__case_id"}
        }
        pair_factors["__case_id"] = item.case.case_id
        pair_key = canonical_json_bytes(pair_factors)
        pair_groups[pair_key][_contrast_arm_key(item, factor_path=factor_path)] = item
        outer_keys[pair_key] = canonical_json_bytes(outer_factors)

    directions: dict[bytes, set[bool]] = defaultdict(set)
    counts: Counter[bytes] = Counter()
    for pair_key, pair in pair_groups.items():
        if set(pair) != {control_key, treatment_key}:
            return False
        control_first = (
            positions[(
            pair[control_key].plan.manifest.metadata.run_id,
                pair[control_key].case.case_id,
            )]
            < positions[(
            pair[treatment_key].plan.manifest.metadata.run_id,
                pair[treatment_key].case.case_id,
            )]
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


def _report_subject_identity(
    items: Iterable[CompletedRun],
) -> tuple[ComparisonKind | None, tuple[SubjectKind, ...]]:
    """Derive semantic comparison kind and all observed packaging kinds."""

    runs = tuple(items)
    comparison_kinds = {
        item.plan.manifest.claim_design.comparison_kind for item in runs
    }
    if len(comparison_kinds) != 1:
        raise ValueError(
            "comparison kind must be invariant across an experiment"
        )
    raw_kinds = tuple(
        sorted(
            {SubjectKind(item.plan.manifest.subject.kind) for item in runs},
            key=lambda value: value.value,
        )
    )
    return next(iter(comparison_kinds)), raw_kinds


def _report_identity(
    items: Iterable[CompletedRun],
) -> tuple[str, str, ComparisonKind | None, tuple[SubjectKind, ...]]:
    """Derive experiment identity fields from the completed run manifests."""

    runs = tuple(items)
    if not runs:
        raise ValueError("cannot derive report identity from no completed runs")
    experiment_ids = {item.plan.manifest.metadata.experiment_id for item in runs}
    if len(experiment_ids) != 1:
        raise ValueError(
            "experiment id must be invariant across runs: "
            + ", ".join(sorted(experiment_ids))
        )
    # RecordIndex stores one immutable manifest per parent run.  Deduplicate
    # here so multi-case reports remain byte-bound to that index.
    manifest_digests = list(dict.fromkeys(item.plan.manifest_digest for item in runs))
    experiment_digest = sha256_bytes(canonical_json_bytes(manifest_digests))
    comparison_kind, subject_kinds = _report_subject_identity(runs)
    return (
        next(iter(experiment_ids)),
        experiment_digest,
        comparison_kind,
        subject_kinds,
    )


def _report_contrast(
    items: Iterable[CompletedRun],
) -> tuple[str, str, bool, bool, str | None, object | None, object | None]:
    """Derive arm and protocol flags from the resolved manifests."""

    runs = tuple(items)
    contrasts = {canonical_json_bytes(item.plan.manifest.contrast) for item in runs}
    if len(contrasts) != 1:
        raise ValueError("experiment contrast must be invariant across runs")
    contrast = runs[0].plan.manifest.contrast
    if (
        contrast.mode != "one_factor"
        or contrast.factor_id is None
        or contrast.control_level is None
        or contrast.treatment_level is None
    ):
        raise ValueError("claim report requires a registered one-factor contrast")
    factor_artifacts = {
        artifact.factor.id: artifact
        for artifact in runs[0].plan.manifest.metadata.factor_artifacts
    }
    try:
        factor = factor_artifacts[contrast.factor_id].factor
        control_value = factor.level(contrast.control_level).value
        treatment_value = factor.level(contrast.treatment_level).value
    except (KeyError, ValueError) as exc:
        raise ValueError("claim contrast factor registry cannot be replayed") from exc
    protocols = [item.plan.manifest.execution.protocol for item in runs]
    protocol_digests = {
        None if protocol is None else canonical_digest(protocol)
        for protocol in protocols
    }
    if len(protocol_digests) != 1:
        raise ValueError("resolved protocol must be invariant across runs")
    protocol = protocols[0]
    deterministic = bool(
        protocol is not None and getattr(protocol, "deterministic_conformance", False)
    )
    return (
        contrast.control_level,
        contrast.treatment_level,
        deterministic,
        contrast.counterbalanced,
        factor.selector_path,
        control_value,
        treatment_value,
    )


def _lineage_ref(item: CompletedRun) -> LineageRef:
    """Build a parent-run/selected-attempt lineage binding from verified state."""

    receipt = item.schedule_receipt
    if receipt is None:
        raise ValueError(f"{item.case.case_id}: schedule receipt is required for lineage")
    if item.schedule_receipt_path is None:
        raise ValueError(f"{item.case.case_id}: schedule receipt path is required for lineage")
    if item.case_set_receipt_path is None:
        raise ValueError(f"{item.case.case_id}: case-set receipt path is required for lineage")
    parent_run_id = receipt.run_id
    attempt_id = item.case.bundle.run_id
    matching = [
        attempt
        for attempt in receipt.attempts
        if attempt.attempt_id == attempt_id and attempt.case_id == item.case.case_id
    ]
    if len(matching) != 1:
        raise ValueError(
            f"{item.case.case_id}: selected attempt {attempt_id!r} is not uniquely "
            "bound by the schedule receipt"
        )
    if not matching[0].selected:
        raise ValueError(
            f"{item.case.case_id}: report lineage attempt {attempt_id!r} is not selected"
        )
    return LineageRef(
        run_id=parent_run_id,
        attempt_id=attempt_id,
        case_id=item.case.case_id,
        evidence_bundle_ref=artifact_ref(item.case.bundle_path),
        schedule_receipt_ref=artifact_ref(item.schedule_receipt_path),
        case_set_receipt_ref=artifact_ref(item.case_set_receipt_path),
    )


def _exploratory_metric_scores(
    items: Iterable[CompletedRun],
) -> tuple[str, tuple[float, ...]]:
    runs = tuple(items)
    metrics = {
        item.plan.manifest.authoritative_reward_metric for item in runs
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


def _receipt_binding_errors(
    item: CompletedRun,
    *,
    allowed_invalid_schedule_reasons: frozenset[str] = frozenset(),
) -> list[str]:
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
    if not receipt.schedule_valid and (
        not receipt.mismatch_reasons
        or not set(receipt.mismatch_reasons).issubset(
            allowed_invalid_schedule_reasons
        )
    ):
        reasons = "; ".join(receipt.mismatch_reasons) or "unspecified mismatch"
        errors.append(f"schedule receipt is invalid: {reasons}")
    if receipt.declared_rollouts_per_case != protocol.rollouts_per_case:
        errors.append("declared rollouts do not match resolved protocol")
    if receipt.declared_parallelism != protocol.parallelism:
        errors.append("declared parallelism does not match resolved protocol")
    if receipt.declared_case_order != protocol.case_order:
        errors.append("declared case_order does not match resolved protocol")
    if protocol.case_order == "explicit":
        expected_case_ids = tuple(protocol.explicit_case_ids)
        if not expected_case_ids:
            errors.append("explicit case_order is missing explicit_case_ids")
        elif receipt.observed_case_order != expected_case_ids:
            errors.append("observed_case_order does not match explicit_case_ids")
    elif protocol.case_order == "custom":
        try:
            expected_case_ids = resolve_selected_case_ids(protocol)
        except CaseOrderError as exc:
            errors.append(str(exc))
        else:
            if receipt.observed_case_order != expected_case_ids:
                errors.append(
                    "observed_case_order does not match custom order artifact"
                )
    if receipt.declared_state_reset != protocol.state_reset:
        errors.append("declared state_reset does not match resolved protocol")
    if receipt.declared_candidate_selection != effective_selection:
        errors.append("declared candidate_selection does not match resolved protocol")
    if receipt.declared_checkpoint_policy != protocol.checkpoint_policy:
        errors.append("declared checkpoint_policy does not match resolved protocol")
    expected_order_seed = (
        manifest.execution.seed if protocol.case_order == "seeded_random" else None
    )
    if receipt.order_seed != expected_order_seed:
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

    save = receipt.checkpoint_save_ref
    if save is not None:
        save_path = Path(save.path)
        if (
            not save_path.is_file()
            or save_path.stat().st_size != save.size_bytes
            or sha256_file(save_path) != save.written_digest
        ):
            errors.append("checkpoint save artifact byte drift")
    load = receipt.checkpoint_load_ref
    ancestor_ref = receipt.ancestor_schedule_receipt_ref
    if load is not None and ancestor_ref is not None:
        ancestor_path = Path(ancestor_ref.path)
        if (
            not ancestor_path.is_file()
            or ancestor_path.stat().st_size != ancestor_ref.size_bytes
            or sha256_file(ancestor_path) != ancestor_ref.sha256
        ):
            errors.append("ancestor schedule receipt byte drift")
        else:
            try:
                ancestor = ScheduleActivationReceipt.model_validate_json(
                    ancestor_path.read_bytes()
                )
            except ValueError:
                errors.append("ancestor schedule receipt is malformed")
            else:
                ancestor_save = ancestor.checkpoint_save_ref
                if ancestor_save is None:
                    errors.append("ancestor checkpoint save receipt is missing")
                else:
                    ancestor_save_path = Path(ancestor_save.path)
                    if (
                        not ancestor_save_path.is_file()
                        or ancestor_save_path.stat().st_size
                        != ancestor_save.size_bytes
                        or sha256_file(ancestor_save_path)
                        != ancestor_save.written_digest
                    ):
                        errors.append("ancestor checkpoint save artifact byte drift")
                    if load.loaded_checkpoint_digest != ancestor_save.written_digest:
                        errors.append(
                            "loaded checkpoint digest does not match ancestor save artifact"
                        )
                ancestor_selected = {
                    attempt.evidence_bundle_ref.sha256
                    for attempt in ancestor.attempts
                    if attempt.selected and attempt.evidence_bundle_ref is not None
                }
                if not ancestor_selected.issubset(load.selected_bundle_digests):
                    errors.append(
                        "checkpoint load selected bundles omit ancestor selection"
                    )

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
        reward_metric = manifest.authoritative_reward_metric
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
            if not scored:
                expected_winner = min(
                    candidates, key=lambda item: item.attempt_index
                ).attempt_id
            else:
                direction = manifest.authoritative_metric_artifact.metric.direction
                selector = max if direction.value == "maximize" else min
                expected_winner = selector(
                    scored,
                    key=lambda attempt: (
                        attempt.reward_value,
                        -attempt.attempt_index
                        if direction.value == "maximize"
                        else attempt.attempt_index,
                    ),
                ).attempt_id
        else:
            expected_winner = min(
                candidates, key=lambda item: item.attempt_index
            ).attempt_id
        selected_ids = [item.attempt_id for item in candidates if item.selected]
        if selected_ids != [expected_winner]:
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


def _case_set_binding_errors(item: CompletedRun) -> list[str]:
    receipt = item.case_set_receipt
    if receipt is None:
        return ["CaseSetActivationReceipt missing"]
    errors: list[str] = []
    if item.case_set_receipt_path is None:
        errors.append("case-set receipt path is missing")
    else:
        receipt_path = Path(item.case_set_receipt_path)
        if not receipt_path.is_file():
            errors.append("case-set receipt path is missing")
        else:
            if sha256_file(receipt_path) != item.case_set_receipt_sha256:
                errors.append("persisted case-set receipt digest drift")
            try:
                persisted_receipt = CaseSetActivationReceipt.model_validate_json(
                    receipt_path.read_bytes()
                )
            except ValueError:
                errors.append("persisted case-set receipt is malformed")
            else:
                if persisted_receipt != receipt:
                    errors.append(
                        "persisted case-set receipt differs from in-memory receipt"
                    )
    artifact_path = Path(receipt.case_set_ref.path)
    if (
        not artifact_path.is_file()
        or artifact_path.stat().st_size != receipt.case_set_ref.size_bytes
        or sha256_file(artifact_path) != receipt.case_set_ref.sha256
    ):
        errors.append("case-set artifact byte drift")
        return errors
    try:
        artifact = CaseSetArtifact.model_validate_json(artifact_path.read_bytes())
    except ValueError:
        errors.append("case-set artifact is malformed")
        return errors
    benchmark = item.plan.manifest.benchmark
    dataset = item.plan.manifest.dataset
    if (
        artifact.benchmark_id != benchmark.id
        or artifact.benchmark_digest != benchmark.artifact_digest
    ):
        errors.append("case-set benchmark identity drift")
    if artifact.loader_adapter != benchmark.adapter:
        errors.append("case-set loader adapter does not match benchmark")
    if (
        artifact.dataset_id != dataset.id
        or artifact.dataset_digest != dataset.artifact_digest
    ):
        errors.append("case-set dataset identity drift")
    if artifact.canonical_digest() != receipt.case_set_digest:
        errors.append("case-set identity digest drift")
    if item.case_set_digest != receipt.case_set_digest:
        errors.append("CompletedRun case-set digest drift")
    if artifact.loader_adapter != receipt.loader_adapter:
        errors.append("case-set loader adapter drift")
    if artifact.loader_digest != receipt.loader_digest:
        errors.append("case-set loader digest drift")
    if artifact.ordered_case_ids != receipt.ordered_case_ids:
        errors.append("case-set activated order drift")
    protocol = item.plan.manifest.execution.protocol
    if protocol is None:
        errors.append("resolved protocol missing")
    else:
        if artifact.case_order != protocol.case_order:
            errors.append("case-set order policy drift")
        expected_case_set_seed = (
            item.plan.manifest.execution.seed
            if protocol.case_order == "seeded_random"
            else None
        )
        if artifact.order_seed != expected_case_set_seed:
            errors.append("case-set order seed drift")
    expected_selection_method = (
        {
            "custom": "custom_order_artifact",
            "explicit": "explicit_case_ids",
        }.get(protocol.case_order, "all_cases")
        if protocol is not None
        else "all_cases"
    )
    if artifact.selection_method != expected_selection_method:
        errors.append("case-set selection method drift")
    if protocol is not None and protocol.case_order == "explicit":
        if artifact.ordered_case_ids != tuple(protocol.explicit_case_ids):
            errors.append("case-set explicit case order drift")
    elif protocol is not None and protocol.case_order == "custom":
        try:
            expected_ids, expected_adapter, expected_ref = custom_order_binding(protocol)
        except CaseOrderError as exc:
            errors.append(str(exc))
        else:
            if artifact.ordered_case_ids != expected_ids:
                errors.append("case-set custom case order drift")
            if artifact.order_strategy_adapter != expected_adapter:
                errors.append("case-set custom order adapter drift")
            if artifact.order_strategy_ref != expected_ref:
                errors.append("case-set custom order content reference drift")
    source = dataset.source
    compiled_source_digest = dataset.source_content_digest
    try:
        observed_source_digest = (
            source_closure_digest(
                Path(source), artifact.source_content_refs
            )
            if source is not None
            else None
        )
    except (OSError, ValueError):
        observed_source_digest = None
    if (
        compiled_source_digest is None
        or artifact.source_content_digest != compiled_source_digest
        or observed_source_digest != compiled_source_digest
    ):
        errors.append("case-set source closure differs from compiled dataset")
    selected_case_ids = tuple(
        attempt.case_id
        for attempt in item.schedule_receipt.attempts
        if attempt.selected
    ) if item.schedule_receipt is not None else ()
    if selected_case_ids != receipt.ordered_case_ids:
        errors.append("selected schedule case order differs from activated case set")
    matching_attempts = [
        attempt
        for attempt in item.schedule_receipt.attempts
        if (
            attempt.selected
            and attempt.evidence_bundle_ref is not None
            and attempt.evidence_bundle_ref.sha256 == item.case.bundle_digest
        )
    ] if item.schedule_receipt is not None else []
    if len(matching_attempts) != 1:
        errors.append("completed case lacks unique selected-attempt lineage")
    content_refs = list(artifact.source_content_refs)
    if artifact.order_strategy_ref is not None:
        content_refs.append(artifact.order_strategy_ref)
    for case in artifact.cases:
        content_refs.extend(
            (
                case.public_input_ref,
                *case.task_contract_refs,
                *case.verifier_contract_refs,
            )
        )
    for ref in content_refs:
        ref_path = Path(ref.path)
        if (
            not ref_path.is_file()
            or ref_path.stat().st_size != ref.size_bytes
            or sha256_file(ref_path) != ref.sha256
        ):
            errors.append(f"case-set content reference drift: {ref.path}")
    return errors


def _evidence_integrity_errors(item: CompletedRun) -> list[str]:
    errors: list[str] = _case_set_binding_errors(item)
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
        elif environment_receipt.spec_digest != environment_spec.canonical_digest():
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
    if bundle.trajectory_ref is None:
        errors.append("RolloutTrajectory missing")
    else:
        refs.append(bundle.trajectory_ref)
        trajectory_path = Path(bundle.trajectory_ref.path)
        try:
            trajectory = RolloutTrajectory.model_validate_json(
                trajectory_path.read_bytes()
            )
        except (OSError, ValueError):
            errors.append("RolloutTrajectory is missing or malformed")
        else:
            if trajectory.attempt_id != bundle.run_id:
                errors.append("RolloutTrajectory attempt identity drift")
            if trajectory.manifest_digest != item.plan.manifest_digest:
                errors.append("RolloutTrajectory manifest digest drift")
            if trajectory.terminal_status != bundle.status:
                errors.append("RolloutTrajectory terminal status drift")
            if trajectory.usage != bundle.usage:
                errors.append("RolloutTrajectory usage drift")
            if (
                manifest.claim_design.purpose == RunPurpose.claim
                and not trajectory.capture.claim_complete
            ):
                errors.append("claim rollout trajectory capture is incomplete")
            refs.extend(
                (
                    *trajectory.input_refs,
                    *trajectory.output_refs,
                    *trajectory.log_refs,
                    *trajectory.native_trace_refs,
                    *trajectory.evaluator_refs,
                    *(
                        ref
                        for event in trajectory.events
                        for ref in (*event.input_refs, *event.output_refs)
                    ),
                )
            )
    if bundle.checkpoint_ref is not None:
        refs.append(bundle.checkpoint_ref)
    network_observation = bundle.network_observation
    if network_observation is not None:
        refs.extend(network_observation.evidence_refs)
    provenance = bundle.provenance
    if provenance.container_receipt_ref is not None:
        refs.append(provenance.container_receipt_ref)
    runtime_receipt = provenance.runtime_manifest_receipt
    if runtime_receipt is not None:
        refs.append(runtime_receipt.trace_ref)
        refs.extend(
            ArtifactRef(
                path=ref.path,
                sha256=ref.sha256,
                size_bytes=ref.size_bytes,
            )
            for ref in runtime_receipt.assembly_sidecar_refs
        )
    if provenance.evolution_evidence_ref is not None:
        refs.append(provenance.evolution_evidence_ref)
    configuration = manifest.metadata.configuration
    configuration_activation = provenance.configuration_activation
    activation_required = manifest.claim_design.purpose == RunPurpose.claim
    if configuration is None:
        if configuration_activation is not None:
            errors.append("undeclared ConfigurationActivationReceipt")
    elif configuration_activation is None and activation_required:
        errors.append("ConfigurationActivationReceipt missing")
    elif configuration_activation is not None:
        if configuration_activation.configuration_digest != configuration.artifact_digest:
            errors.append("configuration activation digest drift")
        expected_consumer_adapter = getattr(
            manifest.subject, "adapter", configuration.adapter
        )
        if configuration_activation.adapter != expected_consumer_adapter:
            errors.append("configuration activation adapter drift")
        if configuration_activation.status != "matched":
            errors.append(
                "configuration activation status is "
                f"{configuration_activation.status!r}"
            )
        refs.extend(configuration_activation.evidence_refs)
    model = manifest.execution.model
    model_activation = provenance.model_activation
    none_models = {"none", "none/deterministic", "none/echo"}
    if model in none_models:
        if manifest.execution.provider_binding is not None:
            errors.append("none-model execution has an undeclared ProviderBinding")
        if model_activation is not None:
            errors.append("none-model execution has an undeclared ModelActivationReceipt")
    elif model_activation is not None:
        binding = manifest.execution.provider_binding
        sources = {
            item.capability.model_activation_source
            for item in manifest.metadata.adapter_capabilities
            if item.capability.adapter_kind == "execution"
            and item.capability.model_activation_source is not None
        }
        if len(sources) != 1:
            errors.append("model activation capability binding is ambiguous")
        elif model_activation.activation_source != next(iter(sources)):
            errors.append("model activation source drift")
        if model_activation.requested_model != model:
            errors.append("model activation requested model drift")
        if binding is None:
            if model_activation.requested_provider_id is not None:
                errors.append("undeclared model activation provider binding")
            if model_activation.binding_digest is not None:
                errors.append("undeclared model activation binding digest")
        else:
            if model_activation.requested_provider_id != binding.provider_id:
                errors.append("model activation provider binding drift")
            if model_activation.requested_model_id != binding.model_id:
                errors.append("model activation model binding drift")
            if model_activation.binding_digest != binding.canonical_digest():
                errors.append("model activation binding digest drift")
        refs.extend(model_activation.evidence_refs)
        errors.extend(
            replay_model_activation_receipt(
                model_activation,
                requested_model=model,
                binding=binding,
                bundle_usage=bundle.usage,
                require_usage=manifest.claim_design.purpose == RunPurpose.claim,
            )
        )
        if manifest.claim_design.purpose == RunPurpose.claim:
            usage = bundle.usage
            if usage is None or usage.total_tokens is None:
                errors.append("real-model claim token usage is unobservable")
            if usage is None or usage.cost is None:
                errors.append("real-model claim cost usage is unobservable")
    evolution_ref = provenance.evolution_evidence_ref
    evolution_required = manifest.claim_design.purpose == RunPurpose.claim
    if manifest.subject.kind in {"evolver", "meta_evolver"}:
        if evolution_ref is None and evolution_required:
            errors.append("EvolutionRunEvidence missing")
        elif evolution_ref is not None:
            try:
                evolution = EvolutionRunEvidence.model_validate_json(
                    Path(evolution_ref.path).read_bytes()
                )
            except (OSError, ValueError):
                errors.append("evolution evidence is malformed")
            else:
                if evolution.kind != manifest.subject.kind:
                    errors.append("evolution evidence kind drift")
                execution_capabilities = tuple(
                    item.capability
                    for item in manifest.metadata.adapter_capabilities
                    if item.capability.adapter_kind == "execution"
                )
                if len(execution_capabilities) != 1:
                    errors.append("evolution execution capability binding is ambiguous")
                elif evolution.adapter_digest != execution_capabilities[0].digest:
                    errors.append("evolution adapter digest drift")
                if evolution.evaluator_digest != manifest.evaluator.artifact_digest:
                    errors.append("evolution registered evaluator digest drift")
                if evolution.budget_digest != canonical_digest(
                    manifest.execution.budget
                ):
                    errors.append("evolution budget digest drift")
                if evolution_required and not evolution.claim_ready:
                    errors.append("evolution evidence is not claim_ready")
                if evolution.run_id not in {
                    bundle.run_id,
                    manifest.metadata.run_id,
                }:
                    errors.append("evolution evidence run_id drift")
    elif evolution_ref is not None:
        errors.append("undeclared evolution evidence reference")
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


def _model_activation_isolation_errors(item: CompletedRun) -> list[str]:
    """Return honest activation blockers without classifying them as drift."""

    manifest = item.plan.manifest
    model = manifest.execution.model
    if model in {"none", "none/deterministic", "none/echo"}:
        return []
    errors: list[str] = []
    if manifest.execution.provider_binding is None:
        errors.append(
            f"{item.case.case_id}: real-model ProviderBinding is missing"
        )
    receipt = item.case.bundle.provenance.model_activation
    if receipt is None:
        errors.append(f"{item.case.case_id}: ModelActivationReceipt is missing")
    elif receipt.status != "matched":
        reasons = "; ".join(receipt.reason) or "native activation did not match"
        errors.append(
            f"{item.case.case_id}: model activation is {receipt.status}: {reasons}"
        )
    return errors


_NETWORK_BOUNDARIES = {
    "fake": NetworkBoundary.process,
    "subprocess": NetworkBoundary.process,
    "harbor-shim": NetworkBoundary.process,
    "aose-docker": NetworkBoundary.task_container,
    "harbor": NetworkBoundary.task_container,
}


def _network_boundary_for_backend(backend: Any) -> NetworkBoundary | None:
    """Resolve registered custom backends without weakening known overrides."""

    explicit = _NETWORK_BOUNDARIES.get(backend.adapter)
    if explicit is not None:
        return explicit
    return {
        "local": NetworkBoundary.process,
        "container": NetworkBoundary.task_container,
    }.get(backend.kind)


def _network_policy_errors(item: CompletedRun) -> list[str]:
    bundle = item.case.bundle
    policy = bundle.network_policy
    observation = bundle.network_observation
    errors: list[str] = []
    if policy is None:
        errors.append(f"{item.case.case_id}: ResolvedNetworkPolicy missing")
    if observation is None:
        errors.append(f"{item.case.case_id}: NetworkObservation missing")
    if policy is None or observation is None:
        return errors

    manifest = item.plan.manifest
    backend_adapter = manifest.execution.backend.adapter
    if policy.execution_adapter != backend_adapter:
        errors.append(
            f"{item.case.case_id}: network policy execution adapter mismatch"
        )
    if policy.case_id != item.case.case_id:
        errors.append(f"{item.case.case_id}: network policy case binding mismatch")
    expected_boundary = _network_boundary_for_backend(manifest.execution.backend)
    if expected_boundary is None:
        errors.append(
            f"{item.case.case_id}: missing NetworkPolicyActivationReceipt for "
            f"adapter {backend_adapter!r}"
        )
    elif policy.boundary != expected_boundary:
        errors.append(f"{item.case.case_id}: network policy boundary mismatch")

    if policy.source == NetworkPolicySource.backend_artifact:
        if policy.resolver_adapter != backend_adapter:
            errors.append(
                f"{item.case.case_id}: network policy resolver adapter mismatch"
            )
        expected_source_digest = manifest.execution.backend.digest or item.runner_digest
        if policy.source_artifact_digest != expected_source_digest:
            errors.append(
                f"{item.case.case_id}: network policy source artifact digest drift"
            )
    else:
        if policy.resolver_adapter != manifest.benchmark.adapter:
            errors.append(
                f"{item.case.case_id}: case-set network policy resolver mismatch"
            )
        expected_case_set_digest = getattr(item, "case_set_digest", None)
        if expected_case_set_digest is None:
            errors.append(
                f"{item.case.case_id}: case-set network policy requires missing "
                "CaseSetActivationReceipt"
            )
        elif policy.source_artifact_digest != expected_case_set_digest:
            errors.append(
                f"{item.case.case_id}: case-set network policy source digest drift"
            )

    if observation.policy_digest != canonical_digest(policy):
        errors.append(f"{item.case.case_id}: network observation policy digest drift")
    if observation.declared_allow_internet != policy.allow_internet:
        errors.append(
            f"{item.case.case_id}: network observation disagrees with resolved policy"
        )
    if observation.mode != policy.required_observation:
        errors.append(
            f"{item.case.case_id}: network observation mode does not satisfy policy"
        )
    if not observation.evidence_refs:
        errors.append(
            f"{item.case.case_id}: NetworkObservation evidence reference missing"
        )
    return errors


def _evaluate_claim(
    *,
    completed: Iterable[CompletedRun],
    expected_run_count: int,
    control_id: str,
    treatment_id: str,
    deterministic_conformance: bool,
    counterbalanced: bool,
    record_index_ref: ArtifactRef | None,
    metric_results: tuple[MetricResult, ...] | None = None,
    contrast_factor_path: str | None = None,
    contrast_control_value: object | None = None,
    contrast_treatment_value: object | None = None,
) -> ClaimReport:
    """Evaluate independent gates and derive claim eligibility."""

    items = list(completed)
    if metric_results is None:
        # Derive the registered metric results from the schedule when this
        # lower-level helper is called directly.  Callers cannot inject a
        # hand-written score; the same replay path as ``evaluate_run_report``
        # remains authoritative.
        derived_results: list[MetricResult] = []
        seen_parents: set[str] = set()
        for item in items:
            parent_run_id = item.plan.manifest.metadata.run_id
            if parent_run_id in seen_parents:
                continue
            seen_parents.add(parent_run_id)
            if item.schedule_receipt is None or item.schedule_receipt_path is None:
                raise ValueError(
                    f"registered metrics require ScheduleActivationReceipt: {parent_run_id}"
                )
            derived_results.extend(
                compute_metric_results(
                    item.plan.manifest,
                    item.plan.manifest_digest,
                    item.schedule_receipt,
                    item.schedule_receipt_path,
                )
            )
        metric_results = tuple(derived_results)
    (
        report_experiment_id,
        report_manifest_digest,
        comparison_kind,
        subject_kinds,
    ) = _report_identity(items)
    if comparison_kind is None:
        raise ValueError("claim report requires a comparison kind")
    authoritative_metrics = {
        item.plan.manifest.authoritative_reward_metric for item in items
    }
    authoritative_metric = (
        next(iter(authoritative_metrics)) if len(authoritative_metrics) == 1 else None
    )
    statuses = [item.case.bundle.status for item in items]
    execution_errors = [status.value for status in statuses if status not in _EXECUTION_VALID]
    execution_gate = (
        _valid(
            "all cases produced contractual, verifiable outputs",
            tuple(str(item.case.bundle_path.resolve()) for item in items),
        )
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
        else _valid(
            "resolved schedule and reset policy match the plan",
            tuple(
                str(item.schedule_receipt_path.resolve())
                for item in items
                if item.schedule_receipt_path is not None
            ),
        )
    )

    # Compiler enforcement is a precondition for reaching execution. Recheck
    # immutable provenance digests so bypassed/corrupt evidence cannot pass.
    isolation_errors: list[str] = []
    positive_isolation_observations = 0
    for item in items:
        bundle = item.case.bundle
        isolation_errors.extend(_evidence_integrity_errors(item))
        isolation_errors.extend(_model_activation_isolation_errors(item))
        policy_errors = _network_policy_errors(item)
        isolation_errors.extend(policy_errors)
        network_observation = bundle.network_observation
        if not policy_errors and network_observation is not None:
            if not network_observation.claim_isolation_valid:
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
            f"({positive_isolation_observations}/{expected_run_count} verified)",
            tuple(
                dict.fromkeys(
                    str(ref.path)
                    for item in items
                    for ref in (
                        item.case.bundle.network_observation.evidence_refs
                        if item.case.bundle.network_observation is not None
                        else ()
                    )
                )
            ),
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
            evidence = bundle.verifier_evidence
            if evidence is None or evidence.score is None:
                scoring_errors.append(f"{bundle.run_id}: verifier evidence missing")
            elif authoritative_metric is None:
                scoring_errors.append(
                    f"{bundle.run_id}: authoritative reward metric differs across runs"
                )
            elif not evidence.metrics:
                scoring_errors.append(
                    f"{bundle.run_id}: verifier metrics are empty for scored evidence"
                )
            elif authoritative_metric not in evidence.metrics:
                scoring_errors.append(
                    f"{bundle.run_id}: authoritative reward metric "
                    f"{authoritative_metric!r} is missing from verifier evidence"
                )
            elif evidence.metrics[authoritative_metric] != evidence.score:
                scoring_errors.append(
                    f"{bundle.run_id}: authoritative reward metric "
                    f"{authoritative_metric!r} disagrees with verifier score"
                )
            elif bundle.status == RunStatus.pass_ and not evidence.passed:
                scoring_errors.append(f"{bundle.run_id}: pass status contradicts verifier")
            elif (
                bundle.status == RunStatus.verified_fail
                and evidence.passed
            ):
                scoring_errors.append(
                    f"{bundle.run_id}: verified_fail status contradicts verifier"
                )
            elif evidence.details.get("scoring_semantics_declared") is False:
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
            f"every verifiable output has authoritative {authoritative_metric!r} "
            f"evidence ({scored_bundles}/{len(items)} scored)",
            tuple(
                dict.fromkeys(
                    [
                        *(str(item.case.bundle_path.resolve()) for item in items),
                        *(
                            str(ref.path)
                            for item in items
                            for ref in (
                                item.case.bundle.verifier_evidence.artifact_refs
                                if item.case.bundle.verifier_evidence is not None
                                else ()
                            )
                        ),
                    ]
                )
            ),
        )

    pairs, pair_error = _subject_pairs(
        items,
        control_id,
        treatment_id,
        factor_path=contrast_factor_path,
        control_value=contrast_control_value,
        treatment_value=contrast_treatment_value,
    )
    statistics_reasons: list[str] = []
    if pair_error:
        statistics_reasons.append(pair_error)
    analysis_plans = {
        canonical_json_bytes(item.plan.manifest.claim_design.statistical_analysis)
        for item in items
        if item.plan.manifest.claim_design.statistical_analysis is not None
    }
    plans_missing = any(
        item.plan.manifest.claim_design.statistical_analysis is None
        for item in items
    )
    if analysis_plans and (plans_missing or len(analysis_plans) != 1):
        statistics_reasons.append(
            "StatisticalAnalysisPlan must be invariant across every arm"
        )
    analysis_plan = (
        None
        if not analysis_plans or plans_missing or len(analysis_plans) != 1
        else next(
            item.plan.manifest.claim_design.statistical_analysis
            for item in items
            if item.plan.manifest.claim_design.statistical_analysis is not None
        )
    )
    statistics_receipt: StatisticalAnalysisReceipt | None = None
    if analysis_plan is not None:
        if not counterbalanced:
            statistics_reasons.append("counterbalance metadata is false")
        elif not _counterbalance_is_valid(
            items,
            control_id,
            treatment_id,
            factor_path=contrast_factor_path,
            control_value=contrast_control_value,
            treatment_value=contrast_treatment_value,
        ):
            statistics_reasons.append(
                "observed control/treatment order is not counterbalanced"
            )
        if len(items) != expected_run_count:
            statistics_reasons.append("not all repetitions are terminal")

        observations: list[PairedScore] = []
        for control, treatment in pairs:
            control_evidence = control.case.bundle.verifier_evidence
            treatment_evidence = treatment.case.bundle.verifier_evidence
            control_score = (
                None
                if control_evidence is None or authoritative_metric is None
                else control_evidence.metrics.get(authoritative_metric)
            )
            treatment_score = (
                None
                if treatment_evidence is None or authoritative_metric is None
                else treatment_evidence.metrics.get(authoritative_metric)
            )
            if control_score is None or treatment_score is None:
                statistics_reasons.append(
                    "statistics require authoritative verifier scores for every pair"
                )
                continue
            unit_values = {
                key: value
                for key, value in control.plan.factor_values.items()
                if key
                not in {
                    "subject",
                    "experiment.subject",
                    "order_position",
                    contrast_factor_path,
                }
            }
            unit_values["case_id"] = control.case.case_id
            observations.append(
                PairedScore(
                    unit_values=unit_values,
                    control_score=control_score,
                    treatment_score=treatment_score,
                )
            )
        analysis = analyze_paired_scores(
            analysis_plan,
            metric=authoritative_metric or "unbound",
            observations=observations,
            evaluation_splits=tuple(
                benchmark_evaluation_split(item.plan.manifest.dataset)
                for item in items
            ),
            allow_no_holdout=deterministic_conformance,
        )
        statistics_receipt = analysis.receipt
        statistics_reasons.extend(analysis.errors)
    elif deterministic_conformance:
        if not counterbalanced:
            statistics_reasons.append("counterbalance metadata is false")
        elif not _counterbalance_is_valid(
            items,
            control_id,
            treatment_id,
            factor_path=contrast_factor_path,
            control_value=contrast_control_value,
            treatment_value=contrast_treatment_value,
        ):
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
        else _valid(
            (
                "paired sample variance and confidence interval match the "
                "StatisticalAnalysisPlan"
                if analysis_plan is not None
                else "deterministic conformance pairing and counterbalance are complete"
            ),
            tuple(
                str(item.schedule_receipt_path.resolve())
                for item in items
                if item.schedule_receipt_path is not None
            ),
        )
    )

    gates = {
        GateName.execution_valid: execution_gate,
        GateName.protocol_valid: protocol_gate,
        GateName.isolation_valid: isolation_gate,
        GateName.scoring_valid: scoring_gate,
        GateName.statistics_valid: statistics_gate,
    }
    effect = None
    if (
        statistics_receipt is not None
        and statistics_receipt.confidence_interval is not None
    ):
        effect = EffectEstimate(
            metric=statistics_receipt.metric,
            point_estimate=statistics_receipt.point_estimate,
            confidence_interval=statistics_receipt.confidence_interval,
            n_runs=len(items),
            n_pairs=statistics_receipt.observed_pair_count,
        )
    elif analysis_plan is None:
        differences: list[float] = []
        for control, treatment in pairs:
            control_score = (
                None
                if authoritative_metric is None
                else (
                    None
                    if control.case.bundle.verifier_evidence is None
                    else control.case.bundle.verifier_evidence.metrics.get(
                        authoritative_metric
                    )
                )
            )
            treatment_score = (
                None
                if authoritative_metric is None
                else (
                    None
                    if treatment.case.bundle.verifier_evidence is None
                    else treatment.case.bundle.verifier_evidence.metrics.get(
                        authoritative_metric
                    )
                )
            )
            if control_score is None or treatment_score is None:
                differences = []
                break
            differences.append(treatment_score - control_score)
        if differences:
            effect = EffectEstimate(
                metric=authoritative_metric,
                point_estimate=mean(differences),
                confidence_interval=(min(differences), max(differences)),
                n_runs=len(items),
                n_pairs=len(pairs),
            )

    counts = Counter(statuses)
    lineage = tuple(_lineage_ref(item) for item in items)
    return ClaimReport(
        purpose=RunPurpose.claim,
        comparison_kind=comparison_kind,
        subject_kinds=subject_kinds,
        experiment_id=report_experiment_id,
        manifest_digest=report_manifest_digest,
        gates=gates,
        effect=effect,
        statistics_receipt=statistics_receipt,
        metric_results=metric_results,
        failure_breakdown=dict(counts),
        lineage=lineage,
        record_index_ref=record_index_ref,
    )


def evaluate_run_report(
    *,
    completed: Iterable[CompletedRun],
    expected_run_ids: tuple[str, ...],
    record_index_ref: ArtifactRef | None = None,
) -> RunReport:
    """Build the report type structurally required by the declared purpose."""

    items = list(completed)
    if not items:
        raise ValueError("cannot report an experiment with no completed runs")
    (
        derived_experiment_id,
        derived_manifest_digest,
        comparison_kind,
        subject_kinds,
    ) = _report_identity(items)
    expected_duplicates = sorted(
        run_id for run_id, count in Counter(expected_run_ids).items() if count > 1
    )
    if expected_duplicates:
        raise ValueError(
            f"expected plan contains duplicate run ids: {expected_duplicates}"
        )
    parent_counts = Counter(item.plan.manifest.metadata.run_id for item in items)
    observed_run_ids = tuple(
        (
            f"{item.plan.manifest.metadata.run_id}::{item.case.case_id}"
            if parent_counts[item.plan.manifest.metadata.run_id] > 1
            else item.plan.manifest.metadata.run_id
        )
        for item in items
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
    metric_results_list: list[MetricResult] = []
    seen_metric_parents: set[str] = set()
    for item in items:
        parent_run_id = item.plan.manifest.metadata.run_id
        if parent_run_id in seen_metric_parents:
            continue
        seen_metric_parents.add(parent_run_id)
        if item.schedule_receipt is None or item.schedule_receipt_path is None:
            raise ValueError(
                f"registered metrics require ScheduleActivationReceipt: {parent_run_id}"
            )
        metric_results_list.extend(
            compute_metric_results(
                item.plan.manifest,
                item.plan.manifest_digest,
                item.schedule_receipt,
                item.schedule_receipt_path,
            )
        )
    metric_results = tuple(metric_results_list)
    if purpose == RunPurpose.claim:
        (
            derived_control_id,
            derived_treatment_id,
            derived_deterministic,
            derived_counterbalanced,
            derived_factor_path,
            derived_control_value,
            derived_treatment_value,
        ) = (
            _report_contrast(items)
        )
        return _evaluate_claim(
            completed=items,
            expected_run_count=expected_run_count,
            control_id=derived_control_id,
            treatment_id=derived_treatment_id,
            deterministic_conformance=derived_deterministic,
            counterbalanced=derived_counterbalanced,
            record_index_ref=record_index_ref,
            metric_results=metric_results,
            contrast_factor_path=derived_factor_path,
            contrast_control_value=derived_control_value,
            contrast_treatment_value=derived_treatment_value,
        )

    integrity_errors = [
        error
        for item in items
        for error in (
            *_evidence_integrity_errors(item),
            *_receipt_binding_errors(
                item,
                allowed_invalid_schedule_reasons=frozenset(
                    {"budget usage is unobservable"}
                ),
            ),
        )
    ]
    if integrity_errors:
        raise ValueError(
            "exploratory evidence integrity failed: "
            + "; ".join(integrity_errors)
        )
    protocol_errors: list[str] = []
    for item in items:
        receipt = item.schedule_receipt
        if receipt is None:
            protocol_errors.append(
                f"{item.case.case_id}: ScheduleActivationReceipt missing"
            )
        elif not receipt.schedule_valid:
            reasons = "; ".join(receipt.mismatch_reasons) or "unspecified mismatch"
            protocol_errors.append(
                f"{item.case.case_id}: schedule receipt is invalid: {reasons}"
            )
    protocol_reasons = tuple(sorted(set(protocol_errors)))
    isolation_errors: list[str] = []
    for item in items:
        isolation_errors.extend(_model_activation_isolation_errors(item))
        policy_errors = _network_policy_errors(item)
        isolation_errors.extend(policy_errors)
        observation = item.case.bundle.network_observation
        if (
            not policy_errors
            and observation is not None
            and not observation.claim_isolation_valid
        ):
            isolation_errors.append(
                f"{item.case.case_id}: NetworkObservation cannot substantiate isolation"
            )
    isolation_reasons = tuple(sorted(set(isolation_errors)))
    statuses = [item.case.bundle.status for item in items]
    metric, scores = _exploratory_metric_scores(items)
    observations = (
        Observation(metric=metric, value=mean(scores), n_runs=len(scores)),
    ) if scores else ()
    lineage = tuple(
        _lineage_ref(item)
        for item in items
    )
    return ObservationReport(
        purpose=RunPurpose.exploratory,
        comparison_kind=comparison_kind,
        subject_kinds=subject_kinds,
        experiment_id=derived_experiment_id,
        manifest_digest=derived_manifest_digest,
        protocol_valid=not protocol_reasons,
        protocol_reasons=protocol_reasons,
        isolation_valid=not isolation_reasons,
        isolation_reasons=isolation_reasons,
        observations=observations,
        metric_results=metric_results,
        failure_breakdown=dict(Counter(statuses)),
        lineage=lineage,
        record_index_ref=record_index_ref,
    )


__all__ = ["CompletedRun", "evaluate_run_report"]
