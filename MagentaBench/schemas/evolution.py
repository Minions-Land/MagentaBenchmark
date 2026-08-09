"""Auditable runtime receipts for evolution and meta-evolution execution.

The neutral :class:`EvolutionRunEvidence` contract records the candidate graph
and search transitions.  These models record the execution details that make
that graph reproducible: every evaluator query, the sealed holdout opening,
and exact budget debits.  Keeping this receipt separate lets external
evolution algorithms remain opaque while BMP verifies the boundary around
them.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from .models import (
    ID_PATTERN,
    SHA256_PATTERN,
    ArtifactRef,
    Budget,
    BudgetAllocation,
    EvolutionCandidateStatus,
    StrictModel,
    UsageRecord,
    _freeze_configuration_tree,
    _validate_json_configuration,
)


def _canonical_digest(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class EvolutionSelectionPolicy(StrictModel):
    """Explicit, replayable policy for choosing a parent/candidate.

    The runtime must receive this object from the experiment configuration (or
    use the documented deterministic default).  Keeping the selector identity,
    direction, tie break, and RNG state in the receipt prevents a later
    implementation change from silently changing which candidate was chosen.
    """

    policy_id: str = Field(pattern=ID_PATTERN)
    selector: Literal["extreme", "weighted"]
    metric: str = Field(min_length=1)
    direction: Literal["maximize", "minimize"]
    tie_break_rule: str = Field(min_length=1)
    child_count_penalty: float = Field(default=0.0, ge=0.0, strict=True)
    rng_algorithm: str = Field(default="none", min_length=1)
    rng_seed: int | None = None
    rng_state_before: str = Field(default="none", min_length=1)
    rng_state_after: str = Field(default="none", min_length=1)

    def identity_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_digest(self) -> str:
        return _canonical_digest(self.identity_data())


class EvolutionArchiveEntry(StrictModel):
    """One candidate's immutable state in an archive snapshot."""

    candidate_id: str = Field(pattern=ID_PATTERN)
    generation: int = Field(ge=0, strict=True)
    parent_ids: tuple[str, ...] = ()
    candidate_ref: ArtifactRef
    score: float | None = None
    score_metric: str | None = Field(default=None, min_length=1)
    status: Literal["ineligible", "eligible", "retained", "promoted", "evicted"]
    child_count: int = Field(default=0, ge=0, strict=True)
    lineage_depth: int = Field(default=0, ge=0, strict=True)

    @field_validator("parent_ids")
    @classmethod
    def archive_parent_ids_are_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("archive parent ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("archive parent ids must be valid BMP ids")
        return values

    @model_validator(mode="after")
    def score_binding_is_closed(self) -> "EvolutionArchiveEntry":
        if (self.score is None) != (self.score_metric is None):
            raise ValueError("archive score and score_metric must be supplied together")
        if self.status in {"eligible", "retained", "promoted"} and self.score is None:
            raise ValueError("eligible archive entries require an observed score")
        if self.status == "promoted" and self.score is None:
            raise ValueError("promoted archive entries require a full score")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "generation": self.generation,
            "parent_ids": list(self.parent_ids),
            "candidate_ref": self.candidate_ref.identity_data(),
            "score": self.score,
            "score_metric": self.score_metric,
            "status": self.status,
            "child_count": self.child_count,
            "lineage_depth": self.lineage_depth,
        }


class EvolutionArchiveSnapshot(StrictModel):
    """A complete archive population at one transition boundary."""

    format: Literal["bmp-evolution-archive-snapshot-v1"] = (
        "bmp-evolution-archive-snapshot-v1"
    )
    evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    metric: str = Field(min_length=1)
    policy_digest: str = Field(pattern=SHA256_PATTERN)
    entries: tuple[EvolutionArchiveEntry, ...] = ()

    @model_validator(mode="after")
    def snapshot_is_closed(self) -> "EvolutionArchiveSnapshot":
        ids = [entry.candidate_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("archive snapshot candidate ids must be unique")
        by_id = {entry.candidate_id: entry for entry in self.entries}
        for entry in self.entries:
            if entry.candidate_id in entry.parent_ids:
                raise ValueError("archive candidate cannot parent itself")
            unknown = sorted(set(entry.parent_ids) - set(by_id))
            if unknown:
                raise ValueError(
                    f"archive snapshot has unknown parents for {entry.candidate_id!r}: {unknown}"
                )
            if any(by_id[parent].generation >= entry.generation for parent in entry.parent_ids):
                raise ValueError("archive parent generation must precede child generation")
            if entry.score_metric is not None and entry.score_metric != self.metric:
                raise ValueError("archive entry score metric differs from snapshot metric")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "evaluator_digest": self.evaluator_digest,
            "metric": self.metric,
            "policy_digest": self.policy_digest,
            "entries": [entry.identity_data() for entry in self.entries],
        }

    def canonical_digest(self) -> str:
        return _canonical_digest(self.identity_data())


class EvolutionArchiveTransitionPhase(str, Enum):
    """Lifecycle boundary represented by an archive transition."""

    seed = "seed"
    generate = "generate"
    staged_evaluation = "staged_evaluation"
    revise = "revise"
    select = "select"
    promote = "promote"
    terminate = "terminate"


