"""Standalone byte and lineage verification for BMP run reports."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import random
from dataclasses import dataclass
from math import isclose
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, TypeVar

from pydantic import ValidationError

from .compiler import canonical_digest
from .models import (
    ArtifactRef,
    AttemptExecution,
    CaseSetActivationReceipt,
    CaseSetArtifact,
    CheckpointSaveReceipt,
    ClaimReport,
    GateName,
    EvidenceBundle,
    ObservationReport,
    RecordIndex,
    ResolvedBmpManifest,
    RunReport,
    RunReportAdapter,
    RunStatus,
    ScheduleActivationReceipt,
)


class ReportVerificationError(ValueError):
    """All integrity mismatches found while verifying one report."""

    def __init__(self, mismatches: list[str]) -> None:
        self.mismatches = tuple(mismatches)
        super().__init__("report verification failed:\n- " + "\n- ".join(mismatches))


@dataclass(frozen=True)
class VerifiedClaimReport:
    report: ClaimReport
    report_path: Path
    record_index: RecordIndex
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedObservationReport:
    report: ObservationReport
    report_path: Path
    record_index: RecordIndex
    warnings: tuple[str, ...] = ()


VerifiedRunReport = VerifiedClaimReport | VerifiedObservationReport
ReportT = TypeVar("ReportT", ClaimReport, ObservationReport)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _compact_json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _resolve_path(path: str, path_map: Mapping[str, str]) -> Path:
    original = Path(path)
    matches = [
        prefix
        for prefix in path_map
        if path == prefix or path.startswith(prefix.rstrip("/") + "/")
    ]
    if not matches:
        return original
    prefix = max(matches, key=len)
    suffix = path[len(prefix) :].lstrip("/")
    if any(part in {".", ".."} for part in suffix.split("/") if part):
        raise ValueError(
            f"mapped path suffix is not normalized and may traverse: {path!r}"
        )
    return Path(path_map[prefix]) / suffix


def _verify_ref(
    ref: ArtifactRef,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> tuple[Path, bytes | None]:
    try:
        path = _resolve_path(ref.path, path_map)
    except ValueError as exc:
        mismatches.append(f"{label}: {exc}")
        return Path(ref.path), None
    try:
        content = path.read_bytes()
    except OSError as exc:
        mismatches.append(f"{label}: cannot read {path}: {exc}")
        return path, None
    observed_digest = _sha256(content)
    if observed_digest != ref.sha256:
        mismatches.append(
            f"{label}: sha256 mismatch at {path}: expected {ref.sha256}, observed {observed_digest}"
        )
    if len(content) != ref.size_bytes:
        mismatches.append(
            f"{label}: size mismatch at {path}: expected {ref.size_bytes}, observed {len(content)}"
        )
    return path, content


def _parse_json_model(model: type[ReportT], content: bytes, *, label: str, mismatches: list[str]) -> ReportT | None:
    try:
        return model.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        mismatches.append(f"{label}: invalid schema: {exc}")
        return None


def _verify_bundle_artifacts(
    bundle: EvidenceBundle,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    """Rehash every artifact reference reachable from one evidence bundle."""

    refs: list[tuple[str, ArtifactRef]] = []
    refs.extend(
        (f"{label}.output_refs[{index}]", ref)
        for index, ref in enumerate(bundle.output_refs)
    )
    refs.extend(
        (f"{label}.log_refs[{index}]", ref)
        for index, ref in enumerate(bundle.log_refs)
    )
    if bundle.trace_ref is not None:
        refs.append((f"{label}.trace_ref", bundle.trace_ref))
    if bundle.checkpoint_ref is not None:
        refs.append((f"{label}.checkpoint_ref", bundle.checkpoint_ref))
    if bundle.network_observation is not None:
        refs.extend(
            (f"{label}.network_observation.evidence_refs[{index}]", ref)
            for index, ref in enumerate(bundle.network_observation.evidence_refs)
        )
    if bundle.provenance.container_receipt_ref is not None:
        refs.append(
            (
                f"{label}.provenance.container_receipt_ref",
                bundle.provenance.container_receipt_ref,
            )
        )
    if bundle.verifier_evidence is not None:
        refs.extend(
            (f"{label}.verifier_evidence.artifact_refs[{index}]", ref)
            for index, ref in enumerate(bundle.verifier_evidence.artifact_refs)
        )
    for ref_label, ref in refs:
        _verify_ref(
            ref,
            label=ref_label,
            path_map=path_map,
            mismatches=mismatches,
        )


def _verify_manifest_configuration(
    manifest: ResolvedBmpManifest,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    """Rehash every configuration source and the resolved tree identity."""

    configuration = manifest.metadata.configuration
    if configuration is None:
        return
    if configuration.canonical_digest() != configuration.artifact_digest:
        mismatches.append(f"{label}: configuration artifact_digest drift")
    for index, ref in enumerate(configuration.source_refs):
        _verify_ref(
            ref,
            label=f"{label}.source_refs[{index}]",
            path_map=path_map,
            mismatches=mismatches,
        )


def _verify_bundle_provenance(
    bundle: EvidenceBundle,
    manifest: ResolvedBmpManifest,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    """Bind persisted bundle provenance fields to the indexed manifest."""

    provenance = bundle.provenance
    if provenance.test_override is not None:
        mismatches.append(
            f"{label}: test_override provenance cannot produce verified benchmark evidence"
        )
    if provenance.manifest_digest != manifest.canonical_digest():
        mismatches.append(f"{label}: provenance manifest_digest drift")
    if provenance.benchmark_digest != manifest.benchmark.artifact_digest:
        mismatches.append(f"{label}: provenance benchmark_digest drift")
    if provenance.subject_digest != manifest.subject.artifact_digest:
        mismatches.append(f"{label}: provenance subject_digest drift")
    expected_backend_digest = manifest.execution.backend.digest
    if expected_backend_digest:
        if provenance.backend_digest != expected_backend_digest:
            mismatches.append(f"{label}: provenance backend_digest drift")
    elif provenance.backend_digest != provenance.runner_digest:
        mismatches.append(f"{label}: provenance backend_digest drift")
    expected_backend_kind = {
        "fake": "fake",
        "subprocess": "subprocess",
        "aose-docker": "docker",
        "harbor": "harbor",
        "harbor-shim": "harbor",
    }.get(manifest.execution.backend.adapter)
    if expected_backend_kind is not None and provenance.backend_kind != expected_backend_kind:
        mismatches.append(f"{label}: provenance backend_kind drift")
    if provenance.test_override != manifest.metadata.test_override:
        mismatches.append(f"{label}: provenance test_override drift")
    if provenance.trace_emission_claimed != bool(getattr(manifest.subject, "emits_trace", False)):
        mismatches.append(f"{label}: provenance trace_emission_claimed drift")
    environment = manifest.execution.backend.environment
    if environment is None and provenance.environment_receipt is not None:
        mismatches.append(f"{label}: undeclared environment receipt")
    elif environment is not None:
        receipt = provenance.environment_receipt
        if receipt is None:
            mismatches.append(f"{label}: environment receipt is missing")
        elif receipt.spec_digest != environment.canonical_digest():
            mismatches.append(f"{label}: environment spec_digest drift")
    container_ref = provenance.container_receipt_ref
    if container_ref is None:
        return
    _, container_bytes = _verify_ref(
        container_ref,
        label=f"{label}.container_receipt",
        path_map=path_map,
        mismatches=mismatches,
    )
    if container_bytes is None:
        return
    try:
        container = json.loads(container_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        mismatches.append(f"{label}: container receipt is malformed")
        return
    if not isinstance(container, dict):
        mismatches.append(f"{label}: container receipt root is not an object")
        return
    if container.get("image_id") != provenance.image_digest:
        mismatches.append(f"{label}: container image digest cross-link drift")
    if container.get("agent_executable_sha256") != provenance.executable_digest:
        mismatches.append(f"{label}: container executable digest cross-link drift")


def _verify_schedule_manifest_binding(
    schedule: ScheduleActivationReceipt,
    manifest: ResolvedBmpManifest,
    *,
    case_set_receipt: CaseSetActivationReceipt | None,
    label: str,
    mismatches: list[str],
) -> None:
    """Check declared schedule identity against the indexed protocol."""

    protocol = manifest.execution.protocol
    if protocol is None:
        mismatches.append(f"{label}: resolved protocol is missing")
        return
    if schedule.protocol_digest != canonical_digest(protocol):
        mismatches.append(f"{label}: protocol_digest does not match manifest protocol")
    checks = (
        ("declared_rollouts_per_case", schedule.declared_rollouts_per_case, protocol.rollouts_per_case),
        ("declared_parallelism", schedule.declared_parallelism, protocol.parallelism),
        ("declared_case_order", schedule.declared_case_order, protocol.case_order),
        ("declared_state_reset", schedule.declared_state_reset, protocol.state_reset),
        ("declared_candidate_selection", schedule.declared_candidate_selection, protocol.candidate_selection),
        ("declared_checkpoint_policy", schedule.declared_checkpoint_policy, protocol.checkpoint_policy),
    )
    for field_name, observed, expected in checks:
        expected_value = getattr(expected, "value", expected)
        if observed != expected_value:
            mismatches.append(
                f"{label}: {field_name} does not match manifest protocol"
            )
    expected_seed = (
        manifest.execution.seed if protocol.case_order == "seeded_random" else None
    )
    if schedule.order_seed != expected_seed:
        mismatches.append(f"{label}: order_seed does not match manifest protocol")

    allocated_case_ids = tuple(
        allocation.case_id
        for allocation in schedule.budget_ledger.case_allocations
    )
    if schedule.observed_case_order != allocated_case_ids:
        mismatches.append(
            f"{label}: observed_case_order does not match case allocations"
        )
    if case_set_receipt is None:
        mismatches.append(
            f"{label}: activated case set is unavailable for schedule binding"
        )
        return

    activated_case_ids = list(case_set_receipt.ordered_case_ids)
    if protocol.case_order == "fixed":
        expected_case_order = tuple(activated_case_ids)
        if schedule.observed_case_order != expected_case_order:
            mismatches.append(
                f"{label}: observed_case_order does not match fixed activated case set"
            )
    elif protocol.case_order == "seeded_random":
        seed = manifest.execution.seed
        if seed is None:
            mismatches.append(
                f"{label}: seeded_random schedule is missing its manifest seed"
            )
        else:
            random.Random(seed).shuffle(activated_case_ids)
            if schedule.observed_case_order != tuple(activated_case_ids):
                mismatches.append(
                    f"{label}: observed_case_order does not match seeded activated case set"
                )
    elif protocol.case_order in {"custom", "explicit"}:
        expected_case_order = tuple(protocol.explicit_case_ids)
        if not expected_case_order:
            mismatches.append(
                f"{label}: explicit case order is missing explicit_case_ids"
            )
        elif tuple(activated_case_ids) != expected_case_order:
            mismatches.append(
                f"{label}: activated case set does not match explicit_case_ids"
            )
        elif schedule.observed_case_order != expected_case_order:
            mismatches.append(
                f"{label}: observed_case_order does not match explicit_case_ids"
            )
    elif Counter(schedule.observed_case_order) != Counter(activated_case_ids):
        mismatches.append(
            f"{label}: observed_case_order is not a permutation of the activated case set"
        )


def _verify_case_set_receipt(
    ref: ArtifactRef,
    *,
    case_id: str,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
    expected_benchmark_id: str | None = None,
    expected_benchmark_digest: str | None = None,
    expected_loader_adapter: str | None = None,
    expected_loader_digest: str | None = None,
    expected_case_order: str | None = None,
    expected_order_seed: int | None = None,
    expected_source_content_digest: str | None = None,
) -> CaseSetActivationReceipt | None:
    """Verify a case-set activation receipt and its content-addressed closure."""

    _, receipt_bytes = _verify_ref(
        ref,
        label=label,
        path_map=path_map,
        mismatches=mismatches,
    )
    if receipt_bytes is None:
        return None
    receipt = _parse_json_model(
        CaseSetActivationReceipt,
        receipt_bytes,
        label=label,
        mismatches=mismatches,
    )
    if receipt is None:
        return None
    if case_id not in receipt.ordered_case_ids:
        mismatches.append(
            f"{label}: case_id {case_id!r} is absent from activated case set"
        )

    _, artifact_bytes = _verify_ref(
        receipt.case_set_ref,
        label=f"{label}.case_set",
        path_map=path_map,
        mismatches=mismatches,
    )
    if artifact_bytes is None:
        return receipt
    artifact = _parse_json_model(
        CaseSetArtifact,
        artifact_bytes,
        label=f"{label}.case_set",
        mismatches=mismatches,
    )
    if artifact is None:
        return receipt
    if artifact.canonical_digest() != receipt.case_set_digest:
        mismatches.append(f"{label}: case_set_digest does not match artifact")
    if (
        expected_benchmark_id is not None
        and artifact.benchmark_id != expected_benchmark_id
    ):
        mismatches.append(f"{label}: benchmark_id does not match indexed manifest")
    if (
        expected_benchmark_digest is not None
        and artifact.benchmark_digest != expected_benchmark_digest
    ):
        mismatches.append(f"{label}: benchmark_digest does not match indexed manifest")
    if (
        expected_loader_adapter is not None
        and artifact.loader_adapter != expected_loader_adapter
    ):
        mismatches.append(f"{label}: loader_adapter does not match indexed manifest")
    if (
        expected_loader_digest is not None
        and artifact.loader_digest != expected_loader_digest
    ):
        mismatches.append(f"{label}: loader_digest does not match indexed manifest")
    if artifact.loader_adapter != receipt.loader_adapter:
        mismatches.append(f"{label}: loader_adapter does not match artifact")
    if artifact.loader_digest != receipt.loader_digest:
        mismatches.append(f"{label}: loader_digest does not match artifact")
    if artifact.ordered_case_ids != receipt.ordered_case_ids:
        mismatches.append(f"{label}: ordered_case_ids do not match artifact")
    expected_selection_method = (
        "explicit_case_ids"
        if expected_case_order in {"custom", "explicit"}
        else "all_cases"
    ) if expected_case_order is not None else None
    if expected_selection_method is not None and artifact.selection_method != expected_selection_method:
        mismatches.append(f"{label}: selection_method does not match manifest protocol")
    if (
        expected_case_order is not None
        and artifact.case_order != expected_case_order
    ):
        mismatches.append(f"{label}: case_order does not match manifest protocol")
    if artifact.order_seed != expected_order_seed:
        mismatches.append(f"{label}: order_seed does not match manifest protocol")
    if (
        expected_source_content_digest is not None
        and artifact.source_content_digest != expected_source_content_digest
    ):
        mismatches.append(
            f"{label}: source_content_digest does not match indexed benchmark"
        )

    content_refs: list[tuple[str, ArtifactRef]] = [
        (
            f"{label}.case_set.source_content_refs[{index}]",
            content_ref,
        )
        for index, content_ref in enumerate(artifact.source_content_refs)
    ]
    for case in artifact.cases:
        content_refs.append(
            (f"{label}.case_set.cases[{case.case_id}].public_input_ref", case.public_input_ref)
        )
        content_refs.extend(
            (
                f"{label}.case_set.cases[{case.case_id}].task_contract_refs[{index}]",
                content_ref,
            )
            for index, content_ref in enumerate(case.task_contract_refs)
        )
        content_refs.extend(
            (
                f"{label}.case_set.cases[{case.case_id}].verifier_contract_refs[{index}]",
                content_ref,
            )
            for index, content_ref in enumerate(case.verifier_contract_refs)
        )
    for content_label, content_ref in content_refs:
        _verify_ref(
            content_ref,
            label=content_label,
            path_map=path_map,
            mismatches=mismatches,
        )
    return receipt


def _verify_schedule_attempt(
    attempt: AttemptExecution,
    *,
    manifest: ResolvedBmpManifest | None,
    case_set_receipt: CaseSetActivationReceipt | None,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> EvidenceBundle | None:
    """Verify one scheduler attempt and return its bound evidence bundle."""

    ref = attempt.evidence_bundle_ref
    if ref is None:
        mismatches.append(f"{label}: evidence_bundle_ref is missing")
        return None
    _, bundle_bytes = _verify_ref(
        ref,
        label=f"{label}.evidence_bundle",
        path_map=path_map,
        mismatches=mismatches,
    )
    if bundle_bytes is None:
        return None
    bundle = _parse_json_model(
        EvidenceBundle,
        bundle_bytes,
        label=f"{label}.evidence_bundle",
        mismatches=mismatches,
    )
    if bundle is None:
        return None
    if bundle.run_id != attempt.attempt_id:
        mismatches.append(
            f"{label}: bundle run_id {bundle.run_id!r} != attempt_id {attempt.attempt_id!r}"
        )
    if attempt.debit is None:
        mismatches.append(f"{label}: budget debit is missing")
    else:
        if attempt.debit.child_run_id != bundle.run_id:
            mismatches.append(f"{label}: budget debit child_run_id drift")
        if attempt.debit.spent != bundle.usage:
            mismatches.append(f"{label}: budget debit usage drift")
        expected_status = (
            RunStatus.agent_error
            if attempt.debit.budget_exceeded
            else bundle.status
        )
        if attempt.status != expected_status:
            mismatches.append(f"{label}: attempt status drift")
    _verify_bundle_artifacts(
        bundle,
        label=f"{label}.evidence_bundle",
        path_map=path_map,
        mismatches=mismatches,
    )
    if manifest is None:
        mismatches.append(f"{label}: indexed manifest is unavailable")
        return bundle
    _verify_bundle_provenance(
        bundle,
        manifest,
        label=f"{label}.evidence_bundle",
        path_map=path_map,
        mismatches=mismatches,
    )
    if (
        case_set_receipt is None
        or attempt.case_id not in case_set_receipt.ordered_case_ids
    ):
        mismatches.append(
            f"{label}: attempt case_id {attempt.case_id!r} is absent from activated case set"
        )

    metric = manifest.benchmark.authoritative_reward_metric
    verifier_evidence = bundle.verifier_evidence
    reward_value = (
        None
        if verifier_evidence is None
        else verifier_evidence.metrics.get(metric)
    )
    expected_reward_metric = metric if reward_value is not None else None
    if (
        attempt.reward_metric != expected_reward_metric
        or attempt.reward_value != reward_value
    ):
        mismatches.append(f"{label}: authoritative reward binding drift")
    if (
        reward_value is not None
        and verifier_evidence is not None
        and verifier_evidence.score != reward_value
    ):
        mismatches.append(
            f"{label}: authoritative reward does not match verifier score"
        )

    for reason in _network_policy_binding_reasons(
        attempt,
        bundle,
        manifest,
        case_set_receipt,
    ):
        mismatches.append(f"{label}: {reason}")
    return bundle


def _verify_checkpoint_save(
    save: CheckpointSaveReceipt,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> tuple[Path, dict[str, Any] | None]:
    path, content = _verify_ref(
        ArtifactRef(
            path=save.path,
            sha256=save.written_digest,
            size_bytes=save.size_bytes,
        ),
        label=label,
        path_map=path_map,
        mismatches=mismatches,
    )
    if content is None:
        return path, None
    try:
        checkpoint = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        mismatches.append(f"{label}: checkpoint save artifact is malformed")
        return path, None
    if not isinstance(checkpoint, dict):
        mismatches.append(f"{label}: checkpoint save root is not an object")
        return path, None
    return path, checkpoint


def _checkpoint_fields(
    checkpoint: Mapping[str, Any],
    *,
    label: str,
    mismatches: list[str],
) -> tuple[
    str,
    int,
    dict[str, str],
    dict[str, str],
    dict[str, str],
    tuple[str, ...],
] | None:
    """Parse the identity-bearing portion of a checkpoint ledger."""

    plan_digest = checkpoint.get("plan_sha256")
    next_index = checkpoint.get("next_index")
    completed = checkpoint.get("completed")
    schedule_receipts = checkpoint.get("schedule_receipts")
    schedule_paths = checkpoint.get("schedule_receipt_paths")
    retained = checkpoint.get("retained_plan_sha256", ())
    if not _is_sha256(plan_digest):
        mismatches.append(f"{label}: plan_sha256 is not a SHA-256 digest")
    if (
        not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or next_index < 0
    ):
        mismatches.append(f"{label}: next_index is not a non-negative integer")
    mappings = (
        ("completed", completed, True),
        ("schedule_receipts", schedule_receipts, True),
        ("schedule_receipt_paths", schedule_paths, False),
    )
    for field_name, value, digest_values in mappings:
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not key for key in value
        ):
            mismatches.append(f"{label}: {field_name} is not a string-keyed object")
            continue
        if digest_values and any(not _is_sha256(item) for item in value.values()):
            mismatches.append(f"{label}: {field_name} contains a non-SHA-256 value")
        if not digest_values and any(
            not isinstance(item, str) or not Path(item).is_absolute()
            for item in value.values()
        ):
            mismatches.append(f"{label}: {field_name} contains a non-absolute path")
    if isinstance(retained, str) or not isinstance(retained, (list, tuple)):
        mismatches.append(f"{label}: retained_plan_sha256 is malformed")
    elif (
        any(not _is_sha256(item) for item in retained)
        or len(set(retained)) != len(retained)
    ):
        mismatches.append(
            f"{label}: retained_plan_sha256 contains invalid or duplicate digests"
        )
    if (
        not _is_sha256(plan_digest)
        or not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or next_index < 0
        or not isinstance(completed, dict)
        or not isinstance(schedule_receipts, dict)
        or not isinstance(schedule_paths, dict)
        or isinstance(retained, str)
        or not isinstance(retained, (list, tuple))
    ):
        return None
    if next_index != len(completed):
        mismatches.append(f"{label}: next_index does not equal completed ledger size")
    if set(completed) != set(schedule_receipts) or set(completed) != set(schedule_paths):
        mismatches.append(f"{label}: checkpoint lineage key sets disagree")
    return (
        plan_digest,
        next_index,
        completed,
        schedule_receipts,
        schedule_paths,
        tuple(retained),
    )


def _provisional_schedule_digest(schedule: ScheduleActivationReceipt) -> str:
    """Reconstruct the pre-finalization receipt stored in its own checkpoint."""

    pending_reason = "checkpoint receipt finalization pending"
    mismatch_reasons = tuple(schedule.mismatch_reasons)
    if pending_reason not in mismatch_reasons:
        mismatch_reasons = (*mismatch_reasons, pending_reason)
    provisional = schedule.model_copy(
        update={
            "checkpoint_save_ref": None,
            "checkpoint_load_ref": None,
            "ancestor_schedule_receipt_ref": None,
            "schedule_valid": False,
            "mismatch_reasons": mismatch_reasons,
        }
    )
    content = json.dumps(
        provisional.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    return _sha256(content)


def _verify_checkpoint_prefix(
    checkpoint: Mapping[str, Any],
    *,
    schedule: ScheduleActivationReceipt,
    run_id: str,
    run_order: tuple[str, ...],
    selected_bundle_digests: Mapping[str, str],
    schedule_refs: Mapping[str, ArtifactRef],
    label: str,
    mismatches: list[str],
) -> tuple[str, tuple[str, ...], dict[str, str]] | None:
    fields = _checkpoint_fields(checkpoint, label=label, mismatches=mismatches)
    if fields is None:
        return None
    (
        plan_digest,
        next_index,
        completed,
        schedule_receipts,
        schedule_paths,
        retained,
    ) = fields
    try:
        position = run_order.index(run_id) + 1
    except ValueError:
        mismatches.append(f"{label}: checkpoint run is absent from indexed plan order")
        return plan_digest, retained, completed
    save = schedule.checkpoint_save_ref
    if save is not None and save.write_completion_sequence != position:
        mismatches.append(
            f"{label}: write_completion_sequence does not match indexed plan position"
        )
    if next_index != position:
        mismatches.append(f"{label}: next_index does not match save position")
    prefix = run_order[:position]
    if set(completed) != set(prefix):
        mismatches.append(f"{label}: completed run ids do not match plan prefix")
    expected_completed = {
        prefix_run_id: selected_bundle_digests[prefix_run_id]
        for prefix_run_id in prefix
        if prefix_run_id in selected_bundle_digests
    }
    if completed != expected_completed:
        mismatches.append(f"{label}: completed bundle digests do not match selections")

    expected_schedule_receipts = {
        prefix_run_id: (
            _provisional_schedule_digest(schedule)
            if prefix_run_id == run_id
            else schedule_refs[prefix_run_id].sha256
        )
        for prefix_run_id in prefix
        if prefix_run_id in schedule_refs
    }
    if schedule_receipts != expected_schedule_receipts:
        mismatches.append(
            f"{label}: schedule_receipts do not match finalized prefix and current provisional receipt"
        )
    expected_schedule_paths = {
        prefix_run_id: schedule_refs[prefix_run_id].path
        for prefix_run_id in prefix
        if prefix_run_id in schedule_refs
    }
    if schedule_paths != expected_schedule_paths:
        mismatches.append(f"{label}: schedule_receipt_paths do not match report lineage")
    return plan_digest, retained, completed


def _verify_schedule_checkpoints(
    schedules: Mapping[str, ScheduleActivationReceipt],
    *,
    schedule_refs: Mapping[str, ArtifactRef],
    selected_bundle_digests: Mapping[str, str],
    run_order: tuple[str, ...],
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    """Verify checkpoint ledgers, active plan binding, and ancestor chains."""

    checkpoint_documents: dict[str, dict[str, Any]] = {}
    checkpoint_fields: dict[
        str, tuple[str, tuple[str, ...], dict[str, str]]
    ] = {}
    roots: set[Path] = set()
    for run_id, schedule in schedules.items():
        label = f"schedule[{run_id}]"
        save = schedule.checkpoint_save_ref
        load = schedule.checkpoint_load_ref
        ancestor_ref = schedule.ancestor_schedule_receipt_ref
        policy = schedule.declared_checkpoint_policy
        if policy == "disabled" and any(
            item is not None for item in (save, load, ancestor_ref)
        ):
            mismatches.append(f"{label}: disabled checkpoint policy has evidence refs")
        elif policy == "save" and (
            save is None or load is not None or ancestor_ref is not None
        ):
            mismatches.append(f"{label}: save checkpoint evidence shape is invalid")
        elif policy == "save_and_resume" and any(
            item is None for item in (save, load, ancestor_ref)
        ):
            mismatches.append(
                f"{label}: save_and_resume checkpoint evidence is incomplete"
            )
        elif policy == "resume" and (
            load is None or ancestor_ref is None or save is not None
        ):
            mismatches.append(f"{label}: resume checkpoint evidence shape is invalid")
        if save is None:
            continue
        save_path, checkpoint = _verify_checkpoint_save(
            save,
            label=f"{label}.checkpoint_save",
            path_map=path_map,
            mismatches=mismatches,
        )
        roots.add(save_path.parent.parent)
        expected_name = f"{save.write_completion_sequence:04d}-{run_id}.json"
        if save_path.parent.name != "checkpoint_saves" or save_path.name != expected_name:
            mismatches.append(f"{label}: checkpoint save path/sequence binding drift")
        if checkpoint is None:
            continue
        checkpoint_documents[run_id] = checkpoint
        verified_fields = _verify_checkpoint_prefix(
            checkpoint,
            schedule=schedule,
            run_id=run_id,
            run_order=run_order,
            selected_bundle_digests=selected_bundle_digests,
            schedule_refs=schedule_refs,
            label=f"{label}.checkpoint_save",
            mismatches=mismatches,
        )
        if verified_fields is not None:
            checkpoint_fields[run_id] = verified_fields

    if not checkpoint_documents:
        return
    if len(roots) != 1:
        mismatches.append("checkpoint saves do not share one experiment root")
        return
    experiment_root = next(iter(roots))
    plan_path = experiment_root / "plan.json"
    try:
        plan_bytes = plan_path.read_bytes()
    except OSError as exc:
        mismatches.append(f"checkpoint plan is unreadable: {plan_path}: {exc}")
        return
    active_plan_digest = _sha256(plan_bytes)
    try:
        plan = json.loads(plan_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        mismatches.append("checkpoint plan is malformed")
        plan = None
    if isinstance(plan, dict) and isinstance(plan.get("runs"), list):
        plan_run_ids = tuple(
            item.get("run_id") if isinstance(item, dict) else None
            for item in plan["runs"]
        )
        if plan_run_ids != run_order:
            mismatches.append("checkpoint plan run order does not match record index")
    else:
        mismatches.append("checkpoint plan does not contain a runs list")

    live_path = experiment_root / "checkpoint.json"
    try:
        live_checkpoint = json.loads(live_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        mismatches.append(f"active checkpoint is unreadable or malformed: {exc}")
        live_checkpoint = None
    live_retained: tuple[str, ...] = ()
    if isinstance(live_checkpoint, dict):
        live_fields = _checkpoint_fields(
            live_checkpoint,
            label="active checkpoint",
            mismatches=mismatches,
        )
        if live_fields is not None:
            (
                live_plan_digest,
                live_next_index,
                live_completed,
                live_schedule_receipts,
                live_schedule_paths,
                live_retained,
            ) = live_fields
            if live_plan_digest != active_plan_digest:
                mismatches.append("active checkpoint plan_sha256 does not match plan bytes")
            if live_next_index != len(run_order) or set(live_completed) != set(run_order):
                mismatches.append("active checkpoint does not cover the indexed run order")
            expected_completed = {
                run_id: selected_bundle_digests[run_id]
                for run_id in run_order
                if run_id in selected_bundle_digests
            }
            if live_completed != expected_completed:
                mismatches.append(
                    "active checkpoint completed ledger does not match selections"
                )
            expected_receipts = {
                run_id: schedule_refs[run_id].sha256
                for run_id in run_order
                if run_id in schedule_refs
            }
            if live_schedule_receipts != expected_receipts:
                mismatches.append(
                    "active checkpoint schedule_receipts do not match final receipts"
                )
            expected_paths = {
                run_id: schedule_refs[run_id].path
                for run_id in run_order
                if run_id in schedule_refs
            }
            if live_schedule_paths != expected_paths:
                mismatches.append(
                    "active checkpoint schedule_receipt_paths do not match lineage"
                )
    elif live_checkpoint is not None:
        mismatches.append("active checkpoint root is not an object")

    saved_plan_digests = {
        plan_digest for plan_digest, _, _ in checkpoint_fields.values()
    }
    expected_retained = saved_plan_digests - {active_plan_digest}
    if set(live_retained) != expected_retained:
        mismatches.append(
            "active checkpoint retained_plan_sha256 does not bind historical plans"
        )
    allowed_plan_digests = {active_plan_digest, *live_retained}
    for run_id, (plan_digest, retained, _) in checkpoint_fields.items():
        label = f"schedule[{run_id}].checkpoint_save"
        if plan_digest not in allowed_plan_digests:
            mismatches.append(f"{label}: plan_sha256 is not active or retained")
        if not set(retained).issubset(set(live_retained)):
            mismatches.append(
                f"{label}: retained_plan_sha256 exceeds active retained lineage"
            )
        if plan_digest == active_plan_digest and set(retained) != set(live_retained):
            mismatches.append(
                f"{label}: active-plan save omits retained ancestor plan digests"
            )

    load_identity: tuple[str, str, tuple[str, ...], str] | None = None
    positions = {run_id: index for index, run_id in enumerate(run_order)}
    for run_id, schedule in schedules.items():
        load = schedule.checkpoint_load_ref
        ancestor_ref = schedule.ancestor_schedule_receipt_ref
        if load is None or ancestor_ref is None:
            continue
        label = f"schedule[{run_id}]"
        _, ancestor_bytes = _verify_ref(
            ancestor_ref,
            label=f"{label}.ancestor_schedule_receipt",
            path_map=path_map,
            mismatches=mismatches,
        )
        if ancestor_bytes is None:
            continue
        ancestor = _parse_json_model(
            ScheduleActivationReceipt,
            ancestor_bytes,
            label=f"{label}.ancestor_schedule_receipt",
            mismatches=mismatches,
        )
        if ancestor is None:
            continue
        canonical_ancestor = schedules.get(ancestor.run_id)
        canonical_ref = schedule_refs.get(ancestor.run_id)
        if canonical_ancestor != ancestor or canonical_ref != ancestor_ref:
            mismatches.append(
                f"{label}: ancestor schedule is not the indexed canonical receipt"
            )
        if (
            run_id not in positions
            or ancestor.run_id not in positions
            or positions[ancestor.run_id] >= positions[run_id]
        ):
            mismatches.append(f"{label}: ancestor schedule is not an earlier plan run")
        ancestor_save = ancestor.checkpoint_save_ref
        ancestor_fields = checkpoint_fields.get(ancestor.run_id)
        if ancestor_save is None or ancestor_fields is None:
            mismatches.append(f"{label}: ancestor checkpoint save evidence is missing")
            continue
        if load.loaded_checkpoint_digest != ancestor_save.written_digest:
            mismatches.append(
                f"{label}: loaded checkpoint digest does not match ancestor save artifact"
            )
        if load.schedule_receipt_digest != ancestor_ref.sha256:
            mismatches.append(
                f"{label}: loaded schedule digest does not match ancestor receipt"
            )
        if load.resolved_plan_digest != active_plan_digest:
            mismatches.append(
                f"{label}: resolved_plan_digest does not match active plan bytes"
            )
        ancestor_completed = ancestor_fields[2]
        if load.selected_bundle_digests != tuple(ancestor_completed.values()):
            mismatches.append(
                f"{label}: checkpoint load selections do not equal ancestor completed ledger"
            )
        identity = (
            load.loaded_checkpoint_digest,
            load.schedule_receipt_digest,
            load.selected_bundle_digests,
            load.resolved_plan_digest,
        )
        if load_identity is None:
            load_identity = identity
        elif load_identity != identity:
            mismatches.append(
                f"{label}: checkpoint load lineage differs across resumed schedules"
            )


def _verify_aggregate(
    aggregate_path: Path,
    *,
    report: ReportT,
    report_bytes: bytes,
    lineage_entries: list[tuple[Any, EvidenceBundle | None]],
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    """Validate every summary field against the verified lineage bytes."""

    try:
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        mismatches.append(f"aggregate: cannot load {aggregate_path}: {exc}")
        return
    if not isinstance(aggregate, dict):
        mismatches.append("aggregate: root must be an object")
        return

    required = {
        "experiment_id",
        "experiment_digest",
        "run_count",
        "statuses",
        "scores",
        "schedule_receipts",
        "run_report_sha256",
    }
    keys = set(aggregate)
    missing = sorted(required - keys)
    unexpected = sorted(keys - required)
    if missing:
        mismatches.append(f"aggregate: missing fields {missing}")
    if unexpected:
        mismatches.append(f"aggregate: unexpected fields {unexpected}")
    if missing or unexpected:
        return

    if aggregate["experiment_id"] != report.experiment_id:
        mismatches.append("aggregate experiment_id does not match report experiment_id")
    if aggregate["experiment_digest"] != report.manifest_digest:
        mismatches.append("aggregate experiment_digest does not match report manifest_digest")
    observed_report_digest = _sha256(report_bytes)
    if aggregate["run_report_sha256"] != observed_report_digest:
        mismatches.append(
            "aggregate run_report_sha256 mismatch: "
            f"expected {aggregate['run_report_sha256']}, observed {observed_report_digest}"
        )

    expected_count = len(report.lineage)
    if (
        not isinstance(aggregate["run_count"], int)
        or isinstance(aggregate["run_count"], bool)
        or aggregate["run_count"] != expected_count
    ):
        mismatches.append(
            f"aggregate run_count mismatch: expected {expected_count}, "
            f"observed {aggregate['run_count']!r}"
        )

    expected_statuses = [
        bundle.status.value
        for _, bundle in lineage_entries
        if bundle is not None
    ]
    expected_scores = [
        (
            None
            if bundle is None or bundle.verifier_evidence is None
            else bundle.verifier_evidence.score
        )
        for _, bundle in lineage_entries
    ]
    if aggregate["statuses"] != expected_statuses:
        mismatches.append(
            f"aggregate statuses mismatch: expected {expected_statuses!r}, "
            f"observed {aggregate['statuses']!r}"
        )
    if aggregate["scores"] != expected_scores:
        mismatches.append(
            f"aggregate scores mismatch: expected {expected_scores!r}, "
            f"observed {aggregate['scores']!r}"
        )

    parent_counts = Counter(lineage.run_id for lineage, _ in lineage_entries)
    expected_receipts: dict[str, str] = {}
    for lineage, _ in lineage_entries:
        digest = lineage.schedule_receipt_ref.sha256
        aggregate_key = (
            lineage.run_id
            if parent_counts[lineage.run_id] == 1
            else f"{lineage.run_id}::{lineage.case_id}"
        )
        previous = expected_receipts.setdefault(aggregate_key, digest)
        if previous != digest:
            mismatches.append(
                f"aggregate schedule receipt binding has conflicting refs for {aggregate_key!r}"
            )
    if aggregate["schedule_receipts"] != expected_receipts:
        mismatches.append(
            "aggregate schedule_receipts mismatch: "
            f"expected {expected_receipts!r}, observed {aggregate['schedule_receipts']!r}"
        )


def _canonical_key(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _active_schedule_digests() -> tuple[str, str] | None:
    """Return the schedule implementation identities trusted by this verifier.

    A receipt's scheduler/pipeline digests are self-asserted unless they are
    compared with an independently loaded implementation.  If the installed
    implementation sources are unavailable, a positive protocol gate cannot
    be reconstructed and therefore fails closed.
    """

    runner_root = Path(__file__).resolve().parents[1] / "runner"
    scheduler_path = runner_root / "scheduler.py"
    pipeline_paths = (runner_root / "pipeline.py", runner_root / "gates.py")
    try:
        scheduler_digest = _sha256(scheduler_path.read_bytes())
        pipeline_digest = hashlib.sha256()
        for path in pipeline_paths:
            pipeline_digest.update(path.name.encode("utf-8"))
            pipeline_digest.update(b"\0")
            pipeline_digest.update(path.read_bytes())
            pipeline_digest.update(b"\0")
    except OSError:
        return None
    return scheduler_digest, pipeline_digest.hexdigest()


def _schedule_substantiates_protocol(
    schedule: ScheduleActivationReceipt | None,
    manifest: ResolvedBmpManifest,
    *,
    active_digests: tuple[str, str] | None,
) -> bool:
    """Recompute the persisted portion of ``_receipt_binding_errors``."""

    protocol = manifest.execution.protocol
    if schedule is None or protocol is None or active_digests is None:
        return False
    scheduler_digest, pipeline_digest = active_digests
    if (
        not schedule.schedule_valid
        or schedule.run_id != manifest.metadata.run_id
        or schedule.protocol_digest != canonical_digest(protocol)
        or schedule.scheduler_digest != scheduler_digest
        or schedule.pipeline_digest != pipeline_digest
        or schedule.declared_rollouts_per_case != protocol.rollouts_per_case
        or schedule.declared_parallelism != protocol.parallelism
        or schedule.declared_case_order != protocol.case_order
        or schedule.declared_state_reset != protocol.state_reset
        or schedule.declared_candidate_selection != protocol.candidate_selection
        or schedule.declared_checkpoint_policy != protocol.checkpoint_policy
        or schedule.order_seed
        != (
            manifest.execution.seed
            if protocol.case_order == "seeded_random"
            else None
        )
        or schedule.observed_attempt_count != len(schedule.attempts)
        or schedule.observed_selection_policy != protocol.candidate_selection
    ):
        return False

    allocated_case_ids = tuple(
        allocation.case_id for allocation in schedule.budget_ledger.case_allocations
    )
    if schedule.observed_case_order != allocated_case_ids:
        return False
    if protocol.case_order in {"custom", "explicit"} and (
        schedule.observed_case_order != tuple(protocol.explicit_case_ids)
    ):
        return False
    launched_case_ids = {attempt.case_id for attempt in schedule.attempts}
    expected_resets = {
        "never": 0,
        "per_case": len(launched_case_ids),
        "per_rollout": len(schedule.attempts),
    }[protocol.state_reset]
    if schedule.observed_state_reset_count != expected_resets:
        return False

    budget = manifest.execution.budget
    for field_name in ("max_tokens", "max_cost"):
        declared = getattr(budget, field_name)
        allocated = [
            getattr(allocation.allocated, field_name)
            for allocation in schedule.budget_ledger.case_allocations
        ]
        if declared is None:
            if any(value is not None for value in allocated):
                return False
            continue
        if any(value is None for value in allocated):
            return False
        observed = sum(allocated)
        equal = (
            observed == declared
            if field_name == "max_tokens"
            else isclose(observed, declared, rel_tol=0.0, abs_tol=1e-12)
        )
        if not equal:
            return False
    return True


def _statistics_are_substantiated(
    lineage_entries: list[tuple[Any, EvidenceBundle | None]],
    manifest_by_run: Mapping[str, ResolvedBmpManifest],
) -> bool:
    """Recompute the currently implemented deterministic statistics gate."""

    if not lineage_entries or not manifest_by_run:
        return False
    ordered: list[tuple[Any, EvidenceBundle, ResolvedBmpManifest]] = []
    for lineage, bundle in lineage_entries:
        manifest = manifest_by_run.get(lineage.run_id)
        if bundle is None or manifest is None:
            return False
        ordered.append((lineage, bundle, manifest))

    contrast_keys = {
        _canonical_key(manifest.contrast.model_dump(mode="json"))
        for _, _, manifest in ordered
    }
    protocol_keys = {
        None
        if manifest.execution.protocol is None
        else canonical_digest(manifest.execution.protocol)
        for _, _, manifest in ordered
    }
    if len(contrast_keys) != 1 or len(protocol_keys) != 1 or None in protocol_keys:
        return False
    contrast = ordered[0][2].contrast
    protocol = ordered[0][2].execution.protocol
    assert protocol is not None
    control_id = contrast.control_id or ordered[0][2].subject.id
    treatment_id = contrast.treatment_id or ordered[-1][2].subject.id

    pairs: dict[bytes, dict[str, ResolvedBmpManifest]] = {}
    for lineage, _, manifest in ordered:
        factors = {
            key: value
            for key, value in manifest.metadata.factors.items()
            if key not in {"subject", "experiment.subject", "order_position"}
        }
        factors["__case_id"] = lineage.case_id
        pair = pairs.setdefault(_canonical_key(factors), {})
        if manifest.subject.id in pair:
            return False
        pair[manifest.subject.id] = manifest
    if not pairs or any(
        set(pair) != {control_id, treatment_id} for pair in pairs.values()
    ):
        return False

    if not protocol.deterministic_conformance or not contrast.counterbalanced:
        return False
    positions = {
        (lineage.run_id, lineage.case_id): index
        for index, (lineage, _, _) in enumerate(ordered)
    }
    directions: dict[bytes, set[bool]] = {}
    counts: Counter[bytes] = Counter()
    for pair in pairs.values():
        control = pair[control_id]
        treatment = pair[treatment_id]
        outer_factors = {
            key: value
            for key, value in control.metadata.factors.items()
            if key
            not in {
                "subject",
                "experiment.subject",
                "order_position",
                "repetition",
            }
        }
        # The case is part of the independent paired unit.  Keeping it in the
        # outer key prevents counterbalance evidence from one case masking a
        # missing direction in another.
        control_lineage = next(
            lineage
            for lineage, _, manifest in ordered
            if manifest is control
        )
        outer_factors["__case_id"] = control_lineage.case_id
        outer_key = _canonical_key(outer_factors)
        directions.setdefault(outer_key, set()).add(
            positions[(control_lineage.run_id, control_lineage.case_id)]
            < positions[
                (
                    next(
                        lineage
                        for lineage, _, manifest in ordered
                        if manifest is treatment
                    ).run_id,
                    control_lineage.case_id,
                )
            ]
        )
        counts[outer_key] += 1
    if not directions or any(
        counts[key] < 2 or values != {False, True}
        for key, values in directions.items()
    ):
        return False
    return all(
        bundle.verifier_evidence is not None
        and bundle.verifier_evidence.score is not None
        for _, bundle, _ in ordered
    )


def _expected_gate_evidence(
    lineage_entries: list[tuple[Any, EvidenceBundle | None]],
) -> Mapping[GateName, tuple[str, ...]]:
    """Rebuild the exact positive-evidence path contract used by the runner."""

    bundle_paths = tuple(
        dict.fromkeys(
            lineage.evidence_bundle_ref.path for lineage, _ in lineage_entries
        )
    )
    schedule_paths = tuple(
        dict.fromkeys(
            lineage.schedule_receipt_ref.path for lineage, _ in lineage_entries
        )
    )
    isolation_paths = tuple(
        dict.fromkeys(
            ref.path
            for _, bundle in lineage_entries
            if bundle is not None and bundle.network_observation is not None
            for ref in bundle.network_observation.evidence_refs
        )
    )
    scoring_paths = tuple(
        dict.fromkeys(
            (
                *bundle_paths,
                *(
                    ref.path
                    for _, bundle in lineage_entries
                    if bundle is not None and bundle.verifier_evidence is not None
                    for ref in bundle.verifier_evidence.artifact_refs
                ),
            )
        )
    )
    return {
        GateName.execution_valid: bundle_paths,
        GateName.protocol_valid: schedule_paths,
        GateName.isolation_valid: isolation_paths,
        GateName.scoring_valid: scoring_paths,
        GateName.statistics_valid: schedule_paths,
    }


def _network_policy_binding_reasons(
    lineage: Any,
    bundle: EvidenceBundle,
    manifest: ResolvedBmpManifest,
    case_set_receipt: CaseSetActivationReceipt | None,
) -> list[str]:
    """Recompute network policy and observation cross-link mismatches."""

    case_id = lineage.case_id
    policy = bundle.network_policy
    observation = bundle.network_observation
    errors: list[str] = []
    # Some adapters (including the conformance fake) intentionally emit no
    # network sidecar.  Absence on both sides is an explicitly unobservable
    # observation, not a cross-link mismatch; a one-sided or populated pair
    # must still be bound below.
    if policy is None and observation is None:
        return errors
    if policy is None:
        errors.append(f"{case_id}: ResolvedNetworkPolicy missing")
    if observation is None:
        errors.append(f"{case_id}: NetworkObservation missing")
    if policy is None or observation is None:
        return errors

    adapter = manifest.execution.backend.adapter
    if policy.execution_adapter != adapter:
        errors.append(f"{case_id}: network policy execution adapter mismatch")
    if policy.case_id != case_id:
        errors.append(f"{case_id}: network policy case binding mismatch")
    expected_boundary = {
        "fake": "process",
        "subprocess": "process",
        "harbor-shim": "process",
        "aose-docker": "task_container",
        "harbor": "task_container",
    }.get(adapter)
    if expected_boundary is None:
        errors.append(
            f"{case_id}: missing NetworkPolicyActivationReceipt for adapter {adapter!r}"
        )
    elif policy.boundary.value != expected_boundary:
        errors.append(f"{case_id}: network policy boundary mismatch")

    if policy.source.value == "backend_artifact":
        if policy.resolver_adapter != adapter:
            errors.append(f"{case_id}: network policy resolver adapter mismatch")
        expected_source = (
            manifest.execution.backend.digest or bundle.provenance.runner_digest
        )
        if policy.source_artifact_digest != expected_source:
            errors.append(f"{case_id}: network policy source artifact digest drift")
    else:
        if policy.resolver_adapter != manifest.benchmark.adapter:
            errors.append(f"{case_id}: case-set network policy resolver mismatch")
        if case_set_receipt is None:
            errors.append(
                f"{case_id}: case-set network policy requires missing "
                "CaseSetActivationReceipt"
            )
        elif policy.source_artifact_digest != case_set_receipt.case_set_digest:
            errors.append(f"{case_id}: case-set network policy source digest drift")

    if observation.policy_digest != canonical_digest(policy):
        errors.append(f"{case_id}: network observation policy digest drift")
    if observation.declared_allow_internet != policy.allow_internet:
        errors.append(
            f"{case_id}: network observation disagrees with resolved policy"
        )
    if observation.mode != policy.required_observation:
        errors.append(
            f"{case_id}: network observation mode does not satisfy policy"
        )
    if not observation.evidence_refs:
        errors.append(f"{case_id}: NetworkObservation evidence reference missing")
    return errors


def _network_isolation_reasons(
    lineage: Any,
    bundle: EvidenceBundle,
    manifest: ResolvedBmpManifest,
    case_set_receipt: CaseSetActivationReceipt | None,
) -> list[str]:
    """Recompute the evaluator's network-isolation reasons from persisted bytes."""

    errors = _network_policy_binding_reasons(
        lineage,
        bundle,
        manifest,
        case_set_receipt,
    )
    observation = bundle.network_observation
    if observation is None:
        if bundle.network_policy is None:
            return [
                f"{lineage.case_id}: ResolvedNetworkPolicy missing",
                f"{lineage.case_id}: NetworkObservation missing",
            ]
        return errors
    if not errors and not observation.claim_isolation_valid:
        errors.append(
            f"{lineage.case_id}: NetworkObservation cannot substantiate isolation"
        )
    return errors


