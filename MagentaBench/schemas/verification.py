"""Standalone byte and lineage verification for BMP run reports."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import random
import copy
import subprocess
from dataclasses import dataclass
from math import isclose
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, TypeVar

import jsonschema

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from pydantic import ValidationError

from .compiler import canonical_digest
from .models import (
    AdapterCapability,
    ArtifactRef,
    AttemptExecution,
    BenchmarkSpecAdapter,
    CaseOrderArtifact,
    CaseSetActivationReceipt,
    CaseSetArtifact,
    CheckpointSaveReceipt,
    ClaimReport,
    ConfigurationActivationReceipt,
    ConfigurationArtifact,
    ConfigurationCompositionStep,
    ConfigurationSpec,
    CustomCaseOrderSpec,
    DatasetSpec,
    GateName,
    EvidenceBundle,
    EvolutionMethodSpec,
    EvolutionRunEvidence,
    EvaluatorArtifact,
    EvaluatorSpec,
    ExperimentRegimeSpec,
    FactorSpec,
    IntegrationProbeRecord,
    ExternalProtocolAuthorityReceipt,
    ObservationReport,
    MetricArtifact,
    MetricFormula,
    MetricSpec,
    MetaEvolutionMethodSpec,
    ProtocolSpec,
    RecordIndex,
    ResolvedBmpManifest,
    RolloutTrajectory,
    RunReport,
    RunReportAdapter,
    RunPurpose,
    RunStatus,
    ScheduleActivationReceipt,
    StatisticalAnalysisPlan,
    StatisticalAnalysisReceipt,
    UsageRecord,
)
from .model_activation import replay_model_activation_receipt
from .metrics import compute_metric_results
from .statistics import PairedScore, StatisticalAnalysisResult, analyze_paired_scores, benchmark_evaluation_split
from .evolution import (
    EvolutionEvaluationStage,
    EvolutionRuntimeReceipt,
)


class ReportVerificationError(ValueError):
    """All integrity mismatches found while verifying one report."""

    def __init__(self, mismatches: list[str]) -> None:
        self.mismatches = tuple(mismatches)
        super().__init__("report verification failed:\n- " + "\n- ".join(mismatches))


# These are the exact adapters instantiated by AdapterRegistry.production().
# Standalone verification cannot import the runtime registry without creating
# a schemas -> runner -> schemas cycle, so keep the closed compatibility set
# beside the manifest contract it verifies.
_BUILTIN_BENCHMARK_LOADER_ADAPTERS = frozenset({"fake"})
_BUILTIN_BACKEND_FACTORY_ADAPTERS = frozenset({"fake", "subprocess"})
_BUILTIN_EXECUTION_COMPATIBILITY = frozenset(
    {
        ("fake", "fake", None),
        ("fake", "subprocess", None),
    }
)


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


@dataclass(frozen=True)
class VerifiedEvolutionRunEvidence:
    """Content-addressed evolution evidence verified outside a run report."""

    evidence: EvolutionRunEvidence
    evidence_path: Path
    nested_parent: "VerifiedEvolutionRunEvidence | None" = None
    runtime_receipt: EvolutionRuntimeReceipt | None = None


@dataclass(frozen=True)
class VerifiedIntegrationProbeRecord:
    """A standalone-verified exploratory integration probe."""

    record: IntegrationProbeRecord
    record_path: Path


@dataclass(frozen=True)
class VerifiedExternalProtocolAuthorityReceipt:
    """Verified authority bytes for a protocol outside BMP ownership."""

    receipt: ExternalProtocolAuthorityReceipt
    receipt_path: Path


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


def _relocate_dataset_spec_source(
    spec: DatasetSpec,
    *,
    declaration_ref: ArtifactRef,
    path_map: Mapping[str, str],
) -> DatasetSpec:
    """Resolve a recorded dataset root before applying explicit relocation.

    A relocated declaration can still contain an absolute path into its old
    checkout. Resolving relative declarations against the *recorded*
    declaration location, normalizing lexically, and only then applying the
    path map prevents verification from falling back to stale old-tree bytes.
    No source path is dereferenced before relocation.
    """

    declared_source = Path(spec.source).expanduser()
    if declared_source.is_absolute():
        recorded_source = declared_source
    else:
        recorded_source = Path(declaration_ref.path).parent / declared_source
    normalized_source = Path(os.path.abspath(os.fspath(recorded_source)))
    relocated_source = _resolve_path(str(normalized_source), path_map)
    return spec.model_copy(update={"source": str(relocated_source)})


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
    trajectory: RolloutTrajectory | None = None
    if bundle.trajectory_ref is None:
        mismatches.append(f"{label}.trajectory_ref: missing")
    else:
        trajectory_path, trajectory_content = _verify_ref(
            bundle.trajectory_ref,
            label=f"{label}.trajectory_ref",
            path_map=path_map,
            mismatches=mismatches,
        )
        if trajectory_content is not None:
            trajectory = _parse_json_model(
                RolloutTrajectory,
                trajectory_content,
                label=f"{label}.trajectory_ref",
                mismatches=mismatches,
            )
        if trajectory is not None:
            if trajectory.attempt_id != bundle.run_id:
                mismatches.append(f"{label}.trajectory_ref: attempt identity drift")
            if trajectory.manifest_digest != bundle.provenance.manifest_digest:
                mismatches.append(f"{label}.trajectory_ref: manifest digest drift")
            if trajectory.terminal_status != bundle.status:
                mismatches.append(f"{label}.trajectory_ref: terminal status drift")
            if trajectory.usage != bundle.usage:
                mismatches.append(f"{label}.trajectory_ref: usage drift")
            trajectory_refs = (
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
            for index, ref in enumerate(trajectory_refs):
                _verify_ref(
                    ref,
                    label=f"{label}.trajectory_ref.artifacts[{index}]",
                    path_map=path_map,
                    mismatches=mismatches,
                )
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
    runtime_receipt = bundle.provenance.runtime_manifest_receipt
    if runtime_receipt is not None:
        refs.append(
            (
                f"{label}.provenance.runtime_manifest_receipt.trace_ref",
                runtime_receipt.trace_ref,
            )
        )
        refs.extend(
            (
                f"{label}.provenance.runtime_manifest_receipt."
                f"assembly_sidecar_refs[{index}]",
                ArtifactRef(
                    path=ref.path,
                    sha256=ref.sha256,
                    size_bytes=ref.size_bytes,
                ),
            )
            for index, ref in enumerate(runtime_receipt.assembly_sidecar_refs)
        )
    configuration_activation = bundle.provenance.configuration_activation
    if configuration_activation is not None:
        refs.extend(
            (
                f"{label}.provenance.configuration_activation.evidence_refs[{index}]",
                ref,
            )
            for index, ref in enumerate(configuration_activation.evidence_refs)
        )
    model_activation = bundle.provenance.model_activation
    if model_activation is not None:
        refs.extend(
            (
                f"{label}.provenance.model_activation.evidence_refs[{index}]",
                ref,
            )
            for index, ref in enumerate(model_activation.evidence_refs)
        )
    if bundle.provenance.evolution_evidence_ref is not None:
        refs.append(
            (
                f"{label}.provenance.evolution_evidence_ref",
                bundle.provenance.evolution_evidence_ref,
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


def _evolution_artifact_refs(
    evidence: EvolutionRunEvidence,
) -> tuple[tuple[str, ArtifactRef], ...]:
    refs: list[tuple[str, ArtifactRef]] = []
    refs.extend(
        (
            f"candidate_ledger[{candidate_index}].artifact_refs[{ref_index}]",
            ref,
        )
        for candidate_index, candidate in enumerate(evidence.candidate_ledger)
        for ref_index, ref in enumerate(candidate.artifact_refs)
    )
    refs.extend(
        (
            f"candidate_ledger[{candidate_index}].feedback_refs[{ref_index}]",
            ref,
        )
        for candidate_index, candidate in enumerate(evidence.candidate_ledger)
        for ref_index, ref in enumerate(candidate.feedback_refs)
    )
    refs.extend(
        (
            f"candidate_ledger[{candidate_index}].search_state_refs[{ref_index}]",
            ref,
        )
        for candidate_index, candidate in enumerate(evidence.candidate_ledger)
        for ref_index, ref in enumerate(candidate.search_state_refs)
    )
    refs.extend(
        (
            f"transition_ledger[{transition_index}].search_state_refs[{ref_index}]",
            ref,
        )
        for transition_index, transition in enumerate(evidence.transition_ledger)
        for ref_index, ref in enumerate(transition.search_state_refs)
    )
    refs.extend(
        (
            f"transition_ledger[{transition_index}].feedback_refs[{ref_index}]",
            ref,
        )
        for transition_index, transition in enumerate(evidence.transition_ledger)
        for ref_index, ref in enumerate(transition.feedback_refs)
    )
    refs.extend(
        (f"search_state_refs[{index}]", ref)
        for index, ref in enumerate(evidence.search_state_refs)
    )
    for ref_name, ref in (
        ("adapter_ref", evidence.adapter_ref),
        ("evaluator_ref", evidence.evaluator_ref),
        ("budget_ref", evidence.budget_ref),
        ("runtime_receipt_ref", evidence.runtime_receipt_ref),
    ):
        if ref is not None:
            refs.append((ref_name, ref))
    return tuple(refs)


def _verify_evolution_runtime_receipt(
    evidence: EvolutionRunEvidence,
    *,
    path_map: Mapping[str, str],
    mismatches: list[str],
    label: str,
) -> EvolutionRuntimeReceipt | None:
    ref = evidence.runtime_receipt_ref
    if ref is None:
        return None
    _, content = _verify_ref(
        ref,
        label=f"{label}.runtime_receipt_ref",
        path_map=path_map,
        mismatches=mismatches,
    )
    if content is None:
        return None
    try:
        receipt = EvolutionRuntimeReceipt.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        mismatches.append(f"{label}.runtime_receipt: invalid schema: {exc}")
        return None

    candidate_digest = canonical_digest(
        [candidate.identity_data() for candidate in evidence.candidate_ledger]
    )
    transition_digest = canonical_digest(
        [transition.identity_data() for transition in evidence.transition_ledger]
    )
    for field_name, observed, expected in (
        ("run_id", receipt.run_id, evidence.run_id),
        ("kind", receipt.kind, evidence.kind),
        ("adapter_digest", receipt.adapter_digest, evidence.adapter_digest),
        ("evaluator_digest", receipt.evaluator_digest, evidence.evaluator_digest),
        ("budget_digest", receipt.budget_digest, evidence.budget_digest),
        (
            "candidate_ledger_digest",
            receipt.candidate_ledger_digest,
            candidate_digest,
        ),
        (
            "transition_ledger_digest",
            receipt.transition_ledger_digest,
            transition_digest,
        ),
        (
            "selected_candidate_id",
            receipt.selected_candidate_id,
            evidence.selected_candidate_id,
        ),
        (
            "parent_evidence_ref",
            receipt.parent_evidence_ref,
            evidence.parent_evidence_ref,
        ),
    ):
        if observed != expected:
            mismatches.append(f"{label}.runtime_receipt: {field_name} drift")

    refs: list[tuple[str, ArtifactRef]] = [
        ("budget_ledger.budget_ref", receipt.budget_ledger.budget_ref),
        (
            "sealed_holdout.split_manifest_ref",
            receipt.sealed_holdout.split_manifest_ref,
        ),
    ]
    if receipt.parent_evidence_ref is not None:
        refs.append(("parent_evidence_ref", receipt.parent_evidence_ref))
    for index, evaluation in enumerate(receipt.evaluations):
        refs.extend(
            (
                (f"evaluations[{index}].evaluator_ref", evaluation.evaluator_ref),
                (
                    f"evaluations[{index}].split_manifest_ref",
                    evaluation.split_manifest_ref,
                ),
                (f"evaluations[{index}].candidate_ref", evaluation.candidate_ref),
                (f"evaluations[{index}].request_ref", evaluation.request_ref),
                (f"evaluations[{index}].result_ref", evaluation.result_ref),
            )
        )
    for ref_label, artifact in refs:
        _verify_ref(
            artifact,
            label=f"{label}.runtime_receipt.{ref_label}",
            path_map=path_map,
            mismatches=mismatches,
        )

    candidates = {
        candidate.candidate_id: candidate for candidate in evidence.candidate_ledger
    }
    selected = candidates.get(receipt.selected_candidate_id)
    for evaluation in receipt.evaluations:
        candidate = candidates.get(evaluation.candidate_id)
        if candidate is None:
            mismatches.append(
                f"{label}.runtime_receipt: evaluation references unknown candidate"
            )
            continue
        if evaluation.candidate_ref not in candidate.artifact_refs:
            mismatches.append(
                f"{label}.runtime_receipt: evaluation candidate artifact drift"
            )
        if evaluation.result_ref not in candidate.feedback_refs:
            mismatches.append(
                f"{label}.runtime_receipt: evaluation feedback artifact drift"
            )

    holdout_records = tuple(
        evaluation
        for evaluation in receipt.evaluations
        if evaluation.stage == EvolutionEvaluationStage.sealed_holdout
    )
    if selected is not None and len(holdout_records) == 1:
        holdout = holdout_records[0]
        if (
            selected.score != holdout.score
            or selected.score_metric != holdout.score_metric
            or selected.evaluator_digest != holdout.evaluator_digest
        ):
            mismatches.append(
                f"{label}.runtime_receipt: selected candidate holdout score drift"
            )
        if evidence.authoritative_metric != holdout.score_metric:
            mismatches.append(
                f"{label}.runtime_receipt: authoritative holdout metric drift"
            )

    selection = next(
        (
            transition
            for transition in evidence.transition_ledger
            if transition.transition_id
            == receipt.sealed_holdout.selection_transition_id
        ),
        None,
    )
    if selection is None:
        mismatches.append(
            f"{label}.runtime_receipt: sealed holdout selection transition missing"
        )
    elif (
        selection.sequence != receipt.sealed_holdout.selection_transition_sequence
        or selection.phase.value != "select"
        or receipt.selected_candidate_id not in selection.output_candidate_ids
    ):
        mismatches.append(
            f"{label}.runtime_receipt: sealed holdout selection transition drift"
        )
    else:
        after_selection = tuple(
            transition.phase.value
            for transition in evidence.transition_ledger
            if transition.sequence > selection.sequence
        )
        if after_selection != ("terminate",):
            mismatches.append(
                f"{label}.runtime_receipt: search transition continued after selection"
            )

    candidate_ids = set(candidates)
    for event in receipt.budget_ledger.events:
        if not set(event.candidate_ids).issubset(candidate_ids):
            mismatches.append(
                f"{label}.runtime_receipt: budget event references unknown candidate"
            )
    return receipt


def _verify_evolution_file(
    path: str | Path,
    *,
    path_map: Mapping[str, str],
    mismatches: list[str],
    seen_digests: set[str],
    label: str,
) -> VerifiedEvolutionRunEvidence | None:
    source = Path(path).expanduser().resolve()
    try:
        content = source.read_bytes()
    except OSError as exc:
        mismatches.append(f"{label}: cannot read {source}: {exc}")
        return None
    try:
        evidence = EvolutionRunEvidence.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        mismatches.append(f"{label}: invalid evolution evidence schema: {exc}")
        return None
    if evidence.canonical_digest() in seen_digests:
        mismatches.append(f"{label}: recursive parent evidence cycle detected")
        return None
    seen_digests.add(evidence.canonical_digest())

    for ref_label, ref in _evolution_artifact_refs(evidence):
        _verify_ref(
            ref,
            label=f"{label}.{ref_label}",
            path_map=path_map,
            mismatches=mismatches,
        )

    runtime_receipt = _verify_evolution_runtime_receipt(
        evidence,
        path_map=path_map,
        mismatches=mismatches,
        label=label,
    )

    nested_parent: VerifiedEvolutionRunEvidence | None = None
    if evidence.parent_evidence_ref is not None:
        _, parent_bytes = _verify_ref(
            evidence.parent_evidence_ref,
            label=f"{label}.parent_evidence_ref",
            path_map=path_map,
            mismatches=mismatches,
        )
        if parent_bytes is not None:
            try:
                parent_path = _resolve_path(
                    evidence.parent_evidence_ref.path, path_map
                )
            except ValueError as exc:
                mismatches.append(f"{label}.parent_evidence_ref: {exc}")
            else:
                nested_parent = _verify_evolution_file(
                    parent_path,
                    path_map=path_map,
                    mismatches=mismatches,
                    seen_digests=seen_digests,
                    label=f"{label}.parent_evidence",
                )
                if nested_parent is not None and nested_parent.evidence.kind not in {
                    "evolver",
                    "meta_evolver",
                }:
                    mismatches.append(
                        f"{label}.parent_evidence: unsupported evidence kind "
                        f"{nested_parent.evidence.kind!r}"
                    )
                elif (
                    nested_parent is not None
                    and evidence.kind == "meta_evolver"
                    and nested_parent.evidence.kind != "evolver"
                ):
                    mismatches.append(
                        f"{label}.parent_evidence: meta_evolver parent must be evolver evidence"
                    )
    if (
        evidence.kind == "meta_evolver"
        and runtime_receipt is not None
        and nested_parent is not None
        and nested_parent.runtime_receipt is not None
    ):
        parent_events = tuple(
            event
            for event in runtime_receipt.budget_ledger.events
            if event.operation.value == "parent_evolution"
        )
        if len(parent_events) == 1:
            parent_usage = nested_parent.runtime_receipt.budget_ledger.total_usage
            if (
                parent_events[0].spent.total_tokens != parent_usage.total_tokens
                or parent_events[0].spent.cost != parent_usage.cost
            ):
                mismatches.append(
                    f"{label}.runtime_receipt: recursive parent budget usage drift"
                )
            if (
                runtime_receipt.budget_ledger.elapsed_wall_seconds
                < nested_parent.runtime_receipt.budget_ledger.elapsed_wall_seconds
            ):
                mismatches.append(
                    f"{label}.runtime_receipt: recursive parent wall usage drift"
                )
    return VerifiedEvolutionRunEvidence(
        evidence=evidence,
        evidence_path=source,
        nested_parent=nested_parent,
        runtime_receipt=runtime_receipt,
    )


def verify_evolution_run_evidence(
    evidence_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedEvolutionRunEvidence:
    """Verify an evolution evidence JSON file and all content-addressed refs.

    ``path_map`` follows report verification's relocation rules. Parent
    evidence is recursively checked, with cycle detection over canonical
    evidence identities.
    """

    relocation = {} if path_map is None else dict(path_map)
    mismatches: list[str] = []
    source = Path(evidence_path).expanduser().resolve()
    verified = _verify_evolution_file(
        source,
        path_map=relocation,
        mismatches=mismatches,
        seen_digests=set(),
        label="evolution evidence",
    )
    if mismatches or verified is None:
        raise ReportVerificationError(mismatches or ["evolution evidence verification failed"])
    return verified


def verify_integration_probe(
    record_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedIntegrationProbeRecord:
    """Verify a probe record and every retained content reference."""

    relocation = {} if path_map is None else dict(path_map)
    source = Path(record_path).expanduser().resolve()
    mismatches: list[str] = []
    try:
        record_bytes = source.read_bytes()
    except OSError as exc:
        raise ReportVerificationError(
            [f"integration probe: cannot read {source}: {exc}"]
        ) from exc
    try:
        record = IntegrationProbeRecord.model_validate_json(record_bytes)
    except (ValidationError, ValueError) as exc:
        raise ReportVerificationError(
            [f"integration probe: invalid schema: {exc}"]
        ) from exc

    refs: list[tuple[str, ArtifactRef]] = []
    if record.manifest_ref is not None:
        refs.append(("manifest_ref", record.manifest_ref))
    if record.public_input_ref is not None:
        refs.append(("public_input_ref", record.public_input_ref))
    refs.extend(
        (f"identity_refs[{index}].artifact_ref", identity.artifact_ref)
        for index, identity in enumerate(record.identity_refs)
    )
    refs.extend(
        (f"evidence_refs[{index}]", ref)
        for index, ref in enumerate(record.evidence_refs)
    )
    verified_bytes: dict[tuple[str, str, int], bytes | None] = {}
    for label, ref in refs:
        identity = (ref.path, ref.sha256, ref.size_bytes)
        if identity not in verified_bytes:
            _, content = _verify_ref(
                ref,
                label=f"integration probe.{label}",
                path_map=relocation,
                mismatches=mismatches,
            )
            verified_bytes[identity] = content

    if record.manifest_ref is not None:
        manifest_identity = (
            record.manifest_ref.path,
            record.manifest_ref.sha256,
            record.manifest_ref.size_bytes,
        )
        manifest_bytes = verified_bytes.get(manifest_identity)
        manifest = (
            None
            if manifest_bytes is None
            else _parse_json_model(
                ResolvedBmpManifest,
                manifest_bytes,
                label="integration probe.manifest_ref",
                mismatches=mismatches,
            )
        )
        if manifest is not None:
            if manifest.canonical_digest() != record.manifest_digest:
                mismatches.append("integration probe: manifest digest drift")
            if manifest.benchmark.adapter != record.benchmark_adapter:
                mismatches.append("integration probe: benchmark adapter drift")

    if record.public_input_ref is not None:
        public_identity = (
            record.public_input_ref.path,
            record.public_input_ref.sha256,
            record.public_input_ref.size_bytes,
        )
        public_bytes = verified_bytes.get(public_identity)
        if public_bytes is not None:
            try:
                public_input = json.loads(public_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                mismatches.append(
                    f"integration probe.public_input_ref: invalid JSON: {exc}"
                )
            else:
                if not isinstance(public_input, Mapping):
                    mismatches.append(
                        "integration probe.public_input_ref: expected a JSON object"
                    )
                else:
                    observed_case_id = public_input.get(
                        "case_id", public_input.get("instance_id")
                    )
                    if observed_case_id != record.case_id:
                        mismatches.append("integration probe: case id drift")

    if mismatches:
        raise ReportVerificationError(mismatches)
    return VerifiedIntegrationProbeRecord(record=record, record_path=source)


def verify_external_protocol_authority(
    receipt_path: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
) -> VerifiedExternalProtocolAuthorityReceipt:
    """Verify tracked authority documents against their declared source commit.

    The verifier treats the external checkout as an authority input.  It does
    not import, instantiate, or resolve anything from that protocol.
    """

    relocation = {} if path_map is None else dict(path_map)
    source = Path(receipt_path).expanduser().resolve()
    mismatches: list[str] = []
    try:
        payload = source.read_bytes()
        receipt = ExternalProtocolAuthorityReceipt.model_validate_json(payload)
    except OSError as exc:
        raise ReportVerificationError(
            [f"external authority: cannot read {source}: {exc}"]
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise ReportVerificationError(
            [f"external authority: invalid schema: {exc}"]
        ) from exc

    root = _resolve_path(receipt.source_root, relocation)
    try:
        observed_commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        mismatches.append(f"external authority: cannot inspect source checkout: {exc}")
        observed_commit = ""
    if observed_commit != receipt.source_commit:
        mismatches.append(
            "external authority: source commit drift "
            f"(expected {receipt.source_commit}, observed {observed_commit or 'unavailable'})"
        )

    refs: list[tuple[str, ArtifactRef, str | None]] = [
        (
            f"authority_documents[{index}]",
            document.artifact_ref,
            document.relative_path,
        )
        for index, document in enumerate(receipt.authority_documents)
    ]
    refs.extend(
        [
            ("contract_ref", receipt.contract_ref, receipt.contract_relative_path),
            ("audit_rules_ref", receipt.audit_rules_ref, None),
        ]
    )
    for label, ref, relative_path in refs:
        _, content = _verify_ref(
            ref,
            label=f"external authority.{label}",
            path_map=relocation,
            mismatches=mismatches,
        )
        if content is None or relative_path is None or not observed_commit:
            continue
        try:
            tracked = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "show",
                    f"{receipt.source_commit}:{relative_path}",
                ],
                capture_output=True,
                timeout=15,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            mismatches.append(f"external authority.{label}: cannot read tracked bytes: {exc}")
            continue
        if tracked != content:
            mismatches.append(
                f"external authority.{label}: bytes differ from source commit"
            )

    if mismatches:
        raise ReportVerificationError(mismatches)
    return VerifiedExternalProtocolAuthorityReceipt(
        receipt=receipt,
        receipt_path=source,
    )


def _configuration_merge(
    base: Mapping[str, Any], overlay: Mapping[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _configuration_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _configuration_ownership(
    base: Mapping[str, str],
    overlay: Mapping[str, Any],
    owner: str,
    *,
    prefix: str = "",
) -> dict[str, str]:
    result = dict(base)
    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        descendants = tuple(
            existing
            for existing in result
            if existing == path or existing.startswith(path + ".")
        )
        if isinstance(value, Mapping) and value:
            result.pop(path, None)
            result = _configuration_ownership(result, value, owner, prefix=path)
        elif isinstance(value, Mapping) and any(
            existing.startswith(path + ".") for existing in result
        ):
            continue
        else:
            for existing in descendants:
                result.pop(existing, None)
            result[path] = owner
    return result


def _verify_manifest_measurement_registry(
    manifest: ResolvedBmpManifest,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    """Rehash and reparse factor/evaluator/metric TOML declarations."""

    entries = [
        ("benchmark", manifest.benchmark, manifest.benchmark),
        ("dataset", manifest.dataset, manifest.dataset),
        *(
            ("factor", artifact.factor, artifact)
            for artifact in manifest.metadata.factor_artifacts
        ),
        ("evaluator", manifest.evaluator.evaluator, manifest.evaluator),
        *(("metric", artifact.metric, artifact) for artifact in manifest.metrics),
        *(
            ()
            if manifest.regime is None
            else (("regime", manifest.regime.regime, manifest.regime),)
        ),
        *(
            ()
            if manifest.metadata.evolver is None
            else (("evolver", manifest.metadata.evolver, manifest.metadata.evolver),)
        ),
        *(
            ()
            if manifest.metadata.meta_evolver is None
            else (
                (
                    "meta_evolver",
                    manifest.metadata.meta_evolver,
                    manifest.metadata.meta_evolver,
                ),
            )
        ),
    ]
    validators = {
        "benchmark": BenchmarkSpecAdapter,
        "dataset": DatasetSpec,
        "factor": FactorSpec,
        "evaluator": EvaluatorSpec,
        "evolver": EvolutionMethodSpec,
        "metric": MetricSpec,
        "meta_evolver": MetaEvolutionMethodSpec,
        "regime": ExperimentRegimeSpec,
    }
    for index, (section, spec, artifact) in enumerate(entries):
        artifact_label = f"{label}.{section}[{index}]"
        if artifact.canonical_digest() != artifact.artifact_digest:
            mismatches.append(f"{artifact_label}: artifact_digest drift")
        _, content = _verify_ref(
            artifact.declaration_ref,
            label=f"{artifact_label}.declaration_ref",
            path_map=path_map,
            mismatches=mismatches,
        )
        if content is None:
            continue
        try:
            document = tomllib.loads(content.decode("utf-8"))
            if set(document) != {section} or not isinstance(
                document.get(section), Mapping
            ):
                raise ValueError(
                    f"declaration must contain only [{section}]"
                )
            validator = validators[section]
            observed = (
                validator.validate_python(document[section])
                if hasattr(validator, "validate_python")
                else validator.model_validate(document[section])
            )
        except (UnicodeDecodeError, ValueError, ValidationError) as exc:
            mismatches.append(f"{artifact_label}: invalid declaration: {exc}")
            continue
        if section == "benchmark":
            expected = artifact.model_dump(
                mode="json",
                exclude={"artifact_digest", "declaration_ref"},
            )
            matches = observed.model_dump(mode="json") == expected
        elif section == "dataset":
            expected = artifact.model_dump(
                mode="json",
                exclude={
                    "artifact_digest",
                    "declaration_ref",
                    "source_content_digest",
                },
            )
            # Registry source paths remain declaration-relative while artifacts
            # carry their resolved absolute provenance path.
            expected["source"] = observed.source
            matches = observed.model_dump(mode="json") == expected
        elif section in {"evolver", "meta_evolver"}:
            matches = observed.model_dump(mode="json") == artifact.spec_data()
        else:
            matches = observed == spec
        if not matches:
            mismatches.append(f"{artifact_label}: declaration/spec drift")

    if manifest.regime is None:
        return
    dependency_validators = {
        "benchmark": BenchmarkSpecAdapter,
        "dataset": DatasetSpec,
        "evaluator": EvaluatorSpec,
        "metric": MetricSpec,
        "protocol": ProtocolSpec,
    }
    for index, dependency in enumerate(manifest.regime.dependencies):
        dependency_label = f"{label}.regime.dependencies[{index}]"
        resolved_path, content = _verify_ref(
            dependency.declaration_ref,
            label=f"{dependency_label}.declaration_ref",
            path_map=path_map,
            mismatches=mismatches,
        )
        if content is None or resolved_path is None:
            continue
        try:
            document = tomllib.loads(content.decode("utf-8"))
            section = dependency.registry_kind
            if set(document) != {section} or not isinstance(
                document.get(section), Mapping
            ):
                raise ValueError(f"declaration must contain only [{section}]")
            validator = dependency_validators[section]
            observed = (
                validator.validate_python(document[section])
                if hasattr(validator, "validate_python")
                else validator.model_validate(document[section])
            )
            if observed.id != dependency.id:
                raise ValueError("dependency declaration id drift")
            from MagentaBench.schemas.compiler import (
                _compile_benchmark_artifact,
                _compile_dataset_artifact,
            )
            if section == "benchmark":
                observed_digest = _compile_benchmark_artifact(
                    observed, declaration_path=resolved_path
                ).artifact_digest
            elif section == "dataset":
                relocated_spec = _relocate_dataset_spec_source(
                    observed,
                    declaration_ref=dependency.declaration_ref,
                    path_map=path_map,
                )
                compiled_dataset = _compile_dataset_artifact(
                    relocated_spec, declaration_path=resolved_path
                )
                # ArtifactRef paths are relocatable provenance, while their
                # byte identity is recorded protocol state. Rebind the replay
                # projection to that recorded identity explicitly rather than
                # letting the relocated compiler path select the identity.
                provisional = compiled_dataset.model_copy(
                    update={
                        "declaration_ref": dependency.declaration_ref,
                        "artifact_digest": "0" * 64,
                    }
                )
                observed_digest = provisional.canonical_digest()
            elif section == "evaluator":
                provisional = EvaluatorArtifact(
                    evaluator=observed,
                    declaration_ref=dependency.declaration_ref,
                    artifact_digest="0" * 64,
                )
                observed_digest = provisional.canonical_digest()
            elif section == "metric":
                provisional = MetricArtifact(
                    metric=observed,
                    declaration_ref=dependency.declaration_ref,
                    artifact_digest="0" * 64,
                )
                observed_digest = provisional.canonical_digest()
            else:
                protocol_identity = {
                    "protocol": observed.identity_data(),
                    "declaration_ref": dependency.declaration_ref.identity_data(),
                }
                observed_digest = _compact_json_digest(protocol_identity)
        except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            mismatches.append(f"{dependency_label}: invalid dependency: {exc}")
            continue
        if observed_digest != dependency.artifact_digest:
            mismatches.append(f"{dependency_label}: artifact_digest drift")


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
    if _compact_json_digest(configuration.json_schema) != configuration.schema_digest:
        mismatches.append(f"{label}: configuration schema_digest drift")
    source_contents: dict[tuple[str, int], bytes] = {}
    for index, ref in enumerate(configuration.source_refs):
        _, content = _verify_ref(
            ref,
            label=f"{label}.source_refs[{index}]",
            path_map=path_map,
            mismatches=mismatches,
        )
        if content is not None:
            source_contents[(ref.sha256, ref.size_bytes)] = content
    if configuration.composition:
        _replay_configuration_composition(
            configuration,
            source_contents=source_contents,
            label=label,
            mismatches=mismatches,
        )


def _replay_configuration_composition(
    configuration: Any,
    *,
    source_contents: Mapping[tuple[str, int], bytes],
    label: str,
    mismatches: list[str],
) -> None:
    """Replay the recorded configuration recipe and compare every projection."""

    steps = tuple(configuration.composition)
    source_keys = {
        (ref.sha256, ref.size_bytes) for ref in configuration.source_refs
    }
    composition_source_keys = {
        (step.source_ref.sha256, step.source_ref.size_bytes)
        for step in steps
        if step.source_ref is not None
    }
    if composition_source_keys != source_keys:
        mismatches.append(
            f"{label}.composition: source_refs/composition coverage mismatch"
        )
    profiles: dict[str, tuple[ConfigurationSpec, ConfigurationCompositionStep]] = {}
    roots: list[tuple[ConfigurationCompositionStep, ConfigurationSpec | None]] = []

    for index, step in enumerate(steps):
        step_label = f"{label}.composition[{index}]"
        spec: ConfigurationSpec | None = None
        observed_values: Mapping[str, Any]
        observed_schema: Mapping[str, Any]
        observed_adapter = "generic"
        observed_extends: tuple[str, ...] = ()
        if step.source_ref is None:
            observed_values = step.values
            observed_schema = step.json_schema
        else:
            key = (step.source_ref.sha256, step.source_ref.size_bytes)
            content = source_contents.get(key)
            if content is None:
                mismatches.append(f"{step_label}: source_ref is absent from source_refs")
                continue
            try:
                document = tomllib.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                mismatches.append(f"{step_label}: source TOML is malformed: {exc}")
                continue
            if step.mode == "envelope":
                if (
                    set(document) != {"configuration"}
                    or not isinstance(document.get("configuration"), Mapping)
                    or document["configuration"].get("kind") != "configuration"
                ):
                    mismatches.append(
                        f"{step_label}: source does not contain a valid [configuration] envelope"
                    )
                    continue
                try:
                    spec = ConfigurationSpec.model_validate(document["configuration"])
                except ValidationError as exc:
                    mismatches.append(f"{step_label}: invalid configuration envelope: {exc}")
                    continue
                observed_values = spec.values
                observed_schema = spec.json_schema
                observed_adapter = spec.adapter
                observed_extends = spec.extends
                if step.id != spec.id:
                    mismatches.append(
                        f"{step_label}: id mismatch: recorded={step.id!r}, observed={spec.id!r}"
                    )
            elif step.mode == "raw":
                if "configuration" in document:
                    mismatches.append(
                        f"{step_label}: raw source contains [configuration]; envelope mode required"
                    )
                    continue
                try:
                    spec = ConfigurationSpec(
                        id=step.id or "raw-source",
                        kind="configuration",
                        adapter="generic",
                        values=document,
                    )
                except ValidationError as exc:
                    mismatches.append(f"{step_label}: invalid raw configuration: {exc}")
                    continue
                observed_values = document
                observed_schema = {}
                observed_adapter = spec.adapter
                observed_extends = spec.extends
            else:
                mismatches.append(f"{step_label}: unknown source mode {step.mode!r}")
                continue
            if observed_values != step.values:
                mismatches.append(f"{step_label}: values do not match source bytes")
            if observed_schema != step.json_schema:
                mismatches.append(f"{step_label}: schema does not match source bytes")
            if observed_adapter != step.adapter:
                mismatches.append(f"{step_label}: adapter does not match source bytes")
            if observed_extends != step.extends:
                mismatches.append(f"{step_label}: extends does not match source bytes")
        if step.id is not None and spec is not None:
            profiles[step.id] = (spec, step)
        if step.root:
            roots.append((step, spec))

    replay_values: Mapping[str, Any] = {}
    replay_schema: Mapping[str, Any] = {}
    replay_ownership: Mapping[str, str] = {}
    applied_ids: list[str] = []
    visiting: list[str] = []

    def apply_profile(profile_id: str) -> None:
        if profile_id in visiting:
            cycle = " -> ".join((*visiting, profile_id))
            mismatches.append(f"{label}.composition: extends cycle: {cycle}")
            return
        entry = profiles.get(profile_id)
        if entry is None:
            mismatches.append(
                f"{label}.composition: missing profile source for {profile_id!r}"
            )
            return
        spec, _step = entry
        visiting.append(profile_id)
        for parent in spec.extends:
            apply_profile(parent)
        visiting.pop()
        nonlocal replay_values, replay_schema, replay_ownership
        replay_values = _configuration_merge(replay_values, spec.values)
        replay_schema = _configuration_merge(replay_schema, spec.json_schema)
        replay_ownership = _configuration_ownership(
            replay_ownership, spec.values, spec.adapter
        )
        if profile_id not in applied_ids:
            applied_ids.append(profile_id)

    for step, spec in roots:
        if step.kind == "profile" or (spec is not None and step.id in profiles):
            if step.id is None:
                mismatches.append(f"{label}.composition: profile root has no id")
                continue
            if step.id in profiles:
                apply_profile(step.id)
            else:
                replay_values = _configuration_merge(replay_values, step.values)
                replay_schema = _configuration_merge(replay_schema, step.json_schema)
                replay_ownership = _configuration_ownership(
                    replay_ownership, step.values, step.adapter
                )
                if step.id not in applied_ids:
                    applied_ids.append(step.id)
        elif spec is not None:
            replay_values = _configuration_merge(replay_values, spec.values)
            replay_schema = _configuration_merge(replay_schema, spec.json_schema)
            replay_ownership = _configuration_ownership(
                replay_ownership, spec.values, spec.adapter
            )
            if spec.id not in applied_ids:
                applied_ids.append(spec.id)
        else:
            replay_values = _configuration_merge(replay_values, step.values)
            replay_schema = _configuration_merge(replay_schema, step.json_schema)
            replay_ownership = _configuration_ownership(
                replay_ownership, step.values, step.adapter
            )

    try:
        validator_cls = jsonschema.validators.validator_for(replay_schema)
        validator_cls.check_schema(replay_schema)
        validator_cls(replay_schema).validate(replay_values)
    except jsonschema.exceptions.SchemaError as exc:
        mismatches.append(f"{label}: replayed JSON Schema is invalid: {exc.message}")
    except jsonschema.exceptions.ValidationError as exc:
        mismatches.append(f"{label}: replayed values fail JSON Schema: {exc.message}")
    if replay_values != configuration.values:
        mismatches.append(f"{label}: replayed values differ from resolved artifact")
    if replay_schema != configuration.json_schema:
        mismatches.append(f"{label}: replayed schema differs from resolved artifact")
    if replay_ownership != configuration.ownership:
        mismatches.append(f"{label}: replayed ownership differs from resolved artifact")
    if tuple(applied_ids) != tuple(configuration.profiles):
        mismatches.append(
            f"{label}: replayed profile order differs from resolved artifact"
        )


def _verify_manifest_adapter_capabilities(
    manifest: ResolvedBmpManifest,
    *,
    label: str,
    path_map: Mapping[str, str],
    mismatches: list[str],
) -> None:
    subject_interface = (
        None
        if manifest.subject.kind == "fake"
        else getattr(manifest.subject, "interface", None)
    )
    subject_adapter = manifest.subject.adapter
    compatibility = (
        manifest.benchmark.adapter,
        manifest.execution.backend.adapter,
        subject_interface,
    )
    required: set[tuple[str, str]] = set()
    if (
        manifest.benchmark.kind == "custom"
        or manifest.benchmark.adapter not in _BUILTIN_BENCHMARK_LOADER_ADAPTERS
    ):
        required.add((manifest.benchmark.adapter, "benchmark_loader"))
    if (
        manifest.execution.backend.adapter
        not in _BUILTIN_BACKEND_FACTORY_ADAPTERS
    ):
        required.add((manifest.execution.backend.adapter, "backend_factory"))
    if (
        manifest.benchmark.kind == "custom"
        or compatibility not in _BUILTIN_EXECUTION_COMPATIBILITY
        or manifest.subject.kind in {"evolver", "meta_evolver"}
        or manifest.execution.model
        not in {"none", "none/deterministic", "none/echo"}
    ):
        required.add((manifest.benchmark.adapter, "execution"))
    for metric_artifact in manifest.metrics:
        if metric_artifact.metric.formula == MetricFormula.external_adapter_v1:
            required.add((metric_artifact.metric.adapter, "metric_source"))

    observed: dict[tuple[str, str], list[int]] = {}
    for index, artifact in enumerate(manifest.metadata.adapter_capabilities):
        key = (
            artifact.capability.adapter,
            artifact.capability.adapter_kind,
        )
        observed.setdefault(key, []).append(index)
    for key, indices in sorted(observed.items()):
        if len(indices) > 1:
            mismatches.append(
                f"{label}: duplicate adapter capability {key!r} at indices {indices}"
            )
    for key in sorted(required - set(observed)):
        mismatches.append(f"{label}: missing required adapter capability {key!r}")
    for key in sorted(set(observed) - required):
        mismatches.append(f"{label}: unexpected adapter capability {key!r}")

    for index, artifact in enumerate(manifest.metadata.adapter_capabilities):
        artifact_label = f"{label}[{index}]"
        capability = artifact.capability
        if artifact.canonical_digest() != artifact.artifact_digest:
            mismatches.append(f"{artifact_label}: artifact_digest drift")
        if not capability.supports(
            benchmark_kind=manifest.benchmark.kind,
            subject_kind=manifest.subject.kind,
            subject_adapter=subject_adapter,
            backend_kind=manifest.execution.backend.kind,
            backend_adapter=manifest.execution.backend.adapter,
            subject_interface=subject_interface,
        ):
            mismatches.append(
                f"{artifact_label}: capability rejects resolved run tuple"
            )
        if capability.adapter_kind == "backend_factory":
            read_set = capability.backend_default_read_set
            if read_set is None:
                mismatches.append(
                    f"{artifact_label}: backend default read-set is undeclared"
                )
            else:
                unknown_defaults = sorted(
                    set(manifest.execution.backend.defaults) - set(read_set)
                )
                if unknown_defaults:
                    mismatches.append(
                        f"{artifact_label}: backend defaults are outside declared "
                        f"read-set: {unknown_defaults}"
                    )
        elif capability.adapter_kind == "execution":
            if not capability.supported_subject_adapters:
                mismatches.append(
                    f"{artifact_label}: subject adapter compatibility is undeclared"
                )
            if manifest.execution.model in {
                "none",
                "none/deterministic",
                "none/echo",
            }:
                if not capability.none_model_sentinels:
                    mismatches.append(
                        f"{artifact_label}: none-model sentinels are undeclared"
                    )
                elif manifest.execution.model not in capability.none_model_sentinels:
                    mismatches.append(
                        f"{artifact_label}: execution model is outside declared "
                        "none-model sentinels"
                    )
            elif capability.model_activation_source is None:
                mismatches.append(
                    f"{artifact_label}: ModelActivationReceipt provenance is undeclared"
                )
            if not capability.supported_state_reset_policies:
                mismatches.append(
                    f"{artifact_label}: state-reset policies are undeclared"
                )
            elif (
                manifest.execution.protocol is None
                or manifest.execution.protocol.state_reset
                not in capability.supported_state_reset_policies
            ):
                mismatches.append(
                    f"{artifact_label}: state reset is outside declared policies"
                )
        elif capability.adapter_kind == "metric_source":
            selected_metrics = [
                artifact.metric
                for artifact in manifest.metrics
                if artifact.metric.adapter == capability.adapter
                and artifact.metric.formula == MetricFormula.external_adapter_v1
            ]
            if not selected_metrics:
                mismatches.append(
                    f"{artifact_label}: metric capability has no selected metric"
                )
            metric_config_validator = None
            try:
                validator_cls = jsonschema.validators.validator_for(
                    capability.metric_config_schema
                )
                validator_cls.check_schema(capability.metric_config_schema)
                metric_config_validator = validator_cls(
                    capability.metric_config_schema
                )
            except jsonschema.exceptions.SchemaError as exc:
                mismatches.append(
                    f"{artifact_label}: capability metric config JSON Schema "
                    f"is invalid: {exc.message}"
                )
            for metric in selected_metrics:
                if metric.source.value not in capability.supported_metric_sources:
                    mismatches.append(
                        f"{artifact_label}: metric source is outside capability"
                    )
                if metric.formula.value not in capability.supported_metric_formulas:
                    mismatches.append(
                        f"{artifact_label}: metric formula is outside capability"
                    )
                if metric_config_validator is not None:
                    try:
                        metric_config_validator.validate(metric.config)
                    except jsonschema.exceptions.ValidationError as exc:
                        mismatches.append(
                            f"{artifact_label}: metric config fails capability "
                            f"JSON Schema for {metric.id!r}: {exc.message}"
                        )
        _, declaration_content = _verify_ref(
            artifact.declaration_ref,
            label=f"{artifact_label}.declaration_ref",
            path_map=path_map,
            mismatches=mismatches,
        )
        if declaration_content is not None:
            try:
                document = tomllib.loads(declaration_content.decode("utf-8"))
                if set(document) != {"adapter"} or not isinstance(
                    document.get("adapter"), Mapping
                ):
                    raise ValueError(
                        "declaration must contain only [adapter]"
                    )
                declared_capability = AdapterCapability.model_validate(
                    document["adapter"]
                )
            except (UnicodeDecodeError, ValueError, ValidationError) as exc:
                mismatches.append(
                    f"{artifact_label}: invalid capability declaration: {exc}"
                )
            else:
                if declared_capability != capability:
                    mismatches.append(
                        f"{artifact_label}: capability declaration/spec drift"
                    )
        _verify_ref(
            artifact.implementation_ref,
            label=f"{artifact_label}.implementation_ref",
            path_map=path_map,
            mismatches=mismatches,
        )
        closure_refs = artifact.source_closure_refs
        closure_paths = artifact.source_closure_paths
        if not closure_refs or artifact.source_closure_digest is None:
            mismatches.append(
                f"{artifact_label}: source import closure is missing"
            )
            continue
        if len(closure_refs) != len(closure_paths):
            mismatches.append(
                f"{artifact_label}: source import closure refs/path count drift"
            )
            continue
        closure_entries = [
            {
                "path": relative,
                "size_bytes": ref.size_bytes,
                "sha256": ref.sha256,
            }
            for relative, ref in zip(closure_paths, closure_refs, strict=True)
        ]
        observed_closure_digest = _compact_json_digest(
            sorted(closure_entries, key=lambda item: item["path"])
        )
        if observed_closure_digest != artifact.source_closure_digest:
            mismatches.append(f"{artifact_label}: source import closure digest drift")
        if not any(
            ref.sha256 == artifact.implementation_ref.sha256
            and ref.size_bytes == artifact.implementation_ref.size_bytes
            for ref in closure_refs
        ):
            mismatches.append(
                f"{artifact_label}: source import closure omits implementation"
            )
        # Re-run the same static, non-importing closure discovery used by the
        # compiler. This catches an entrypoint that was edited to import a new
        # local helper which was absent from the recorded closure.
        try:
            from MagentaBench.runner.adapter_source import (
                closure_digest,
                import_closure,
                resolve_entrypoint,
                resolve_source_root,
            )

            declaration_path = _resolve_path(
                artifact.declaration_ref.path, path_map
            )
            project_root = declaration_path.parent.parent.parent
            source_root = resolve_source_root(
                project_root, artifact.capability.source
            )
            entrypoint_path = resolve_entrypoint(
                source_root, artifact.capability.entrypoint
            )
            observed_paths = import_closure(source_root, entrypoint_path)
            expected_paths = tuple(
                path.relative_to(source_root).as_posix()
                for path in observed_paths
            )
            if expected_paths != tuple(closure_paths):
                mismatches.append(
                    f"{artifact_label}: source import closure paths drift"
                )
            observed_digest = closure_digest(source_root, observed_paths)
            if observed_digest != artifact.source_closure_digest:
                mismatches.append(
                    f"{artifact_label}: source import closure source drift"
                )
        except (OSError, ValueError) as exc:
            mismatches.append(
                f"{artifact_label}: source import closure cannot be replayed: {exc}"
            )
        for closure_index, ref in enumerate(closure_refs):
            _verify_ref(
                ref,
                label=f"{artifact_label}.source_closure_refs[{closure_index}]",
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
    configuration = manifest.metadata.configuration
    activation = provenance.configuration_activation
    if configuration is None:
        if activation is not None:
            mismatches.append(f"{label}: undeclared configuration activation receipt")
    elif activation is None and manifest.claim_design.purpose == RunPurpose.claim:
        mismatches.append(f"{label}: ConfigurationActivationReceipt is missing")
    elif activation is not None:
        if activation.configuration_digest != configuration.artifact_digest:
            mismatches.append(f"{label}: configuration activation digest drift")
        expected_consumer_adapter = getattr(
            manifest.subject, "adapter", configuration.adapter
        )
        if activation.adapter != expected_consumer_adapter:
            mismatches.append(f"{label}: configuration activation adapter drift")
        if activation.status != "matched":
            mismatches.append(
                f"{label}: configuration activation status is {activation.status!r}"
            )
    model = manifest.execution.model
    model_activation = provenance.model_activation
    none_models = {"none", "none/deterministic", "none/echo"}
    if model in none_models:
        if manifest.execution.provider_binding is not None:
            mismatches.append(
                f"{label}: none-model execution has an undeclared ProviderBinding"
            )
        if model_activation is not None:
            mismatches.append(
                f"{label}: none-model execution has an undeclared ModelActivationReceipt"
            )
    elif model_activation is not None:
        binding = manifest.execution.provider_binding
        sources = {
            item.capability.model_activation_source
            for item in manifest.metadata.adapter_capabilities
            if item.capability.adapter_kind == "execution"
            and item.capability.model_activation_source is not None
        }
        if len(sources) != 1:
            mismatches.append(
                f"{label}: model activation capability binding is ambiguous"
            )
        elif model_activation.activation_source != next(iter(sources)):
            mismatches.append(f"{label}: model activation source drift")
        if model_activation.requested_model != model:
            mismatches.append(f"{label}: model activation requested model drift")
        if binding is None:
            if model_activation.requested_provider_id is not None:
                mismatches.append(
                    f"{label}: undeclared model activation provider binding"
                )
            if model_activation.binding_digest is not None:
                mismatches.append(
                    f"{label}: undeclared model activation binding digest"
                )
        else:
            if model_activation.requested_provider_id != binding.provider_id:
                mismatches.append(f"{label}: model activation provider binding drift")
            if model_activation.requested_model_id != binding.model_id:
                mismatches.append(f"{label}: model activation model binding drift")
            if model_activation.binding_digest != binding.canonical_digest():
                mismatches.append(f"{label}: model activation binding digest drift")
        activation_evidence_path: Path | None = None
        activation_path_valid = True
        if model_activation.evidence_refs:
            try:
                activation_evidence_path = _resolve_path(
                    model_activation.evidence_refs[0].path,
                    path_map,
                )
            except ValueError as exc:
                activation_path_valid = False
                mismatches.append(f"{label}: model activation evidence path: {exc}")
        activation_errors = (
            replay_model_activation_receipt(
                model_activation,
                requested_model=model,
                binding=binding,
                bundle_usage=bundle.usage,
                require_usage=manifest.claim_design.purpose == RunPurpose.claim,
                evidence_path=activation_evidence_path,
            )
            if activation_path_valid
            else ()
        )
        mismatches.extend(f"{label}: {error}" for error in activation_errors)
        if manifest.claim_design.purpose == RunPurpose.claim:
            if bundle.usage is None or bundle.usage.total_tokens is None:
                mismatches.append(f"{label}: real-model token usage is unobservable")
            if bundle.usage is None or bundle.usage.cost is None:
                mismatches.append(f"{label}: real-model cost usage is unobservable")
    evolution_ref = provenance.evolution_evidence_ref
    evolution_required = manifest.claim_design.purpose == RunPurpose.claim
    if manifest.subject.kind in {"evolver", "meta_evolver"}:
        if evolution_ref is None and evolution_required:
            mismatches.append(f"{label}: EvolutionRunEvidence is missing")
        elif evolution_ref is not None:
            try:
                evolution_path = _resolve_path(evolution_ref.path, path_map)
            except ValueError as exc:
                mismatches.append(f"{label}: evolution evidence path is invalid: {exc}")
            else:
                verified_evolution = _verify_evolution_file(
                    evolution_path,
                    path_map=path_map,
                    mismatches=mismatches,
                    seen_digests=set(),
                    label=f"{label}.evolution_evidence",
                )
                if verified_evolution is not None:
                    evidence = verified_evolution.evidence
                    if evidence.kind != manifest.subject.kind:
                        mismatches.append(
                            f"{label}: evolution evidence kind does not match subject"
                        )
                    execution_capabilities = tuple(
                        item.capability
                        for item in manifest.metadata.adapter_capabilities
                        if item.capability.adapter_kind == "execution"
                    )
                    if len(execution_capabilities) != 1:
                        mismatches.append(
                            f"{label}: evolution execution capability binding is ambiguous"
                        )
                    elif evidence.adapter_digest != execution_capabilities[0].digest:
                        mismatches.append(
                            f"{label}: evolution adapter digest does not match capability"
                        )
                    if evidence.evaluator_digest != manifest.evaluator.artifact_digest:
                        mismatches.append(
                            f"{label}: evolution evaluator digest does not match registered evaluator"
                        )
                    if evidence.budget_digest != canonical_digest(
                        manifest.execution.budget
                    ):
                        mismatches.append(
                            f"{label}: evolution budget digest does not match manifest"
                        )
                    if evolution_required and not evidence.claim_ready:
                        mismatches.append(
                            f"{label}: evolution evidence is not claim_ready"
                        )
                    if evidence.run_id not in {
                        bundle.run_id,
                        manifest.metadata.run_id,
                    }:
                        mismatches.append(
                            f"{label}: evolution evidence run_id is not bound to attempt"
                        )
    elif evolution_ref is not None:
        mismatches.append(f"{label}: undeclared evolution evidence reference")
    runtime_receipt = provenance.runtime_manifest_receipt
    if manifest.subject.kind == "hcp_harness":
        if runtime_receipt is None:
            mismatches.append(f"{label}: runtime manifest receipt is missing")
        else:
            try:
                mapped_trace_path = _resolve_path(
                    runtime_receipt.trace_ref.path, path_map
                )
                from MagentaBench.adapters.subjects.cli_agent import (
                    MagentaJsonlError,
                    parse_magenta_jsonl,
                )

                parsed_trace = parse_magenta_jsonl(
                    mapped_trace_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError, MagentaJsonlError) as exc:
                mismatches.append(
                    f"{label}: runtime manifest trace is invalid: {exc}"
                )
            else:
                expected_manifest_digests = tuple(
                    _compact_json_digest(item)
                    for item in parsed_trace.runtime_manifests
                )
                if runtime_receipt.run_id != parsed_trace.run_end.get("runId"):
                    mismatches.append(f"{label}: runtime manifest run_id drift")
                if runtime_receipt.manifest_sha256 != expected_manifest_digests:
                    mismatches.append(f"{label}: runtime manifest digest lineage drift")
                expected_sequence = int(
                    parsed_trace.effective_runtime_manifest["sequence"]
                )
                if runtime_receipt.effective_sequence != expected_sequence:
                    mismatches.append(f"{label}: effective runtime manifest drift")
                expected_sidecars = []
                for item in parsed_trace.runtime_manifests:
                    sidecar = item.get("assembly")
                    if not isinstance(sidecar, Mapping):
                        continue
                    encoded = json.dumps(
                        sidecar,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"
                    expected_sidecars.append(
                        (
                            int(item["sequence"]),
                            _sha256(encoded),
                            len(encoded),
                        )
                    )
                observed_sidecars = [
                    (ref.sequence, ref.sha256, ref.size_bytes)
                    for ref in runtime_receipt.assembly_sidecar_refs
                ]
                if observed_sidecars != expected_sidecars:
                    mismatches.append(f"{label}: runtime assembly sidecar lineage drift")
    elif runtime_receipt is not None:
        mismatches.append(f"{label}: undeclared runtime manifest receipt")
    environment = manifest.execution.backend.environment
    if environment is None and provenance.environment_receipt is not None:
        mismatches.append(f"{label}: undeclared environment receipt")
    elif environment is not None:
        receipt = provenance.environment_receipt
        if receipt is None:
            mismatches.append(f"{label}: environment receipt is missing")
        elif receipt.spec_digest != environment.canonical_digest():
            mismatches.append(f"{label}: environment spec_digest drift")
    backend = manifest.execution.backend
    runtime_identity = backend.defaults.get("runtime_identity")
    harbor_identity_required = (
        backend.adapter == "harbor"
        and isinstance(runtime_identity, Mapping)
        and runtime_identity.get("required") is True
    )
    if harbor_identity_required:
        if (
            backend.executable is None
            or provenance.executable != backend.executable
            or provenance.executable_digest is None
            or provenance.executable_digest != backend.digest
        ):
            mismatches.append(f"{label}: Harbor executable digest missing or drifted")
        if provenance.image_digest is None:
            mismatches.append(f"{label}: Harbor task image digest is missing")
    container_ref = provenance.container_receipt_ref
    if container_ref is None:
        if harbor_identity_required:
            mismatches.append(f"{label}: Harbor container receipt is missing")
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
    if harbor_identity_required:
        lifecycle = container.get("lifecycle")
        image = container.get("image")
        harbor = container.get("harbor")
        if container.get("format") != "magentabench-harbor-container-receipt-v1":
            mismatches.append(f"{label}: Harbor container receipt format drift")
        if (
            not isinstance(lifecycle, dict)
            or lifecycle.get("removed") is not True
            or lifecycle.get("network_removed") is not True
        ):
            mismatches.append(f"{label}: Harbor container cleanup receipt is missing")
        if (
            not isinstance(image, dict)
            or image.get("config_digest") != provenance.image_digest
        ):
            mismatches.append(f"{label}: Harbor OCI config digest cross-link drift")
        if (
            not isinstance(harbor, dict)
            or harbor.get("executable_sha256") != provenance.executable_digest
        ):
            mismatches.append(f"{label}: Harbor executable receipt cross-link drift")


def _schedule_attempt_allocation_mismatches(
    schedule: ScheduleActivationReceipt,
) -> tuple[str, ...]:
    """Return identity drift between planned slots and launched executions.

    Keep this check in the standalone verifier even though the receipt model
    enforces the same invariant.  Verification must not rely on callers having
    constructed ``ScheduleActivationReceipt`` through Pydantic validation.
    """

    mismatches: list[str] = []
    allocations = schedule.budget_ledger.attempt_allocations
    allocation_by_id = {item.attempt_id: item for item in allocations}
    expected_slots = {
        (case_id, attempt_index)
        for case_id in schedule.observed_case_order
        for attempt_index in range(schedule.declared_rollouts_per_case)
    }
    allocation_slots = {
        (item.case_id, item.attempt_index) for item in allocations
    }
    if len(allocation_by_id) != len(allocations):
        mismatches.append("attempt allocation ids are not unique")
    if len(allocation_slots) != len(allocations):
        mismatches.append("attempt allocation slots are not unique")
    if allocation_slots != expected_slots:
        mismatches.append("attempt allocations do not cover the planned slot matrix")

    launched_ids = {item.attempt_id for item in allocations if item.launched}
    execution_ids = {item.attempt_id for item in schedule.attempts}
    if launched_ids != execution_ids:
        mismatches.append("launched allocation/execution ids differ")
    for attempt in schedule.attempts:
        allocation = allocation_by_id.get(attempt.attempt_id)
        if allocation is None:
            continue
        if (
            allocation.case_id != attempt.case_id
            or allocation.attempt_index != attempt.attempt_index
        ):
            mismatches.append(
                f"attempt {attempt.attempt_id!r} allocation/execution slot drift"
            )
    return tuple(mismatches)


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

    for reason in _schedule_attempt_allocation_mismatches(schedule):
        mismatches.append(f"{label}: {reason}")

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
    elif protocol.case_order == "explicit":
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
    elif protocol.case_order == "custom":
        expected_case_order = tuple(activated_case_ids)
        if schedule.observed_case_order != expected_case_order:
            mismatches.append(
                f"{label}: observed_case_order does not match activated custom order"
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
    expected_dataset_id: str | None = None,
    expected_dataset_digest: str | None = None,
    expected_loader_adapter: str | None = None,
    expected_loader_digest: str | None = None,
    expected_case_order: str | None = None,
    expected_order_seed: int | None = None,
    expected_custom_order: CustomCaseOrderSpec | None = None,
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
    if artifact.dataset_id != expected_dataset_id:
        mismatches.append(f"{label}: dataset_id does not match indexed manifest")
    if artifact.dataset_digest != expected_dataset_digest:
        mismatches.append(f"{label}: dataset_digest does not match indexed manifest")
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
    if artifact.dataset_id != receipt.dataset_id:
        mismatches.append(f"{label}: dataset_id does not match artifact")
    if artifact.dataset_digest != receipt.dataset_digest:
        mismatches.append(f"{label}: dataset_digest does not match artifact")
    if artifact.ordered_case_ids != receipt.ordered_case_ids:
        mismatches.append(f"{label}: ordered_case_ids do not match artifact")
    expected_selection_method = (
        {
            "custom": "custom_order_artifact",
            "explicit": "explicit_case_ids",
        }.get(expected_case_order, "all_cases")
        if expected_case_order is not None
        else None
    )
    if expected_selection_method is not None and artifact.selection_method != expected_selection_method:
        mismatches.append(f"{label}: selection_method does not match manifest protocol")
    if (
        expected_case_order is not None
        and artifact.case_order != expected_case_order
    ):
        mismatches.append(f"{label}: case_order does not match manifest protocol")
    if artifact.order_seed != expected_order_seed:
        mismatches.append(f"{label}: order_seed does not match manifest protocol")
    if expected_case_order == "custom":
        if expected_custom_order is None:
            mismatches.append(f"{label}: custom order declaration is missing")
        else:
            strategy_ref = artifact.order_strategy_ref
            if artifact.order_strategy_adapter != expected_custom_order.adapter:
                mismatches.append(
                    f"{label}: custom order adapter does not match manifest protocol"
                )
            if strategy_ref is None:
                mismatches.append(f"{label}: custom order content reference is missing")
            else:
                if (
                    strategy_ref.sha256 != expected_custom_order.sha256
                    or strategy_ref.size_bytes != expected_custom_order.size_bytes
                ):
                    mismatches.append(
                        f"{label}: custom order content identity does not match manifest"
                    )
                _, strategy_bytes = _verify_ref(
                    strategy_ref,
                    label=f"{label}.case_set.order_strategy_ref",
                    path_map=path_map,
                    mismatches=mismatches,
                )
                strategy = (
                    None
                    if strategy_bytes is None
                    else _parse_json_model(
                        CaseOrderArtifact,
                        strategy_bytes,
                        label=f"{label}.case_set.order_strategy_ref",
                        mismatches=mismatches,
                    )
                )
                if (
                    strategy is not None
                    and artifact.ordered_case_ids != strategy.ordered_case_ids
                ):
                    mismatches.append(
                        f"{label}: case order does not match custom order artifact"
                    )
    elif expected_custom_order is not None:
        mismatches.append(f"{label}: non-custom protocol declares custom order")
    if (
        expected_source_content_digest is not None
        and artifact.source_content_digest != expected_source_content_digest
    ):
        mismatches.append(
            f"{label}: source_content_digest does not match indexed dataset"
        )

    content_refs: list[tuple[str, ArtifactRef]] = [
        (
            f"{label}.case_set.source_content_refs[{index}]",
            content_ref,
        )
        for index, content_ref in enumerate(artifact.source_content_refs)
    ]
    if artifact.order_strategy_ref is not None:
        content_refs.append(
            (f"{label}.case_set.order_strategy_ref", artifact.order_strategy_ref)
        )
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

    metric = manifest.authoritative_reward_metric
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
    case_set_receipt: CaseSetActivationReceipt | None = None,
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
        or _schedule_attempt_allocation_mismatches(schedule)
    ):
        return False

    allocated_case_ids = tuple(
        allocation.case_id for allocation in schedule.budget_ledger.case_allocations
    )
    if schedule.observed_case_order != allocated_case_ids:
        return False
    if protocol.case_order == "explicit" and (
        schedule.observed_case_order != tuple(protocol.explicit_case_ids)
    ):
        return False
    if protocol.case_order == "custom":
        # The scheduler receipt must be tied to the exact content-addressed
        # case-set activation.  The case-set verifier separately checks the
        # strategy adapter/ref and JSON artifact; this check ensures the
        # protocol gate cannot be evaluated from a forged schedule alone.
        if case_set_receipt is None:
            return False
        if schedule.observed_case_order != case_set_receipt.ordered_case_ids:
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


def _statistical_plan_for_lineage(
    lineage_entries: list[tuple[Any, EvidenceBundle | None]],
    manifest_by_run: Mapping[str, ResolvedBmpManifest],
) -> tuple[StatisticalAnalysisPlan | None, tuple[str, ...]]:
    """Recover one invariant analysis plan from the indexed manifests.

    A claim is never allowed to silently mix planned and unplanned arms.  The
    runner applies the same rule before constructing a receipt; keeping this
    small helper here lets a relocated report be checked without importing the
    runtime compiler.
    """

    plans = [
        manifest_by_run[lineage.run_id].claim_design.statistical_analysis
        for lineage, _ in lineage_entries
        if lineage.run_id in manifest_by_run
    ]
    present = [plan for plan in plans if plan is not None]
    if not present:
        return None, ()
    digests = {plan.canonical_digest() for plan in present}
    if len(present) != len(plans) or len(digests) != 1:
        return None, ("StatisticalAnalysisPlan must be invariant across every arm",)
    return present[0], ()


def _statistical_factor_values(
    lineage: Any,
    manifest: ResolvedBmpManifest,
    *,
    exclude_factor: str | None = None,
) -> dict[str, Any]:
    """Project manifest factors exactly as the runner's paired observations."""

    values = {
        key: value
        for key, value in manifest.metadata.factors.items()
        if key
        not in {
            "subject",
            "experiment.subject",
            "order_position",
            exclude_factor,
        }
    }
    values["case_id"] = lineage.case_id
    return values