class EvolutionArchiveTransition(StrictModel):
    """Hash-chained before/after archive state for one lifecycle transition."""

    transition_id: str = Field(pattern=ID_PATTERN)
    sequence: int = Field(ge=0, strict=True)
    phase: EvolutionArchiveTransitionPhase
    before: EvolutionArchiveSnapshot
    after: EvolutionArchiveSnapshot
    before_archive_digest: str = Field(pattern=SHA256_PATTERN)
    after_archive_digest: str = Field(pattern=SHA256_PATTERN)
    previous_transition_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    candidate_ids: tuple[str, ...] = ()
    eligible_candidate_ids: tuple[str, ...] = ()
    selected_candidate_id: str | None = Field(default=None, pattern=ID_PATTERN)
    promotion_gate_id: str | None = Field(default=None, pattern=ID_PATTERN)
    policy_digest: str = Field(pattern=SHA256_PATTERN)
    reason: str = Field(min_length=1)

    @field_validator("candidate_ids", "eligible_candidate_ids")
    @classmethod
    def transition_candidate_ids_are_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("archive transition candidate ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("archive transition candidate ids must be valid BMP ids")
        return values

    @model_validator(mode="after")
    def transition_bindings_are_closed(self) -> "EvolutionArchiveTransition":
        if self.before_archive_digest != self.before.canonical_digest():
            raise ValueError("archive transition before digest drift")
        if self.after_archive_digest != self.after.canonical_digest():
            raise ValueError("archive transition after digest drift")
        if self.before.policy_digest != self.policy_digest or self.after.policy_digest != self.policy_digest:
            raise ValueError("archive transition policy digest drift")
        all_ids = {
            entry.candidate_id for entry in (*self.before.entries, *self.after.entries)
        }
        if not set(self.candidate_ids).issubset(all_ids):
            raise ValueError("archive transition names an unknown candidate")
        after_by_id = {entry.candidate_id: entry for entry in self.after.entries}
        if not set(self.eligible_candidate_ids).issubset(
            {candidate_id for candidate_id, entry in after_by_id.items() if entry.status in {"eligible", "retained", "promoted"}}
        ):
            raise ValueError("archive transition eligible set disagrees with after snapshot")
        if self.selected_candidate_id is not None:
            selected = after_by_id.get(self.selected_candidate_id)
            if selected is None or selected.status not in {"retained", "promoted"}:
                raise ValueError("archive transition selected candidate is not retained")
        if self.phase == EvolutionArchiveTransitionPhase.promote and self.promotion_gate_id is None:
            raise ValueError("promotion transition requires a promotion gate")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "sequence": self.sequence,
            "phase": self.phase.value,
            "before_archive_digest": self.before_archive_digest,
            "after_archive_digest": self.after_archive_digest,
            "previous_transition_digest": self.previous_transition_digest,
            "candidate_ids": list(self.candidate_ids),
            "eligible_candidate_ids": list(self.eligible_candidate_ids),
            "selected_candidate_id": self.selected_candidate_id,
            "promotion_gate_id": self.promotion_gate_id,
            "policy_digest": self.policy_digest,
            "reason": self.reason,
            "before": self.before.identity_data(),
            "after": self.after.identity_data(),
        }

    def canonical_digest(self) -> str:
        return _canonical_digest(self.identity_data())


class EvolutionArchiveLedger(StrictModel):
    """Complete archive history; no state may be reconstructed from a snapshot."""

    format: Literal["bmp-evolution-archive-ledger-v1"] = (
        "bmp-evolution-archive-ledger-v1"
    )
    policy: EvolutionSelectionPolicy
    evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    transitions: tuple[EvolutionArchiveTransition, ...] = Field(min_length=1)
    final_archive_digest: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def archive_chain_is_replayable(self) -> "EvolutionArchiveLedger":
        policy_digest = self.policy.canonical_digest()
        sequences = [item.sequence for item in self.transitions]
        if sequences != list(range(len(self.transitions))):
            raise ValueError("archive transition sequences must be contiguous")
        previous: EvolutionArchiveTransition | None = None
        for transition in self.transitions:
            if transition.policy_digest != policy_digest:
                raise ValueError("archive transition policy differs from ledger policy")
            if transition.before.evaluator_digest != self.evaluator_digest or transition.after.evaluator_digest != self.evaluator_digest:
                raise ValueError("archive transition evaluator authority drift")
            if previous is None:
                if transition.previous_transition_digest is not None:
                    raise ValueError("first archive transition cannot have a predecessor")
            else:
                if transition.previous_transition_digest != previous.canonical_digest():
                    raise ValueError("archive transition hash chain drift")
                if transition.before_archive_digest != previous.after_archive_digest:
                    raise ValueError("archive transition state chain drift")
            previous = transition
        assert previous is not None
        if self.final_archive_digest != previous.after_archive_digest:
            raise ValueError("final archive digest drift")
        return self


