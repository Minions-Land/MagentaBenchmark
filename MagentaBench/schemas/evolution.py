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
        return self


__all__ = [
    "EvolutionBudgetEvent",
    "EvolutionBudgetLedger",
    "EvolutionBudgetOperation",
    "EvolutionEvaluationRecord",
    "EvolutionEvaluationStage",
    "EvolutionRuntimeReceipt",
    "EvolutionSealedHoldoutReceipt",
]