def _verify_report_semantics(
    report: ReportT,
    *,
    lineage_entries: list[tuple[Any, EvidenceBundle | None]],
    manifest_by_run: Mapping[str, ResolvedBmpManifest],
    schedules: Mapping[
        tuple[str, str, str], ScheduleActivationReceipt | None
    ],
    case_set_receipts: Mapping[
        tuple[str, str, str], CaseSetActivationReceipt | None
    ],
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    """Recompute report summaries from the verified lineage bytes."""

    if any(bundle is None for _, bundle in lineage_entries):
        return
    bundles = [bundle for _, bundle in lineage_entries if bundle is not None]
    expected_breakdown = {
        status.value: count
        for status, count in Counter(bundle.status for bundle in bundles).items()
    }
    actual_breakdown = {
        (key.value if isinstance(key, RunStatus) else str(key)): value
        for key, value in report.failure_breakdown.items()
    }
    if actual_breakdown != expected_breakdown:
        mismatches.append(
            "report failure_breakdown does not match verified lineage statuses"
        )

    if isinstance(report, ClaimReport):
        expected_gate_evidence = _expected_gate_evidence(lineage_entries)
        for gate_name, gate in report.gates.items():
            if gate.valid and gate.evidence_refs != expected_gate_evidence[gate_name]:
                mismatches.append(
                    f"claim gate {gate_name.value} evidence_refs do not match "
                    "verified runner evidence"
                )
        execution_expected = bool(bundles) and all(
            bundle.status in {RunStatus.pass_, RunStatus.verified_fail}
            for bundle in bundles
        ) and len(bundles) == len(lineage_entries)
        execution_gate = report.gates[GateName.execution_valid]
        if execution_gate.valid != execution_expected:
            mismatches.append("claim execution_valid does not match verified lineage")
        active_schedule_digests = _active_schedule_digests()
        protocol_expected = bool(lineage_entries) and all(
            lineage.run_id in manifest_by_run
            and _schedule_substantiates_protocol(
                schedules.get(
                    (lineage.run_id, lineage.attempt_id, lineage.case_id)
                ),
                manifest_by_run[lineage.run_id],
                active_digests=active_schedule_digests,
            )
            for lineage, _ in lineage_entries
        )
        protocol_gate = report.gates[GateName.protocol_valid]
        if protocol_gate.valid != protocol_expected:
            mismatches.append("claim protocol_valid does not match verified schedule")
        scoring_expected = False
        if execution_expected:
            scoring_expected = all(
                lineage.run_id in manifest_by_run
                and bundle.verifier_evidence is not None
                and bundle.verifier_evidence.score is not None
                and manifest_by_run[lineage.run_id].benchmark.authoritative_reward_metric
                in bundle.verifier_evidence.metrics
                and bundle.verifier_evidence.metrics[
                    manifest_by_run[lineage.run_id].benchmark.authoritative_reward_metric
                ]
                == bundle.verifier_evidence.score
                for lineage, bundle in lineage_entries
                if bundle is not None
            )
        scoring_gate = report.gates[GateName.scoring_valid]
        if scoring_gate.valid != scoring_expected:
            mismatches.append("claim scoring_valid does not match verified lineage")
        isolation_reasons = []
        for lineage, bundle in lineage_entries:
            if bundle is None or lineage.run_id not in manifest_by_run:
                continue
            isolation_reasons.extend(
                _network_isolation_reasons(
                    lineage,
                    bundle,
                    manifest_by_run[lineage.run_id],
                    case_set_receipts.get(
                        (lineage.run_id, lineage.attempt_id, lineage.case_id)
                    ),
                )
            )
        isolation_gate = report.gates[GateName.isolation_valid]
        isolation_expected = bool(lineage_entries) and not isolation_reasons
        if isolation_gate.valid != isolation_expected:
            mismatches.append("claim isolation_valid does not match verified network evidence")
        statistics_expected = _statistics_are_substantiated(
            lineage_entries, manifest_by_run
        )
        statistics_gate = report.gates[GateName.statistics_valid]
        if statistics_gate.valid != statistics_expected:
            mismatches.append(
                "claim statistics_valid does not match verified pairing and scores"
            )

    metric_scores: list[tuple[str, float, tuple[str, str]]] = []
    for lineage, bundle in lineage_entries:
        assert bundle is not None
        manifest = manifest_by_run.get(lineage.run_id)
        if manifest is None or bundle.verifier_evidence is None:
            continue
        score = bundle.verifier_evidence.score
        if score is None:
            continue
        metric = manifest.benchmark.authoritative_reward_metric
        named_score = bundle.verifier_evidence.metrics.get(metric)
        if named_score is None or named_score != score:
            mismatches.append(
                f"{lineage.attempt_id}: authoritative metric {metric!r} "
                "does not bind to verifier score"
            )
            continue
        metric_scores.append((metric, named_score, (lineage.run_id, lineage.case_id)))

    if isinstance(report, ObservationReport):
        if not lineage_entries:
            if report.isolation_valid:
                mismatches.append(
                    "report isolation_valid cannot be positive without executed lineage"
                )
        else:
            expected_isolation_reasons: list[str] = []
            for lineage, bundle in lineage_entries:
                if bundle is None:
                    continue
                manifest = manifest_by_run.get(lineage.run_id)
                if manifest is None:
                    continue
                key = (lineage.run_id, lineage.attempt_id, lineage.case_id)
                expected_isolation_reasons.extend(
                    _network_isolation_reasons(
                        lineage,
                        bundle,
                        manifest,
                        case_set_receipts.get(key),
                    )
                )
            expected_isolation_reasons = sorted(set(expected_isolation_reasons))
            if report.isolation_valid != (not expected_isolation_reasons):
                mismatches.append(
                    "report isolation_valid does not match verified network evidence"
                )
            if tuple(report.isolation_reasons) != tuple(expected_isolation_reasons):
                mismatches.append(
                    "report isolation_reasons do not match verified network evidence"
                )
        metrics = {metric for metric, _, _ in metric_scores}
        if len(metrics) > 1:
            mismatches.append("observation report contains multiple authoritative metrics")
            return
        expected_observations = (
            ()
            if not metric_scores
            else (
                {
                    "metric": next(iter(metrics)),
                    "value": mean(score for _, score, _ in metric_scores),
                    "n_runs": len(metric_scores),
                },
            )
        )
        actual_observations = tuple(
            {
                "metric": observation.metric,
                "value": observation.value,
                "n_runs": observation.n_runs,
            }
            for observation in report.observations
        )
        if actual_observations != expected_observations:
            mismatches.append(
                "observation report summaries do not match authoritative lineage scores"
            )
        return

    # Claim effects are paired by the same factor projection used by the
    # evaluator. This catches a report that changes metric/effect fields while
    # preserving all content-addressed source refs.
    if not isinstance(report, ClaimReport):
        return
    metric_set = {
        manifest.benchmark.authoritative_reward_metric
        for manifest in manifest_by_run.values()
    }
    authoritative_metric = next(iter(metric_set)) if len(metric_set) == 1 else None
    scores_by_lineage = {
        lineage_key: score for _, score, lineage_key in metric_scores
    }
    if authoritative_metric is None or len(scores_by_lineage) != len(lineage_entries):
        expected_effect = None
    else:
        first_manifest = next(iter(manifest_by_run.values()), None)
        contrast = None if first_manifest is None else first_manifest.contrast
        if contrast is None:
            expected_effect = None
        else:
            control_id = contrast.control_id or first_manifest.subject.id
            treatment_id = contrast.treatment_id or next(
                iter(manifest_by_run.values())
            ).subject.id
            groups: dict[bytes, dict[str, tuple[tuple[str, str], float]]] = {}
            for lineage, _, manifest in (
                (lineage, bundle, manifest_by_run[lineage.run_id])
                for lineage, bundle in lineage_entries
            ):
                factors = {
                    key: value
                    for key, value in manifest.metadata.factors.items()
                    if key not in {"subject", "experiment.subject", "order_position"}
                }
                factors["__case_id"] = lineage.case_id
                groups.setdefault(_canonical_key(factors), {})[
                    manifest.subject.id
                ] = (
                    (lineage.run_id, lineage.case_id),
                    scores_by_lineage.get(
                        (lineage.run_id, lineage.case_id), float("nan")
                    ),
                )
            differences: list[float] = []
            for pair in groups.values():
                if control_id not in pair or treatment_id not in pair:
                    differences = []
                    break
                control_score = pair[control_id][1]
                treatment_score = pair[treatment_id][1]
                if control_score != control_score or treatment_score != treatment_score:
                    differences = []
                    break
                differences.append(treatment_score - control_score)
            expected_effect = (
                None
                if not differences
                else {
                    "metric": authoritative_metric,
                    "point_estimate": mean(differences),
                    "confidence_interval": (min(differences), max(differences)),
                    "n_runs": len(lineage_entries),
                    "n_pairs": len(differences),
                }
            )
    actual_effect = (
        None
        if report.effect is None
        else {
            "metric": report.effect.metric,
            "point_estimate": report.effect.point_estimate,
            "confidence_interval": tuple(report.effect.confidence_interval),
            "n_runs": report.effect.n_runs,
            "n_pairs": report.effect.n_pairs,
        }
    )
    if actual_effect != expected_effect:
        mismatches.append("claim effect estimate does not match authoritative lineage scores")


def _verify_report(
    report_path: str | Path,
    *,
    expected_type: type[ReportT],
    path_map: Mapping[str, str] | None = None,
) -> tuple[ReportT, Path, RecordIndex]:
    relocation = {} if path_map is None else dict(path_map)
    path = Path(report_path).expanduser().resolve()
    mismatches: list[str] = []
    try:
        report_bytes = path.read_bytes()
    except OSError as exc:
        raise ReportVerificationError([f"report: cannot read {path}: {exc}"]) from exc
    report = _parse_json_model(expected_type, report_bytes, label="report", mismatches=mismatches)
    if report is None:
        raise ReportVerificationError(mismatches)
    if report.record_index_ref is None:
        mismatches.append("report: record_index_ref is missing; standalone provenance is incomplete")
        raise ReportVerificationError(mismatches)

    _, index_bytes = _verify_ref(
        report.record_index_ref,
        label="record index",
        path_map=relocation,
        mismatches=mismatches,
    )
    index = (
        None
        if index_bytes is None
        else _parse_json_model(
            RecordIndex,
            index_bytes,
            label="record index",
            mismatches=mismatches,
        )
    )
    if index is None:
        raise ReportVerificationError(mismatches)
    if index.experiment_id != report.experiment_id:
        mismatches.append(
            f"record index: experiment_id mismatch: report={report.experiment_id}, index={index.experiment_id}"
        )

    manifests: list[ResolvedBmpManifest] = []
    manifest_by_run: dict[str, ResolvedBmpManifest] = {}
    manifest_digests: list[str] = []
    for position, ref in enumerate(index.manifest_refs):
        _, manifest_bytes = _verify_ref(
            ref,
            label=f"manifest[{position}]",
            path_map=relocation,
            mismatches=mismatches,
        )
        if manifest_bytes is None:
            continue
        manifest = _parse_json_model(
            ResolvedBmpManifest,
            manifest_bytes,
            label=f"manifest[{position}]",
            mismatches=mismatches,
        )
        if manifest is None:
            continue
        manifests.append(manifest)
        manifest_digests.append(manifest.canonical_digest())
        run_id = manifest.metadata.run_id
        if run_id in manifest_by_run:
            mismatches.append(f"manifest[{position}]: duplicate run_id {run_id!r}")
        else:
            manifest_by_run[run_id] = manifest
        if manifest.metadata.experiment_id != report.experiment_id:
            mismatches.append(
                f"manifest[{position}]: experiment_id does not match report"
            )
        if manifest.claim_design.purpose != report.purpose:
            mismatches.append(
                f"manifest[{position}]: claim_design purpose does not match report purpose"
            )
        if manifest.metadata.test_override is not None:
            mismatches.append(
                f"manifest[{position}]: test_override lineage cannot produce a verified report"
            )
        _verify_manifest_configuration(
            manifest,
            label=f"manifest[{position}].configuration",
            path_map=relocation,
            mismatches=mismatches,
        )
    observed_experiment_digest = _compact_json_digest(manifest_digests)
    if observed_experiment_digest != report.manifest_digest:
        mismatches.append(
            "report manifest_digest mismatch: "
            f"expected {report.manifest_digest}, observed {observed_experiment_digest}"
        )
    subject_kinds = {manifest.subject.kind for manifest in manifests}
    if manifests and (
        len(subject_kinds) != 1 or report.subject_kind.value not in subject_kinds
    ):
        mismatches.append(
            "report subject_kind does not match indexed resolved manifests"
        )

    lineage_keys: set[tuple[str, str, str]] = set()
    selected_attempt_keys: set[tuple[str, str, str]] = set()
    seen_schedule_refs: set[tuple[str, str]] = set()
    lineage_entries: list[tuple[Any, EvidenceBundle | None]] = []
    case_set_receipts: dict[
        tuple[str, str, str], CaseSetActivationReceipt | None
    ] = {}
    schedules: dict[
        tuple[str, str, str], ScheduleActivationReceipt | None
    ] = {}
    checkpoint_schedules: dict[str, ScheduleActivationReceipt] = {}
    checkpoint_schedule_refs: dict[str, ArtifactRef] = {}
    selected_bundle_digests: dict[str, str] = {}
    schedule_attempt_bundles: dict[tuple[str, str], EvidenceBundle | None] = {}
    lineage_parent_counts = Counter(item.run_id for item in report.lineage)
    for position, lineage in enumerate(report.lineage):
        label = f"lineage[{position}]"
        key = (lineage.run_id, lineage.attempt_id, lineage.case_id)
        if key in lineage_keys:
            mismatches.append(f"{label}: duplicate lineage key {key!r}")
        lineage_keys.add(key)
        if lineage.run_id not in manifest_by_run:
            mismatches.append(
                f"{label}: parent run_id {lineage.run_id!r} is absent from record index"
            )
        # A schedule receipt is shared by all cases in one parent run, but
        # each selected case has its own evidence bundle identity.
        selected_key = (
            lineage.run_id
            if lineage_parent_counts[lineage.run_id] == 1
            else f"{lineage.run_id}::{lineage.case_id}"
        )
        previous_selected = selected_bundle_digests.setdefault(
            selected_key, lineage.evidence_bundle_ref.sha256
        )
        if previous_selected != lineage.evidence_bundle_ref.sha256:
            mismatches.append(
                f"{label}: selected case has conflicting bundle digests"
            )
        schedule_key_for_checkpoint = (
            lineage.run_id
            if lineage_parent_counts[lineage.run_id] == 1
            else f"{lineage.run_id}::{lineage.case_id}"
        )
        previous_schedule_ref = checkpoint_schedule_refs.setdefault(
            schedule_key_for_checkpoint, lineage.schedule_receipt_ref
        )
        if previous_schedule_ref != lineage.schedule_receipt_ref:
            mismatches.append(
                f"{label}: selected case has conflicting schedule receipt refs"
            )

        _, bundle_bytes = _verify_ref(
            lineage.evidence_bundle_ref,
            label=f"{label}.evidence_bundle",
            path_map=relocation,
            mismatches=mismatches,
        )
        bundle = (
            None
            if bundle_bytes is None
            else _parse_json_model(
                EvidenceBundle,
                bundle_bytes,
                label=f"{label}.evidence_bundle",
                mismatches=mismatches,
            )
        )
        if bundle is not None:
            if bundle.run_id != lineage.attempt_id:
                mismatches.append(
                    f"{label}: bundle run_id {bundle.run_id!r} != attempt_id {lineage.attempt_id!r}"
                )
            _verify_bundle_artifacts(
                bundle,
                label=f"{label}.evidence_bundle",
                path_map=relocation,
                mismatches=mismatches,
            )
            manifest = manifest_by_run.get(lineage.run_id)
            if manifest is not None:
                _verify_bundle_provenance(
                    bundle,
                    manifest,
                    label=f"{label}.evidence_bundle",
                    path_map=relocation,
                    mismatches=mismatches,
                )

        manifest = manifest_by_run.get(lineage.run_id)
        protocol = None if manifest is None else manifest.execution.protocol
        case_set_receipt = _verify_case_set_receipt(
            lineage.case_set_receipt_ref,
            case_id=lineage.case_id,
            label=f"{label}.case_set_receipt",
            path_map=relocation,
            mismatches=mismatches,
            expected_benchmark_id=(
                None if manifest is None else manifest.benchmark.id
            ),
            expected_benchmark_digest=(
                None if manifest is None else manifest.benchmark.artifact_digest
            ),
            expected_loader_adapter=(
                None if manifest is None else manifest.benchmark.adapter
            ),
            expected_case_order=(
                None if protocol is None else protocol.case_order
            ),
            expected_order_seed=(
                manifest.execution.seed
                if protocol is not None and protocol.case_order == "seeded_random"
                else None
            ),
            expected_source_content_digest=(
                None
                if manifest is None
                else manifest.benchmark.source_content_digest
            ),
        )
        case_set_receipts[key] = case_set_receipt

        _, schedule_bytes = _verify_ref(
            lineage.schedule_receipt_ref,
            label=f"{label}.schedule_receipt",
            path_map=relocation,
            mismatches=mismatches,
        )
        schedule = (
            None
            if schedule_bytes is None
            else _parse_json_model(
                ScheduleActivationReceipt,
                schedule_bytes,
                label=f"{label}.schedule_receipt",
                mismatches=mismatches,
            )
        )
        schedules[key] = schedule
        if schedule is not None:
            if schedule.run_id != lineage.run_id:
                mismatches.append(
                    f"{label}: schedule run_id {schedule.run_id!r} != lineage run_id {lineage.run_id!r}"
                )
            manifest = manifest_by_run.get(schedule.run_id)
            if manifest is not None:
                _verify_schedule_manifest_binding(
                    schedule,
                    manifest,
                    case_set_receipt=case_set_receipt,
                    label=f"{label}.schedule_receipt",
                    mismatches=mismatches,
                )
            schedule_key = (lineage.run_id, lineage.schedule_receipt_ref.sha256)
            if schedule_key not in seen_schedule_refs:
                seen_schedule_refs.add(schedule_key)
                checkpoint_schedules.setdefault(schedule.run_id, schedule)
                if checkpoint_schedules[schedule.run_id] != schedule:
                    mismatches.append(
                        f"{label}: conflicting schedule receipts for parent run"
                    )
                for attempt_index, attempt in enumerate(schedule.attempts):
                    attempt_label = f"{label}.schedule_receipt.attempts[{attempt_index}]"
                    attempt_bundle = _verify_schedule_attempt(
                        attempt,
                        manifest=manifest,
                        case_set_receipt=case_set_receipt,
                        label=attempt_label,
                        path_map=relocation,
                        mismatches=mismatches,
                    )
                    schedule_attempt_bundles[(schedule_key[1], attempt.attempt_id)] = (
                        attempt_bundle
                    )
                    if attempt.selected:
                        selected_attempt_keys.add(
                            (schedule.run_id, attempt.attempt_id, attempt.case_id)
                        )
            matches = [
                attempt
                for attempt in schedule.attempts
                if attempt.attempt_id == lineage.attempt_id
            ]
            if len(matches) != 1:
                mismatches.append(
                    f"{label}: attempt_id {lineage.attempt_id!r} is not unique in schedule"
                )
            else:
                attempt = matches[0]
                if attempt.case_id != lineage.case_id:
                    mismatches.append(
                        f"{label}: attempt case_id {attempt.case_id!r} "
                        f"!= lineage case_id {lineage.case_id!r}"
                    )
                if not attempt.selected:
                    mismatches.append(f"{label}: lineage attempt is not selected")
                if attempt.evidence_bundle_ref != lineage.evidence_bundle_ref:
                    mismatches.append(
                        f"{label}: lineage evidence ref differs from schedule attempt"
                    )
                attempt_bundle = schedule_attempt_bundles.get(
                    (lineage.schedule_receipt_ref.sha256, lineage.attempt_id)
                )
                if (
                    attempt_bundle is not None
                    and bundle is not None
                    and attempt_bundle != bundle
                ):
                    mismatches.append(
                        f"{label}: schedule attempt bundle differs from lineage bundle"
                    )

        lineage_entries.append((lineage, bundle))

    indexed_run_ids = set(manifest_by_run)
    lineage_run_ids = {lineage.run_id for lineage in report.lineage}
    if indexed_run_ids != lineage_run_ids:
        mismatches.append(
            "record index/run lineage coverage mismatch: "
            f"manifests={sorted(indexed_run_ids)}, lineage={sorted(lineage_run_ids)}"
        )
    if selected_attempt_keys and selected_attempt_keys != lineage_keys:
        mismatches.append(
            "schedule selected-attempt/lineage coverage mismatch: "
            f"selected={sorted(selected_attempt_keys)}, lineage={sorted(lineage_keys)}"
        )

    _verify_schedule_checkpoints(
        checkpoint_schedules,
        schedule_refs=checkpoint_schedule_refs,
        selected_bundle_digests=selected_bundle_digests,
        run_order=tuple(manifest.metadata.run_id for manifest in manifests),
        path_map=relocation,
        mismatches=mismatches,
    )

    _verify_report_semantics(
        report,
        lineage_entries=lineage_entries,
        manifest_by_run=manifest_by_run,
        schedules=schedules,
        case_set_receipts=case_set_receipts,
        path_map=relocation,
        mismatches=mismatches,
    )
    try:
        aggregate_path = _resolve_path(index.aggregate_path, relocation)
    except ValueError as exc:
        mismatches.append(f"aggregate: {exc}")
    else:
        _verify_aggregate(
            aggregate_path,
            report=report,
            report_bytes=report_bytes,
            lineage_entries=lineage_entries,
            path_map=relocation,
            mismatches=mismatches,
        )

    if mismatches:
        raise ReportVerificationError(mismatches)
    return report, path, index


def verify_claim_report(
    report_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedClaimReport:
    report, path, index = _verify_report(
        report_path,
        expected_type=ClaimReport,
        path_map=path_map,
    )
    return VerifiedClaimReport(
        report=report,
        report_path=path,
        record_index=index,
        warnings=(),
    )


def verify_observation_report(
    report_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedObservationReport:
    report, path, index = _verify_report(
        report_path,
        expected_type=ObservationReport,
        path_map=path_map,
    )
    return VerifiedObservationReport(report=report, report_path=path, record_index=index)


def verify_run_report(
    report_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedRunReport:
    path = Path(report_path).expanduser().resolve()
    try:
        parsed: RunReport = RunReportAdapter.validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ReportVerificationError([f"report syntax: {exc}"]) from exc
    if isinstance(parsed, ClaimReport):
        return verify_claim_report(path, path_map=path_map)
    return verify_observation_report(path, path_map=path_map)


def _parse_path_maps(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("--map values must be OLD=NEW")
        old, new = value.split("=", 1)
        if (
            not old
            or not new
            or not Path(old).is_absolute()
            or not Path(new).is_absolute()
        ):
            raise argparse.ArgumentTypeError(
                "--map values must use absolute OLD=NEW paths"
            )
        normalized_old = old.rstrip("/") or "/"
        if normalized_old in result:
            raise argparse.ArgumentTypeError(
                f"duplicate --map source prefix: {normalized_old}"
            )
        result[normalized_old] = new.rstrip("/") or "/"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a BMP report and all indexed lineage")
    parser.add_argument("report", help="Path to claim_report.json or observation_report.json")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Relocate an absolute recorded path prefix",
    )
    args = parser.parse_args(argv)
    try:
        verified = verify_run_report(args.report, path_map=_parse_path_maps(args.map))
    except (ReportVerificationError, argparse.ArgumentTypeError) as exc:
        parser.exit(1, f"{exc}\n")
    for warning in verified.warnings:
        print(f"warning: {warning}")
    print(f"verified: {verified.report_path}")
    return 0


__all__ = [
    "ReportVerificationError",
    "VerifiedClaimReport",
    "VerifiedObservationReport",
    "VerifiedRunReport",
    "verify_claim_report",
    "verify_observation_report",
    "verify_run_report",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