class EvolutionSelectionCandidate(StrictModel):
    """One member of the complete parent-selection population."""

    candidate_id: str = Field(pattern=ID_PATTERN)
    eligible: bool
    raw_score: float | None = None
    transformed_score: float | None = None
    child_count: int = Field(default=0, ge=0, strict=True)
    weight: float = Field(default=0.0, ge=0.0, strict=True)
    probability: float = Field(default=0.0, ge=0.0, le=1.0, strict=True)

    @model_validator(mode="after")
    def selection_score_shape_is_closed(self) -> "EvolutionSelectionCandidate":
        if self.eligible and (self.raw_score is None or self.transformed_score is None):
            raise ValueError("eligible selection candidates require scores")
        if not self.eligible and (self.weight != 0.0 or self.probability != 0.0):
            raise ValueError("ineligible selection candidates cannot carry probability")
        return self

    def identity_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class EvolutionParentSelectionReceipt(StrictModel):
    """Standalone receipt for the parent/candidate selection decision."""

    format: Literal["bmp-evolution-parent-selection-v1"] = (
        "bmp-evolution-parent-selection-v1"
    )
    selection_id: str = Field(pattern=ID_PATTERN)
    transition_id: str = Field(pattern=ID_PATTERN)
    transition_sequence: int = Field(ge=0, strict=True)
    policy: EvolutionSelectionPolicy
    candidate_set: tuple[EvolutionSelectionCandidate, ...] = Field(min_length=1)
    candidate_set_digest: str = Field(pattern=SHA256_PATTERN)
    selected_candidate_id: str = Field(pattern=ID_PATTERN)
    fallback_reason: str | None = None

    @model_validator(mode="after")
    def selection_is_replayable(self) -> "EvolutionParentSelectionReceipt":
        ids = [item.candidate_id for item in self.candidate_set]
        if len(set(ids)) != len(ids):
            raise ValueError("selection candidate set must be complete and unique")
        if self.candidate_set_digest != _canonical_digest(
            [item.identity_data() for item in self.candidate_set]
        ):
            raise ValueError("selection candidate set digest drift")
        selected = next((item for item in self.candidate_set if item.candidate_id == self.selected_candidate_id), None)
        if selected is None or not selected.eligible:
            raise ValueError("selected parent must be an eligible candidate")
        probabilities = sum(item.probability for item in self.candidate_set)
        if abs(probabilities - 1.0) > 1e-9:
            raise ValueError("selection probabilities must sum to one")
        if selected.probability <= 0.0:
            raise ValueError("selected parent must have positive selection probability")
        if self.policy.metric != self.policy.metric.strip():
            raise ValueError("selection policy metric is not canonical")
        return self


class EvolutionPromotionGateReceipt(StrictModel):
    """Explicit staged-to-full promotion decision."""

    format: Literal["bmp-evolution-promotion-gate-v1"] = (
        "bmp-evolution-promotion-gate-v1"
    )
    gate_id: str = Field(pattern=ID_PATTERN)
    candidate_id: str = Field(pattern=ID_PATTERN)
    selection_transition_id: str = Field(pattern=ID_PATTERN)
    staged_evaluation_id: str = Field(pattern=ID_PATTERN)
    full_evaluation_id: str = Field(pattern=ID_PATTERN)
    staged_evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    full_evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    staged_metric: str = Field(min_length=1)
    full_metric: str = Field(min_length=1)
    staged_score: float
    full_score: float
    decision: Literal["promote", "reject"]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def promotion_lineage_is_closed(self) -> "EvolutionPromotionGateReceipt":
        if self.staged_evaluator_digest == self.full_evaluator_digest:
            raise ValueError("staged and full promotion authorities must differ")
        if self.staged_evaluation_id == self.full_evaluation_id:
            raise ValueError("staged and full evaluation ids must differ")
        if self.decision == "promote" and not self.full_evaluation_id:
            raise ValueError("promotion requires a full evaluation")
        return self


class EvolutionFailureSample(StrictModel):
    """One member of the complete population considered for diagnosis.

    Successful and failed members are retained together.  This keeps the
    failure-sampling denominator auditable and prevents adapters from building
    a convenient sample by silently dropping infrastructure failures.
    """

    item_id: str = Field(pattern=ID_PATTERN)
    candidate_id: str = Field(pattern=ID_PATTERN)
    outcome: Literal[
        "success",
        "task_failure",
        "no_output",
        "invalid_output",
        "timeout",
        "agent_error",
        "harness_fault",
        "verifier_error",
        "infra_error",
        "unsupported",
    ]
    eligible: bool
    included: bool
    inclusion_probability: float = Field(ge=0.0, le=1.0, strict=True)
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def sampling_state_is_coherent(self) -> "EvolutionFailureSample":
        is_failure = self.outcome != "success"
        if self.eligible != is_failure:
            raise ValueError("failure-sampling eligibility must match the observed outcome")
        if self.included and not self.eligible:
            raise ValueError("successful population members cannot enter a failure sample")
        if not self.eligible and self.inclusion_probability != 0.0:
            raise ValueError("ineligible failure samples require zero probability")
        if self.included and self.inclusion_probability <= 0.0:
            raise ValueError("included failure samples require positive probability")
        ref_keys = [(ref.sha256, ref.size_bytes) for ref in self.evidence_refs]
        if len(set(ref_keys)) != len(ref_keys):
            raise ValueError("failure-sampling evidence refs must be unique")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "candidate_id": self.candidate_id,
            "outcome": self.outcome,
            "eligible": self.eligible,
            "included": self.included,
            "inclusion_probability": self.inclusion_probability,
            "evidence_refs": [ref.identity_data() for ref in self.evidence_refs],
        }