def _verification_contrast_binding(
    manifest: ResolvedBmpManifest,
) -> tuple[str, bytes, bytes] | None:
    """Resolve a one-factor contrast only from its registered factor artifact."""

    contrast = manifest.contrast
    if contrast.mode != "one_factor":
        return None
    if (
        contrast.factor_id is None
        or contrast.control_level is None
        or contrast.treatment_level is None
    ):
        return None
    matches = [
        artifact.factor
        for artifact in manifest.metadata.factor_artifacts
        if artifact.factor.id == contrast.factor_id
    ]
    if len(matches) != 1:
        return None
    factor = matches[0]
    try:
        control = factor.level(contrast.control_level).value
        treatment = factor.level(contrast.treatment_level).value
    except ValueError:
        return None
    return factor.selector_path, _canonical_key(control), _canonical_key(treatment)


def _verification_arm_key(
    lineage: Any,
    manifest: ResolvedBmpManifest,
    *,
    factor_path: str,
) -> bytes:
    values = manifest.metadata.factors
    if factor_path not in values:
        return b"__missing_factor__"
    return _canonical_key(values[factor_path])


def _counterbalance_for_statistical_plan(
    lineage_entries: list[tuple[Any, EvidenceBundle | None]],
    manifest_by_run: Mapping[str, ResolvedBmpManifest],
    *,
    control_key: bytes,
    treatment_key: bytes,
    factor_path: str,
) -> bool:
    """Check both treatment/control order directions for every outer unit."""

    positions = {
        (lineage.run_id, lineage.case_id): index
        for index, (lineage, _) in enumerate(lineage_entries)
    }
    groups: dict[bytes, dict[bytes, tuple[Any, ResolvedBmpManifest]]] = {}
    outer_keys: dict[bytes, bytes] = {}
    for lineage, _ in lineage_entries:
        manifest = manifest_by_run.get(lineage.run_id)
        if manifest is None:
            return False
        factors = _statistical_factor_values(
            lineage,
            manifest,
            exclude_factor=factor_path,
        )
        pair_key = _canonical_key(factors)
        arm = _verification_arm_key(lineage, manifest, factor_path=factor_path)
        if arm in groups.setdefault(pair_key, {}):
            return False
        groups[pair_key][arm] = (lineage, manifest)
        outer = {key: value for key, value in factors.items() if key != "repetition"}
        outer_keys[pair_key] = _canonical_key(outer)

    directions: dict[bytes, set[bool]] = {}
    counts: Counter[bytes] = Counter()
    for pair_key, pair in groups.items():
        if set(pair) != {control_key, treatment_key}:
            return False
        control_lineage, _ = pair[control_key]
        treatment_lineage, _ = pair[treatment_key]
        control_position = positions.get(
            (control_lineage.run_id, control_lineage.case_id)
        )
        treatment_position = positions.get(
            (treatment_lineage.run_id, treatment_lineage.case_id)
        )
        if control_position is None or treatment_position is None:
            return False
        outer_key = outer_keys[pair_key]
        directions.setdefault(outer_key, set()).add(
            control_position < treatment_position
        )
        counts[outer_key] += 1
    return bool(directions) and all(
        counts[key] >= 2 and values == {False, True}
        for key, values in directions.items()
    )