class EvolutionFailureSamplingReceipt(StrictModel):
    """Replayable selection of failures passed to a diagnosis component."""

    format: Literal["bmp-evolution-failure-sampling-v1"] = (
        "bmp-evolution-failure-sampling-v1"
    )
    sampling_id: str = Field(pattern=ID_PATTERN)
    policy_id: str = Field(pattern=ID_PATTERN)
    population: tuple[EvolutionFailureSample, ...] = Field(min_length=1)
    population_digest: str = Field(pattern=SHA256_PATTERN)
    selected_item_ids: tuple[str, ...] = ()
    sample_size: int = Field(ge=0, strict=True)
    sampling_algorithm: str = Field(min_length=1)
    rng_algorithm: str = Field(default="none", min_length=1)
    rng_seed: int | None = None
    rng_state_before: str = Field(default="none", min_length=1)
    rng_state_after: str = Field(default="none", min_length=1)
    complete_population: Literal[True] = True
    budget_event_id: str | None = Field(default=None, pattern=ID_PATTERN)
    reason: str = Field(min_length=1)

    @field_validator("selected_item_ids")
    @classmethod
    def selected_ids_are_unique_and_valid(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("selected failure-sampling ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("selected failure-sampling ids must be BMP ids")
        return values

    @model_validator(mode="after")
    def sampling_population_is_replayable(self) -> "EvolutionFailureSamplingReceipt":
        ids = [item.item_id for item in self.population]
        if len(set(ids)) != len(ids):
            raise ValueError("failure-sampling population ids must be unique")
        observed_digest = _canonical_digest(
            [item.identity_data() for item in self.population]
        )
        if self.population_digest != observed_digest:
            raise ValueError("failure-sampling population digest drift")
        included = tuple(item.item_id for item in self.population if item.included)
        if self.selected_item_ids != included:
            raise ValueError("failure-sampling selected ids disagree with population")
        if self.sample_size != len(included):
            raise ValueError("failure-sampling sample_size drift")
        if self.rng_algorithm == "none" and (
            self.rng_seed is not None
            or self.rng_state_before != "none"
            or self.rng_state_after != "none"
        ):
            raise ValueError("deterministic failure sampling cannot carry RNG state")
        return self


class EvolutionDiagnosisReceipt(StrictModel):
    """Content-addressed diagnosis of one sampled failure."""

    format: Literal["bmp-evolution-diagnosis-v1"] = "bmp-evolution-diagnosis-v1"
    diagnosis_id: str = Field(pattern=ID_PATTERN)
    failure_sampling_id: str = Field(pattern=ID_PATTERN)
    sampled_item_id: str = Field(pattern=ID_PATTERN)
    candidate_id: str = Field(pattern=ID_PATTERN)
    diagnosis_component_digest: str = Field(pattern=SHA256_PATTERN)
    prompt_ref: ArtifactRef
    result_ref: ArtifactRef
    failure_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    budget_event_id: str = Field(pattern=ID_PATTERN)
    root_cause: str = Field(min_length=1)
    retry_recommended: bool
    attributes: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("failure_evidence_refs")
    @classmethod
    def diagnosis_evidence_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        keys = [(ref.sha256, ref.size_bytes) for ref in values]
        if len(set(keys)) != len(keys):
            raise ValueError("diagnosis failure evidence refs must be unique")
        return values

    @field_validator("attributes", mode="before")
    @classmethod
    def diagnosis_attributes_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="EvolutionDiagnosisReceipt.attributes"
        )

    @model_validator(mode="after")
    def diagnosis_is_content_addressed(self) -> "EvolutionDiagnosisReceipt":
        if self.diagnosis_component_digest not in {
            self.prompt_ref.sha256,
            self.result_ref.sha256,
        }:
            # The component may be a separate implementation artifact.  It
            # must nevertheless be represented in the evidence closure.
            if all(
                ref.sha256 != self.diagnosis_component_digest
                for ref in self.failure_evidence_refs
            ):
                raise ValueError(
                    "diagnosis component digest lacks a content-addressed evidence ref"
                )
        object.__setattr__(
            self, "attributes", _freeze_configuration_tree(self.attributes)
        )
        return self


class MetaEvolutionEditPolicy(StrictModel):
    """Closed edit/visibility boundary for a meta-evolution method."""

    policy_id: str = Field(pattern=ID_PATTERN)
    editable_paths: tuple[str, ...] = Field(min_length=1)
    protected_paths: tuple[str, ...] = Field(min_length=1)
    feedback_visibility: tuple[
        Literal[
            "search_scores",
            "search_feedback",
            "failure_diagnostics",
            "candidate_history",
            "resource_usage",
        ],
        ...,
    ] = ()
    sealed_holdout_visible: Literal[False] = False

    @field_validator("editable_paths", "protected_paths")
    @classmethod
    def edit_paths_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("meta-evolution policy paths must be unique")
        path_pattern = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
        if any(path_pattern.fullmatch(value) is None for value in values):
            raise ValueError("meta-evolution policy paths must be canonical dotted paths")
        return values

    @field_validator("feedback_visibility")
    @classmethod
    def visibility_entries_are_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("meta-evolution feedback visibility entries must be unique")
        return values

    @model_validator(mode="after")
    def editable_and_protected_paths_do_not_overlap(self) -> "MetaEvolutionEditPolicy":
        for editable in self.editable_paths:
            for protected in self.protected_paths:
                if (
                    editable == protected
                    or editable.startswith(f"{protected}.")
                    or protected.startswith(f"{editable}.")
                ):
                    raise ValueError(
                        "meta-evolution editable and protected paths overlap"
                    )
        return self

    def identity_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_digest(self) -> str:
        return _canonical_digest(self.identity_data())


class MetaEvolutionEditReceipt(StrictModel):
    """Replayable prompt/patch application under a meta-evolution policy."""

    format: Literal["bmp-meta-evolution-edit-v1"] = "bmp-meta-evolution-edit-v1"
    edit_id: str = Field(pattern=ID_PATTERN)
    policy: MetaEvolutionEditPolicy
    policy_digest: str = Field(pattern=SHA256_PATTERN)
    target_before_digest: str = Field(pattern=SHA256_PATTERN)
    target_after_digest: str = Field(pattern=SHA256_PATTERN)
    changed_paths: tuple[str, ...] = Field(min_length=1)
    prompt_ref: ArtifactRef
    patch_ref: ArtifactRef
    result_ref: ArtifactRef
    visible_feedback_refs: tuple[ArtifactRef, ...] = ()
    sealed_holdout_access_count: Literal[0] = 0
    budget_event_id: str = Field(pattern=ID_PATTERN)

    @field_validator("changed_paths")
    @classmethod
    def changed_paths_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("meta-evolution changed paths must be unique")
        return values

    @field_validator("visible_feedback_refs")
    @classmethod
    def feedback_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        keys = [(ref.sha256, ref.size_bytes) for ref in values]
        if len(set(keys)) != len(keys):
            raise ValueError("meta-evolution feedback refs must be unique")
        return values

    @model_validator(mode="after")
    def edit_respects_policy(self) -> "MetaEvolutionEditReceipt":
        if self.policy_digest != self.policy.canonical_digest():
            raise ValueError("meta-evolution edit policy digest drift")
        if self.target_before_digest == self.target_after_digest:
            raise ValueError("meta-evolution edit must change the target digest")

        def covered(path: str, roots: tuple[str, ...]) -> bool:
            return any(path == root or path.startswith(f"{root}.") for root in roots)

        for path in self.changed_paths:
            if not covered(path, self.policy.editable_paths):
                raise ValueError("meta-evolution edit changed a non-editable path")
            if covered(path, self.policy.protected_paths):
                raise ValueError("meta-evolution edit changed a protected path")
        return self


# Concise protocol names used by papers and external adapters.  The prefixed
# names remain canonical inside BMP and in generated schema filenames.
FailureSamplingReceipt = EvolutionFailureSamplingReceipt
DiagnosisReceipt = EvolutionDiagnosisReceipt


class EvolutionEvaluationStage(str, Enum):
    """Evaluator authority used for one query."""

    search = "search"
    sealed_holdout = "sealed_holdout"


class EvolutionBudgetOperation(str, Enum):
    """Billable operations understood by the neutral runtime."""

    parent_evolution = "parent_evolution"
    seed = "seed"
    generate = "generate"
    search_evaluate = "search_evaluate"
    revise = "revise"
    holdout_evaluate = "holdout_evaluate"
    # The following operations are explicit so adapters cannot hide work in a
    # free-form ``attributes`` map.  Existing v1 operations remain stable for
    # backwards-compatible receipts; new runtimes should use the more precise
    # staged/full names when they can distinguish the boundary.
    coding_agent_call = "coding_agent_call"
    meta_agent_call = "meta_agent_call"
    diagnosis = "diagnosis"
    parent_selection = "parent_selection"
    staged_evaluation = "staged_evaluation"
    full_evaluation = "full_evaluation"
    retry = "retry"
    checkpoint = "checkpoint"
    container_build = "container_build"
    failure_sampling = "failure_sampling"


class EvolutionBudgetEvent(StrictModel):
    """One measured operation debit and its root-budget lineage."""

    event_id: str = Field(pattern=ID_PATTERN)
    sequence: int = Field(ge=0, strict=True)
    operation: EvolutionBudgetOperation
    candidate_ids: tuple[str, ...]
    spent: UsageRecord
    usage_observable: bool
    remaining_after: BudgetAllocation
    budget_exceeded: bool = False
    attributes: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("candidate_ids")
    @classmethod
    def candidate_ids_are_unique_and_valid(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("evolution budget event candidate ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("evolution budget event candidate ids must be BMP ids")
        return values

    @field_validator("attributes", mode="before")
    @classmethod
    def attributes_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="EvolutionBudgetEvent.attributes"
        )

    @model_validator(mode="after")
    def observation_matches_usage(self) -> "EvolutionBudgetEvent":
        if self.operation == EvolutionBudgetOperation.parent_evolution:
            if self.candidate_ids:
                raise ValueError("parent evolution budget cannot name child candidates")
        elif not self.candidate_ids:
            raise ValueError("evolution budget events require candidate ids")
        token_cost_observed = (
            self.spent.total_tokens is not None and self.spent.cost is not None
        )
        if self.usage_observable != token_cost_observed:
            raise ValueError(
                "usage_observable must exactly reflect token and cost counters"
            )
        object.__setattr__(
            self, "attributes", _freeze_configuration_tree(self.attributes)
        )
        return self


class EvolutionBudgetLedger(StrictModel):
    """Root evolution budget replayed across every billable operation."""

    budget_digest: str = Field(pattern=SHA256_PATTERN)
    budget_ref: ArtifactRef
    declared_budget: Budget
    events: tuple[EvolutionBudgetEvent, ...] = Field(min_length=1)
    total_usage: UsageRecord
    elapsed_wall_seconds: float = Field(ge=0, strict=True)
    usage_observable: bool
    reconciles_exactly: bool
    budget_exceeded: bool

    @model_validator(mode="after")
    def replay_debits(self) -> "EvolutionBudgetLedger":
        if self.budget_ref.sha256 != self.budget_digest:
            raise ValueError("evolution budget ref digest drift")
        if _canonical_digest(self.declared_budget) != self.budget_digest:
            raise ValueError("declared evolution budget digest drift")

        sequences = [event.sequence for event in self.events]
        event_ids = [event.event_id for event in self.events]
        if sequences != list(range(len(self.events))):
            raise ValueError("evolution budget event sequences must be contiguous")
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("evolution budget event ids must be unique")

        observable = all(event.usage_observable for event in self.events)
        if self.usage_observable != observable:
            raise ValueError("evolution budget observability drift")

        token_values = [event.spent.total_tokens for event in self.events]
        cost_values = [event.spent.cost for event in self.events]
        exact = observable
        if exact:
            expected_tokens = sum(value for value in token_values if value is not None)
            expected_cost = sum(value for value in cost_values if value is not None)
            if self.total_usage.total_tokens != expected_tokens:
                raise ValueError("evolution total token usage does not reconcile")
            if self.total_usage.cost != expected_cost:
                raise ValueError("evolution total cost does not reconcile")
        if self.reconciles_exactly != exact:
            raise ValueError("evolution budget reconciliation flag drift")

        token_cap = self.declared_budget.max_tokens
        cost_cap = self.declared_budget.max_cost
        spent_tokens = 0
        spent_cost = 0.0
        observed_exceeded = False
        for index, event in enumerate(self.events):
            if event.usage_observable:
                assert event.spent.total_tokens is not None
                assert event.spent.cost is not None
                spent_tokens += event.spent.total_tokens
                spent_cost += event.spent.cost
                expected_remaining_tokens = (
                    None if token_cap is None else max(0, token_cap - spent_tokens)
                )
                expected_remaining_cost = (
                    None if cost_cap is None else max(0.0, cost_cap - spent_cost)
                )
                if event.remaining_after.max_tokens != expected_remaining_tokens:
                    raise ValueError("evolution remaining token budget drift")
                if event.remaining_after.max_cost != expected_remaining_cost:
                    raise ValueError("evolution remaining cost budget drift")
                event_exceeded = (
                    (token_cap is not None and spent_tokens > token_cap)
                    or (cost_cap is not None and spent_cost > cost_cap + 1e-15)
                )
                if event.budget_exceeded != event_exceeded:
                    raise ValueError("evolution budget-exceeded event flag drift")
                observed_exceeded = observed_exceeded or event_exceeded
                if event_exceeded and index != len(self.events) - 1:
                    raise ValueError("evolution execution continued after budget exhaustion")
            elif event.budget_exceeded:
                raise ValueError("unobservable usage cannot assert budget_exceeded")

        if self.budget_exceeded != observed_exceeded:
            raise ValueError("evolution root budget-exceeded flag drift")
        if (
            self.declared_budget.max_wall_seconds is not None
            and self.elapsed_wall_seconds > self.declared_budget.max_wall_seconds
            and not self.budget_exceeded
        ):
            raise ValueError("evolution wall budget exceeded without a failure flag")
        return self


class EvolutionEvaluationRecord(StrictModel):
    """One content-addressed query to search or sealed-holdout evaluation."""

    evaluation_id: str = Field(pattern=ID_PATTERN)
    sequence: int = Field(ge=0, strict=True)
    stage: EvolutionEvaluationStage
    candidate_id: str = Field(pattern=ID_PATTERN)
    evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    evaluator_ref: ArtifactRef
    split_manifest_ref: ArtifactRef
    candidate_ref: ArtifactRef
    request_ref: ArtifactRef
    result_ref: ArtifactRef
    score: float
    score_metric: str = Field(min_length=1)
    budget_event_id: str = Field(pattern=ID_PATTERN)
    after_transition_sequence: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def evaluator_ref_matches_digest(self) -> "EvolutionEvaluationRecord":
        if self.evaluator_ref.sha256 != self.evaluator_digest:
            raise ValueError("evaluation evaluator ref digest drift")
        return self


class EvolutionSealedHoldoutReceipt(StrictModel):
    """Evidence that the authoritative split opened only after selection."""

    format: Literal["bmp-evolution-sealed-holdout-v1"] = (
        "bmp-evolution-sealed-holdout-v1"
    )
    split_manifest_ref: ArtifactRef
    search_evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    holdout_evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    selected_candidate_id: str = Field(pattern=ID_PATTERN)
    selection_transition_id: str = Field(pattern=ID_PATTERN)
    selection_transition_sequence: int = Field(ge=0, strict=True)
    holdout_evaluation_id: str = Field(pattern=ID_PATTERN)
    holdout_evaluation_sequence: int = Field(ge=0, strict=True)
    access_count: Literal[1] = 1

    @model_validator(mode="after")
    def evaluator_authorities_are_distinct(self) -> "EvolutionSealedHoldoutReceipt":
        if self.search_evaluator_digest == self.holdout_evaluator_digest:
            raise ValueError("search and sealed-holdout evaluators must be distinct")
        return self


class EvolutionRuntimeReceipt(StrictModel):
    """Complete execution audit bound to one ``EvolutionRunEvidence`` record."""

    format: Literal["bmp-evolution-runtime-v1"] = "bmp-evolution-runtime-v1"
    run_id: str = Field(pattern=ID_PATTERN)
    kind: Literal["evolver", "meta_evolver"]
    adapter_digest: str = Field(pattern=SHA256_PATTERN)
    evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    budget_digest: str = Field(pattern=SHA256_PATTERN)
    candidate_ledger_digest: str = Field(pattern=SHA256_PATTERN)
    transition_ledger_digest: str = Field(pattern=SHA256_PATTERN)
    selected_candidate_id: str = Field(pattern=ID_PATTERN)
    evaluations: tuple[EvolutionEvaluationRecord, ...] = Field(min_length=2)
    budget_ledger: EvolutionBudgetLedger
    sealed_holdout: EvolutionSealedHoldoutReceipt
    # These receipts are optional when reading legacy v1 evidence, but every
    # runtime produced by the current implementation populates all three.
    # Keeping them as typed fields lets standalone verification replay archive
    # state and selection without trusting adapter-specific ``attributes``.
    archive_ledger: EvolutionArchiveLedger | None = None
    parent_selection: EvolutionParentSelectionReceipt | None = None
    promotion_gate: EvolutionPromotionGateReceipt | None = None
    failure_sampling: EvolutionFailureSamplingReceipt | None = None
    diagnoses: tuple[EvolutionDiagnosisReceipt, ...] = ()
    meta_edit: MetaEvolutionEditReceipt | None = None
    parent_evidence_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def runtime_lineage_is_closed(self) -> "EvolutionRuntimeReceipt":
        if self.budget_ledger.budget_digest != self.budget_digest:
            raise ValueError("runtime budget ledger digest drift")
        if self.sealed_holdout.holdout_evaluator_digest != self.evaluator_digest:
            raise ValueError("runtime sealed-holdout evaluator digest drift")

        ids = [record.evaluation_id for record in self.evaluations]
        sequences = [record.sequence for record in self.evaluations]
        if len(set(ids)) != len(ids):
            raise ValueError("evolution evaluation ids must be unique")
        if sequences != list(range(len(self.evaluations))):
            raise ValueError("evolution evaluation sequences must be contiguous")

        holdout = [
            record
            for record in self.evaluations
            if record.stage == EvolutionEvaluationStage.sealed_holdout
        ]
        search = [
            record
            for record in self.evaluations
            if record.stage == EvolutionEvaluationStage.search
        ]
        if not search or len(holdout) != 1:
            raise ValueError(
                "runtime requires search evaluations and exactly one sealed holdout"
            )
        holdout_record = holdout[0]
        if holdout_record != self.evaluations[-1]:
            raise ValueError("sealed holdout must be the final evaluator query")
        if any(
            record.evaluator_digest
            != self.sealed_holdout.search_evaluator_digest
            for record in search
        ):
            raise ValueError("search evaluation authority drift")
        if len({record.split_manifest_ref.sha256 for record in search}) != 1:
            raise ValueError("search split authority drift")
        if holdout_record.evaluator_digest != self.evaluator_digest:
            raise ValueError("holdout evaluation authority drift")
        if holdout_record.split_manifest_ref != self.sealed_holdout.split_manifest_ref:
            raise ValueError("sealed holdout split authority drift")
        if (
            search[0].split_manifest_ref.sha256
            == holdout_record.split_manifest_ref.sha256
        ):
            raise ValueError("search and sealed-holdout splits must be distinct")
        if holdout_record.candidate_id != self.selected_candidate_id:
            raise ValueError("holdout evaluated a non-selected candidate")
        if (
            holdout_record.evaluation_id
            != self.sealed_holdout.holdout_evaluation_id
            or holdout_record.sequence
            != self.sealed_holdout.holdout_evaluation_sequence
        ):
            raise ValueError("sealed holdout access/evaluation lineage drift")
        selection_sequence = self.sealed_holdout.selection_transition_sequence
        if holdout_record.after_transition_sequence != selection_sequence:
            raise ValueError("sealed holdout must open immediately after selection")
        if any(
            record.after_transition_sequence >= selection_sequence
            for record in search
        ):
            raise ValueError("search evaluation occurred after selection")
        transition_positions = [
            record.after_transition_sequence for record in self.evaluations
        ]
        if transition_positions != sorted(transition_positions):
            raise ValueError("evaluator query transition lineage is not monotonic")

        budget_ids = {event.event_id for event in self.budget_ledger.events}
        evaluation_budget_ids = {record.budget_event_id for record in self.evaluations}
        if not evaluation_budget_ids.issubset(budget_ids):
            raise ValueError("evaluator query lacks budget lineage")
        event_by_id = {
            event.event_id: event for event in self.budget_ledger.events
        }
        for record in self.evaluations:
            expected_operation = (
                EvolutionBudgetOperation.search_evaluate
                if record.stage == EvolutionEvaluationStage.search
                else EvolutionBudgetOperation.holdout_evaluate
            )
            event = event_by_id[record.budget_event_id]
            if (
                event.operation != expected_operation
                or record.candidate_id not in event.candidate_ids
            ):
                raise ValueError("evaluator query/budget event lineage drift")

        parent_events = [
            event
            for event in self.budget_ledger.events
            if event.operation == EvolutionBudgetOperation.parent_evolution
        ]
        if self.kind == "meta_evolver":
            if self.parent_evidence_ref is None:
                raise ValueError("meta-evolution runtime requires parent evidence")
            if parent_events != [self.budget_ledger.events[0]]:
                raise ValueError(
                    "meta-evolution budget requires one leading parent debit"
                )
            if (
                parent_events[0].attributes.get("parent_evidence_sha256")
                != self.parent_evidence_ref.sha256
            ):
                raise ValueError("meta-evolution parent budget lineage drift")
        elif self.parent_evidence_ref is not None:
            raise ValueError("evolution runtime cannot carry parent evidence")
        elif parent_events:
            raise ValueError("evolver budget cannot debit parent evolution")

        event_by_id = {event.event_id: event for event in self.budget_ledger.events}
        if self.failure_sampling is not None:
            sampling_event_id = self.failure_sampling.budget_event_id
            if sampling_event_id is not None:
                sampling_event = event_by_id.get(sampling_event_id)
                if (
                    sampling_event is None
                    or sampling_event.operation
                    != EvolutionBudgetOperation.failure_sampling
                ):
                    raise ValueError("failure sampling lacks matching budget lineage")
        elif self.diagnoses:
            raise ValueError("diagnosis receipts require a failure-sampling receipt")

        sampled_ids = (
            set()
            if self.failure_sampling is None
            else set(self.failure_sampling.selected_item_ids)
        )
        diagnosis_ids: set[str] = set()
        for diagnosis in self.diagnoses:
            if diagnosis.diagnosis_id in diagnosis_ids:
                raise ValueError("evolution diagnosis ids must be unique")
            diagnosis_ids.add(diagnosis.diagnosis_id)
            if self.failure_sampling is None or (
                diagnosis.failure_sampling_id != self.failure_sampling.sampling_id
                or diagnosis.sampled_item_id not in sampled_ids
            ):
                raise ValueError("diagnosis/failure-sampling lineage drift")
            event = event_by_id.get(diagnosis.budget_event_id)
            if event is None or event.operation != EvolutionBudgetOperation.diagnosis:
                raise ValueError("diagnosis lacks matching budget lineage")

        if self.kind == "evolver" and self.meta_edit is not None:
            raise ValueError("evolver runtime cannot carry a meta-evolution edit")
        if self.meta_edit is not None:
            event = event_by_id.get(self.meta_edit.budget_event_id)
            if event is None or event.operation != EvolutionBudgetOperation.meta_agent_call:
                raise ValueError("meta-evolution edit lacks matching budget lineage")

        receipts = (self.archive_ledger, self.parent_selection, self.promotion_gate)
        if any(receipt is not None for receipt in receipts):
            if not all(receipt is not None for receipt in receipts):
                raise ValueError(
                    "archive, parent-selection, and promotion receipts must be emitted together"
                )
            assert self.archive_ledger is not None
            assert self.parent_selection is not None
            assert self.promotion_gate is not None
            if self.archive_ledger.evaluator_digest != self.evaluator_digest:
                raise ValueError("archive evaluator authority drift")
            if self.archive_ledger.final_archive_digest != self.archive_ledger.transitions[-1].after_archive_digest:
                raise ValueError("archive final state drift")
            if self.parent_selection.selected_candidate_id != self.selected_candidate_id:
                raise ValueError("selection/runtime selected candidate drift")
            if self.parent_selection.policy.canonical_digest() != self.archive_ledger.policy.canonical_digest():
                raise ValueError("selection/archive policy drift")
            if self.promotion_gate.candidate_id != self.selected_candidate_id:
                raise ValueError("promotion/runtime selected candidate drift")
            if self.promotion_gate.full_evaluation_id != self.sealed_holdout.holdout_evaluation_id:
                raise ValueError("promotion/full evaluation lineage drift")
        return self


__all__ = [
    "DiagnosisReceipt",
    "EvolutionArchiveEntry",
    "EvolutionArchiveLedger",
    "EvolutionArchiveSnapshot",
    "EvolutionArchiveTransition",
    "EvolutionArchiveTransitionPhase",
    "EvolutionBudgetEvent",
    "EvolutionBudgetLedger",
    "EvolutionBudgetOperation",
    "EvolutionDiagnosisReceipt",
    "EvolutionEvaluationRecord",
    "EvolutionEvaluationStage",
    "EvolutionFailureSample",
    "EvolutionFailureSamplingReceipt",
    "EvolutionParentSelectionReceipt",
    "EvolutionPromotionGateReceipt",
    "EvolutionRuntimeReceipt",
    "EvolutionSelectionCandidate",
    "EvolutionSelectionPolicy",
    "EvolutionSealedHoldoutReceipt",
    "FailureSamplingReceipt",
    "MetaEvolutionEditPolicy",
    "MetaEvolutionEditReceipt",
]