def _replay_statistical_plan(
    lineage_entries: list[tuple[Any, EvidenceBundle | None]],
    manifest_by_run: Mapping[str, ResolvedBmpManifest],
    plan: StatisticalAnalysisPlan,
) -> tuple[StatisticalAnalysisResult, bool, str | None]:
    """Rebuild the receipt and the gate-side pairing checks from report bytes."""

    manifests = [
        manifest_by_run.get(lineage.run_id)
        for lineage, _ in lineage_entries
    ]
    errors: list[str] = []
    if any(manifest is None for manifest in manifests):
        errors.append("statistical analysis lineage manifest is missing")
        return (
            StatisticalAnalysisResult(receipt=None, errors=tuple(errors)),
            False,
            None,
        )
    resolved_manifests = [manifest for manifest in manifests if manifest is not None]
    metrics = {manifest.authoritative_reward_metric for manifest in resolved_manifests}
    metric = next(iter(metrics), None) if len(metrics) == 1 else None
    if metric is None:
        errors.append("authoritative reward metric differs across runs")

    contrast_keys = {
        _canonical_key(manifest.contrast.model_dump(mode="json"))
        for manifest in resolved_manifests
    }
    if len(contrast_keys) != 1:
        errors.append("experiment contrast differs across runs")
        binding = None
    else:
        binding = _verification_contrast_binding(resolved_manifests[0])
        if binding is None:
            errors.append("StatisticalAnalysisPlan requires a one_factor contrast")
    factor_path = None if binding is None else binding[0]
    control_key = None if binding is None else binding[1]
    treatment_key = None if binding is None else binding[2]
    observations: list[PairedScore] = []
    if metric is not None and binding is not None:
        grouped: dict[
            bytes, dict[str | bytes, tuple[Any, EvidenceBundle, ResolvedBmpManifest]]
        ] = {}
        for lineage, bundle in lineage_entries:
            manifest = manifest_by_run.get(lineage.run_id)
            if manifest is None or bundle is None:
                errors.append("statistics require complete lineage evidence")
                continue
            factors = _statistical_factor_values(
                lineage,
                manifest,
                exclude_factor=factor_path,
            )
            pair_key = _canonical_key(
                {
                    key: value
                    for key, value in factors.items()
                    if key not in {"case_id", factor_path}
                }
                | {"case_id": lineage.case_id}
            )
            arm = _verification_arm_key(
                lineage,
                manifest,
                factor_path=factor_path,
            )
            if arm in grouped.setdefault(pair_key, {}):
                errors.append("paired control/treatment structure contains duplicate arms")
                continue
            grouped[pair_key][arm] = (lineage, bundle, manifest)
        for pair in grouped.values():
            if set(pair) != {control_key, treatment_key}:
                errors.append("paired control/treatment structure is incomplete")
                continue
            control_lineage, control_bundle, control_manifest = pair[control_key]
            treatment_lineage, treatment_bundle, _ = pair[treatment_key]
            control_evidence = control_bundle.verifier_evidence
            treatment_evidence = treatment_bundle.verifier_evidence
            control_score = (
                None
                if control_evidence is None
                else control_evidence.metrics.get(metric)
            )
            treatment_score = (
                None
                if treatment_evidence is None
                else treatment_evidence.metrics.get(metric)
            )
            if control_score is None or treatment_score is None:
                errors.append("statistics require authoritative verifier scores for every pair")
                continue
            observations.append(
                PairedScore(
                    unit_values=_statistical_factor_values(
                        control_lineage,
                        control_manifest,
                        exclude_factor=factor_path,
                    ),
                    control_score=control_score,
                    treatment_score=treatment_score,
                )
            )
    deterministic = bool(
        resolved_manifests
        and resolved_manifests[0].execution.protocol is not None
        and resolved_manifests[0].execution.protocol.deterministic_conformance
    )
    result = analyze_paired_scores(
        plan,
        metric=metric or "unbound",
        observations=observations,
        evaluation_splits=tuple(
            benchmark_evaluation_split(manifest.dataset)
            for manifest in resolved_manifests
        ),
        allow_no_holdout=deterministic,
    )
    result = StatisticalAnalysisResult(
        receipt=result.receipt,
        errors=tuple(dict.fromkeys((*errors, *result.errors))),
    )
    counterbalanced = bool(
        (
            factor_path is not None
            or binding is not None
        )
        and resolved_manifests
        and resolved_manifests[0].contrast.counterbalanced
        and _counterbalance_for_statistical_plan(
            lineage_entries,
            manifest_by_run,
            control_key=control_key or b"",
            treatment_key=treatment_key or b"",
            factor_path=factor_path or "",
        )
    )
    return result, counterbalanced, metric


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
    binding = _verification_contrast_binding(ordered[0][2])
    if binding is None:
        return False
    factor_path, control_key, treatment_key = binding
    protocol = ordered[0][2].execution.protocol
    assert protocol is not None

    # Keep the lineage next to its manifest.  A multi-case parent run shares
    # one immutable manifest object across all selected cases, so recovering
    # lineage later with ``manifest is ...`` is ambiguous and can silently
    # attribute one case's ordering to another.
    pairs: dict[
        bytes,
        dict[str | bytes, tuple[Any, ResolvedBmpManifest]],
    ] = {}
    for lineage, _, manifest in ordered:
        factors = {
            key: value
            for key, value in manifest.metadata.factors.items()
            if key
            not in {
                "subject",
                "experiment.subject",
                "order_position",
                factor_path,
            }
        }
        factors["__case_id"] = lineage.case_id
        pair = pairs.setdefault(_canonical_key(factors), {})
        arm = _verification_arm_key(lineage, manifest, factor_path=factor_path)
        if arm in pair:
            return False
        pair[arm] = (lineage, manifest)
    if not pairs or any(
        set(pair) != {control_key, treatment_key} for pair in pairs.values()
    ):
        return False

    if not protocol.deterministic_conformance or not ordered[0][2].contrast.counterbalanced:
        return False
    positions = {
        (lineage.run_id, lineage.case_id): index
        for index, (lineage, _, _) in enumerate(ordered)
    }
    directions: dict[bytes, set[bool]] = {}
    counts: Counter[bytes] = Counter()
    for pair in pairs.values():
        control_lineage, control = pair[control_key]
        treatment_lineage, treatment = pair[treatment_key]
        outer_factors = {
            key: value
            for key, value in control.metadata.factors.items()
            if key
            not in {
                "subject",
                "experiment.subject",
                "order_position",
                "repetition",
                factor_path,
            }
        }
        # The case is part of the independent paired unit.  Keeping it in the
        # outer key prevents counterbalance evidence from one case masking a
        # missing direction in another.
        outer_factors["__case_id"] = control_lineage.case_id
        outer_key = _canonical_key(outer_factors)
        directions.setdefault(outer_key, set()).add(
            positions[(control_lineage.run_id, control_lineage.case_id)]
            < positions[(treatment_lineage.run_id, treatment_lineage.case_id)]
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
        expected_boundary = {
            "local": "process",
            "container": "task_container",
        }.get(manifest.execution.backend.kind)
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


def _model_activation_isolation_reasons(
    lineage: Any,
    bundle: EvidenceBundle,
    manifest: ResolvedBmpManifest,
) -> list[str]:
    """Recompute honest provider/model activation blockers from lineage."""

    model = manifest.execution.model
    if model in {"none", "none/deterministic", "none/echo"}:
        return []
    errors: list[str] = []
    if manifest.execution.provider_binding is None:
        errors.append(f"{lineage.case_id}: real-model ProviderBinding is missing")
    receipt = bundle.provenance.model_activation
    if receipt is None:
        errors.append(f"{lineage.case_id}: ModelActivationReceipt is missing")
    elif receipt.status != "matched":
        reasons = "; ".join(receipt.reason) or "native activation did not match"
        errors.append(
            f"{lineage.case_id}: model activation is {receipt.status}: {reasons}"
        )
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

    expected_metric_results = []
    seen_metric_runs: set[str] = set()
    for lineage, _ in lineage_entries:
        if lineage.run_id in seen_metric_runs:
            continue
        seen_metric_runs.add(lineage.run_id)
        manifest = manifest_by_run.get(lineage.run_id)
        schedule = schedules.get(
            (lineage.run_id, lineage.attempt_id, lineage.case_id)
        )
        if manifest is None or schedule is None:
            mismatches.append(
                f"{lineage.run_id}: registered metrics lack manifest or schedule"
            )
            continue
        try:
            resolved_schedule_path = _resolve_path(
                lineage.schedule_receipt_ref.path,
                path_map,
            )
            expected_metric_results.extend(
                compute_metric_results(
                    manifest,
                    manifest.canonical_digest(),
                    schedule,
                    resolved_schedule_path,
                    schedule_receipt_ref=lineage.schedule_receipt_ref,
                    resolve_path=lambda value, mapping=path_map: _resolve_path(
                        value, mapping
                    ),
                )
            )
        except (OSError, ValueError) as exc:
            mismatches.append(
                f"{lineage.run_id}: registered metric replay failed: {exc}"
            )
    if tuple(report.metric_results) != tuple(expected_metric_results):
        mismatches.append(
            "report metric_results do not match replayed registered metrics"
        )

    if isinstance(report, ClaimReport):
        analysis_plan, plan_errors = _statistical_plan_for_lineage(
            lineage_entries, manifest_by_run
        )
        analysis_result: StatisticalAnalysisResult | None = None
        plan_counterbalanced = False
        if analysis_plan is not None:
            analysis_result, plan_counterbalanced, _ = _replay_statistical_plan(
                lineage_entries, manifest_by_run, analysis_plan
            )
        expected_statistics_receipt = (
            None if analysis_result is None else analysis_result.receipt
        )
        if report.statistics_receipt != expected_statistics_receipt:
            mismatches.append(
                "claim statistics_receipt does not match the replayed "
                "StatisticalAnalysisPlan"
            )
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
                case_set_receipt=case_set_receipts.get(
                    (lineage.run_id, lineage.attempt_id, lineage.case_id)
                ),
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
                and manifest_by_run[lineage.run_id].authoritative_reward_metric
                in bundle.verifier_evidence.metrics
                and bundle.verifier_evidence.metrics[
                    manifest_by_run[lineage.run_id].authoritative_reward_metric
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
                _model_activation_isolation_reasons(
                    lineage,
                    bundle,
                    manifest_by_run[lineage.run_id],
                )
            )
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
            mismatches.append(
                "claim isolation_valid does not match verified isolation evidence"
            )
        if plan_errors:
            statistics_expected = False
        elif analysis_plan is not None:
            statistics_expected = bool(
                plan_counterbalanced
                and analysis_result is not None
                and analysis_result.valid
            )
        else:
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
        metric = manifest.authoritative_reward_metric
        named_score = bundle.verifier_evidence.metrics.get(metric)
        if named_score is None or named_score != score:
            mismatches.append(
                f"{lineage.attempt_id}: authoritative metric {metric!r} "
                "does not bind to verifier score"
            )
            continue
        metric_scores.append((metric, named_score, (lineage.run_id, lineage.case_id)))

    if isinstance(report, ObservationReport):
        expected_protocol_reasons: list[str] = []
        for lineage, _ in lineage_entries:
            schedule = schedules.get(
                (lineage.run_id, lineage.attempt_id, lineage.case_id)
            )
            if schedule is None:
                expected_protocol_reasons.append(
                    f"{lineage.case_id}: ScheduleActivationReceipt missing"
                )
            elif not schedule.schedule_valid:
                reasons = "; ".join(schedule.mismatch_reasons) or "unspecified mismatch"
                expected_protocol_reasons.append(
                    f"{lineage.case_id}: schedule receipt is invalid: {reasons}"
                )
        expected_protocol_reasons = sorted(set(expected_protocol_reasons))
        if report.protocol_valid != (not expected_protocol_reasons):
            mismatches.append(
                "report protocol_valid does not match verified schedule evidence"
            )
        if tuple(report.protocol_reasons) != tuple(expected_protocol_reasons):
            mismatches.append(
                "report protocol_reasons do not match verified schedule evidence"
            )
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
                    _model_activation_isolation_reasons(
                        lineage,
                        bundle,
                        manifest,
                    )
                )
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
                    "report isolation_valid does not match verified isolation evidence"
                )
            if tuple(report.isolation_reasons) != tuple(expected_isolation_reasons):
                mismatches.append(
                    "report isolation_reasons do not match verified isolation evidence"
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
        manifest.authoritative_reward_metric
        for manifest in manifest_by_run.values()
    }
    authoritative_metric = next(iter(metric_set)) if len(metric_set) == 1 else None
    scores_by_lineage = {
        lineage_key: score for _, score, lineage_key in metric_scores
    }
    if analysis_plan is not None:
        # A registered statistical plan owns the estimand and interval method.
        # Reuse the independently replayed receipt instead of falling back to
        # the historical exploratory min/max interval.
        receipt = None if analysis_result is None else analysis_result.receipt
        expected_effect = (
            None
            if receipt is None or receipt.confidence_interval is None
            else {
                "metric": receipt.metric,
                "point_estimate": receipt.point_estimate,
                "confidence_interval": tuple(receipt.confidence_interval),
                "n_runs": len(lineage_entries),
                "n_pairs": receipt.observed_pair_count,
            }
        )
    elif authoritative_metric is None or len(scores_by_lineage) != len(lineage_entries):
        expected_effect = None
    else:
        first_manifest = next(iter(manifest_by_run.values()), None)
        contrast = None if first_manifest is None else first_manifest.contrast
        if contrast is None:
            expected_effect = None
        else:
            binding = _verification_contrast_binding(first_manifest) if first_manifest else None
            if binding is None:
                expected_effect = None
                binding = None
            else:
                factor_path, control_key, treatment_key = binding
            groups: dict[
                bytes,
                dict[str | bytes, tuple[tuple[str, str], float]],
            ] = {}
            if binding is not None:
                for lineage, _, manifest in (
                    (lineage, bundle, manifest_by_run[lineage.run_id])
                    for lineage, bundle in lineage_entries
                ):
                    factors = {
                        key: value
                        for key, value in manifest.metadata.factors.items()
                        if key
                        not in {
                            "subject",
                            "experiment.subject",
                            "order_position",
                            factor_path,
                        }
                    }
                    factors["__case_id"] = lineage.case_id
                    groups.setdefault(_canonical_key(factors), {})[
                        _verification_arm_key(
                            lineage,
                            manifest,
                            factor_path=factor_path,
                        )
                    ] = (
                        (lineage.run_id, lineage.case_id),
                        scores_by_lineage.get(
                            (lineage.run_id, lineage.case_id), float("nan")
                        ),
                    )
                differences: list[float] = []
                for pair in groups.values():
                    if control_key not in pair or treatment_key not in pair:
                        differences = []
                        break
                    control_score = pair[control_key][1]
                    treatment_score = pair[treatment_key][1]
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
        _verify_manifest_measurement_registry(
            manifest,
            label=f"manifest[{position}].registry_artifacts",
            path_map=relocation,
            mismatches=mismatches,
        )
        _verify_manifest_adapter_capabilities(
            manifest,
            label=f"manifest[{position}].adapter_capabilities",
            path_map=relocation,
            mismatches=mismatches,
        )
    observed_experiment_digest = _compact_json_digest(manifest_digests)
    if observed_experiment_digest != report.manifest_digest:
        mismatches.append(
            "report manifest_digest mismatch: "
            f"expected {report.manifest_digest}, observed {observed_experiment_digest}"
        )
    comparison_kinds = {
        manifest.claim_design.comparison_kind for manifest in manifests
    }
    if manifests and (
        len(comparison_kinds) != 1
        or report.comparison_kind not in comparison_kinds
    ):
        mismatches.append(
            "report comparison_kind does not match indexed resolved manifests"
        )
    subject_kinds = tuple(
        sorted(
            {manifest.subject.kind for manifest in manifests},
        )
    )
    if manifests and tuple(kind.value for kind in report.subject_kinds) != subject_kinds:
        mismatches.append(
            "report subject_kinds do not match indexed resolved manifests"
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
            expected_dataset_id=(
                None if manifest is None else manifest.dataset.id
            ),
            expected_dataset_digest=(
                None if manifest is None else manifest.dataset.artifact_digest
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
            expected_custom_order=(
                None if protocol is None else protocol.custom_order
            ),
            expected_source_content_digest=(
                None
                if manifest is None
                else manifest.dataset.source_content_digest
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


def probe_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a BMP integration probe and all retained artifacts"
    )
    parser.add_argument("record", help="Path to integration_probe.json")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Relocate an absolute recorded path prefix",
    )
    args = parser.parse_args(argv)
    try:
        verified = verify_integration_probe(
            args.record, path_map=_parse_path_maps(args.map)
        )
    except (ReportVerificationError, argparse.ArgumentTypeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"verified: {verified.record_path}")
    return 0


def authority_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an external protocol authority receipt"
    )
    parser.add_argument("receipt", help="Path to authority receipt JSON")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Relocate an absolute recorded path prefix",
    )
    args = parser.parse_args(argv)
    try:
        verified = verify_external_protocol_authority(
            args.receipt, path_map=_parse_path_maps(args.map)
        )
    except (ReportVerificationError, argparse.ArgumentTypeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"verified: {verified.receipt_path}")
    return 0


__all__ = [
    "ReportVerificationError",
    "VerifiedClaimReport",
    "VerifiedEvolutionRunEvidence",
    "VerifiedIntegrationProbeRecord",
    "VerifiedExternalProtocolAuthorityReceipt",
    "VerifiedObservationReport",
    "VerifiedRunReport",
    "verify_claim_report",
    "verify_evolution_run_evidence",
    "verify_integration_probe",
    "verify_external_protocol_authority",
    "verify_observation_report",
    "verify_run_report",
    "main",
    "probe_main",
    "authority_main",
]


if __name__ == "__main__":
    raise SystemExit(main())
