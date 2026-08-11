"""Pydantic contracts for the benchmark-side BMP 0.1 protocol.

Hand-written TOML declarations are parsed into ``*Spec`` models.  Compilation
normalizes and pins them into ``*Artifact`` and ``Resolved*`` models suitable
for canonical hashing and execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal, Mapping, Union
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    computed_field,
    field_validator,
    model_validator,
)

BMP_VERSION = "0.1"
ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
ADAPTER_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
OCI_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
SECRET_KEY_PATTERN = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL",
    re.IGNORECASE,
)
NON_SECRET_TOKEN_KEY_PATTERN = re.compile(
    r"^(?:(?:cache|completion|context|generation|input|max|max_context|"
    r"max_generation|output|prompt|request|response|retry|total)_)?tokens$|"
    r"^token_(?:budget|capacity|count|limit|quota|window)$",
    re.IGNORECASE,
)
IDENTITY_EXCLUDE: frozenset[str] = frozenset(
    {
        "created_at",
        "wall_clock_start",
        "wall_clock_end",
        "record_root",
        "resume_count",
        "runner_invocation_id",
    }
)


def _reject_secret_like_keys(value: Any, *, field_name: str) -> Any:
    """Reject secret-bearing keys recursively inside a generic metadata value."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key_text)
            normalized_key = re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized_key
            ).replace("-", "_")
            if SECRET_KEY_PATTERN.search(key_text) and not (
                "TOKEN" in key_text.upper()
                and NON_SECRET_TOKEN_KEY_PATTERN.fullmatch(normalized_key)
            ):
                raise ValueError(
                    f"{field_name} must not contain secret-like key {key!r}"
                )
            _reject_secret_like_keys(item, field_name=field_name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_like_keys(item, field_name=field_name)
    return value


class StrictModel(BaseModel):
    """Base class shared by all BMP wire contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> "StrictModel":
        """Preserve recursive immutability across Pydantic's unchecked copy API."""

        copied = super().model_copy(update=update, deep=deep)
        for field_name in ("values", "json_schema", "ownership", "config"):
            if field_name in type(copied).model_fields:
                object.__setattr__(
                    copied,
                    field_name,
                    _freeze_configuration_tree(getattr(copied, field_name)),
                )
        return copied


class RegistryEntry(StrictModel):
    """Fields required on every registry declaration."""

    id: str = Field(pattern=ID_PATTERN)
    kind: str = Field(min_length=1)
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION


class SourceRegistryEntry(RegistryEntry):
    """Registry declaration backed by a code or content source."""

    source: str = Field(min_length=1)
    commit: str | None = Field(default=None, min_length=1)


class _FrozenDict(dict[str, Any]):
    """JSON-serializable recursively immutable mapping used by config trees."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("configuration trees are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        import copy

        return {
            copy.deepcopy(key, memo): copy.deepcopy(value, memo)
            for key, value in self.items()
        }


class _FrozenList(list[Any]):
    """JSON-serializable recursively immutable list used by config trees."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("configuration trees are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    clear = _immutable
    sort = _immutable
    reverse = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        import copy

        return [copy.deepcopy(value, memo) for value in self]


def _freeze_configuration_tree(value: Any) -> Any:
    """Recursively freeze mappings/lists while retaining JSON behavior."""

    if isinstance(value, Mapping):
        return _FrozenDict(
            {
                key: _freeze_configuration_tree(child)
                for key, child in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze_configuration_tree(child) for child in value)
    return value


def _validate_json_configuration(value: Any, *, field_name: str) -> Any:
    """Validate an extensible configuration tree without admitting secrets."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    _reject_secret_like_keys(value, field_name=field_name)

    def normalize(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                key: normalize(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        if isinstance(item, time):
            if item.tzinfo is not None and item.utcoffset() is not None:
                raise ValueError(
                    f"{field_name} time values must not contain an offset"
                )
            return item.isoformat()
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return item

    normalized = normalize(value)

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(f"{field_name} keys must be non-empty strings")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(normalized)
    try:
        json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-compatible") from exc
    return normalized


class ConfigurationSpec(RegistryEntry):
    """Open, identity-bearing TOML configuration profile.

    The profile deliberately carries a generic JSON-compatible tree.  Adapter
    code owns the meaning of its paths; BMP owns merge order, secret rejection,
    source bytes, and the resulting digest.
    """

    kind: Literal["configuration"]
    extends: tuple[str, ...] = ()
    values: Mapping[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(populate_by_name=True)
    json_schema: Mapping[str, Any] = Field(default_factory=dict, alias="schema")

    @field_validator("extends")
    @classmethod
    def parent_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("configuration extends ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("configuration extends ids must be valid BMP ids")
        return values

    @field_validator("values", "json_schema", mode="before")
    @classmethod
    def values_are_json_compatible(cls, value: Any, info: Any) -> Any:
        return _validate_json_configuration(
            value, field_name=f"ConfigurationSpec.{info.field_name}"
        )

    @model_validator(mode="after")
    def freeze_configuration_trees(self) -> "ConfigurationSpec":
        object.__setattr__(self, "values", _freeze_configuration_tree(self.values))
        object.__setattr__(
            self, "json_schema", _freeze_configuration_tree(self.json_schema)
        )
        return self


class ConfigurationSelection(StrictModel):
    """Experiment-local composition of registry profiles and external TOML.

    ``files`` are explicit ``[configuration]`` envelopes.  ``raw_files`` is
    an opt-in escape hatch for adapter-owned raw TOML documents; keeping the
    two namespaces separate prevents a malformed envelope from silently being
    interpreted as generic configuration.
    """

    profiles: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    raw_files: tuple[str, ...] = ()
    values: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def profile_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("configuration profiles must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("configuration profile ids must be valid BMP ids")
        return values

    @field_validator("files")
    @classmethod
    def files_are_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            candidate = value.replace("\\", "/")
            parts = candidate.split("/")
            if (
                not candidate
                or candidate.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ValueError("configuration files must be normalized relative paths")
            normalized.append(candidate)
        if len(set(normalized)) != len(normalized):
            raise ValueError("configuration files must be unique")
        return tuple(normalized)

    @field_validator("raw_files")
    @classmethod
    def raw_files_are_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            candidate = value.replace("\\", "/")
            parts = candidate.split("/")
            if (
                not candidate
                or candidate.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ValueError("configuration raw_files must be normalized relative paths")
            normalized.append(candidate)
        if len(set(normalized)) != len(normalized):
            raise ValueError("configuration raw_files must be unique")
        return tuple(normalized)

    @field_validator("values", mode="before")
    @classmethod
    def inline_values_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="ConfigurationSelection.values"
        )

    @model_validator(mode="after")
    def freeze_configuration_tree(self) -> "ConfigurationSelection":
        object.__setattr__(self, "values", _freeze_configuration_tree(self.values))
        return self


class ArtifactRef(StrictModel):
    """Content-addressed reference to an artifact stored outside the manifest."""

    path: str = Field(min_length=1, pattern=r"^/")
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("artifact path must be absolute")
        return value

    def identity_data(self) -> dict[str, int | str]:
        return {"sha256": self.sha256, "size_bytes": self.size_bytes}


class EvolutionCandidateStatus(str, Enum):
    """Lifecycle state retained for every candidate an evolver produced."""

    generated = "generated"
    revised = "revised"
    accepted = "accepted"
    rejected = "rejected"
    invalid = "invalid"
    selected = "selected"


class EvolutionTransitionPhase(str, Enum):
    """Neutral phases for a candidate-search transition ledger."""

    seed = "seed"
    generate = "generate"
    feedback = "feedback"
    revise = "revise"
    select = "select"
    terminate = "terminate"


def _validate_evolution_refs(
    refs: tuple[ArtifactRef, ...], *, field_name: str
) -> tuple[ArtifactRef, ...]:
    """Require each reference list to be content-unique and retain its order."""

    identities = [(ref.sha256, ref.size_bytes) for ref in refs]
    if len(set(identities)) != len(identities):
        raise ValueError(f"{field_name} must not contain duplicate content refs")
    return refs


class EvolutionCandidateRecord(StrictModel):
    """One immutable candidate record in an evolution ledger.

    BMP intentionally stores artifacts and feedback as opaque content-addressed
    refs. Adapter-owned metadata may describe arbitrary search mechanisms but
    cannot contain credentials or other secret-like keys.
    """

    candidate_id: str = Field(pattern=ID_PATTERN)
    generation: int = Field(ge=0, strict=True)
    parent_ids: tuple[str, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    feedback_refs: tuple[ArtifactRef, ...] = ()
    status: EvolutionCandidateStatus
    score: float | None = None
    score_metric: str | None = Field(default=None, min_length=1)
    evaluator_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    search_state_refs: tuple[ArtifactRef, ...] = ()
    attributes: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("parent_ids")
    @classmethod
    def parent_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("candidate parent_ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("candidate parent_ids must be valid BMP ids")
        return values

    @field_validator("artifact_refs")
    @classmethod
    def artifact_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _validate_evolution_refs(values, field_name="candidate artifact_refs")

    @field_validator("feedback_refs")
    @classmethod
    def feedback_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _validate_evolution_refs(values, field_name="candidate feedback_refs")

    @field_validator("search_state_refs")
    @classmethod
    def search_state_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _validate_evolution_refs(
            values, field_name="candidate search_state_refs"
        )

    @field_validator("attributes", mode="before")
    @classmethod
    def attributes_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="EvolutionCandidateRecord.attributes"
        )

    @model_validator(mode="after")
    def score_has_metric(self) -> "EvolutionCandidateRecord":
        if (self.score is None) != (self.score_metric is None):
            raise ValueError("candidate score and score_metric must be supplied together")
        object.__setattr__(self, "attributes", _freeze_configuration_tree(self.attributes))
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "generation": self.generation,
            "parent_ids": list(self.parent_ids),
            "artifact_refs": [ref.identity_data() for ref in self.artifact_refs],
            "feedback_refs": [ref.identity_data() for ref in self.feedback_refs],
            "status": self.status.value,
            "score": self.score,
            "score_metric": self.score_metric,
            "evaluator_digest": self.evaluator_digest,
            "search_state_refs": [
                ref.identity_data() for ref in self.search_state_refs
            ],
            "attributes": self.attributes,
        }


class EvolutionTransitionRecord(StrictModel):
    """One ordered search transition connecting candidate records."""

    transition_id: str = Field(pattern=ID_PATTERN)
    sequence: int = Field(ge=0, strict=True)
    phase: EvolutionTransitionPhase
    input_candidate_ids: tuple[str, ...] = ()
    output_candidate_ids: tuple[str, ...] = ()
    search_state_refs: tuple[ArtifactRef, ...] = ()
    feedback_refs: tuple[ArtifactRef, ...] = ()
    attributes: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("input_candidate_ids", "output_candidate_ids")
    @classmethod
    def candidate_ids_are_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("transition candidate ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("transition candidate ids must be valid BMP ids")
        return values

    @field_validator("search_state_refs")
    @classmethod
    def search_state_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _validate_evolution_refs(
            values, field_name="transition search_state_refs"
        )

    @field_validator("feedback_refs")
    @classmethod
    def feedback_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _validate_evolution_refs(
            values, field_name="transition feedback_refs"
        )

    @field_validator("attributes", mode="before")
    @classmethod
    def attributes_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="EvolutionTransitionRecord.attributes"
        )

    @model_validator(mode="after")
    def phase_shape_is_coherent(self) -> "EvolutionTransitionRecord":
        if self.phase == EvolutionTransitionPhase.seed and self.input_candidate_ids:
            raise ValueError("seed transitions cannot consume candidate ids")
        if self.phase == EvolutionTransitionPhase.terminate and self.output_candidate_ids:
            raise ValueError("terminate transitions cannot produce candidate ids")
        if self.phase != EvolutionTransitionPhase.terminate and not (
            self.input_candidate_ids or self.output_candidate_ids
        ):
            raise ValueError(
                "non-terminate transitions must reference an input or output candidate"
            )
        object.__setattr__(self, "attributes", _freeze_configuration_tree(self.attributes))
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "sequence": self.sequence,
            "phase": self.phase.value,
            "input_candidate_ids": list(self.input_candidate_ids),
            "output_candidate_ids": list(self.output_candidate_ids),
            "search_state_refs": [
                ref.identity_data() for ref in self.search_state_refs
            ],
            "feedback_refs": [ref.identity_data() for ref in self.feedback_refs],
            "attributes": self.attributes,
        }


class EvolutionRunEvidence(StrictModel):
    """Complete, algorithm-neutral evidence for an evolver or meta-evolver.

    A meta-evolver binds its parent evolver evidence by an immutable artifact
    reference. BMP does not inspect the referenced implementation; a caller
    can recursively verify it with the same contract. Every generated,
    rejected, and invalid candidate remains in ``candidate_ledger`` so search
    outcomes cannot be made to look better by filtering failures.
    """

    format: Literal["bmp-evolution-evidence-v1"] = "bmp-evolution-evidence-v1"
    run_id: str = Field(pattern=ID_PATTERN)
    kind: Literal["evolver", "meta_evolver"] = Field(
        validation_alias=AliasChoices("kind", "subject_kind")
    )
    adapter_digest: str = Field(pattern=SHA256_PATTERN)
    evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    budget_digest: str = Field(pattern=SHA256_PATTERN)
    adapter_ref: ArtifactRef | None = None
    evaluator_ref: ArtifactRef | None = None
    budget_ref: ArtifactRef | None = None
    authoritative_metric: str | None = Field(default=None, min_length=1)
    candidate_ledger: tuple[EvolutionCandidateRecord, ...] = Field(
        min_length=1,
        validation_alias=AliasChoices("candidate_ledger", "candidates"),
    )
    transition_ledger: tuple[EvolutionTransitionRecord, ...] = Field(
        min_length=1,
        validation_alias=AliasChoices("transition_ledger", "transitions"),
    )
    selected_candidate_id: str | None = Field(default=None, pattern=ID_PATTERN)
    termination_reason: str = Field(min_length=1)
    search_state_refs: tuple[ArtifactRef, ...] = ()
    parent_evidence_ref: ArtifactRef | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "parent_evidence_ref", "nested_parent_evidence_ref"
        ),
    )
    runtime_receipt_ref: ArtifactRef | None = None
    candidate_ledger_complete: bool = True
    transition_ledger_complete: bool = True
    attributes: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("search_state_refs")
    @classmethod
    def search_state_refs_are_unique(
        cls, values: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _validate_evolution_refs(values, field_name="run search_state_refs")

    @field_validator("attributes", mode="before")
    @classmethod
    def attributes_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="EvolutionRunEvidence.attributes"
        )

    @model_validator(mode="after")
    def ledgers_are_complete_and_bound(self) -> "EvolutionRunEvidence":
        candidates = {item.candidate_id: item for item in self.candidate_ledger}
        if len(candidates) != len(self.candidate_ledger):
            raise ValueError("candidate ledger ids must be unique")
        transitions = {
            item.transition_id: item for item in self.transition_ledger
        }
        if len(transitions) != len(self.transition_ledger):
            raise ValueError("transition ledger ids must be unique")
        sequences = [item.sequence for item in self.transition_ledger]
        if len(set(sequences)) != len(sequences) or sequences != sorted(sequences):
            raise ValueError("transition ledger sequences must be unique and ordered")
        if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
            raise ValueError("transition ledger sequences must be contiguous")
        if not any(
            item.phase == EvolutionTransitionPhase.terminate
            for item in self.transition_ledger
        ):
            raise ValueError("transition ledger requires a terminate phase")
        termination_indices = [
            index
            for index, item in enumerate(self.transition_ledger)
            if item.phase == EvolutionTransitionPhase.terminate
        ]
        if termination_indices != [len(self.transition_ledger) - 1]:
            raise ValueError("terminate phase must be the final transition and occur once")

        for digest_name, digest, ref in (
            ("adapter", self.adapter_digest, self.adapter_ref),
            ("evaluator", self.evaluator_digest, self.evaluator_ref),
            ("budget", self.budget_digest, self.budget_ref),
        ):
            if ref is not None and ref.sha256 != digest:
                raise ValueError(f"{digest_name} ref digest does not match {digest_name}_digest")

        for candidate in self.candidate_ledger:
            if candidate.candidate_id in candidate.parent_ids:
                raise ValueError("candidate cannot name itself as a parent")
            for parent_id in candidate.parent_ids:
                parent = candidates.get(parent_id)
                if parent is None:
                    raise ValueError(
                        f"candidate {candidate.candidate_id!r} references unknown parent "
                        f"{parent_id!r}"
                    )
                if parent.generation >= candidate.generation:
                    raise ValueError(
                        "candidate parent generation must precede child generation"
                    )
            if candidate.evaluator_digest is not None and (
                candidate.evaluator_digest != self.evaluator_digest
            ):
                raise ValueError(
                    f"candidate {candidate.candidate_id!r} evaluator digest differs "
                    "from the run evaluator digest"
                )

        for transition in self.transition_ledger:
            referenced = (
                *transition.input_candidate_ids,
                *transition.output_candidate_ids,
            )
            unknown = sorted(set(referenced) - set(candidates))
            if unknown:
                raise ValueError(
                    f"transition {transition.transition_id!r} references unknown "
                    f"candidates: {unknown}"
                )
            if transition.phase == EvolutionTransitionPhase.seed and any(
                candidates[candidate_id].generation != 0
                for candidate_id in transition.output_candidate_ids
            ):
                raise ValueError(
                    "seed transitions must output generation-zero candidates"
                )

        selected = [
            item for item in self.candidate_ledger
            if item.status == EvolutionCandidateStatus.selected
        ]
        if self.selected_candidate_id is None:
            if selected:
                raise ValueError(
                    "candidate ledger contains selected status without selected_candidate_id"
                )
        else:
            selected_candidate = candidates.get(self.selected_candidate_id)
            if selected_candidate is None:
                raise ValueError("selected_candidate_id is absent from candidate ledger")
            if selected_candidate.status != EvolutionCandidateStatus.selected:
                raise ValueError("selected candidate must have status='selected'")
            if selected_candidate.score is None:
                raise ValueError("selected candidate must retain an evaluator score")
            if selected_candidate.evaluator_digest != self.evaluator_digest:
                raise ValueError(
                    "selected candidate evaluator digest must match the run evaluator digest"
                )
            if len(selected) != 1:
                raise ValueError("candidate ledger must contain exactly one selected candidate")
            if not any(
                transition.phase == EvolutionTransitionPhase.select
                and self.selected_candidate_id in transition.output_candidate_ids
                for transition in self.transition_ledger
            ):
                raise ValueError("selected candidate must be emitted by a select transition")

        phases = [item.phase for item in self.transition_ledger]
        if EvolutionTransitionPhase.seed not in phases:
            raise ValueError("evolution transition ledger requires a seed phase")
        terminate_positions = [
            index
            for index, phase in enumerate(phases)
            if phase == EvolutionTransitionPhase.terminate
        ]
        if len(terminate_positions) != 1 or terminate_positions[0] != len(phases) - 1:
            raise ValueError(
                "evolution transition ledger requires one final terminate phase"
            )

        if self.kind == "meta_evolver" and self.parent_evidence_ref is None:
            raise ValueError("meta_evolver evidence requires parent_evidence_ref")
        if self.kind == "evolver" and self.parent_evidence_ref is not None:
            raise ValueError("evolver evidence cannot carry parent_evidence_ref")
        object.__setattr__(self, "attributes", _freeze_configuration_tree(self.attributes))
        return self

    @property
    def evidence_complete(self) -> bool:
        """Whether this record is eligible for a positive evolution claim gate."""

        return self.candidate_ledger_complete and self.transition_ledger_complete

    @property
    def claim_ready(self) -> bool:
        """Whether the record has the bindings needed for a positive claim.

        Structural validity alone cannot prove that adapter/evaluator/budget
        implementations were captured or that the benchmark's authoritative
        metric was used.  Those bindings are optional for exploratory records
        but required by a claim gate.
        """

        if not self.evidence_complete:
            return False
        if (
            self.adapter_ref is None
            or self.evaluator_ref is None
            or self.budget_ref is None
            or self.runtime_receipt_ref is None
            or self.authoritative_metric is None
            or self.selected_candidate_id is None
        ):
            return False
        selected = next(
            item
            for item in self.candidate_ledger
            if item.candidate_id == self.selected_candidate_id
        )
        return (
            bool(selected.artifact_refs)
            and selected.evaluator_digest == self.evaluator_digest
            and selected.score_metric == self.authoritative_metric
        )

    def identity_data(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "run_id": self.run_id,
            "kind": self.kind,
            "adapter_digest": self.adapter_digest,
            "evaluator_digest": self.evaluator_digest,
            "budget_digest": self.budget_digest,
            "adapter_ref": (
                None if self.adapter_ref is None else self.adapter_ref.identity_data()
            ),
            "evaluator_ref": (
                None if self.evaluator_ref is None else self.evaluator_ref.identity_data()
            ),
            "budget_ref": (
                None if self.budget_ref is None else self.budget_ref.identity_data()
            ),
            "authoritative_metric": self.authoritative_metric,
            "candidate_ledger": [item.identity_data() for item in self.candidate_ledger],
            "transition_ledger": [
                item.identity_data() for item in self.transition_ledger
            ],
            "selected_candidate_id": self.selected_candidate_id,
            "termination_reason": self.termination_reason,
            "search_state_refs": [
                ref.identity_data() for ref in self.search_state_refs
            ],
            "parent_evidence_ref": (
                None
                if self.parent_evidence_ref is None
                else self.parent_evidence_ref.identity_data()
            ),
            "runtime_receipt_ref": (
                None
                if self.runtime_receipt_ref is None
                else self.runtime_receipt_ref.identity_data()
            ),
            "candidate_ledger_complete": self.candidate_ledger_complete,
            "transition_ledger_complete": self.transition_ledger_complete,
            "attributes": self.attributes,
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConfigurationCompositionStep(StrictModel):
    """One deterministic layer used to produce a resolved configuration.

    Source refs alone prove that the input bytes still exist, but do not prove
    how profiles, external files, and inline overlays were ordered.  The
    compiler records this small, JSON-only recipe so a standalone verifier can
    replay the composition without importing adapter code.
    """

    kind: Literal["profile", "file", "inline"]
    id: str | None = None
    source_ref: ArtifactRef | None = None
    mode: Literal["envelope", "raw"] | None = None
    root: bool = True
    values: Mapping[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(populate_by_name=True)
    json_schema: Mapping[str, Any] = Field(default_factory=dict, alias="schema")
    adapter: str = Field(default="generic", pattern=ADAPTER_PATTERN)
    extends: tuple[str, ...] = ()

    @field_validator("values", "json_schema", mode="before")
    @classmethod
    def composition_trees_are_json_compatible(cls, value: Any, info: Any) -> Any:
        return _validate_json_configuration(
            value,
            field_name=f"ConfigurationCompositionStep.{info.field_name}",
        )

    @field_validator("id")
    @classmethod
    def composition_id_is_normalized(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(ID_PATTERN, value) is None:
            raise ValueError("configuration composition ids must be valid BMP ids")
        return value

    @model_validator(mode="after")
    def composition_kind_contract(self) -> "ConfigurationCompositionStep":
        if self.kind == "inline":
            if self.source_ref is not None or self.mode is not None:
                raise ValueError("inline composition layers cannot carry source refs")
        elif self.source_ref is None or self.mode is None:
            raise ValueError("source composition layers require source_ref and mode")
        if self.kind == "profile" and self.id is None:
            raise ValueError("profile composition layers require an id")
        object.__setattr__(self, "values", _freeze_configuration_tree(self.values))
        object.__setattr__(
            self, "json_schema", _freeze_configuration_tree(self.json_schema)
        )
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "source_ref": (
                None if self.source_ref is None else self.source_ref.identity_data()
            ),
            "mode": self.mode,
            "root": self.root,
            "values": self.values,
            "schema": self.json_schema,
            "adapter": self.adapter,
            "extends": list(self.extends),
        }


class ConfigurationArtifact(StrictModel):
    """Resolved configuration tree bound to every profile/source byte.

    ``json_schema`` is the merged, replayable schema tree and ``ownership``
    records which adapter contributed each resolved dotted path. ``adapter``
    remains a compatibility summary (``composite`` when several namespaces
    participate).
    """

    id: str = Field(pattern=ID_PATTERN)
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    profiles: tuple[str, ...] = ()
    source_refs: tuple[ArtifactRef, ...] = ()
    schema_digest: str = Field(pattern=SHA256_PATTERN)
    values: Mapping[str, Any] = Field(default_factory=dict)
    json_schema: Mapping[str, Any] = Field(default_factory=dict)
    ownership: Mapping[str, str] = Field(default_factory=dict)
    composition: tuple[ConfigurationCompositionStep, ...] = ()
    artifact_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("values", mode="before")
    @classmethod
    def artifact_values_are_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="ConfigurationArtifact.values"
        )

    @field_validator("json_schema", mode="before")
    @classmethod
    def artifact_schema_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="ConfigurationArtifact.json_schema"
        )

    @field_validator("ownership", mode="before")
    @classmethod
    def ownership_is_valid(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            raise ValueError("ConfigurationArtifact.ownership must be a JSON object")
        for path, adapter in value.items():
            if not isinstance(path, str) or not path:
                raise ValueError(
                    "ConfigurationArtifact.ownership paths must be non-empty strings"
                )
            if not isinstance(adapter, str) or re.fullmatch(ADAPTER_PATTERN, adapter) is None:
                raise ValueError(
                    "ConfigurationArtifact.ownership adapters must be normalized identifiers"
                )
        return dict(value)

    @model_validator(mode="after")
    def freeze_configuration_trees(self) -> "ConfigurationArtifact":
        object.__setattr__(self, "values", _freeze_configuration_tree(self.values))
        object.__setattr__(
            self, "json_schema", _freeze_configuration_tree(self.json_schema)
        )
        object.__setattr__(
            self, "ownership", _freeze_configuration_tree(self.ownership)
        )
        return self

    @model_validator(mode="after")
    def source_refs_are_unique(self) -> "ConfigurationArtifact":
        identities = [(ref.sha256, ref.size_bytes) for ref in self.source_refs]
        if len(set(identities)) != len(identities):
            raise ValueError("configuration source refs must be content-unique")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "adapter": self.adapter,
            "profiles": list(self.profiles),
            "source_refs": [ref.identity_data() for ref in self.source_refs],
            "schema_digest": self.schema_digest,
            "values": self.values,
            "json_schema": self.json_schema,
            "ownership": self.ownership,
            "composition": [item.identity_data() for item in self.composition],
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AdapterCapability(RegistryEntry):
    """Declarative capability contract for a pluggable BMP adapter.

    The entrypoint is metadata only until a host explicitly registers the
    implementation object.  This keeps a TOML declaration from silently
    selecting executable code while still making supported kinds and config
    paths inspectable and hashable.
    """

    kind: Literal["adapter"]
    adapter_kind: Literal[
        "benchmark_loader", "backend_factory", "execution", "metric_source"
    ]
    entrypoint: str = Field(min_length=1)
    source: str = "."
    digest: str = Field(pattern=SHA256_PATTERN)
    config_paths: tuple[str, ...] = ()
    supported_benchmark_kinds: tuple[str, ...] = ()
    supported_subject_kinds: tuple[str, ...] = ()
    supported_backend_kinds: tuple[str, ...] = ()
    supported_backend_adapters: tuple[str, ...] = ()
    supported_subject_adapters: tuple[str, ...] = ()
    supported_subject_interfaces: tuple[str, ...] = ()
    # ``None`` means that a backend factory did not declare which keys it
    # reads.  An explicit empty tuple is a closed, valid read-set.
    backend_default_read_set: tuple[str, ...] | None = None
    # These are deliberately named *none* model sentinels.  A capability may
    # bind only the non-provider execution modes closed by ExecutionSpec;
    # activating a real model still requires ModelActivationReceipt.
    none_model_sentinels: tuple[
        Literal["none", "none/deterministic", "none/echo"], ...
    ] = ()
    # A real model may enter execution only when the selected execution
    # adapter declares where its runtime activation evidence comes from.
    # This is deliberately an enum rather than an adapter-name allowlist:
    # providers/harnesses remain pluggable while the evidence semantics stay
    # closed and independently replayable.
    model_activation_source: Literal[
        "provider_response",
        "runtime_manifest",
        "native_result",
        "adapter_receipt",
    ] | None = None
    supported_state_reset_policies: tuple[
        Literal["per_case", "per_rollout", "never"], ...
    ] = ()
    supported_metric_sources: tuple[
        Literal["regime", "evolution", "external"], ...
    ] = ()
    supported_metric_formulas: tuple[str, ...] = ()
    metric_config_schema: Mapping[str, Any] = Field(default_factory=dict)
    metric_config_schema_digest: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )

    @field_validator("source")
    @classmethod
    def source_is_relative(cls, value: str) -> str:
        if value == ".":
            return value
        return _validate_logical_relative_path(value, field_name="source")

    @field_validator("entrypoint")
    @classmethod
    def entrypoint_has_object_name(cls, value: str) -> str:
        module_path, separator, object_name = value.partition(":")
        if (
            separator != ":"
            or not module_path
            or not object_name
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", object_name)
        ):
            raise ValueError("adapter entrypoint must be module:object")
        if module_path.endswith(".py"):
            _validate_logical_relative_path(module_path, field_name="entrypoint module")
        elif not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module_path
        ):
            raise ValueError(
                "adapter entrypoint module must be a relative .py path or dotted module"
            )
        return value

    @field_validator(
        "config_paths",
        "supported_benchmark_kinds",
        "supported_subject_kinds",
        "supported_backend_kinds",
        "supported_backend_adapters",
        "supported_subject_adapters",
        "supported_subject_interfaces",
        "none_model_sentinels",
        "supported_state_reset_policies",
        "supported_metric_sources",
        "supported_metric_formulas",
    )
    @classmethod
    def capability_values_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("adapter capability values must be unique")
        if any(not value.strip() for value in values):
            raise ValueError("adapter capability values must be non-empty")
        return values

    @field_validator("backend_default_read_set")
    @classmethod
    def backend_default_keys_are_unique(
        cls, values: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if values is None:
            return None
        if len(set(values)) != len(values):
            raise ValueError("adapter backend default read-set values must be unique")
        if any(not value.strip() for value in values):
            raise ValueError(
                "adapter backend default read-set values must be non-empty"
            )
        return values

    @field_validator("metric_config_schema", mode="before")
    @classmethod
    def metric_schema_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(
            value, field_name="AdapterCapability.metric_config_schema"
        )

    @model_validator(mode="after")
    def policy_fields_match_capability_kind(self) -> "AdapterCapability":
        if self.adapter_kind != "backend_factory" and (
            self.backend_default_read_set is not None
        ):
            raise ValueError(
                "backend_default_read_set is allowed only for backend_factory"
            )
        if self.adapter_kind != "execution" and (
            self.none_model_sentinels
            or self.model_activation_source is not None
            or self.supported_state_reset_policies
        ):
            raise ValueError(
                "model activation policies and state-reset policies are allowed "
                "only for execution"
            )
        metric_fields_present = bool(
            self.supported_metric_sources
            or self.supported_metric_formulas
            or self.metric_config_schema
            or self.metric_config_schema_digest is not None
        )
        if self.adapter_kind != "metric_source" and metric_fields_present:
            raise ValueError(
                "metric source policies are allowed only for metric_source adapters"
            )
        if self.adapter_kind == "metric_source":
            if not self.supported_metric_sources or not self.supported_metric_formulas:
                raise ValueError(
                    "metric_source adapters require supported sources and formulas"
                )
            observed_schema_digest = hashlib.sha256(
                json.dumps(
                    self.metric_config_schema,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if self.metric_config_schema_digest != observed_schema_digest:
                raise ValueError("metric adapter config schema digest drift")
        object.__setattr__(
            self,
            "metric_config_schema",
            _freeze_configuration_tree(self.metric_config_schema),
        )
        return self

    def supports(
        self,
        *,
        benchmark_kind: str,
        subject_kind: str,
        subject_adapter: str,
        backend_kind: str,
        backend_adapter: str,
        subject_interface: str | None,
    ) -> bool:
        """Check the declaration's compatibility selectors without fallback."""

        return (
            (not self.supported_benchmark_kinds
             or benchmark_kind in self.supported_benchmark_kinds)
            and (not self.supported_subject_kinds
                 or subject_kind in self.supported_subject_kinds)
            and (not self.supported_subject_adapters
                 or subject_adapter in self.supported_subject_adapters)
            and (not self.supported_backend_kinds
                 or backend_kind in self.supported_backend_kinds)
            and (not self.supported_backend_adapters
                 or backend_adapter in self.supported_backend_adapters)
            and (not self.supported_subject_interfaces
                 or subject_interface in self.supported_subject_interfaces)
        )

    def owns_configuration(self, values: Mapping[str, Any]) -> bool:
        """Return whether a declared path is present in the resolved tree.

        ``config_paths`` describes dotted paths in configuration values, not a
        configuration adapter id. Empty declarations and ``*`` retain the
        historical unrestricted behavior. A table path owns its whole subtree,
        so every mapping node is included in the set of resolved paths.
        """

        if not self.config_paths or "*" in self.config_paths:
            return True

        resolved_paths: set[str] = set()

        def visit(value: Mapping[str, Any], prefix: str = "") -> None:
            for key, child in value.items():
                if not isinstance(key, str) or not key:
                    continue
                path = f"{prefix}.{key}" if prefix else key
                resolved_paths.add(path)
                if isinstance(child, Mapping):
                    visit(child, path)

        visit(values)
        return any(path in resolved_paths for path in self.config_paths)


class AdapterCapabilityArtifact(StrictModel):
    """Resolved plugin declaration and implementation byte identity.

    ``source_closure_*`` records the local Python helpers imported by the
    entrypoint.  Keeping relative names separate from absolute artifact refs
    makes the digest stable across record relocation while still allowing the
    verifier to rehash every persisted byte.
    """

    capability: AdapterCapability
    declaration_ref: ArtifactRef
    implementation_ref: ArtifactRef
    source_closure_refs: tuple[ArtifactRef, ...] = ()
    source_closure_paths: tuple[str, ...] = ()
    source_closure_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("source_closure_paths")
    @classmethod
    def closure_paths_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            candidate = value.replace("\\", "/")
            parts = candidate.split("/")
            if (
                not candidate
                or candidate.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ValueError(
                    "adapter source closure paths must be normalized relative paths"
                )
            normalized.append(candidate)
        if len(set(normalized)) != len(normalized):
            raise ValueError("adapter source closure paths must be unique")
        return tuple(normalized)

    @model_validator(mode="after")
    def implementation_matches_declared_digest(self) -> "AdapterCapabilityArtifact":
        if self.implementation_ref.sha256 != self.capability.digest:
            raise ValueError(
                "adapter implementation ref must match the declared capability digest"
            )
        if bool(self.source_closure_refs) != bool(self.source_closure_paths):
            raise ValueError(
                "adapter source closure refs and paths must be provided together"
            )
        if self.source_closure_refs:
            if self.source_closure_digest is None:
                raise ValueError("adapter source closure digest is required")
            identities = [
                (ref.sha256, ref.size_bytes, path)
                for ref, path in zip(
                    self.source_closure_refs,
                    self.source_closure_paths,
                    strict=True,
                )
            ]
            if len({(sha, size) for sha, size, _ in identities}) != len(identities):
                raise ValueError("adapter source closure refs must be content-unique")
            if not any(
                ref.sha256 == self.implementation_ref.sha256
                and ref.size_bytes == self.implementation_ref.size_bytes
                for ref in self.source_closure_refs
            ):
                raise ValueError(
                    "adapter source closure must include the implementation ref"
                )
        elif self.source_closure_digest is not None:
            raise ValueError(
                "adapter source closure digest requires closure refs and paths"
            )
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "capability": self.capability.model_dump(mode="json"),
            "declaration_ref": self.declaration_ref.identity_data(),
            "implementation_ref": self.implementation_ref.identity_data(),
            "source_closure_refs": [
                ref.identity_data() for ref in self.source_closure_refs
            ],
            "source_closure_paths": list(self.source_closure_paths),
            "source_closure_digest": self.source_closure_digest,
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EnvironmentBindingRef(StrictModel):
    """Environment value identity without serializing the value itself."""

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_digest: str = Field(pattern=SHA256_PATTERN)
    secret: bool
    source_name: str = Field(pattern=ID_PATTERN)


class ResourceSpec(StrictModel):
    """Task resources, separate from Python environment requirements.

    REQUIRED-IN-STEP-2: claim runs must reject a missing docker_image_digest.
    """

    build_timeout_sec: float = Field(gt=0, strict=True)
    docker_image: str = Field(min_length=1)
    docker_image_digest: str | None = Field(default=None, pattern=OCI_SHA256_PATTERN)
    cpus: int = Field(gt=0, strict=True)
    memory_mb: int = Field(gt=0, strict=True)
    storage_mb: int = Field(gt=0, strict=True)
    gpus: int = Field(ge=0, strict=True)
    allow_internet: bool
    mcp_servers: tuple[str, ...] = ()
    env: tuple[EnvironmentBindingRef, ...] = ()
    agent_timeout_sec: float = Field(gt=0, strict=True)
    verifier_timeout_sec: float = Field(gt=0, strict=True)

    @property
    def claim_image_identity_valid(self) -> bool:
        """Whether this image is immutable enough for a claim run."""

        return self.docker_image_digest is not None

    @model_validator(mode="after")
    def names_are_unique(self) -> "ResourceSpec":
        if any(not server.strip() for server in self.mcp_servers):
            raise ValueError("mcp_servers must contain non-empty names")
        if len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError("mcp_servers must be unique")
        env_names = [binding.name for binding in self.env]
        if len(set(env_names)) != len(env_names):
            raise ValueError("environment binding names must be unique")
        return self


class CredentialRef(StrictModel):
    """Identity-bearing credential digest; secret values are never serialized."""

    name: str = Field(pattern=ID_PATTERN)
    value_sha256: str = Field(pattern=SHA256_PATTERN)
    secret: Literal[True]
    source_file: str = Field(min_length=1)

    def identity_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value_sha256": self.value_sha256,
            "secret": self.secret,
        }


class ProviderBinding(StrictModel):
    """Resolved provider, transport, model, and credential identity."""

    provider_id: str = Field(pattern=ID_PATTERN)
    base_url: str = Field(min_length=1)
    wire_api: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    credential_ref: CredentialRef

    @field_validator("base_url")
    @classmethod
    def base_url_is_secret_free_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        return value

    def identity_data(self) -> dict[str, Any]:
        """Return the relocatable, secret-free provider identity projection."""

        return {
            "provider_id": self.provider_id,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "model_id": self.model_id,
            "credential_ref": self.credential_ref.identity_data(),
        }

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ModelActivationUsage(StrictModel):
    """Provider-reported usage carried by the same native activation evidence.

    Wall time is intentionally excluded: BMP measures it at the scheduler
    boundary, while token and monetary usage must be replayed from the provider
    or harness evidence that identified the active model.
    """

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def token_total_is_consistent(self) -> "ModelActivationUsage":
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class ModelActivationEvidence(StrictModel):
    """Closed JSON projection replayed to derive a model activation receipt.

    ``runtime_manifest`` evidence is the native JSONL trace itself and is
    replayed separately. Other activation sources use this envelope so an
    arbitrary file or caller-supplied scalar cannot substantiate activation.
    """

    format: Literal["bmp-model-activation-evidence-v1"] = (
        "bmp-model-activation-evidence-v1"
    )
    activation_source: Literal[
        "provider_response",
        "native_result",
        "adapter_receipt",
    ]
    provider_id: str = Field(pattern=ID_PATTERN)
    base_url: str = Field(min_length=1)
    wire_api: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    credential_name: str = Field(pattern=ID_PATTERN)
    credential_value_sha256: str = Field(pattern=SHA256_PATTERN)
    usage: ModelActivationUsage | None = None

    @field_validator("base_url")
    @classmethod
    def base_url_is_secret_free_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        return value

    def binding_identity_data(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "model_id": self.model_id,
            "credential_ref": {
                "name": self.credential_name,
                "value_sha256": self.credential_value_sha256,
                "secret": True,
            },
        }

    def binding_digest(self) -> str:
        encoded = json.dumps(
            self.binding_identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ModelActivationReceipt(StrictModel):
    """Runtime proof that the requested provider/model reached execution.

    A positive receipt binds a resolved :class:`ProviderBinding` without
    serializing a credential value. ``binding_digest`` is computed from the
    binding's identity projection (which intentionally omits
    ``CredentialRef.source_file``), while ``evidence_refs`` retain the
    adapter-native result/trace bytes used to observe activation. An exploratory
    run without a binding or observation is retained as ``unobserved``. A real
    model is never inferred from a command line or manifest declaration alone.
    """

    protocol_version: Literal[1] = 1
    requested_model: str = Field(min_length=1)
    requested_provider_id: str | None = Field(default=None, pattern=ID_PATTERN)
    requested_model_id: str = Field(min_length=1)
    activated_provider_id: str | None = Field(default=None, pattern=ID_PATTERN)
    activated_model_id: str | None = Field(default=None, min_length=1)
    binding_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    activated_binding_digest: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    activation_source: Literal[
        "provider_response",
        "runtime_manifest",
        "native_result",
        "adapter_receipt",
    ]
    status: Literal["matched", "mismatch", "unobserved"]
    reason: tuple[str, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()

    @field_validator("reason")
    @classmethod
    def model_activation_reasons_are_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("model activation reasons must be non-empty")
        if len(set(values)) != len(values):
            raise ValueError("model activation reasons must be unique")
        return values

    @model_validator(mode="after")
    def activation_is_coherent(self) -> "ModelActivationReceipt":
        if self.requested_model != self.requested_model_id:
            raise ValueError(
                "model activation requested_model must equal requested_model_id"
            )
        activated = (self.activated_provider_id, self.activated_model_id)
        requested = (self.requested_provider_id, self.requested_model_id)
        if self.status == "matched":
            if self.requested_provider_id is None or activated != requested:
                raise ValueError(
                    "matched model activation requires the requested provider/model"
                )
            if self.binding_digest is None:
                raise ValueError("matched model activation requires a binding digest")
            if self.activated_binding_digest != self.binding_digest:
                raise ValueError(
                    "matched model activation requires the observed binding digest"
                )
            if self.reason:
                raise ValueError("matched model activation cannot have reasons")
            if not self.evidence_refs:
                raise ValueError("matched model activation requires evidence refs")
        elif self.status == "mismatch":
            if (
                self.requested_provider_id is None
                or self.binding_digest is None
                or any(value is None for value in activated)
                or self.activated_binding_digest is None
                or (
                    activated == requested
                    and self.activated_binding_digest == self.binding_digest
                )
            ):
                raise ValueError(
                    "mismatched model activation requires a different observed provider/model"
                )
            if not self.reason:
                raise ValueError("mismatched model activation requires a reason")
            if not self.evidence_refs:
                raise ValueError("mismatched model activation requires evidence refs")
        else:
            if any(value is not None for value in activated):
                raise ValueError("unobserved model activation cannot claim an active model")
            if self.activated_binding_digest is not None:
                raise ValueError(
                    "unobserved model activation cannot claim an active binding"
                )
            if not self.reason:
                raise ValueError("unobserved model activation requires a reason")
        ref_keys = [
            (ref.path, ref.sha256, ref.size_bytes) for ref in self.evidence_refs
        ]
        if len(set(ref_keys)) != len(ref_keys):
            raise ValueError("model activation evidence refs must be unique")
        if self.status in {"matched", "mismatch"} and len(self.evidence_refs) != 1:
            raise ValueError(
                "observed model activation requires exactly one replayable evidence ref"
            )
        return self


class NetworkObservationMode(str, Enum):
    active_probe = "active_probe"
    connection_log = "connection_log"
    unobservable = "unobservable"


class NetworkPolicySource(str, Enum):
    backend_artifact = "backend_artifact"
    case_set_artifact = "case_set_artifact"


class NetworkBoundary(str, Enum):
    process = "process"
    task_container = "task_container"


class ResolvedNetworkPolicy(StrictModel):
    """Concrete adapter-resolved network policy bound to one executed case."""

    resolver_adapter: str = Field(pattern=ADAPTER_PATTERN)
    execution_adapter: str = Field(pattern=ADAPTER_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    boundary: NetworkBoundary
    allow_internet: bool
    required_observation: NetworkObservationMode
    source: NetworkPolicySource
    source_artifact_digest: str = Field(pattern=SHA256_PATTERN)


class NetworkEndpointRecord(StrictModel):
    protocol: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9+.-]*$")
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535, strict=True)
    outcome: str = Field(min_length=1)

    @field_validator("host")
    @classmethod
    def host_contains_no_url_or_credentials(cls, value: str) -> str:
        if (
            any(character.isspace() for character in value)
            or any(marker in value for marker in ("://", "/", "?", "#", "@", "="))
        ):
            raise ValueError("network endpoint host must not contain URL data or credentials")
        return value

    @field_validator("outcome")
    @classmethod
    def outcome_contains_no_url_or_credentials(cls, value: str) -> str:
        if any(marker in value for marker in ("://", "?", "@", "=")):
            raise ValueError("network endpoint outcome must not contain URL or credential data")
        return value


class NetworkObservation(StrictModel):
    """Observed network behavior bound to a resolved, content-addressed policy.

    ``declared_allow_internet`` is a redundant observation-side cross-check;
    the bound ``ResolvedNetworkPolicy`` remains authoritative. A deny policy
    requires an active failed-egress probe; unobservable fails isolation.
    """

    policy_digest: str = Field(pattern=SHA256_PATTERN)
    declared_allow_internet: bool
    mode: NetworkObservationMode
    egress_attempted: bool
    egress_succeeded: bool
    reached_endpoints: tuple[NetworkEndpointRecord, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()

    @property
    def claim_isolation_valid(self) -> bool:
        """Whether this observation can substantiate claim-run isolation."""

        if self.mode == NetworkObservationMode.unobservable:
            return False
        if not self.declared_allow_internet:
            return (
                self.mode == NetworkObservationMode.active_probe
                and self.egress_attempted
                and not self.egress_succeeded
            )
        return True

    @model_validator(mode="after")
    def observation_is_coherent(self) -> "NetworkObservation":
        if self.egress_succeeded and not self.egress_attempted:
            raise ValueError("successful egress requires an egress attempt")
        if self.mode == NetworkObservationMode.active_probe and not self.egress_attempted:
            raise ValueError("active_probe requires egress_attempted=true")
        if self.mode == NetworkObservationMode.unobservable and (
            self.egress_attempted or self.egress_succeeded or self.reached_endpoints
        ):
            raise ValueError("unobservable network mode cannot claim observed activity")
        endpoint_successes = {
            "allowed",
            "connected",
            "connection_succeeded",
            "reached",
            "success",
            "ok",
        }
        if any(
            endpoint.outcome.strip().casefold() in endpoint_successes
            for endpoint in self.reached_endpoints
        ):
            if not self.egress_succeeded:
                raise ValueError(
                    "successful network endpoint requires egress_succeeded=true"
                )
            if not self.declared_allow_internet:
                raise ValueError(
                    "denied network policy cannot record a successful endpoint"
                )
        return self


class SubjectKind(str, Enum):
    """Execution/artifact shape that actually entered an execution path.

    This is deliberately separate from :class:`ComparisonKind`: two subjects
    with different runtime packaging (for example ``hcp_harness`` and
    ``opaque_agent``) can both represent a complete Coding Agent.
    """

    hcp_harness = "hcp_harness"
    opaque_agent = "opaque_agent"
    evolver = "evolver"
    meta_evolver = "meta_evolver"
    fake = "fake"


class ComparisonKind(str, Enum):
    """The four semantic subjects that BMP is allowed to compare."""

    agent = "agent"
    coding_agent = "coding_agent"
    evolution_method = "evolution_method"
    meta_evolution_method = "meta_evolution_method"


class JournalRecord(StrictModel):
    format: Literal["harnessx-journal-v2"]
    session_id: str = Field(pattern=ID_PATTERN)
    run_ids: tuple[str, ...]
    segment_refs: tuple[ArtifactRef, ...]
    trace_refs: tuple[ArtifactRef, ...]
    state_refs: tuple[ArtifactRef, ...]
    session_index_ref: ArtifactRef

    @field_validator("run_ids")
    @classmethod
    def run_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("journal run_ids must be valid BMP ids")
        if len(set(values)) != len(values):
            raise ValueError("journal run_ids must be unique")
        return values


class SystemPromptRecord(StrictModel):
    step_id: str = Field(pattern=ID_PATTERN)
    prompt_ref: ArtifactRef


class WorkspaceRecord(StrictModel):
    namespace: str = Field(min_length=1)
    setup_refs: tuple[ArtifactRef, ...]
    state_refs: tuple[ArtifactRef, ...]
    journal: JournalRecord


class ScoringKind(str, Enum):
    """Benchmark-owned verdict semantics.

    Continuous scoring supports metric/effect claims but not pass-rate claims;
    downstream compilation must reject pass-rate estimands without a threshold.
    """

    binary = "binary"
    continuous = "continuous"


class RunStatus(str, Enum):
    """Closed outcome taxonomy retained for every planned rollout."""

    pass_ = "pass"
    verified_fail = "verified_fail"
    scored = "scored"
    no_output = "no_output"
    invalid_output = "invalid_output"
    timeout = "timeout"
    agent_error = "agent_error"
    harness_fault = "harness_fault"
    verifier_error = "verifier_error"
    infra_error = "infra_error"
    unsupported = "unsupported"


class MetricValueKind(str, Enum):
    binary = "binary"
    count = "count"
    continuous = "continuous"
    duration = "duration"
    tokens = "tokens"
    currency = "currency"
    rate = "rate"
    efficiency = "efficiency"


class MetricLevel(str, Enum):
    event = "event"
    rollout = "rollout"
    task = "task"
    configuration = "configuration"
    experiment = "experiment"


class MetricDirection(str, Enum):
    maximize = "maximize"
    minimize = "minimize"
    neutral = "neutral"


class MetricSource(str, Enum):
    evaluator = "evaluator"
    usage = "usage"
    trajectory = "trajectory"
    schedule = "schedule"
    regime = "regime"
    evolution = "evolution"
    external = "external"
    derived = "derived"


class MetricFormula(str, Enum):
    direct_v1 = "direct_v1"
    mean_v1 = "mean_v1"
    sum_v1 = "sum_v1"
    minimum_v1 = "minimum_v1"
    maximum_v1 = "maximum_v1"
    median_v1 = "median_v1"
    quantile_linear_v1 = "quantile_linear_v1"
    variance_population_v1 = "variance_population_v1"
    variance_sample_v1 = "variance_sample_v1"
    standard_deviation_population_v1 = "standard_deviation_population_v1"
    standard_deviation_sample_v1 = "standard_deviation_sample_v1"
    pass_at_1_v1 = "pass_at_1_v1"
    pass_at_k_unbiased_v1 = "pass_at_k_unbiased_v1"
    pass_power_k_v1 = "pass_power_k_v1"
    empirical_any_at_k_v1 = "empirical_any_at_k_v1"
    empirical_all_at_k_v1 = "empirical_all_at_k_v1"
    expected_max_at_k_v1 = "expected_max_at_k_v1"
    ratio_v1 = "ratio_v1"
    difference_v1 = "difference_v1"
    successes_per_million_tokens_v1 = "successes_per_million_tokens_v1"
    completed_per_hour_v1 = "completed_per_hour_v1"
    external_adapter_v1 = "external_adapter_v1"


class MetricPopulation(str, Enum):
    evaluator_observations = "evaluator_observations"
    planned_rollouts = "planned_rollouts"
    planned_tasks = "planned_tasks"
    launched_rollouts = "launched_rollouts"
    completed_rollouts = "completed_rollouts"
    observed_rollouts = "observed_rollouts"
    stages = "stages"
    domains = "domains"
    generations = "generations"
    experiment_wall_clock = "experiment_wall_clock"


class MetricGroupKey(str, Enum):
    """Identity keys available to an intermediate metric reduction.

    A reducer first operates inside every declared group and the resulting
    group values are macro-averaged unless the formula says otherwise.  The
    task key is available from every schedule receipt; stage/domain/generation
    keys require their corresponding typed receipt and therefore fail closed
    when selected on a plain single-stage schedule.
    """

    task = "task"
    stage = "stage"
    checkpoint = "checkpoint"
    domain = "domain"
    scenario = "scenario"
    variant = "variant"
    candidate = "candidate"
    generation = "generation"
    time_window = "time_window"


class MetricFormulaParameters(StrictModel):
    """Typed parameters whose meaning is owned by ``MetricFormula``."""

    k: int | None = Field(default=None, ge=1, strict=True)
    quantile: float | None = Field(default=None, ge=0.0, le=1.0, strict=True)


class MetricAcrossGroupAggregation(str, Enum):
    macro_mean = "macro_mean"
    minimum = "minimum"


class MetricSamplingDesign(str, Enum):
    exchangeable_rollouts = "exchangeable_rollouts"
    ordered_prefix = "ordered_prefix"


class MetricSamplingSpec(StrictModel):
    """Sampling assumptions required by a repeated-rollout estimand."""

    design: MetricSamplingDesign
    subset_policy: Literal["uniform_without_replacement", "first_k"]
    exchangeability_keys: tuple[str, ...] = ()

    @field_validator("exchangeability_keys")
    @classmethod
    def exchangeability_keys_are_closed(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("exchangeability keys must be unique")
        if any(
            re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_.-]*$", value) is None
            for value in values
        ):
            raise ValueError("exchangeability keys must be normalized paths")
        return values

    @model_validator(mode="after")
    def sampling_design_matches_subset_policy(self) -> "MetricSamplingSpec":
        if self.design == MetricSamplingDesign.exchangeable_rollouts:
            if self.subset_policy != "uniform_without_replacement":
                raise ValueError(
                    "exchangeable rollouts require uniform_without_replacement"
                )
            if "task" not in self.exchangeability_keys:
                raise ValueError("exchangeable rollouts must bind the task key")
        else:
            if self.subset_policy != "first_k":
                raise ValueError("ordered prefix requires subset_policy='first_k'")
            if self.exchangeability_keys:
                raise ValueError("ordered prefix forbids exchangeability claims")
        return self


class MetricUncertaintyMethod(str, Enum):
    wilson_score_v1 = "wilson_score_v1"
    bootstrap_percentile_v1 = "bootstrap_percentile_v1"


class MetricUncertaintySpec(StrictModel):
    """Pre-registered deterministic uncertainty calculation."""

    method: MetricUncertaintyMethod
    estimand: Literal["mean"] = "mean"
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1.0, strict=True)
    resampling_unit: Literal["rollout", "task"]
    cluster_by: tuple[MetricGroupKey, ...] = ()
    resamples: int | None = Field(default=None, ge=100, strict=True)
    seed: int | None = None
    rng_algorithm: Literal["sha256_counter_v1"] | None = None
    degenerate_policy: Literal["point_interval"] = "point_interval"

    @model_validator(mode="after")
    def uncertainty_parameters_match_method(self) -> "MetricUncertaintySpec":
        if self.method == MetricUncertaintyMethod.wilson_score_v1:
            if self.resampling_unit != "rollout":
                raise ValueError("Wilson uncertainty requires rollout units")
            if (
                self.cluster_by
                or self.resamples is not None
                or self.seed is not None
                or self.rng_algorithm is not None
            ):
                raise ValueError("Wilson uncertainty forbids bootstrap parameters")
        else:
            if (
                self.resamples is None
                or self.seed is None
                or self.rng_algorithm is None
            ):
                raise ValueError(
                    "bootstrap uncertainty requires deterministic resamples, seed, and RNG"
                )
            if self.resampling_unit == "task" and self.cluster_by != (
                MetricGroupKey.task,
            ):
                raise ValueError("task bootstrap requires cluster_by=['task']")
            if self.resampling_unit == "rollout" and self.cluster_by:
                raise ValueError("rollout bootstrap forbids cluster_by")
        return self


class MetricStatusDisposition(str, Enum):
    observe = "observe"
    zero = "zero"
    exclude = "exclude"
    invalidate = "invalidate"


class MetricMissingDisposition(str, Enum):
    zero = "zero"
    exclude = "exclude"
    invalidate = "invalidate"


class MetricSpec(RegistryEntry):
    """Versioned metric semantics selected only through a TOML registry.

    The formula, population, failure handling, unit, and dependency identities
    are all part of the metric digest. This prevents a familiar display name
    such as ``pass@1`` from silently changing denominator or exception policy.
    """

    kind: Literal["metric"]
    value_kind: MetricValueKind
    level: MetricLevel
    direction: MetricDirection
    unit: str = Field(min_length=1)
    source: MetricSource
    source_field: str | None = Field(default=None, min_length=1)
    formula: MetricFormula
    population: MetricPopulation
    group_by: tuple[MetricGroupKey, ...] = ()
    across_groups: MetricAcrossGroupAggregation | None = None
    parameters: MetricFormulaParameters | None = None
    sampling: MetricSamplingSpec | None = None
    uncertainty: MetricUncertaintySpec | None = None
    config: Mapping[str, Any] = Field(default_factory=dict)
    inputs: tuple[str, ...] = ()
    scale: float = Field(default=1.0, gt=0, strict=True)
    missing_observation: MetricMissingDisposition
    status_policy: Mapping[str, MetricStatusDisposition] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def input_metric_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("metric input ids must be unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("metric input ids must be valid BMP ids")
        return values

    @field_validator("group_by")
    @classmethod
    def group_keys_are_unique(
        cls, values: tuple[MetricGroupKey, ...]
    ) -> tuple[MetricGroupKey, ...]:
        if len(set(values)) != len(values):
            raise ValueError("metric group_by keys must be unique")
        return values

    @field_validator("status_policy", mode="before")
    @classmethod
    def status_policy_is_closed(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            raise ValueError("metric status_policy must be a table")
        unknown = sorted(set(value) - {status.value for status in RunStatus})
        if unknown:
            raise ValueError(f"metric status_policy contains unknown statuses: {unknown}")
        return dict(value)

    @field_validator("config", mode="before")
    @classmethod
    def metric_config_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(value, field_name="MetricSpec.config")

    @model_validator(mode="after")
    def metric_contract_is_coherent(self) -> "MetricSpec":
        direct_sources = {
            MetricSource.evaluator,
            MetricSource.usage,
            MetricSource.trajectory,
            MetricSource.schedule,
            MetricSource.regime,
            MetricSource.evolution,
            MetricSource.external,
        }
        if self.source in direct_sources and self.source_field is None:
            raise ValueError("direct metric sources require source_field")
        if self.source == MetricSource.derived and self.source_field is not None:
            raise ValueError("derived metrics forbid source_field")
        if self.source == MetricSource.derived and not self.inputs:
            raise ValueError("derived metrics require input metric ids")
        if self.source != MetricSource.derived and self.inputs:
            raise ValueError("non-derived metrics forbid input metric ids")
        expected_input_count = {
            MetricFormula.ratio_v1: 2,
            MetricFormula.difference_v1: 2,
            MetricFormula.successes_per_million_tokens_v1: 2,
        }.get(self.formula)
        if expected_input_count is not None and len(self.inputs) != expected_input_count:
            raise ValueError(
                f"metric formula {self.formula.value!r} requires "
                f"{expected_input_count} inputs"
            )
        if self.formula in {
            MetricFormula.ratio_v1,
            MetricFormula.difference_v1,
            MetricFormula.successes_per_million_tokens_v1,
        } and self.source != MetricSource.derived:
            raise ValueError("multi-input formulas require source='derived'")
        parameterized_k_formulas = {
            MetricFormula.pass_at_k_unbiased_v1,
            MetricFormula.pass_power_k_v1,
            MetricFormula.empirical_any_at_k_v1,
            MetricFormula.empirical_all_at_k_v1,
            MetricFormula.expected_max_at_k_v1,
        }
        if self.formula in parameterized_k_formulas:
            if self.parameters is None or self.parameters.k is None:
                raise ValueError(f"{self.formula.value} requires parameters.k")
            if self.parameters.quantile is not None:
                raise ValueError(f"{self.formula.value} forbids parameters.quantile")
            if self.group_by != (MetricGroupKey.task,):
                raise ValueError(
                    f"{self.formula.value} requires group_by=['task']"
                )
            if self.population != MetricPopulation.planned_tasks:
                raise ValueError(
                    f"{self.formula.value} requires population='planned_tasks'"
                )
            if self.across_groups != MetricAcrossGroupAggregation.macro_mean:
                raise ValueError(
                    f"{self.formula.value} requires across_groups='macro_mean'"
                )
        elif self.formula == MetricFormula.quantile_linear_v1:
            if self.parameters is None or self.parameters.quantile is None:
                raise ValueError("quantile_linear_v1 requires parameters.quantile")
            if self.parameters.k is not None:
                raise ValueError("quantile_linear_v1 forbids parameters.k")
        elif self.parameters is not None:
            raise ValueError(
                f"metric formula {self.formula.value!r} forbids parameters"
            )
        if self.group_by and self.across_groups is None:
            raise ValueError("grouped metrics require across_groups")
        if not self.group_by and self.across_groups is not None:
            raise ValueError("ungrouped metrics forbid across_groups")
        combinatorial_formulas = {
            MetricFormula.pass_at_k_unbiased_v1,
            MetricFormula.pass_power_k_v1,
            MetricFormula.expected_max_at_k_v1,
        }
        prefix_formulas = {
            MetricFormula.empirical_any_at_k_v1,
            MetricFormula.empirical_all_at_k_v1,
        }
        if self.formula in combinatorial_formulas:
            if (
                self.sampling is None
                or self.sampling.design
                != MetricSamplingDesign.exchangeable_rollouts
            ):
                raise ValueError(
                    f"{self.formula.value} requires an exchangeable sampling design"
                )
        elif self.formula in prefix_formulas:
            if (
                self.sampling is None
                or self.sampling.design != MetricSamplingDesign.ordered_prefix
            ):
                raise ValueError(
                    f"{self.formula.value} requires an ordered-prefix sampling design"
                )
        elif self.sampling is not None:
            raise ValueError(
                f"metric formula {self.formula.value!r} forbids sampling assumptions"
            )
        binary_k_formulas = {
            MetricFormula.pass_at_k_unbiased_v1,
            MetricFormula.pass_power_k_v1,
            MetricFormula.empirical_any_at_k_v1,
            MetricFormula.empirical_all_at_k_v1,
        }
        if self.formula in binary_k_formulas:
            if self.scale != 1.0:
                raise ValueError("binary k-success formulas require scale=1")
            if self.source != MetricSource.evaluator:
                raise ValueError(
                    f"{self.formula.value} requires evaluator reward evidence"
                )
            expected_statuses = {status.value for status in RunStatus}
            if set(self.status_policy) != expected_statuses:
                raise ValueError(
                    f"{self.formula.value} requires an explicit policy for every "
                    "rollout status"
                )
            if any(
                disposition
                not in {MetricStatusDisposition.observe, MetricStatusDisposition.zero}
                for disposition in self.status_policy.values()
            ) or self.missing_observation != MetricMissingDisposition.zero:
                raise ValueError(
                    f"{self.formula.value} requires a value for every planned slot"
                )
        if self.formula == MetricFormula.expected_max_at_k_v1:
            if self.source != MetricSource.evaluator:
                raise ValueError(
                    "expected_max_at_k_v1 requires evaluator reward evidence"
                )
            expected_statuses = {status.value for status in RunStatus}
            if set(self.status_policy) != expected_statuses:
                raise ValueError(
                    "expected_max_at_k_v1 requires an explicit policy for every "
                    "rollout status"
                )
            if any(
                disposition
                not in {MetricStatusDisposition.observe, MetricStatusDisposition.zero}
                for disposition in self.status_policy.values()
            ) or self.missing_observation != MetricMissingDisposition.zero:
                raise ValueError(
                    "expected_max_at_k_v1 requires a value for every planned slot"
                )
        if self.formula == MetricFormula.pass_at_1_v1:
            if self.scale != 1.0:
                raise ValueError("pass_at_1_v1 requires scale=1")
            if self.source != MetricSource.evaluator:
                raise ValueError("pass_at_1_v1 requires evaluator reward evidence")
            if self.population != MetricPopulation.planned_rollouts:
                raise ValueError("pass_at_1_v1 requires population='planned_rollouts'")
            expected_statuses = {status.value for status in RunStatus}
            if set(self.status_policy) != expected_statuses:
                raise ValueError(
                    "pass_at_1_v1 requires an explicit policy for every rollout status"
                )
            if any(
                disposition
                not in {MetricStatusDisposition.observe, MetricStatusDisposition.zero}
                for disposition in self.status_policy.values()
            ) or self.missing_observation != MetricMissingDisposition.zero:
                raise ValueError(
                    "pass_at_1_v1 requires a value for every planned rollout slot"
                )
        if self.uncertainty is not None:
            if self.source == MetricSource.derived:
                raise ValueError(
                    "derived metric uncertainty requires a covariance receipt and is "
                    "not supported by this formula version"
                )
            if self.formula == MetricFormula.direct_v1:
                raise ValueError("direct metrics cannot compute aggregate uncertainty")
            if (
                self.uncertainty.method
                == MetricUncertaintyMethod.wilson_score_v1
                and self.formula != MetricFormula.pass_at_1_v1
            ):
                raise ValueError("Wilson uncertainty is defined only for pass_at_1_v1")
            if (
                self.uncertainty.resampling_unit == "task"
                and self.group_by != (MetricGroupKey.task,)
            ):
                raise ValueError(
                    "task bootstrap uncertainty requires group_by=['task']"
                )
            if (
                self.uncertainty.resampling_unit == "rollout"
                and self.group_by
            ):
                raise ValueError(
                    "rollout uncertainty forbids intermediate grouping"
                )
        if self.source == MetricSource.derived and self.status_policy:
            raise ValueError("derived metrics inherit inputs and forbid status_policy")
        if self.formula == MetricFormula.external_adapter_v1:
            if self.source not in {
                MetricSource.regime,
                MetricSource.evolution,
                MetricSource.external,
            }:
                raise ValueError(
                    "external_adapter_v1 requires regime/evolution/external source"
                )
            if self.adapter == "magentabench.measurement":
                raise ValueError("external adapter metric requires a plugin adapter id")
        elif self.config:
            raise ValueError("built-in metric formulas forbid adapter config")
        object.__setattr__(self, "config", _freeze_configuration_tree(self.config))
        return self


class MetricArtifact(StrictModel):
    metric: MetricSpec
    declaration_ref: ArtifactRef
    artifact_digest: str = Field(pattern=SHA256_PATTERN)

    def identity_data(self) -> dict[str, Any]:
        return {
            "metric": self.metric.model_dump(mode="json"),
            "declaration_ref": self.declaration_ref.identity_data(),
        }

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class EvaluatorMetricBinding(StrictModel):
    """Maps one adapter-native output key onto a registered metric identity."""

    metric_id: str = Field(pattern=ID_PATTERN)
    source_key: str = Field(min_length=1)
    authoritative: bool = False
    success_operator: Literal["eq", "gte", "lte", "gt", "lt", "range"] | None = None
    success_threshold: float | None = None
    success_upper_bound: float | None = None
    absolute_tolerance: float = Field(default=0.0, ge=0, strict=True)


class EvaluatorSpec(RegistryEntry):
    """TOML-registered evaluator and its emitted metric bindings."""

    kind: Literal["evaluator"]
    implementation: str = Field(min_length=1)
    scoring_kind: ScoringKind
    metrics: tuple[EvaluatorMetricBinding, ...] = Field(min_length=1)
    config: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("config", mode="before")
    @classmethod
    def evaluator_config_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(value, field_name="EvaluatorSpec.config")

    @model_validator(mode="after")
    def evaluator_contract_is_closed(self) -> "EvaluatorSpec":
        metric_ids = [binding.metric_id for binding in self.metrics]
        source_keys = [binding.source_key for binding in self.metrics]
        if len(set(metric_ids)) != len(metric_ids):
            raise ValueError("evaluator metric ids must be unique")
        if len(set(source_keys)) != len(source_keys):
            raise ValueError("evaluator source keys must be unique")
        authoritative = [binding for binding in self.metrics if binding.authoritative]
        if len(authoritative) != 1:
            raise ValueError("evaluator requires exactly one authoritative metric")
        reward = authoritative[0]
        if self.scoring_kind == ScoringKind.binary and (
            reward.success_operator is None or reward.success_threshold is None
        ):
            raise ValueError(
                "binary evaluator authoritative metric requires success operator and threshold"
            )
        if self.scoring_kind == ScoringKind.binary:
            if (
                reward.success_operator == "range"
                and (
                    reward.success_upper_bound is None
                    or reward.success_upper_bound < reward.success_threshold
                )
            ):
                raise ValueError("range success requires an ordered upper bound")
            if (
                reward.success_operator != "range"
                and reward.success_upper_bound is not None
            ):
                raise ValueError("success_upper_bound is allowed only for range")
        if self.scoring_kind == ScoringKind.continuous and (
            reward.success_operator is not None
            or reward.success_threshold is not None
            or reward.success_upper_bound is not None
            or reward.absolute_tolerance != 0
        ):
            raise ValueError("continuous evaluator authoritative metric forbids success rules")
        if any(
            (
                binding.success_operator is not None
                or binding.success_threshold is not None
                or binding.success_upper_bound is not None
                or binding.absolute_tolerance != 0
            )
            and not binding.authoritative
            for binding in self.metrics
        ):
            raise ValueError("only the authoritative evaluator metric may set success rules")
        object.__setattr__(self, "config", _freeze_configuration_tree(self.config))
        return self

    @property
    def authoritative_metric(self) -> EvaluatorMetricBinding:
        return next(binding for binding in self.metrics if binding.authoritative)


class EvaluatorArtifact(StrictModel):
    evaluator: EvaluatorSpec
    declaration_ref: ArtifactRef
    artifact_digest: str = Field(pattern=SHA256_PATTERN)

    def identity_data(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator.model_dump(mode="json"),
            "declaration_ref": self.declaration_ref.identity_data(),
        }

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class TaskSuiteBenchmarkSpec(RegistryEntry):
    kind: Literal["task_suite"]


class ToolAgentSuiteBenchmarkSpec(RegistryEntry):
    kind: Literal["tool_agent_suite"]
    input_contract: str = Field(min_length=1)
    output_contract: tuple[str, ...] = Field(min_length=1)


class CustomBenchmarkSpec(RegistryEntry):
    """Generic benchmark declaration owned by an external BMP adapter."""

    kind: Literal["custom"]

BenchmarkSpec = Annotated[
    Union[TaskSuiteBenchmarkSpec, ToolAgentSuiteBenchmarkSpec, CustomBenchmarkSpec],
    Field(discriminator="kind"),
]
BenchmarkSpecAdapter = TypeAdapter(BenchmarkSpec)


class ArtifactIdentity(StrictModel):
    """Compiler-generated identity shared by resolved registry artifacts."""

    artifact_digest: str = Field(pattern=SHA256_PATTERN)


class AbsoluteSourceArtifact(ArtifactIdentity):
    source: str = Field(min_length=1)
    source_content_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("source")
    @classmethod
    def source_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("artifact source must be an absolute path")
        return value


class DeclaredRegistryArtifact(ArtifactIdentity):
    declaration_ref: ArtifactRef

    def identity_data(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"artifact_digest", "declaration_ref"})
        data["declaration_ref"] = self.declaration_ref.identity_data()
        return data

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class TaskSuiteBenchmarkArtifact(DeclaredRegistryArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["task_suite"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION


class ToolAgentSuiteBenchmarkArtifact(DeclaredRegistryArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["tool_agent_suite"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    input_contract: str = Field(min_length=1)
    output_contract: tuple[str, ...] = Field(min_length=1)


class CustomBenchmarkArtifact(DeclaredRegistryArtifact):
    """Resolved form of a benchmark implemented by an external adapter."""

    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["custom"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION

BenchmarkArtifact = Annotated[
    Union[TaskSuiteBenchmarkArtifact, ToolAgentSuiteBenchmarkArtifact, CustomBenchmarkArtifact],
    Field(discriminator="kind"),
]
BenchmarkArtifactAdapter = TypeAdapter(BenchmarkArtifact)


class DatasetSpec(SourceRegistryEntry):
    """TOML-registered dataset content and adapter configuration."""

    kind: Literal["dataset"]
    content_globs: tuple[str, ...] = Field(min_length=1)
    format: str = Field(default="opaque", min_length=1, pattern=ID_PATTERN)
    split: str | None = Field(default=None, min_length=1)
    config: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("content_globs")
    @classmethod
    def content_globs_are_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("dataset content_globs must be unique")
        return tuple(
            _validate_logical_relative_path(value, field_name="dataset content_globs")
            for value in values
        )

    @field_validator("config", mode="before")
    @classmethod
    def config_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(value, field_name="DatasetSpec.config")

    @model_validator(mode="after")
    def freeze_config_tree(self) -> "DatasetSpec":
        object.__setattr__(self, "config", _freeze_configuration_tree(self.config))
        return self


class DatasetArtifact(AbsoluteSourceArtifact):
    """Digest-bound dataset declaration carried by a resolved manifest."""

    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["dataset"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    declaration_ref: ArtifactRef
    commit: str | None = Field(default=None, min_length=1)
    content_globs: tuple[str, ...] = Field(min_length=1)
    format: str = Field(default="opaque", min_length=1, pattern=ID_PATTERN)
    split: str | None = Field(default=None, min_length=1)
    config: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("content_globs")
    @classmethod
    def content_globs_are_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("dataset content_globs must be unique")
        return tuple(
            _validate_logical_relative_path(value, field_name="dataset content_globs")
            for value in values
        )

    @field_validator("config", mode="before")
    @classmethod
    def config_is_json_compatible(cls, value: Any) -> Any:
        return _validate_json_configuration(value, field_name="DatasetArtifact.config")

    @model_validator(mode="after")
    def freeze_config_tree(self) -> "DatasetArtifact":
        object.__setattr__(self, "config", _freeze_configuration_tree(self.config))
        return self

    def identity_data(self) -> dict[str, Any]:
        data = self.model_dump(
            mode="json",
            exclude={"artifact_digest", "source", "declaration_ref"},
        )
        data["source_content_digest"] = self.source_content_digest
        data["declaration_ref"] = self.declaration_ref.identity_data()
        return data

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class EvolutionSelectionSpec(StrictModel):
    """Registered parent-selection semantics for an evolution method.

    The registry owns the selector and every parameter that can change its
    realized choice. Runtime receipts add the concrete candidate population
    and RNG state; they must not invent a selector that is absent here.
    """

    policy_id: str = Field(pattern=ID_PATTERN)
    selector: Literal["extreme", "weighted"]
    metric_id: str = Field(pattern=ID_PATTERN)
    direction: Literal["maximize", "minimize"]
    tie_break_rule: str = Field(pattern=ID_PATTERN)
    child_count_penalty: float = Field(default=0.0, ge=0.0, strict=True)
    rng_algorithm: str = Field(default="none", pattern=ID_PATTERN)
    rng_seed: int | None = None

    @model_validator(mode="after")
    def randomness_matches_selector(self) -> "EvolutionSelectionSpec":
        if self.selector == "extreme":
            if self.rng_algorithm != "none" or self.rng_seed is not None:
                raise ValueError(
                    "extreme evolution selection forbids RNG algorithm and seed"
                )
        elif self.rng_algorithm == "none" or self.rng_seed is None:
            raise ValueError(
                "weighted evolution selection requires an RNG algorithm and seed"
            )
        return self

    def configuration_data(self) -> dict[str, Any]:
        """Return the exact adapter configuration projection this spec binds."""

        return self.model_dump(mode="json", exclude_none=True)


_REQUIRED_PROTECTED_EVOLUTION_SURFACES = frozenset(
    {"dataset", "evaluator", "metrics", "sealed_holdout"}
)


def _surface_contains(root: str, path: str) -> bool:
    return path == root or path.startswith(root + ".")


class EvolutionSurfacePolicy(StrictModel):
    """Explicit mutation boundary for evolution and meta-evolution.

    Paths use a semantic namespace rather than host filesystem paths. A change
    is authorized only when it is under an editable root and under no protected
    root. Evaluation authority is always protected so a method cannot improve
    its own score by rewriting the measurement contract.
    """

    editable_paths: tuple[str, ...] = Field(min_length=1)
    protected_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("editable_paths", "protected_paths")
    @classmethod
    def paths_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        path_pattern = re.compile(
            r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$"
        )
        if any(path_pattern.fullmatch(value) is None for value in values):
            raise ValueError("evolution surfaces must be normalized dotted paths")
        if len(set(values)) != len(values):
            raise ValueError("evolution surfaces must be unique")
        if values != tuple(sorted(values)):
            raise ValueError("evolution surfaces must be lexicographically sorted")
        for index, path in enumerate(values):
            if any(
                _surface_contains(other, path) or _surface_contains(path, other)
                for other in values[index + 1 :]
            ):
                raise ValueError("evolution surfaces must not contain redundant roots")
        return values

    @model_validator(mode="after")
    def surfaces_are_disjoint_and_fail_closed(self) -> "EvolutionSurfacePolicy":
        overlap = [
            (editable, protected)
            for editable in self.editable_paths
            for protected in self.protected_paths
            if _surface_contains(editable, protected)
            or _surface_contains(protected, editable)
        ]
        if overlap:
            raise ValueError(
                "editable and protected evolution surfaces overlap: "
                f"{overlap}"
            )
        missing = sorted(
            required
            for required in _REQUIRED_PROTECTED_EVOLUTION_SURFACES
            if not any(
                _surface_contains(protected, required)
                for protected in self.protected_paths
            )
        )
        if missing:
            raise ValueError(
                "evolution surfaces must protect measurement authority: "
                f"{missing}"
            )
        return self

    def assert_changes_allowed(self, changed_paths: tuple[str, ...]) -> None:
        """Reject a realized mutation outside the registered editable surface."""

        if len(set(changed_paths)) != len(changed_paths):
            raise ValueError("realized evolution change paths must be unique")
        forbidden = sorted(
            path
            for path in changed_paths
            if any(
                _surface_contains(protected, path)
                for protected in self.protected_paths
            )
            or not any(
                _surface_contains(editable, path)
                for editable in self.editable_paths
            )
        )
        if forbidden:
            raise ValueError(
                "realized evolution changes exceed the registered editable surface: "
                f"{forbidden}"
            )


class EvolutionMethodSpec(RegistryEntry):
    """TOML-registered method for evolving a harness candidate."""

    kind: Literal["evolver"]
    comparison_kind: Literal["evolution_method"]
    subject_adapter: str = Field(pattern=ADAPTER_PATTERN)
    target: Literal["harness"]
    configuration_profile_id: str = Field(pattern=ID_PATTERN)
    selection_configuration_path: str = Field(
        default="evolution.selection",
        pattern=r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$",
    )
    selection: EvolutionSelectionSpec
    surface: EvolutionSurfacePolicy


class MetaEvolutionMethodSpec(RegistryEntry):
    """TOML-registered method that changes a registered evolver."""

    kind: Literal["meta_evolver"]
    comparison_kind: Literal["meta_evolution_method"]
    subject_adapter: str = Field(pattern=ADAPTER_PATTERN)
    target: Literal["evolver"]
    parent_evolver_id: str = Field(pattern=ID_PATTERN)
    configuration_profile_id: str = Field(pattern=ID_PATTERN)
    selection_configuration_path: str = Field(
        default="evolution.selection",
        pattern=r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$",
    )
    selection: EvolutionSelectionSpec
    surface: EvolutionSurfacePolicy


class EvolutionMethodArtifact(DeclaredRegistryArtifact):
    """Resolved evolver declaration bound to the full configuration identity."""

    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["evolver"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    comparison_kind: Literal["evolution_method"]
    subject_adapter: str = Field(pattern=ADAPTER_PATTERN)
    target: Literal["harness"]
    configuration_profile_id: str = Field(pattern=ID_PATTERN)
    configuration_digest: str = Field(pattern=SHA256_PATTERN)
    selection_configuration_path: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$"
    )
    selection: EvolutionSelectionSpec
    surface: EvolutionSurfacePolicy

    def spec_data(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"artifact_digest", "declaration_ref", "configuration_digest"},
        )


class MetaEvolutionMethodArtifact(DeclaredRegistryArtifact):
    """Resolved meta-evolver bound to its parent method and configuration."""

    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["meta_evolver"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    comparison_kind: Literal["meta_evolution_method"]
    subject_adapter: str = Field(pattern=ADAPTER_PATTERN)
    target: Literal["evolver"]
    parent_evolver_id: str = Field(pattern=ID_PATTERN)
    parent_evolver_digest: str = Field(pattern=SHA256_PATTERN)
    configuration_profile_id: str = Field(pattern=ID_PATTERN)
    configuration_digest: str = Field(pattern=SHA256_PATTERN)
    selection_configuration_path: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$"
    )
    selection: EvolutionSelectionSpec
    surface: EvolutionSurfacePolicy

    def spec_data(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={
                "artifact_digest",
                "declaration_ref",
                "configuration_digest",
                "parent_evolver_digest",
            },
        )


class AssemblySidecarRef(StrictModel):
    """Opaque reference to the assembly sidecar produced by ``magenta_hcp``.

    BMP records path and digest for provenance but does not interpret contents.
    """

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    sidecar_schema_version: str = Field(default="0.1", min_length=1)

    @field_validator("path")
    @classmethod
    def sidecar_path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("sidecar path must be absolute")
        return value


class RuntimeAssemblySidecarRef(StrictModel):
    """One persisted sidecar emitted by a sequenced runtime manifest."""

    sequence: int = Field(ge=1)
    path: str = Field(min_length=1, pattern=r"^/")
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    sidecar_schema_version: Literal["1"] = "1"

    @field_validator("path")
    @classmethod
    def runtime_sidecar_path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("runtime assembly sidecar path must be absolute")
        return value


class RuntimeManifestReceipt(StrictModel):
    """Public Magenta runtime-manifest lineage retained by one attempt."""

    protocol_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    manifest_sha256: tuple[str, ...] = Field(min_length=1)
    trace_ref: ArtifactRef
    assembly_sidecar_refs: tuple[RuntimeAssemblySidecarRef, ...] = ()
    effective_sequence: int = Field(ge=1)
    effective_assembly_sidecar_ref: RuntimeAssemblySidecarRef | None = None

    @field_validator("manifest_sha256")
    @classmethod
    def manifest_digests_are_sha256(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(re.fullmatch(SHA256_PATTERN, value) is None for value in values):
            raise ValueError("runtime manifest digests must be lowercase SHA-256")
        return values

    @model_validator(mode="after")
    def sequences_are_contiguous_and_effective_is_final(
        self,
    ) -> "RuntimeManifestReceipt":
        if self.effective_sequence != len(self.manifest_sha256):
            raise ValueError("effective runtime manifest sequence must be final")
        sequences = tuple(ref.sequence for ref in self.assembly_sidecar_refs)
        if len(set(sequences)) != len(sequences) or any(
            sequence > self.effective_sequence for sequence in sequences
        ):
            raise ValueError("runtime assembly sidecar sequences are invalid")
        expected_effective = next(
            (
                ref
                for ref in self.assembly_sidecar_refs
                if ref.sequence == self.effective_sequence
            ),
            None,
        )
        if self.effective_assembly_sidecar_ref != expected_effective:
            raise ValueError(
                "effective assembly sidecar ref must match the final runtime manifest"
            )
        return self

    def effective_sidecar_artifact_ref(self) -> ArtifactRef | None:
        """Return the final sidecar as BMP's generic byte reference."""

        ref = self.effective_assembly_sidecar_ref
        if ref is None:
            return None
        return ArtifactRef(
            path=ref.path,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
        )


class ConfigurationActivationReceipt(StrictModel):
    """Neutral evidence that a resolved configuration reached the adapter.

    BMP owns the configuration artifact and its identity.  An adapter owns
    the meaning of its settings and reports only a secret-free projection of
    what it requested and what the runtime observed.  This keeps activation
    evidence useful for arbitrary adapters without importing their protocols.
    """

    protocol_version: Literal[1] = 1
    configuration_digest: str = Field(pattern=SHA256_PATTERN)
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    requested_paths: tuple[str, ...] = ()
    activated_paths: tuple[str, ...] = ()
    requested_values: Mapping[str, Any] | None = None
    activated_values: Mapping[str, Any] | None = None
    requested_sha256: str = Field(pattern=SHA256_PATTERN)
    activated_sha256: str = Field(pattern=SHA256_PATTERN)
    status: Literal[
        "matched",
        "unobserved",
        "mismatch",
        "missing_activation_receipt",
        "unrequested",
        "invalid_activation_receipt",
        "activation_unknown",
    ]
    reason: tuple[str, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()

    @field_validator("requested_paths", "activated_paths")
    @classmethod
    def paths_are_unique_dotted_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value.strip()
            or value.startswith(".")
            or value.endswith(".")
            or ".." in value
            or any(not segment.strip() for segment in value.split("."))
            for value in values
        ):
            raise ValueError("configuration activation paths must be dotted names")
        if len(set(values)) != len(values):
            raise ValueError("configuration activation paths must be unique")
        return tuple(values)

    @field_validator("requested_values", "activated_values", mode="before")
    @classmethod
    def values_are_secret_free_json(cls, value: Any) -> Any:
        if value is None:
            return None
        return _validate_json_configuration(
            value, field_name="ConfigurationActivationReceipt.values"
        )

    @field_validator("reason")
    @classmethod
    def reasons_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("configuration activation reasons must be non-empty")
        if len(set(values)) != len(values):
            raise ValueError("configuration activation reasons must be unique")
        return values

    @model_validator(mode="after")
    def activation_is_coherent(self) -> "ConfigurationActivationReceipt":
        def projection_paths(value: Mapping[str, Any] | None) -> tuple[str, ...]:
            if value is None:
                return ()
            paths: list[str] = []

            def visit(item: Mapping[str, Any], prefix: str = "") -> None:
                for key, child in item.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    if isinstance(child, Mapping):
                        visit(child, path)
                    else:
                        paths.append(path)

            visit(value)
            return tuple(sorted(paths))

        def projection_digest(value: Mapping[str, Any] | None) -> str | None:
            if value is None:
                return None
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return hashlib.sha256(encoded).hexdigest()

        if self.status == "matched":
            if self.requested_values is None or self.activated_values is None:
                raise ValueError("matched configuration activation requires projections")
            if projection_digest(self.requested_values) != self.requested_sha256:
                raise ValueError("requested configuration projection digest drift")
            if projection_digest(self.activated_values) != self.activated_sha256:
                raise ValueError("activated configuration projection digest drift")
            if self.requested_sha256 != self.activated_sha256:
                raise ValueError("matched configuration activation requires equal digests")
            if self.requested_paths != self.activated_paths:
                raise ValueError("matched configuration activation requires equal paths")
            if self.requested_paths != projection_paths(self.requested_values):
                raise ValueError("requested configuration paths do not cover projection")
            if self.activated_paths != projection_paths(self.activated_values):
                raise ValueError("activated configuration paths do not cover projection")
            if self.requested_values != self.activated_values:
                raise ValueError("matched configuration activation values differ")
            if self.reason:
                raise ValueError("matched configuration activation cannot have reasons")
        elif self.status in {"unobserved", "unrequested"}:
            if self.activated_paths:
                raise ValueError("unobserved configuration activation cannot claim paths")
            if not self.reason:
                raise ValueError("unobserved configuration activation requires a reason")
        elif self.status in {"mismatch", "invalid_activation_receipt", "activation_unknown"}:
            if (
                self.requested_sha256 == self.activated_sha256
                and self.requested_paths == self.activated_paths
            ):
                raise ValueError("mismatch configuration activation must differ")
            if not self.reason:
                raise ValueError("mismatch configuration activation requires a reason")
        elif self.status == "missing_activation_receipt":
            if self.activated_paths:
                raise ValueError(
                    "missing configuration activation cannot claim activated paths"
                )
            if not self.reason:
                raise ValueError(
                    "missing configuration activation requires a reason"
                )
        ref_keys = [(ref.sha256, ref.size_bytes) for ref in self.evidence_refs]
        if len(set(ref_keys)) != len(ref_keys):
            raise ValueError("configuration activation evidence refs must be unique")
        return self


class AssemblySubjectSpec(SourceRegistryEntry):
    kind: Literal["hcp_harness"]
    comparison_kind: Literal["agent", "coding_agent"]
    assembly_profile: str = Field(default="default", min_length=1)
    emits_trace: bool = False


class OpaqueAgentSubjectSpec(SourceRegistryEntry):
    kind: Literal["opaque_agent"]
    comparison_kind: Literal["agent", "coding_agent"]
    entrypoint: str = Field(min_length=1)
    launch_argv: tuple[str, ...] | None = None
    interface: str = Field(min_length=1)
    emits_trace: bool = False

    @model_validator(mode="after")
    def launch_argv_matches_entrypoint(self) -> "OpaqueAgentSubjectSpec":
        if self.launch_argv is not None:
            if not self.launch_argv or any(not item for item in self.launch_argv):
                raise ValueError("launch_argv must contain non-empty arguments")
            if self.launch_argv[0] != self.entrypoint:
                raise ValueError("launch_argv[0] must equal entrypoint")
        return self


class EvolverSubjectSpec(SourceRegistryEntry):
    kind: Literal["evolver"]
    comparison_kind: Literal["evolution_method"]
    target: Literal["harness"]
    emits_trace: bool = False


class MetaEvolverSubjectSpec(SourceRegistryEntry):
    kind: Literal["meta_evolver"]
    comparison_kind: Literal["meta_evolution_method"]
    target: Literal["evolver"]
    emits_trace: bool = False


class FakeSubjectSpec(StrictModel):
    """Deterministic subject reserved for protocol conformance tests."""

    kind: Literal["fake"]
    id: str = Field(pattern=ID_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    adapter: Literal["fake"] = "fake"
    fixed_answer: str = "BMP_OK"
    fault_mode: Literal[
        "none",
        "no_output",
        "invalid_output",
        "timeout",
        "agent_error",
        "harness_fault",
        "verifier_error",
        "infra_error",
        "unsupported",
    ] = "none"


SubjectSpec = Annotated[
    Union[
        AssemblySubjectSpec,
        OpaqueAgentSubjectSpec,
        EvolverSubjectSpec,
        MetaEvolverSubjectSpec,
        FakeSubjectSpec,
    ],
    Field(discriminator="kind"),
]
SubjectSpecAdapter = TypeAdapter(SubjectSpec)


class AssemblySubjectArtifact(AbsoluteSourceArtifact):
    """Pinned harness whose assembly evidence remains opaque to BMP core."""

    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["hcp_harness"]
    comparison_kind: Literal["agent", "coding_agent"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    assembly_profile: str = Field(default="default", min_length=1)
    sidecar_ref: AssemblySidecarRef | None = None
    emits_trace: bool = False


class OpaqueAgentSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["opaque_agent"]
    comparison_kind: Literal["agent", "coding_agent"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    entrypoint: str = Field(min_length=1)
    launch_argv: tuple[str, ...] | None = None
    interface: str = Field(min_length=1)
    emits_trace: bool = False

    @model_validator(mode="after")
    def launch_argv_matches_entrypoint(self) -> "OpaqueAgentSubjectArtifact":
        if self.launch_argv is not None:
            if not self.launch_argv or any(not item for item in self.launch_argv):
                raise ValueError("launch_argv must contain non-empty arguments")
            if self.launch_argv[0] != self.entrypoint:
                raise ValueError("launch_argv[0] must equal entrypoint")
        return self


class EvolverSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["evolver"]
    comparison_kind: Literal["evolution_method"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    target: Literal["harness"]
    emits_trace: bool = False


class MetaEvolverSubjectArtifact(AbsoluteSourceArtifact):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["meta_evolver"]
    comparison_kind: Literal["meta_evolution_method"]
    adapter: str = Field(pattern=ADAPTER_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    commit: str | None = Field(default=None, min_length=1)
    target: Literal["evolver"]
    emits_trace: bool = False


class FakeSubjectArtifact(ArtifactIdentity):
    kind: Literal["fake"]
    id: str = Field(pattern=ID_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    adapter: Literal["fake"] = "fake"
    fixed_answer: str = "BMP_OK"
    fault_mode: Literal[
        "none",
        "no_output",
        "invalid_output",
        "timeout",
        "agent_error",
        "harness_fault",
        "verifier_error",
        "infra_error",
        "unsupported",
    ] = "none"


SubjectArtifact = Annotated[
    Union[
        AssemblySubjectArtifact,
        OpaqueAgentSubjectArtifact,
        EvolverSubjectArtifact,
        MetaEvolverSubjectArtifact,
        FakeSubjectArtifact,
    ],
    Field(discriminator="kind"),
]
SubjectArtifactAdapter = TypeAdapter(SubjectArtifact)


class Budget(StrictModel):
    """Hard limits for a run; absent fields mean that limit is unspecified."""

    max_tokens: int | None = Field(default=None, ge=0)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_cost: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def at_least_one_limit(self) -> "Budget":
        if all(value is None for value in (self.max_tokens, self.max_wall_seconds, self.max_cost)):
            raise ValueError("budget must declare at least one limit")
        return self


class MountSpec(StrictModel):
    """A content-addressed host-to-runtime mount declaration."""

    host_path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    container_path: str = Field(min_length=1)
    read_only: bool = True

    @field_validator("host_path", "container_path")
    @classmethod
    def mount_paths_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("mount paths must be absolute")
        return value

    def identity_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "container_path": self.container_path,
            "read_only": self.read_only,
            "content_sha256": self.content_sha256,
        }


class EnvironmentSpec(StrictModel):
    """Reproducible environment requirements; interpreter pin is mandatory."""

    id: str = Field(pattern=ID_PATTERN)
    bmp_version: Literal["0.1"] = BMP_VERSION
    python_version: str = Field(pattern=r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
    packages: tuple[str, ...] = ()
    env_var_names: tuple[str, ...] = ()
    mounts: tuple[MountSpec, ...] = ()
    build_timeout_seconds: float = Field(default=600.0, gt=0, strict=True)

    @field_validator("packages")
    @classmethod
    def package_requirements_are_nonempty(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("package requirements must be non-empty")
        if len(set(values)) != len(values):
            raise ValueError("package requirements must be unique")
        return values

    @field_validator("env_var_names")
    @classmethod
    def env_vars_are_names_only(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        name_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        invalid = [value for value in values if not name_pattern.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid environment variable names: {invalid}")
        if len(set(values)) != len(values):
            raise ValueError("environment variable names must be unique")
        return values

    def identity_data(self) -> dict[str, Any]:
        """Return path-independent environment identity.

        Host mount paths are provenance.  The declared content digest and the
        runtime-visible mount shape identify the environment across checkouts.
        """

        return {
            "id": self.id,
            "bmp_version": self.bmp_version,
            "python_version": self.python_version,
            "packages": list(self.packages),
            "env_var_names": list(self.env_var_names),
            "mounts": [mount.identity_data() for mount in self.mounts],
            "build_timeout_seconds": self.build_timeout_seconds,
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PackageRecord(StrictModel):
    """Installed package name/version observed by an environment builder.

    A package wheel hash is not yet verified by the environment builder; add it
    when the builder provides content-addressed install receipts.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class EnvironmentReceipt(StrictModel):
    """Observed environment identity recorded in execution provenance."""

    spec_id: str = Field(pattern=ID_PATTERN)
    spec_digest: str = Field(pattern=SHA256_PATTERN)
    python_executable: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    installed_packages: tuple[PackageRecord, ...]
    build_duration_seconds: float = Field(ge=0, strict=True)
    built_at: str = Field(min_length=1)

    @field_validator("python_executable")
    @classmethod
    def executable_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("python_executable must be an absolute path")
        return value

    @field_validator("built_at")
    @classmethod
    def built_at_must_be_timezone_aware_iso8601(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("built_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("built_at must include a timezone")
        return value

    @model_validator(mode="after")
    def package_names_are_unique(self) -> "EnvironmentReceipt":
        normalized = [package.name.casefold() for package in self.installed_packages]
        if len(set(normalized)) != len(normalized):
            raise ValueError("installed package names must be unique")
        return self


class BackendSpec(RegistryEntry):
    """Pinned execution backend registry entry."""

    image: str | None = Field(default=None, min_length=1)
    executable: str | None = Field(default=None, min_length=1)
    version: str | None = Field(default=None, min_length=1)
    digest: str | None = Field(default=None, min_length=1)
    # Warning: never use for API keys or secrets; names only.
    defaults: Mapping[str, Any] = Field(default_factory=dict)
    environment: EnvironmentSpec | None = None

    @field_validator("defaults", mode="before")
    @classmethod
    def defaults_reject_secret_keys(cls, value: Any) -> Any:
        return _reject_secret_like_keys(value, field_name="BackendSpec.defaults")

    @model_validator(mode="after")
    def adapter_fields_match_read_set(self) -> "BackendSpec":
        expected_kind = {
            "harbor": "local",
            "harbor-shim": "local",
            "subprocess": "local",
            "fake": "local",
            "aose-docker": "container",
        }.get(self.adapter)
        if expected_kind is not None and self.kind != expected_kind:
            raise ValueError(
                f"backend adapter {self.adapter!r} requires kind={expected_kind!r}"
            )
        if self.adapter in {"harbor", "harbor-shim"}:
            if self.executable is None or self.version is None or self.digest is None:
                raise ValueError("harbor requires executable, version, and digest")
            if self.image is not None:
                raise ValueError("harbor forbids backend image; task identity owns images")
            if self.adapter == "harbor" and re.fullmatch(SHA256_PATTERN, self.digest) is None:
                raise ValueError("harbor digest must be lowercase SHA-256")
        elif self.adapter == "subprocess":
            if self.executable is None or self.digest is None:
                raise ValueError("subprocess requires executable and digest")
            if re.fullmatch(SHA256_PATTERN, self.digest) is None:
                raise ValueError("subprocess digest must be lowercase SHA-256")
            if self.image is not None or self.version is not None:
                raise ValueError("subprocess forbids image and version")
        elif self.adapter == "aose-docker":
            if self.image is None or self.digest is None:
                raise ValueError("aose-docker requires image and digest")
            if re.fullmatch(OCI_SHA256_PATTERN, self.image) is None or re.fullmatch(SHA256_PATTERN, self.digest) is None:
                raise ValueError("aose-docker image/digest must be lowercase SHA-256")
            if self.executable is not None or self.version is not None:
                raise ValueError("aose-docker forbids executable and version")
            if self.image.removeprefix("sha256:") != self.digest:
                raise ValueError("aose-docker image and digest must identify the same image")
        elif self.adapter == "fake":
            if any(
                value is not None
                for value in (self.image, self.executable, self.version, self.digest)
            ):
                raise ValueError("fake forbids image, executable, version, and digest")
        return self


def _validate_logical_relative_path(value: str, *, field_name: str) -> str:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"{field_name} must be a normalized relative path")
    return normalized


def _validate_execution_model_name(value: str) -> str:
    if value.startswith("none/") and value not in {
        "none/deterministic",
        "none/echo",
    }:
        raise ValueError(
            "model none/* suffix must be one of none/deterministic or none/echo"
        )
    return value


ProtocolKind = Literal[
    "test_time_scaling",
    "mechanism_validation",
    "benchmark_evaluation",
]


class CaseOrderArtifact(StrictModel):
    """Portable ordered case selection consumed by the custom order adapter."""

    schema_version: Literal["bmp.case-order.v1"] = "bmp.case-order.v1"
    ordered_case_ids: tuple[str, ...]

    @field_validator("ordered_case_ids")
    @classmethod
    def ordered_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("custom case-order artifact must select at least one case")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("custom case-order artifact contains an invalid BMP id")
        if len(set(values)) != len(values):
            raise ValueError("custom case-order artifact ids must be unique")
        return values


class CustomCaseOrderSpec(StrictModel):
    """Content-addressed external strategy input for ``case_order=custom``."""

    adapter: str = Field(
        default="magentabench.case-order.json.v1",
        pattern=ADAPTER_PATTERN,
    )
    source: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)


class ExperimentRegimeKind(str, Enum):
    """Research setting orthogonal to the four comparison subjects."""

    iid_evaluation = "iid_evaluation"
    repeated_sampling = "repeated_sampling"
    generalization = "generalization"
    cross_domain_transfer = "cross_domain_transfer"
    continual_learning = "continual_learning"
    curriculum = "curriculum"
    online_adaptation = "online_adaptation"
    robustness_stress = "robustness_stress"
    evolutionary_search = "evolutionary_search"
    meta_evolution = "meta_evolution"


class ExperimentStageRole(str, Enum):
    train = "train"
    adapt = "adapt"
    search = "search"
    validation = "validation"
    selection = "selection"
    evaluate = "evaluate"
    holdout = "holdout"
    stress = "stress"


class StageStatePolicy(str, Enum):
    reset = "reset"
    carry = "carry"
    fork = "fork"
    read_only = "read_only"


class StageFeedbackVisibility(str, Enum):
    none = "none"
    aggregate_only = "aggregate_only"
    evaluator_feedback = "evaluator_feedback"
    full_trajectory = "full_trajectory"


class ExperimentStageSpec(StrictModel):
    """One node in a pre-registered experiment-stage DAG."""

    id: str = Field(pattern=ID_PATTERN)
    role: ExperimentStageRole
    predecessors: tuple[str, ...] = ()
    benchmark_id: str = Field(pattern=ID_PATTERN)
    dataset_id: str = Field(pattern=ID_PATTERN)
    evaluator_id: str = Field(pattern=ID_PATTERN)
    protocol_id: str = Field(pattern=ID_PATTERN)
    metric_ids: tuple[str, ...]
    domains: tuple[str, ...] = ()
    state_policy: StageStatePolicy
    feedback_visibility: StageFeedbackVisibility
    sealed: bool = False
    budget: Budget | None = None
    evaluation_cadence: int = Field(default=1, ge=1, strict=True)

    @field_validator("predecessors", "metric_ids", "domains")
    @classmethod
    def stage_lists_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("experiment stage lists must contain unique values")
        if any(not value.strip() for value in values):
            raise ValueError("experiment stage list values must be non-empty")
        return values

    @model_validator(mode="after")
    def stage_boundary_is_coherent(self) -> "ExperimentStageSpec":
        if not self.metric_ids:
            raise ValueError("experiment stage requires metric_ids")
        if self.id in self.predecessors:
            raise ValueError("experiment stage cannot depend on itself")
        if self.sealed:
            if self.role != ExperimentStageRole.holdout:
                raise ValueError("only holdout stages may be sealed")
            if self.feedback_visibility != StageFeedbackVisibility.none:
                raise ValueError("sealed holdout forbids feedback visibility")
            if self.state_policy != StageStatePolicy.read_only:
                raise ValueError("sealed holdout must use read_only state")
        if self.role == ExperimentStageRole.holdout and not self.sealed:
            raise ValueError("holdout stages must be sealed")
        if self.state_policy == StageStatePolicy.fork and len(self.predecessors) != 1:
            raise ValueError("fork state requires exactly one predecessor")
        if self.state_policy in {StageStatePolicy.carry, StageStatePolicy.read_only} and (
            not self.predecessors
        ):
            raise ValueError("carry/read_only state requires a predecessor")
        return self


class ExperimentRegimeSpec(RegistryEntry):
    """TOML authority for multi-stage research settings.

    The DAG expresses generalization, transfer, continual learning, curriculum,
    online adaptation, robustness, evolution, and meta-evolution without
    introducing another semantic comparison kind.
    """

    kind: Literal["regime"]
    regime_kind: ExperimentRegimeKind
    stages: tuple[ExperimentStageSpec, ...]

    @model_validator(mode="after")
    def regime_dag_is_closed(self) -> "ExperimentRegimeSpec":
        if not self.stages:
            raise ValueError("experiment regime requires at least one stage")
        stage_by_id = {stage.id: stage for stage in self.stages}
        if len(stage_by_id) != len(self.stages):
            raise ValueError("experiment regime stage ids must be unique")
        position = {stage.id: index for index, stage in enumerate(self.stages)}
        for stage in self.stages:
            unknown = sorted(set(stage.predecessors) - set(stage_by_id))
            if unknown:
                raise ValueError(
                    f"experiment stage {stage.id!r} has unknown predecessors: {unknown}"
                )
            if any(position[parent] >= position[stage.id] for parent in stage.predecessors):
                raise ValueError(
                    "experiment stages must be topologically ordered by predecessors"
                )
        holdout_indexes = [
            index
            for index, stage in enumerate(self.stages)
            if stage.role == ExperimentStageRole.holdout
        ]
        if holdout_indexes and any(
            later.role in {
                ExperimentStageRole.train,
                ExperimentStageRole.adapt,
                ExperimentStageRole.search,
                ExperimentStageRole.selection,
                ExperimentStageRole.validation,
            }
            for index in holdout_indexes
            for later in self.stages[index + 1 :]
        ):
            raise ValueError(
                "sealed holdout must not precede train/adapt/search/selection/validation"
            )
        if self.regime_kind in {
            ExperimentRegimeKind.generalization,
            ExperimentRegimeKind.cross_domain_transfer,
        } and not holdout_indexes:
            raise ValueError("generalization/transfer regimes require a sealed holdout")
        if self.regime_kind == ExperimentRegimeKind.continual_learning:
            learning_stages = [
                stage
                for stage in self.stages
                if stage.role
                in {ExperimentStageRole.train, ExperimentStageRole.adapt}
            ]
            if len(learning_stages) < 2 or not any(stage.domains for stage in self.stages):
                raise ValueError(
                    "continual learning requires at least two learning stages and domains"
                )
        if self.regime_kind == ExperimentRegimeKind.evolutionary_search and not any(
            stage.role == ExperimentStageRole.search for stage in self.stages
        ):
            raise ValueError("evolutionary search requires a search stage")
        if self.regime_kind == ExperimentRegimeKind.meta_evolution and not any(
            stage.role == ExperimentStageRole.search for stage in self.stages
        ):
            raise ValueError("meta-evolution requires a search stage")
        return self


class RegimeDependencyArtifact(StrictModel):
    registry_kind: Literal[
        "benchmark", "dataset", "evaluator", "metric", "protocol"
    ]
    id: str = Field(pattern=ID_PATTERN)
    declaration_ref: ArtifactRef
    artifact_digest: str = Field(pattern=SHA256_PATTERN)


class ExperimentRegimeArtifact(StrictModel):
    regime: ExperimentRegimeSpec
    declaration_ref: ArtifactRef
    dependencies: tuple[RegimeDependencyArtifact, ...]
    artifact_digest: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def dependency_closure_matches_stages(self) -> "ExperimentRegimeArtifact":
        expected = {
            ("benchmark", stage.benchmark_id)
            for stage in self.regime.stages
        } | {
            ("dataset", stage.dataset_id)
            for stage in self.regime.stages
        } | {
            ("evaluator", stage.evaluator_id)
            for stage in self.regime.stages
        } | {
            ("metric", metric_id)
            for stage in self.regime.stages
            for metric_id in stage.metric_ids
        } | {
            ("protocol", stage.protocol_id)
            for stage in self.regime.stages
        }
        observed = {
            (dependency.registry_kind, dependency.id)
            for dependency in self.dependencies
        }
        if observed != expected:
            raise ValueError(
                "experiment regime dependency closure differs from stage references"
            )
        if len(observed) != len(self.dependencies):
            raise ValueError("experiment regime dependencies must be unique")
        dependency_keys = [
            (dependency.registry_kind, dependency.id)
            for dependency in self.dependencies
        ]
        if dependency_keys != sorted(dependency_keys):
            raise ValueError("experiment regime dependencies must be sorted")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "regime": self.regime.model_dump(mode="json"),
            "declaration_ref": self.declaration_ref.identity_data(),
            "dependencies": [
                {
                    "registry_kind": dependency.registry_kind,
                    "id": dependency.id,
                    "declaration_ref": dependency.declaration_ref.identity_data(),
                    "artifact_digest": dependency.artifact_digest,
                }
                for dependency in self.dependencies
            ],
        }

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ProtocolSpec(RegistryEntry):
    """Execution schedule defaults resolved before local execution overrides."""

    kind: ProtocolKind
    rollouts_per_case: int = Field(default=1, ge=1)
    parallelism: int = Field(default=1, ge=1)
    case_order: Literal[
        "fixed", "seeded_random", "random", "custom", "explicit"
    ] = "fixed"
    explicit_case_ids: tuple[str, ...] = ()
    custom_order: CustomCaseOrderSpec | None = None
    adaptive_budget: bool = False
    candidate_selection: Literal["single", "exact", "best_of_n"]
    state_reset: Literal["per_case", "per_rollout", "never"] = "per_case"
    budget: Budget | None = None
    checkpoint_policy: Literal["disabled", "save", "resume", "save_and_resume"] = "disabled"
    deterministic_conformance: bool = False

    @field_validator("explicit_case_ids")
    @classmethod
    def explicit_ids_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("explicit case ids must be valid BMP ids")
        if len(set(values)) != len(values):
            raise ValueError("explicit case ids must be unique")
        return values

    @model_validator(mode="after")
    def explicit_ids_match_order_policy(self) -> "ProtocolSpec":
        if self.case_order == "explicit" and not self.explicit_case_ids:
            raise ValueError(
                "explicit_case_ids must be non-empty when case_order is explicit"
            )
        if self.case_order != "explicit" and self.explicit_case_ids:
            raise ValueError(
                "explicit_case_ids are forbidden unless case_order is explicit"
            )
        if self.case_order == "custom" and self.custom_order is None:
            raise ValueError("custom_order is required when case_order is custom")
        if self.case_order != "custom" and self.custom_order is not None:
            raise ValueError("custom_order is forbidden unless case_order is custom")
        return self

    def identity_data(self) -> dict[str, Any]:
        """Return a relocatable protocol identity projection.

        A custom-order source path is provenance, while its declared adapter,
        byte digest, and size are protocol identity.  Compilation may resolve
        the source to an absolute path without changing the registered
        protocol's identity.
        """

        data = self.model_dump(mode="json")
        if self.custom_order is not None:
            data["custom_order"] = {
                "adapter": self.custom_order.adapter,
                "sha256": self.custom_order.sha256,
                "size_bytes": self.custom_order.size_bytes,
            }
        return data


class ExecutionSpec(StrictModel):
    """TOML execution declaration: registry references plus local overrides."""

    backend: str = Field(pattern=ID_PATTERN)
    model: str = Field(min_length=1)
    provider_binding: ProviderBinding | None = None
    seed: int | None = None
    budget: Budget | None = None

    @field_validator("model")
    @classmethod
    def model_name_is_closed(cls, value: str) -> str:
        return _validate_execution_model_name(value)

    # Warning: never use for API keys or secrets; names only.
    backend_overrides: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("backend_overrides", mode="before")
    @classmethod
    def overrides_reject_secret_keys(cls, value: Any) -> Any:
        return _reject_secret_like_keys(
            value,
            field_name="ExecutionSpec.backend_overrides",
        )

    @model_validator(mode="after")
    def provider_binding_matches_model(self) -> "ExecutionSpec":
        if self.provider_binding is None:
            return self
        if self.model in {"none", "none/deterministic", "none/echo"}:
            raise ValueError("none-model execution forbids provider_binding")
        if self.provider_binding.model_id != self.model:
            raise ValueError("provider_binding.model_id must equal execution.model")
        return self


class ResolvedExecutionSpec(StrictModel):
    """Execution contract with registry references inlined.

    ``provider_binding`` is absent for the closed ``none/*`` execution modes.
    It is optional for an exploratory real-model run so missing provider
    identity can be recorded explicitly, but a positive runtime result requires
    it plus a matching :class:`ModelActivationReceipt`.
    """

    backend: BackendSpec
    model: str = Field(min_length=1)
    provider_binding: ProviderBinding | None = None
    seed: int | None = None
    budget: Budget
    protocol: ProtocolSpec | None = None

    @field_validator("model")
    @classmethod
    def model_name_is_closed(cls, value: str) -> str:
        return _validate_execution_model_name(value)

    @model_validator(mode="after")
    def seed_matches_case_order(self) -> "ResolvedExecutionSpec":
        case_order = None if self.protocol is None else self.protocol.case_order
        if case_order == "seeded_random" and self.seed is None:
            raise ValueError("seed is required when case_order=seeded_random")
        if case_order != "seeded_random" and self.seed is not None:
            raise ValueError("seed is forbidden unless case_order=seeded_random")
        if self.provider_binding is not None:
            if self.model in {"none", "none/deterministic", "none/echo"}:
                raise ValueError("none-model execution forbids provider_binding")
            if self.provider_binding.model_id != self.model:
                raise ValueError("provider_binding.model_id must equal execution.model")
        return self


class CaseArtifact(StrictModel):
    case_id: str = Field(pattern=ID_PATTERN)
    public_input_ref: ArtifactRef
    task_contract_refs: tuple[ArtifactRef, ...] = ()
    verifier_contract_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def refs_are_unique(self) -> "CaseArtifact":
        refs = (
            self.public_input_ref,
            *self.task_contract_refs,
            *self.verifier_contract_refs,
        )
        # Distinct contract files can share bytes (for example a fixture
        # copied into both the environment and verifier trees).  Preserve
        # each path in the activated contract closure; only an identical
        # reference at the same path is a duplicate.
        identities = [(ref.path, ref.sha256, ref.size_bytes) for ref in refs]
        if len(set(identities)) != len(identities):
            raise ValueError("case artifact refs must be content-unique")
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "public_input_ref": self.public_input_ref.identity_data(),
            "task_contract_refs": [
                ref.identity_data() for ref in self.task_contract_refs
            ],
            "verifier_contract_refs": [
                ref.identity_data() for ref in self.verifier_contract_refs
            ],
        }


class CaseSetArtifact(StrictModel):
    benchmark_id: str = Field(pattern=ID_PATTERN)
    benchmark_digest: str = Field(pattern=SHA256_PATTERN)
    dataset_id: str | None = Field(default=None, pattern=ID_PATTERN)
    dataset_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    loader_adapter: str = Field(pattern=ADAPTER_PATTERN)
    loader_digest: str = Field(pattern=SHA256_PATTERN)
    selection_method: Literal[
        "all_cases", "explicit_case_ids", "custom_order_artifact"
    ] = "all_cases"
    case_order: Literal[
        "fixed", "seeded_random", "random", "custom", "explicit"
    ] = "fixed"
    order_seed: int | None = None
    order_strategy_adapter: str | None = Field(
        default=None,
        pattern=ADAPTER_PATTERN,
    )
    order_strategy_ref: ArtifactRef | None = None
    source_content_digest: str = Field(pattern=SHA256_PATTERN)
    source_content_refs: tuple[ArtifactRef, ...]
    ordered_case_ids: tuple[str, ...]
    cases: tuple[CaseArtifact, ...]

    @model_validator(mode="after")
    def order_exactly_matches_cases(self) -> "CaseSetArtifact":
        if (self.dataset_id is None) != (self.dataset_digest is None):
            raise ValueError("case set dataset id and digest must be present together")
        case_ids = tuple(case.case_id for case in self.cases)
        if not case_ids:
            raise ValueError("case set must contain at least one case")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case set ids must be unique")
        if self.ordered_case_ids != case_ids:
            raise ValueError("ordered_case_ids must exactly match case order")
        if self.case_order == "seeded_random" and self.order_seed is None:
            raise ValueError("order_seed is required for seeded_random case order")
        if self.case_order != "seeded_random" and self.order_seed is not None:
            raise ValueError("order_seed is forbidden for non-seeded case order")
        if self.case_order == "explicit" and self.selection_method != "explicit_case_ids":
            raise ValueError(
                "explicit case order requires selection_method=explicit_case_ids"
            )
        if (
            self.case_order != "explicit"
            and self.selection_method == "explicit_case_ids"
        ):
            raise ValueError(
                "selection_method=explicit_case_ids requires explicit case order"
            )
        if self.case_order == "custom":
            if self.selection_method != "custom_order_artifact":
                raise ValueError(
                    "custom case order requires selection_method=custom_order_artifact"
                )
            if self.order_strategy_adapter is None or self.order_strategy_ref is None:
                raise ValueError(
                    "custom case order requires an adapter and content reference"
                )
        elif self.order_strategy_adapter is not None or self.order_strategy_ref is not None:
            raise ValueError(
                "order strategy adapter/ref are forbidden for non-custom case order"
            )
        # Different source files may legitimately have identical bytes (for
        # example shared Dockerfiles).  Preserve every path in the source
        # closure; only the same content reference at the same path is a
        # duplicate.  The closure digest still binds path plus bytes.
        source_identities = [
            (ref.path, ref.sha256, ref.size_bytes) for ref in self.source_content_refs
        ]
        if (
            not source_identities
            or len(set(source_identities)) != len(source_identities)
        ):
            raise ValueError(
                "source content refs must be non-empty and content-unique"
            )
        return self

    def identity_data(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "benchmark_digest": self.benchmark_digest,
            "dataset_id": self.dataset_id,
            "dataset_digest": self.dataset_digest,
            "loader_adapter": self.loader_adapter,
            "loader_digest": self.loader_digest,
            "selection_method": self.selection_method,
            "case_order": self.case_order,
            "order_seed": self.order_seed,
            "order_strategy_adapter": self.order_strategy_adapter,
            "order_strategy_ref": (
                None
                if self.order_strategy_ref is None
                else self.order_strategy_ref.identity_data()
            ),
            "source_content_digest": self.source_content_digest,
            "source_content_refs": [
                ref.identity_data() for ref in self.source_content_refs
            ],
            "ordered_case_ids": list(self.ordered_case_ids),
            "cases": [case.identity_data() for case in self.cases],
        }

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CaseSetActivationReceipt(StrictModel):
    case_set_ref: ArtifactRef
    case_set_digest: str = Field(pattern=SHA256_PATTERN)
    loader_adapter: str = Field(pattern=ADAPTER_PATTERN)
    loader_digest: str = Field(pattern=SHA256_PATTERN)
    dataset_id: str | None = Field(default=None, pattern=ID_PATTERN)
    dataset_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    ordered_case_ids: tuple[str, ...]

    @field_validator("ordered_case_ids")
    @classmethod
    def ordered_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("activated case ids must be non-empty and unique")
        return value

    @model_validator(mode="after")
    def dataset_binding_is_complete(self) -> "CaseSetActivationReceipt":
        if (self.dataset_id is None) != (self.dataset_digest is None):
            raise ValueError(
                "case-set activation dataset id and digest must be present together"
            )
        return self


class RunPurpose(str, Enum):
    exploratory = "exploratory"
    claim = "claim"


SUBJECT_KIND_COMPARISON_MATRIX: Mapping[
    str, frozenset[ComparisonKind]
] = MappingProxyType(
    {
        "hcp_harness": frozenset(
            {ComparisonKind.agent, ComparisonKind.coding_agent}
        ),
        "opaque_agent": frozenset(
            {ComparisonKind.agent, ComparisonKind.coding_agent}
        ),
        "evolver": frozenset({ComparisonKind.evolution_method}),
        "meta_evolver": frozenset({ComparisonKind.meta_evolution_method}),
        # Fake is an execution conformance fixture, not a fifth research subject.
        "fake": frozenset(),
    }
)


class FactorCategory(str, Enum):
    """Closed semantic classes for registered intervention/nuisance factors."""

    subject_identity = "subject_identity"
    agent_component = "agent_component"
    model = "model"
    model_checkpoint = "model_checkpoint"
    configuration = "configuration"
    hyperparameter = "hyperparameter"
    dataset = "dataset"
    protocol = "protocol"
    schedule = "schedule"
    backend = "backend"
    repetition = "repetition"
    conformance_fixture = "conformance_fixture"


class FactorActivationEvidence(str, Enum):
    subject_activation = "subject_activation"
    component_activation = "component_activation"
    model_activation = "model_activation"
    configuration_activation = "configuration_activation"
    dataset_activation = "dataset_activation"
    schedule_activation = "schedule_activation"
    backend_activation = "backend_activation"
    none = "none"


_FACTOR_EVIDENCE_BY_CATEGORY: Mapping[
    FactorCategory, FactorActivationEvidence
] = MappingProxyType(
    {
        FactorCategory.subject_identity: FactorActivationEvidence.subject_activation,
        FactorCategory.agent_component: FactorActivationEvidence.component_activation,
        FactorCategory.model: FactorActivationEvidence.model_activation,
        FactorCategory.model_checkpoint: FactorActivationEvidence.model_activation,
        FactorCategory.configuration: FactorActivationEvidence.configuration_activation,
        FactorCategory.hyperparameter: FactorActivationEvidence.configuration_activation,
        FactorCategory.dataset: FactorActivationEvidence.dataset_activation,
        FactorCategory.protocol: FactorActivationEvidence.schedule_activation,
        FactorCategory.schedule: FactorActivationEvidence.schedule_activation,
        FactorCategory.backend: FactorActivationEvidence.backend_activation,
        FactorCategory.repetition: FactorActivationEvidence.none,
        FactorCategory.conformance_fixture: FactorActivationEvidence.none,
    }
)


class FactorLevel(StrictModel):
    """One named, TOML-registered value in a factor domain."""

    id: str = Field(pattern=ID_PATTERN)
    value: Any

    @field_validator("value", mode="before")
    @classmethod
    def value_is_secret_free_json(cls, value: Any) -> Any:
        _reject_secret_like_keys(value, field_name="FactorLevel.value")
        try:
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("factor level value must be JSON-compatible") from exc
        return _freeze_configuration_tree(value)


class FactorSpec(RegistryEntry):
    """Single source of truth for a sweep axis and its isolation contract.

    A factor declaration replaces the former four-way repetition across
    ``ClaimScope``, ``factor_path``, ``allowed_diff`` and ``vary``.  The
    compiler derives every arm and allowed resolved diff from this object.
    """

    kind: Literal["factor"]
    category: FactorCategory
    selector_path: str
    applies_to: tuple[ComparisonKind, ...]
    levels: tuple[FactorLevel, ...]
    resolved_diff_paths: tuple[str, ...] = ()
    activation_evidence: FactorActivationEvidence
    value_registry: Literal[
        "subject",
        "dataset",
        "protocol",
        "backend",
        "configuration",
        "evolver",
        "meta_evolver",
    ] | None = None
    metadata_only: bool = False

    @field_validator("selector_path")
    @classmethod
    def selector_path_is_normalized(cls, value: str) -> str:
        if re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_.-]*$", value) is None:
            raise ValueError("factor selector_path must be a normalized dotted path")
        return value

    @field_validator("resolved_diff_paths")
    @classmethod
    def diff_paths_are_normalized_and_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("factor resolved_diff_paths must be unique")
        if any(
            re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_.-]*$", value) is None
            for value in values
        ):
            raise ValueError(
                "factor resolved_diff_paths must be normalized dotted paths"
            )
        return values

    @model_validator(mode="after")
    def factor_contract_is_closed(self) -> "FactorSpec":
        if not self.levels:
            raise ValueError("factor levels must be non-empty")
        level_ids = [level.id for level in self.levels]
        if len(set(level_ids)) != len(level_ids):
            raise ValueError("factor level ids must be unique")
        encoded_values = [
            json.dumps(
                level.value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for level in self.levels
        ]
        if len(set(encoded_values)) != len(encoded_values):
            raise ValueError("factor level values must be unique")
        if len(set(self.applies_to)) != len(self.applies_to):
            raise ValueError("factor applies_to values must be unique")
        expected_evidence = _FACTOR_EVIDENCE_BY_CATEGORY[self.category]
        if self.activation_evidence != expected_evidence:
            raise ValueError(
                f"factor category {self.category.value!r} requires "
                f"activation_evidence={expected_evidence.value!r}"
            )
        if self.category == FactorCategory.repetition:
            if not self.metadata_only or self.resolved_diff_paths or self.applies_to:
                raise ValueError(
                    "repetition factors must be metadata-only with no diffs or subject"
                )
        elif self.category == FactorCategory.conformance_fixture:
            if self.metadata_only or self.applies_to or not self.resolved_diff_paths:
                raise ValueError(
                    "conformance_fixture factors require diffs and no research subject"
                )
        else:
            if self.metadata_only:
                raise ValueError("research factors cannot be metadata-only")
            if not self.applies_to:
                raise ValueError("research factors require applies_to")
            if not self.resolved_diff_paths:
                raise ValueError("research factors require resolved_diff_paths")
        if self.value_registry is not None and any(
            not isinstance(level.value, str) for level in self.levels
        ):
            raise ValueError("registry-backed factor levels must contain string ids")
        return self

    def level(self, level_id: str) -> FactorLevel:
        try:
            return next(level for level in self.levels if level.id == level_id)
        except StopIteration as exc:
            raise ValueError(
                f"factor {self.id!r} has no level {level_id!r}"
            ) from exc


class FactorArtifact(StrictModel):
    factor: FactorSpec
    declaration_ref: ArtifactRef
    artifact_digest: str = Field(pattern=SHA256_PATTERN)

    def identity_data(self) -> dict[str, Any]:
        return {
            "factor": self.factor.model_dump(mode="json"),
            "declaration_ref": self.declaration_ref.identity_data(),
        }

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ExperimentContrast(StrictModel):
    mode: Literal["one_factor", "all_arms"]
    design: Literal["direct", "ablation"] = "direct"
    factor_id: str | None = Field(default=None, pattern=ID_PATTERN)
    control_level: str | None = Field(default=None, pattern=ID_PATTERN)
    treatment_level: str | None = Field(default=None, pattern=ID_PATTERN)
    counterbalanced: bool

    @model_validator(mode="after")
    def contrast_shape_matches_mode(self) -> "ExperimentContrast":
        if self.mode == "one_factor":
            if (
                self.factor_id is None
                or self.control_level is None
                or self.treatment_level is None
            ):
                raise ValueError(
                    "one_factor contrast requires factor_id and two registered levels"
                )
            if self.control_level == self.treatment_level:
                raise ValueError("control_level and treatment_level must be distinct")
        else:
            if (
                self.factor_id is not None
                or self.control_level is not None
                or self.treatment_level is not None
            ):
                raise ValueError(
                    "all_arms contrast forbids control/treatment filtering and factor_id"
                )
            if self.counterbalanced:
                raise ValueError("all_arms contrast cannot be counterbalanced")
        if self.design == "ablation" and self.mode != "one_factor":
            raise ValueError("ablation requires a one_factor contrast")
        return self


class StatisticalAnalysisPlan(StrictModel):
    """Pre-registered paired analysis contract carried by every arm.

    ``paired_unit`` names the complete factor key used to match one control
    observation with one treatment observation. ``case_id`` is the runtime
    case identity; all other names refer to ``ResolvedManifestMetadata.factors``.
    The implementation is intentionally small and versioned so a standalone
    verifier can reproduce the exact variance and interval calculation.
    """

    format: Literal["bmp-statistical-analysis-plan-v1"] = (
        "bmp-statistical-analysis-plan-v1"
    )
    paired_unit: tuple[str, ...] = ("case_id", "repetition")
    repetition_field: Literal["repetition"] = "repetition"
    minimum_repetitions: int = Field(default=2, ge=2, strict=True)
    variance_method: Literal["sample_variance_v1"] = "sample_variance_v1"
    ci_method: Literal["normal_approximation_v1"] = "normal_approximation_v1"
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1.0, strict=True)
    holdout_required: bool = True
    holdout_split: str | None = Field(default=None, pattern=ID_PATTERN)
    multiple_comparison_method: Literal["none", "bonferroni"] = "none"
    family_size: int = Field(default=1, ge=1, strict=True)
    family_id: str | None = Field(default=None, pattern=ID_PATTERN)

    @field_validator("paired_unit")
    @classmethod
    def paired_unit_is_complete_and_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        pattern = re.compile(r"^(?:case_id|[A-Za-z_][A-Za-z0-9_.-]*)$")
        if not values or len(set(values)) != len(values):
            raise ValueError("paired_unit must be non-empty and unique")
        if any(pattern.fullmatch(value) is None for value in values):
            raise ValueError("paired_unit contains an invalid factor name")
        if "case_id" not in values:
            raise ValueError("paired_unit must include case_id")
        return values

    @model_validator(mode="after")
    def plan_shape_is_coherent(self) -> "StatisticalAnalysisPlan":
        if self.repetition_field not in self.paired_unit:
            raise ValueError("paired_unit must include repetition_field")
        if self.holdout_required != (self.holdout_split is not None):
            raise ValueError(
                "holdout_required=true requires holdout_split and false forbids it"
            )
        if self.family_size == 1:
            if self.multiple_comparison_method != "none" or self.family_id is not None:
                raise ValueError(
                    "a single comparison requires method='none' and no family_id"
                )
        elif (
            self.multiple_comparison_method != "bonferroni"
            or self.family_id is None
        ):
            raise ValueError(
                "multiple comparisons require Bonferroni and a family_id"
            )
        return self

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ClaimDesign(StrictModel):
    """Orthogonal research subject, registered intervention, and purpose."""

    comparison_kind: ComparisonKind | None = None
    purpose: RunPurpose
    intervention_factor_id: str | None = Field(default=None, pattern=ID_PATTERN)
    statistical_analysis: StatisticalAnalysisPlan | None = None

    @model_validator(mode="after")
    def comparison_and_purpose_are_coherent(self) -> "ClaimDesign":
        if self.comparison_kind is None:
            if self.purpose != RunPurpose.exploratory:
                raise ValueError("claim purpose requires one of the four comparison kinds")
        elif self.purpose == RunPurpose.claim and self.intervention_factor_id is None:
            raise ValueError("claim purpose requires a registered intervention factor")
        return self


class TestOverrideReceipt(StrictModel):
    reason: str = Field(min_length=1)
    forced_purpose: Literal["exploratory"] = "exploratory"
    forced_comparison_kind: None = None


class ResolvedManifestMetadata(StrictModel):
    experiment_id: str = Field(pattern=ID_PATTERN)
    run_id: str = Field(pattern=ID_PATTERN)
    regime_stage_id: str | None = Field(default=None, pattern=ID_PATTERN)
    allowed_diff: tuple[str, ...] = ()
    factors: Mapping[str, Any] = Field(default_factory=dict)
    factor_artifacts: tuple[FactorArtifact, ...] = ()
    configuration: ConfigurationArtifact | None = None
    evolver: EvolutionMethodArtifact | None = None
    meta_evolver: MetaEvolutionMethodArtifact | None = None
    adapter_capabilities: tuple[AdapterCapabilityArtifact, ...] = ()
    test_override: TestOverrideReceipt | None = None

    @model_validator(mode="after")
    def factor_artifacts_are_unique(self) -> "ResolvedManifestMetadata":
        ids = [artifact.factor.id for artifact in self.factor_artifacts]
        if len(set(ids)) != len(ids):
            raise ValueError("resolved factor artifacts must have unique ids")
        digests = [artifact.artifact_digest for artifact in self.factor_artifacts]
        if len(set(digests)) != len(digests):
            raise ValueError("resolved factor artifacts must be content-unique")
        if self.meta_evolver is not None:
            if self.evolver is None:
                raise ValueError("a resolved meta-evolver requires its parent evolver")
            if self.meta_evolver.parent_evolver_id != self.evolver.id:
                raise ValueError("meta-evolver parent id differs from resolved evolver")
            if self.meta_evolver.parent_evolver_digest != self.evolver.artifact_digest:
                raise ValueError("meta-evolver parent digest differs from resolved evolver")
        for label, artifact in (
            ("evolver", self.evolver),
            ("meta-evolver", self.meta_evolver),
        ):
            if artifact is not None and artifact.canonical_digest() != artifact.artifact_digest:
                raise ValueError(f"resolved {label} artifact digest drift")
        return self

    @field_validator("allowed_diff")
    @classmethod
    def validate_dotted_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        dotted_path = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*$")
        if len(set(value)) != len(value):
            raise ValueError("allowed_diff paths must be unique")
        invalid = [path for path in value if not dotted_path.fullmatch(path)]
        if invalid:
            raise ValueError(f"invalid dotted allowed_diff paths: {invalid}")
        return value


class ResolvedBmpManifest(StrictModel):
    IDENTITY_EXCLUDE: ClassVar[frozenset[str]] = IDENTITY_EXCLUDE

    bmp_version: Literal["0.1"] = BMP_VERSION
    benchmark: BenchmarkArtifact
    dataset: DatasetArtifact
    evaluator: EvaluatorArtifact
    metrics: tuple[MetricArtifact, ...]
    regime: ExperimentRegimeArtifact | None = None
    subject: SubjectArtifact
    execution: ResolvedExecutionSpec
    claim_design: ClaimDesign
    contrast: ExperimentContrast
    metadata: ResolvedManifestMetadata
    created_at: str | None = None
    wall_clock_start: str | None = None
    wall_clock_end: str | None = None
    record_root: str | None = None
    resume_count: int = Field(default=0, ge=0)
    runner_invocation_id: str | None = None

    @model_validator(mode="after")
    def active_regime_stage_matches_manifest(self) -> "ResolvedBmpManifest":
        stage_id = self.metadata.regime_stage_id
        if self.regime is None:
            if stage_id is not None:
                raise ValueError("regime_stage_id requires a resolved regime artifact")
            return self
        if self.regime.canonical_digest() != self.regime.artifact_digest:
            raise ValueError("resolved regime artifact digest drift")
        if stage_id is None:
            raise ValueError("resolved regime artifact requires regime_stage_id")
        try:
            stage = next(
                item for item in self.regime.regime.stages if item.id == stage_id
            )
        except StopIteration as exc:
            raise ValueError("regime_stage_id is absent from the resolved regime") from exc
        protocol = self.execution.protocol
        if protocol is None:
            raise ValueError("an active regime stage requires a resolved protocol")
        observed = {
            "benchmark": self.benchmark.id,
            "dataset": self.dataset.id,
            "evaluator": self.evaluator.evaluator.id,
            "protocol": protocol.id,
        }
        expected = {
            "benchmark": stage.benchmark_id,
            "dataset": stage.dataset_id,
            "evaluator": stage.evaluator_id,
            "protocol": stage.protocol_id,
        }
        if observed != expected:
            raise ValueError("active manifest registry ids differ from regime stage")
        if tuple(artifact.metric.id for artifact in self.metrics) != stage.metric_ids:
            raise ValueError(
                "active manifest metric order differs from the regime stage declaration"
            )
        dependencies = {
            (dependency.registry_kind, dependency.id): dependency
            for dependency in self.regime.dependencies
        }
        protocol_dependency = dependencies[("protocol", stage.protocol_id)]
        if stage.budget is None or self.execution.budget != stage.budget:
            raise ValueError(
                "active execution budget must equal the registered regime stage budget"
            )
        registered_protocol_projection = protocol.model_copy(
            update={"budget": stage.budget}
        )
        protocol_identity = {
            "protocol": registered_protocol_projection.identity_data(),
            "declaration_ref": protocol_dependency.declaration_ref.identity_data(),
        }
        protocol_digest = hashlib.sha256(
            json.dumps(
                protocol_identity,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        active_dependency_digests = {
            ("benchmark", self.benchmark.id): self.benchmark.artifact_digest,
            ("dataset", self.dataset.id): self.dataset.artifact_digest,
            (
                "evaluator",
                self.evaluator.evaluator.id,
            ): self.evaluator.artifact_digest,
            ("protocol", protocol.id): protocol_digest,
            **{
                ("metric", artifact.metric.id): artifact.artifact_digest
                for artifact in self.metrics
            },
        }
        drifted_dependencies = sorted(
            key
            for key, digest in active_dependency_digests.items()
            if dependencies[key].artifact_digest != digest
        )
        if drifted_dependencies:
            raise ValueError(
                "active manifest artifacts differ from regime dependencies: "
                f"{drifted_dependencies}"
            )
        return self

    @model_validator(mode="after")
    def measurement_registry_is_closed(self) -> "ResolvedBmpManifest":
        metric_ids = [artifact.metric.id for artifact in self.metrics]
        if not metric_ids or len(set(metric_ids)) != len(metric_ids):
            raise ValueError("manifest metrics must be non-empty with unique ids")
        digests = [artifact.artifact_digest for artifact in self.metrics]
        if len(set(digests)) != len(digests):
            raise ValueError("manifest metric artifacts must be content-unique")
        selected = set(metric_ids)
        emitted = {
            binding.metric_id for binding in self.evaluator.evaluator.metrics
        }
        missing_emitted = sorted(emitted - selected)
        if missing_emitted:
            raise ValueError(
                f"manifest omits evaluator metric artifacts: {missing_emitted}"
            )
        missing_inputs = sorted(
            {
                input_id
                for artifact in self.metrics
                for input_id in artifact.metric.inputs
                if input_id not in selected
            }
        )
        if missing_inputs:
            raise ValueError(f"manifest omits metric dependencies: {missing_inputs}")
        if self.execution.protocol is not None:
            repeated_wilson = [
                artifact.metric.id
                for artifact in self.metrics
                if artifact.metric.uncertainty is not None
                and artifact.metric.uncertainty.method
                == MetricUncertaintyMethod.wilson_score_v1
                and self.execution.protocol.rollouts_per_case != 1
            ]
            if repeated_wilson:
                raise ValueError(
                    "rollout-level Wilson uncertainty requires exactly one rollout "
                    f"per task; use task-cluster bootstrap instead: {repeated_wilson}"
                )
            too_large = [
                (artifact.metric.id, artifact.metric.parameters.k)
                for artifact in self.metrics
                if artifact.metric.parameters is not None
                and artifact.metric.parameters.k is not None
                and artifact.metric.parameters.k
                > self.execution.protocol.rollouts_per_case
            ]
            if too_large:
                raise ValueError(
                    f"metric k exceeds planned rollouts per task: {too_large}"
                )
        return self

    @model_validator(mode="after")
    def evolution_method_registry_is_closed(self) -> "ResolvedBmpManifest":
        """Bind method selection to subject, comparison, metrics, and config."""

        evolver = self.metadata.evolver
        meta_evolver = self.metadata.meta_evolver
        subject_kind = self.subject.kind
        comparison_kind = self.claim_design.comparison_kind
        if subject_kind == "evolver":
            if evolver is None or meta_evolver is not None:
                raise ValueError(
                    "an evolver subject requires exactly one registered evolver method"
                )
            if comparison_kind != ComparisonKind.evolution_method:
                raise ValueError(
                    "registered evolver requires comparison_kind='evolution_method'"
                )
            active_method: EvolutionMethodArtifact | MetaEvolutionMethodArtifact = evolver
        elif subject_kind == "meta_evolver":
            if evolver is None or meta_evolver is None:
                raise ValueError(
                    "a meta-evolver subject requires registered parent and meta methods"
                )
            if comparison_kind != ComparisonKind.meta_evolution_method:
                raise ValueError(
                    "registered meta-evolver requires "
                    "comparison_kind='meta_evolution_method'"
                )
            active_method = meta_evolver
        else:
            if evolver is not None or meta_evolver is not None:
                raise ValueError(
                    "non-evolution subjects cannot carry evolution method artifacts"
                )
            return self

        if active_method.subject_adapter != self.subject.adapter:
            raise ValueError(
                "evolution method subject_adapter differs from the resolved subject"
            )
        configuration = self.metadata.configuration
        if configuration is None:
            raise ValueError("registered evolution methods require configuration")
        method_artifacts = (
            (evolver,) if meta_evolver is None else (evolver, meta_evolver)
        )
        metric_by_id = {artifact.metric.id: artifact.metric for artifact in self.metrics}
        for method in method_artifacts:
            assert method is not None
            if method.configuration_profile_id not in configuration.profiles:
                raise ValueError(
                    f"evolution method {method.id!r} configuration profile is not active"
                )
            if method.configuration_digest != configuration.artifact_digest:
                raise ValueError(
                    f"evolution method {method.id!r} configuration digest drift"
                )
            metric = metric_by_id.get(method.selection.metric_id)
            if metric is None:
                raise ValueError(
                    f"evolution method {method.id!r} selection metric is not registered"
                )
            if metric.direction.value != method.selection.direction:
                raise ValueError(
                    f"evolution method {method.id!r} selection metric direction drift"
                )
            observed: Any = configuration.values
            try:
                for part in method.selection_configuration_path.split("."):
                    if not isinstance(observed, Mapping):
                        raise KeyError(part)
                    observed = observed[part]
            except KeyError as exc:
                raise ValueError(
                    f"evolution method {method.id!r} selection configuration is absent"
                ) from exc
            if observed != method.selection.configuration_data():
                raise ValueError(
                    f"evolution method {method.id!r} selection configuration drift"
                )
            selection_leaf_paths = (
                f"{method.selection_configuration_path}.{key}"
                for key in method.selection.configuration_data()
            )
            wrong_owners = sorted(
                path
                for path in selection_leaf_paths
                if configuration.ownership.get(path) != method.adapter
            )
            if wrong_owners:
                raise ValueError(
                    f"evolution method {method.id!r} selection configuration "
                    f"has wrong ownership: {wrong_owners}"
                )
        return self

    def identity_data(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude=self.IDENTITY_EXCLUDE)

        def project_artifact_refs(source: Any, serialized: Any) -> Any:
            """Project typed nested ArtifactRefs without guessing dict shapes."""

            if isinstance(source, ArtifactRef):
                return source.identity_data()
            if isinstance(source, BaseModel) and isinstance(serialized, Mapping):
                projected = dict(serialized)
                for field_name in type(source).model_fields:
                    if field_name in projected:
                        projected[field_name] = project_artifact_refs(
                            getattr(source, field_name), projected[field_name]
                        )
                return projected
            if isinstance(source, Mapping) and isinstance(serialized, Mapping):
                return {
                    key: project_artifact_refs(source[key], item)
                    if key in source
                    else item
                    for key, item in serialized.items()
                }
            if isinstance(source, (list, tuple)) and isinstance(serialized, list):
                return [
                    project_artifact_refs(source_item, serialized_item)
                    for source_item, serialized_item in zip(
                        source, serialized, strict=True
                    )
                ]
            return serialized

        data = project_artifact_refs(self, data)
        data["benchmark"].pop("source", None)
        data["dataset"].pop("source", None)
        data["subject"].pop("source", None)
        environment = data["execution"]["backend"].get("environment")
        source_environment = self.execution.backend.environment
        if environment is not None and source_environment is not None:
            environment["mounts"] = [
                mount.identity_data() for mount in source_environment.mounts
            ]
        binding = data["execution"].get("provider_binding")
        source_binding = self.execution.provider_binding
        if binding is not None and source_binding is not None:
            binding["credential_ref"] = source_binding.credential_ref.identity_data()
        source_script_ref = getattr(self.subject, "script_ref", None)
        if source_script_ref is not None:
            data["subject"]["script_ref"] = source_script_ref.identity_data()
        return data

    def canonical_digest(self) -> str:
        canonical = json.dumps(
            self.identity_data(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def authoritative_metric_binding(self) -> EvaluatorMetricBinding:
        return self.evaluator.evaluator.authoritative_metric

    @property
    def authoritative_reward_metric(self) -> str:
        """Adapter-native key bound to the registered authoritative metric."""

        return self.authoritative_metric_binding.source_key

    @property
    def authoritative_metric_artifact(self) -> MetricArtifact:
        metric_id = self.authoritative_metric_binding.metric_id
        return next(
            artifact for artifact in self.metrics if artifact.metric.id == metric_id
        )

    @property
    def reward_pass_value(self) -> float | None:
        return self.authoritative_metric_binding.success_threshold

    @property
    def reward_success_operator(self) -> str | None:
        return self.authoritative_metric_binding.success_operator

    @property
    def scoring_kind(self) -> ScoringKind:
        return self.evaluator.evaluator.scoring_kind


class VerifierEvidence(StrictModel):
    verifier: str = Field(min_length=1)
    passed: bool | None = None
    score: float | None = None
    metrics: Mapping[str, float] = Field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    # Warning: never use for API keys or secrets; names only.
    details: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("details", mode="before")
    @classmethod
    def details_reject_secret_keys(cls, value: Any) -> Any:
        return _reject_secret_like_keys(value, field_name="VerifierEvidence.details")


class UsageRecord(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    wall_clock_seconds: float | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    tool_errors: int | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    cpu_seconds: float | None = Field(default=None, ge=0)
    peak_memory_bytes: int | None = Field(default=None, ge=0)
    io_read_bytes: int | None = Field(default=None, ge=0)
    io_write_bytes: int | None = Field(default=None, ge=0)
    network_ingress_bytes: int | None = Field(default=None, ge=0)
    network_egress_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def token_total_is_consistent(self) -> "UsageRecord":
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class ProvenanceRecord(StrictModel):
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    runner_digest: str = Field(pattern=SHA256_PATTERN)
    benchmark_digest: str = Field(pattern=SHA256_PATTERN)
    subject_digest: str = Field(pattern=SHA256_PATTERN)
    backend_digest: str = Field(min_length=1)
    trace_emission_claimed: bool = False
    executable: str | None = None
    executable_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    distribution: str | None = None
    version: str | None = None
    commit: str | None = None
    image_digest: str | None = None
    backend_kind: str | None = Field(default=None, min_length=1)
    network_mode: str | None = Field(default=None, min_length=1)
    workspace_namespace: str | None = Field(default=None, min_length=1)
    environment_receipt: EnvironmentReceipt | None = None
    container_receipt_ref: ArtifactRef | None = None
    runtime_manifest_receipt: RuntimeManifestReceipt | None = None
    configuration_activation: ConfigurationActivationReceipt | None = None
    model_activation: ModelActivationReceipt | None = None
    evolution_evidence_ref: ArtifactRef | None = None
    test_override: TestOverrideReceipt | None = None

    @model_validator(mode="after")
    def no_equals_in_direct_strings(self) -> "ProvenanceRecord":
        for field_name, value in self.__dict__.items():
            if isinstance(value, str) and "=" in value:
                raise ValueError(
                    f"ProvenanceRecord.{field_name} must not contain '=' "
                    "(possible key=value secret)"
                )
        return self


class EvidenceBundle(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "status": {
                                "enum": ["pass", "verified_fail", "scored"]
                            }
                        },
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "output_refs": {"minItems": 1},
                            "verifier_evidence": {"type": "object"},
                        },
                        "required": ["output_refs", "verifier_evidence"],
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "pass"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "verifier_evidence": {
                                "properties": {"passed": {"const": True}},
                                "required": ["passed"],
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "verified_fail"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "verifier_evidence": {
                                "properties": {"passed": {"const": False}},
                                "required": ["passed"],
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "scored"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "verifier_evidence": {
                                "properties": {
                                    "passed": {"type": "null"},
                                    "score": {"type": "number"},
                                },
                                "required": ["passed", "score"],
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {
                            "provenance": {
                                "properties": {
                                    "trace_emission_claimed": {"const": True}
                                },
                                "required": ["trace_emission_claimed"],
                            }
                        },
                        "required": ["provenance"],
                    },
                    "then": {
                        "properties": {"trace_ref": {"type": "object"}},
                        "required": ["trace_ref"],
                    },
                },
            ]
        }
    )

    run_id: str = Field(pattern=ID_PATTERN)
    status: RunStatus
    output_refs: tuple[ArtifactRef, ...] = ()
    trace_ref: ArtifactRef | None = None
    trajectory_ref: ArtifactRef | None = None
    checkpoint_ref: ArtifactRef | None = None
    log_refs: tuple[ArtifactRef, ...] = ()
    verifier_evidence: VerifierEvidence | None = None
    usage: UsageRecord | None = None
    network_policy: ResolvedNetworkPolicy | None = None
    network_observation: NetworkObservation | None = None
    provenance: ProvenanceRecord

    @model_validator(mode="after")
    def validate_status_evidence(self) -> "EvidenceBundle":
        scored_statuses = {
            RunStatus.pass_,
            RunStatus.verified_fail,
            RunStatus.scored,
        }
        if self.status in scored_statuses:
            if not self.output_refs:
                raise ValueError(f"status {self.status.value!r} requires output_refs")
            if self.verifier_evidence is None:
                raise ValueError(f"status {self.status.value!r} requires verifier_evidence")
        if self.status == RunStatus.pass_ and (
            self.verifier_evidence is None or self.verifier_evidence.passed is not True
        ):
            raise ValueError("status 'pass' requires verifier_evidence.passed=true")
        if self.status == RunStatus.verified_fail and (
            self.verifier_evidence is None or self.verifier_evidence.passed is not False
        ):
            raise ValueError("status 'verified_fail' requires verifier_evidence.passed=false")
        if self.status == RunStatus.scored and (
            self.verifier_evidence is None
            or self.verifier_evidence.passed is not None
            or self.verifier_evidence.score is None
        ):
            raise ValueError(
                "status 'scored' requires a score and no binary passed verdict"
            )
        if self.provenance.trace_emission_claimed and self.trace_ref is None:
            raise ValueError("trace_ref is required when the subject claims trace emission")
        return self

    @property
    def effective_assembly_sidecar_ref(self) -> ArtifactRef | None:
        """Expose the runtime-effective assembly bytes through bundle lineage."""

        receipt = self.provenance.runtime_manifest_receipt
        return None if receipt is None else receipt.effective_sidecar_artifact_ref()


class TrajectoryCaptureState(str, Enum):
    complete = "complete"
    partial = "partial"
    unavailable = "unavailable"
    not_applicable = "not_applicable"


class TrajectoryCapture(StrictModel):
    """Explicit completeness ledger; missing evidence is never implicit."""

    model_io: TrajectoryCaptureState
    tool_io: TrajectoryCaptureState
    process_io: TrajectoryCaptureState
    evaluator_io: TrajectoryCaptureState
    environment: TrajectoryCaptureState
    resource_usage: TrajectoryCaptureState
    reasons: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("reasons")
    @classmethod
    def reasons_are_secret_free(cls, values: Mapping[str, str]) -> Mapping[str, str]:
        _reject_secret_like_keys(values, field_name="TrajectoryCapture.reasons")
        if any(not key.strip() or not value.strip() for key, value in values.items()):
            raise ValueError("trajectory capture reasons must be non-empty")
        return dict(values)

    @property
    def claim_complete(self) -> bool:
        return all(
            state in {
                TrajectoryCaptureState.complete,
                TrajectoryCaptureState.not_applicable,
            }
            for state in (
                self.model_io,
                self.tool_io,
                self.process_io,
                self.evaluator_io,
                self.environment,
                self.resource_usage,
            )
        )


class TrajectoryEventKind(str, Enum):
    rollout_started = "rollout_started"
    environment_snapshot = "environment_snapshot"
    model_request = "model_request"
    model_response = "model_response"
    tool_request = "tool_request"
    tool_response = "tool_response"
    process_io = "process_io"
    evaluator_request = "evaluator_request"
    evaluator_response = "evaluator_response"
    state_delta = "state_delta"
    exception = "exception"
    native_trace_attached = "native_trace_attached"
    rollout_finished = "rollout_finished"


class TrajectoryEvent(StrictModel):
    event_id: str = Field(pattern=ID_PATTERN)
    parent_event_id: str | None = Field(default=None, pattern=ID_PATTERN)
    sequence: int = Field(ge=1, strict=True)
    kind: TrajectoryEventKind
    occurred_at: str
    duration_seconds: float | None = Field(default=None, ge=0, strict=True)
    input_refs: tuple[ArtifactRef, ...] = ()
    output_refs: tuple[ArtifactRef, ...] = ()
    usage: UsageRecord | None = None
    status: RunStatus | None = None
    details: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_utc(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("trajectory event timestamp must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("trajectory event timestamp must be UTC")
        return value

    @field_validator("details", mode="before")
    @classmethod
    def details_are_secret_free_json(cls, value: Any) -> Any:
        _reject_secret_like_keys(value, field_name="TrajectoryEvent.details")
        return _validate_json_configuration(value, field_name="TrajectoryEvent.details")


class RolloutTrajectory(StrictModel):
    """Content-addressed, adapter-neutral evidence for one launched attempt."""

    format: Literal["bmp-rollout-trajectory-v1"] = "bmp-rollout-trajectory-v1"
    parent_run_id: str = Field(pattern=ID_PATTERN)
    attempt_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    attempt_index: int = Field(ge=0, strict=True)
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    evaluator_digest: str = Field(pattern=SHA256_PATTERN)
    metric_digests: tuple[str, ...]
    configuration_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    started_at: str
    finished_at: str
    elapsed_seconds: float = Field(ge=0, strict=True)
    terminal_status: RunStatus
    input_refs: tuple[ArtifactRef, ...]
    output_refs: tuple[ArtifactRef, ...] = ()
    log_refs: tuple[ArtifactRef, ...] = ()
    native_trace_refs: tuple[ArtifactRef, ...] = ()
    evaluator_refs: tuple[ArtifactRef, ...] = ()
    provenance: ProvenanceRecord
    verifier_evidence: VerifierEvidence | None = None
    usage: UsageRecord
    capture: TrajectoryCapture
    events: tuple[TrajectoryEvent, ...]

    @field_validator("started_at", "finished_at")
    @classmethod
    def boundary_timestamp_is_utc(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("trajectory boundary must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("trajectory boundary must be UTC")
        return value

    @model_validator(mode="after")
    def trajectory_is_closed(self) -> "RolloutTrajectory":
        if not self.input_refs:
            raise ValueError("rollout trajectory requires input refs")
        if not self.metric_digests or len(set(self.metric_digests)) != len(
            self.metric_digests
        ):
            raise ValueError("rollout trajectory metric digests must be non-empty and unique")
        if any(re.fullmatch(SHA256_PATTERN, value) is None for value in self.metric_digests):
            raise ValueError("rollout trajectory metric digests must be SHA-256")
        if not self.events:
            raise ValueError("rollout trajectory requires events")
        if tuple(event.sequence for event in self.events) != tuple(
            range(1, len(self.events) + 1)
        ):
            raise ValueError("trajectory event sequences must be contiguous")
        event_ids = [event.event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("trajectory event ids must be unique")
        seen: set[str] = set()
        for event in self.events:
            if event.parent_event_id is not None and event.parent_event_id not in seen:
                raise ValueError("trajectory event parent must precede its child")
            seen.add(event.event_id)
        if self.events[0].kind != TrajectoryEventKind.rollout_started:
            raise ValueError("trajectory must begin with rollout_started")
        final = self.events[-1]
        if final.kind != TrajectoryEventKind.rollout_finished:
            raise ValueError("trajectory must end with rollout_finished")
        if final.status != self.terminal_status:
            raise ValueError("terminal trajectory event status drift")
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
        if finished < started:
            raise ValueError("trajectory finished_at precedes started_at")
        if self.provenance.manifest_digest != self.manifest_digest:
            raise ValueError("trajectory provenance manifest digest drift")
        return self

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class BudgetAllocation(StrictModel):
    """Pre-launch token/cost allocation; wall time remains global."""

    max_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_cost: float | None = Field(default=None, ge=0, strict=True)

def _allocation_sums(total: BudgetAllocation, parts: tuple[BudgetAllocation, ...]) -> bool:
    for field_name in ("max_tokens", "max_cost"):
        value = getattr(total, field_name)
        values = [getattr(part, field_name) for part in parts]
        if value is None:
            if any(item is not None for item in values):
                return False
        elif any(item is None for item in values) or value != sum(values):
            return False
    return True


class CaseAllocation(StrictModel):
    """Per-case cap allocated before dividing it among attempts."""

    case_id: str = Field(pattern=ID_PATTERN)
    allocation_id: str = Field(pattern=ID_PATTERN)
    allocated: BudgetAllocation
    attempt_count: int = Field(ge=1, strict=True)


class AttemptContext(StrictModel):
    """Scheduler-derived context passed atomically to one backend attempt."""

    case_id: str = Field(pattern=ID_PATTERN)
    execution_run_id: str = Field(pattern=ID_PATTERN)
    attempt_index: int = Field(ge=0, strict=True)
    attempt_budget: BudgetAllocation
    remaining_global_budget: BudgetAllocation
    remaining_wall_seconds: float | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def attempt_fits_remaining_budget(self) -> "AttemptContext":
        for field_name in ("max_tokens", "max_cost"):
            attempt = getattr(self.attempt_budget, field_name)
            remaining = getattr(self.remaining_global_budget, field_name)
            if remaining is not None and (attempt is None or attempt > remaining):
                raise ValueError(
                    f"attempt {field_name} must not exceed remaining global budget"
                )
        return self


class AttemptAllocation(StrictModel):
    """Per-rollout cap reserved from a case allocation before launch."""

    attempt_id: str = Field(pattern=ID_PATTERN)
    case_allocation_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    attempt_index: int = Field(ge=0, strict=True)
    allocated: BudgetAllocation
    reservation_sequence: int = Field(ge=0, strict=True)
    launched: bool
    launch_sequence: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def reservation_precedes_launch(self) -> "AttemptAllocation":
        if self.launched and self.launch_sequence is None:
            raise ValueError("launched attempt allocation requires launch_sequence")
        if not self.launched and self.launch_sequence is not None:
            raise ValueError("unlaunched attempt allocation must not have launch_sequence")
        if self.launched and self.reservation_sequence >= self.launch_sequence:
            raise ValueError("attempt allocation must be reserved before launch")
        return self


class BudgetDebit(StrictModel):
    """Measured leaf usage and returned unused cap at completion.

    Some native backends (for example a no-op agent) do not expose token or
    monetary counters.  ``usage_observable`` records that limitation instead
    of allowing an adapter to silently coerce an unknown value to zero.
    """

    attempt_id: str = Field(pattern=ID_PATTERN)
    child_run_id: str = Field(pattern=ID_PATTERN)
    completion_sequence: int = Field(ge=1, strict=True)
    spent: UsageRecord
    released: BudgetAllocation
    budget_exceeded: bool = False
    usage_observable: bool = True


class AttemptExecution(StrictModel):
    """One launched scheduler attempt; unlaunched slots have no execution record."""

    attempt_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    attempt_index: int = Field(ge=0, strict=True)
    status: RunStatus
    evidence_bundle_ref: ArtifactRef | None
    debit: BudgetDebit | None
    selected: bool
    selection_reason: str | None = None
    reward_value: float | None = None
    reward_metric: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def launched_attempt_has_evidence(self) -> "AttemptExecution":
        if self.evidence_bundle_ref is None:
            raise ValueError("launched attempts require evidence_bundle_ref")
        if self.debit is None:
            raise ValueError("launched attempts require a budget debit")
        if self.debit.attempt_id != self.attempt_id:
            raise ValueError("attempt debit must match attempt_id")
        if self.debit.budget_exceeded and self.status != RunStatus.agent_error:
            raise ValueError("budget-exceeded attempts require agent_error status")
        if (self.reward_value is None) != (self.reward_metric is None):
            raise ValueError("reward_value and reward_metric must be provided together")
        return self


_USAGE_LEDGER_FIELDS = ("total_tokens", "cost")


def _usage_reconciles(total: UsageRecord, parts: tuple[UsageRecord, ...]) -> bool:
    for field_name in _USAGE_LEDGER_FIELDS:
        total_value = getattr(total, field_name)
        part_values = [getattr(part, field_name) for part in parts]
        if total_value is None or any(value is None for value in part_values):
            return False
        if total_value != sum(part_values):
            return False
    return True


class BudgetLedger(StrictModel):
    """Planned allocation hierarchy plus derived aggregate usage."""

    case_allocations: tuple[CaseAllocation, ...]
    attempt_allocations: tuple[AttemptAllocation, ...]
    aborted_at_exhaustion: bool
    aborted_children: tuple[str, ...]
    total_usage: UsageRecord
    parent_overhead: UsageRecord
    global_elapsed_wall_seconds: float = Field(ge=0, strict=True)
    reconciles_exactly: bool

    @model_validator(mode="after")
    def validate_ledger(self) -> "BudgetLedger":
        if self.total_usage.wall_clock_seconds is not None:
            raise ValueError(
                "BudgetLedger.total_usage must not sum attempt wall-clock seconds"
            )
        allocation_ids = [item.allocation_id for item in self.case_allocations]
        case_ids = [item.case_id for item in self.case_allocations]
        if len(set(allocation_ids)) != len(allocation_ids):
            raise ValueError("case allocation ids must be unique")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case allocations must be unique per case")
        attempt_ids = [item.attempt_id for item in self.attempt_allocations]
        reservation_sequences = [
            item.reservation_sequence for item in self.attempt_allocations
        ]
        launch_sequences = [
            item.launch_sequence for item in self.attempt_allocations if item.launched
        ]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("attempt allocation ids must be unique")
        if (
            len(set(reservation_sequences)) != len(reservation_sequences)
            or reservation_sequences != sorted(reservation_sequences)
        ):
            raise ValueError("attempt reservation sequences must increase monotonically")
        if (
            len(set(launch_sequences)) != len(launch_sequences)
            or launch_sequences != sorted(launch_sequences)
        ):
            raise ValueError("attempt launch sequences must increase monotonically")
        case_by_allocation_id = {
            item.allocation_id: item for item in self.case_allocations
        }
        attempts_by_case_allocation: dict[str, list[AttemptAllocation]] = {}
        for item in self.attempt_allocations:
            parent = case_by_allocation_id.get(item.case_allocation_id)
            if parent is None or parent.case_id != item.case_id:
                raise ValueError("attempt allocations must reference their case allocation")
            attempts_by_case_allocation.setdefault(item.case_allocation_id, []).append(item)
        for parent in self.case_allocations:
            children = attempts_by_case_allocation.get(parent.allocation_id, [])
            if len(children) != parent.attempt_count or not _allocation_sums(
                parent.allocated,
                tuple(child.allocated for child in children),
            ):
                raise ValueError("attempt allocations must exactly divide the case allocation")
            planned_indices = tuple(child.attempt_index for child in children)
            if planned_indices != tuple(range(parent.attempt_count)):
                raise ValueError(
                    "attempt allocations must bind ordered, contiguous planned indices"
                )
        unlaunched_attempt_ids = {
            item.attempt_id for item in self.attempt_allocations if not item.launched
        }
        if len(set(self.aborted_children)) != len(self.aborted_children):
            raise ValueError("aborted child ids must be unique")
        if set(self.aborted_children) != unlaunched_attempt_ids:
            raise ValueError("aborted children must equal unlaunched attempt allocations")
        if bool(self.aborted_children) != self.aborted_at_exhaustion:
            raise ValueError("aborted_at_exhaustion must exactly reflect aborted children")
        return self


class CheckpointSaveReceipt(StrictModel):
    written_digest: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0, strict=True)
    write_completion_sequence: int = Field(ge=1, strict=True)
    path: str = Field(min_length=1, pattern=r"^/")

    @field_validator("path")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("checkpoint save path must be absolute")
        return value


class CheckpointLoadReceipt(StrictModel):
    loaded_checkpoint_digest: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_digest: str = Field(pattern=SHA256_PATTERN)
    schedule_receipt_digest: str = Field(pattern=SHA256_PATTERN)
    selected_bundle_digests: tuple[str, ...]

    @field_validator("selected_bundle_digests")
    @classmethod
    def selected_digests_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(SHA256_PATTERN, value) is None for value in values):
            raise ValueError("selected bundle digests must be SHA-256 values")
        if len(set(values)) != len(values):
            raise ValueError("selected bundle digests must be unique")
        return values


class ScheduleActivationReceipt(StrictModel):
    """Declared schedule compared with measured scheduler activation."""

    run_id: str = Field(pattern=ID_PATTERN)
    protocol_digest: str = Field(pattern=SHA256_PATTERN)
    scheduler_digest: str = Field(pattern=SHA256_PATTERN)
    pipeline_digest: str = Field(pattern=SHA256_PATTERN)
    reservation_policy: Literal["equal_division_per_case"]
    global_deadline_at: str | None = Field(default=None, min_length=1)
    declared_rollouts_per_case: int = Field(ge=1, strict=True)
    observed_attempt_count: int = Field(ge=0, strict=True)
    declared_parallelism: int = Field(ge=1, strict=True)
    observed_max_concurrency: int = Field(ge=0, strict=True)
    declared_case_order: Literal[
        "fixed", "seeded_random", "random", "custom", "explicit"
    ]
    observed_case_order: tuple[str, ...]
    declared_state_reset: Literal["per_case", "per_rollout", "never"]
    observed_state_reset_count: int = Field(ge=0, strict=True)
    declared_candidate_selection: str = Field(min_length=1)
    observed_selection_policy: str = Field(min_length=1)
    declared_checkpoint_policy: Literal[
        "disabled", "save", "resume", "save_and_resume"
    ]
    checkpoint_save_ref: CheckpointSaveReceipt | None = None
    checkpoint_load_ref: CheckpointLoadReceipt | None = None
    ancestor_schedule_receipt_ref: ArtifactRef | None = None
    order_seed: int | None = None
    attempts: tuple[AttemptExecution, ...]
    budget_ledger: BudgetLedger
    schedule_valid: bool
    mismatch_reasons: tuple[str, ...]

    @field_validator("global_deadline_at")
    @classmethod
    def deadline_is_timezone_aware_iso8601(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("global_deadline_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("global_deadline_at must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def validate_schedule_receipt(self) -> "ScheduleActivationReceipt":
        policy = self.declared_checkpoint_policy
        if self.schedule_valid:
            if policy == "resume":
                raise ValueError("checkpoint_policy=resume requires CheckpointLoadReceipt")
            if policy == "disabled" and (
                self.checkpoint_save_ref is not None or self.checkpoint_load_ref is not None
            ):
                raise ValueError("disabled checkpoint policy forbids save/load receipts")
            if policy == "save" and (
                self.checkpoint_save_ref is None or self.checkpoint_load_ref is not None
            ):
                raise ValueError(
                    "save checkpoint policy requires save ref and forbids load ref"
                )
            if policy == "save_and_resume" and (
                self.checkpoint_save_ref is None
                or self.checkpoint_load_ref is None
                or self.ancestor_schedule_receipt_ref is None
            ):
                raise ValueError(
                    "save_and_resume requires save/load refs and ancestor schedule lineage"
                )
        if (
            self.checkpoint_load_ref is not None
            and self.ancestor_schedule_receipt_ref is not None
            and self.checkpoint_load_ref.schedule_receipt_digest
            != self.ancestor_schedule_receipt_ref.sha256
        ):
            raise ValueError("checkpoint load schedule digest must match ancestor receipt")
        if self.declared_case_order == "seeded_random" and self.order_seed is None:
            raise ValueError("order_seed is required for seeded_random case order")
        if self.declared_case_order != "seeded_random" and self.order_seed is not None:
            raise ValueError("order_seed is forbidden for non-seeded case order")
        if self.observed_attempt_count != len(self.attempts):
            raise ValueError("observed_attempt_count must equal launched attempts")
        attempt_ids = [item.attempt_id for item in self.attempts]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("attempt execution ids must be unique")
        slots = [(item.case_id, item.attempt_index) for item in self.attempts]
        if len(set(slots)) != len(slots):
            raise ValueError("attempt execution slots must be unique")
        if any(index >= self.declared_rollouts_per_case for _, index in slots):
            raise ValueError("attempt_index exceeds declared rollouts per case")
        allocation_by_id = {
            item.attempt_id: item
            for item in self.budget_ledger.attempt_allocations
        }
        launched_ids = {
            item.attempt_id
            for item in self.budget_ledger.attempt_allocations
            if item.launched
        }
        unlaunched_ids = {
            item.attempt_id
            for item in self.budget_ledger.attempt_allocations
            if not item.launched
        }
        if set(attempt_ids) != launched_ids:
            raise ValueError("launched allocations and attempt executions must match exactly")
        if set(self.budget_ledger.aborted_children) != unlaunched_ids:
            raise ValueError("aborted children must equal unlaunched attempt allocations")
        child_run_ids: list[str] = []
        completion_sequences: list[int] = []
        spent_records: list[UsageRecord] = []
        for attempt in self.attempts:
            allocation = allocation_by_id[attempt.attempt_id]
            if allocation.case_id != attempt.case_id:
                raise ValueError("attempt execution case must match its allocation")
            if allocation.attempt_index != attempt.attempt_index:
                raise ValueError("attempt execution index must match its allocation")
            if attempt.debit is None:
                raise ValueError("launched attempt execution requires a debit")
            child_run_ids.append(attempt.debit.child_run_id)
            completion_sequences.append(attempt.debit.completion_sequence)
            spent_records.append(attempt.debit.spent)
            if attempt.debit.completion_sequence <= (allocation.launch_sequence or 0):
                raise ValueError("attempt debit completion must follow launch")
            if not attempt.debit.budget_exceeded and attempt.debit.usage_observable:
                cap = allocation.allocated
                released = attempt.debit.released
                if cap.max_tokens is not None and (
                    attempt.debit.spent.total_tokens is None
                    or released.max_tokens is None
                    or attempt.debit.spent.total_tokens + released.max_tokens
                    != cap.max_tokens
                ):
                    raise ValueError("spent plus released tokens must equal allocated cap")
                if cap.max_cost is not None and (
                    attempt.debit.spent.cost is None
                    or released.max_cost is None
                    or attempt.debit.spent.cost + released.max_cost != cap.max_cost
                ):
                    raise ValueError("spent plus released cost must equal allocated cap")
            elif not attempt.debit.budget_exceeded and not attempt.debit.usage_observable:
                # Unknown usage is a valid observation, but it can never
                # produce a valid schedule under a finite token/cost cap.
                # The scheduler must retain the unknown fields as ``None`` and
                # add a mismatch reason below; rejecting the whole receipt
                # would discard useful verifier and infrastructure evidence.
                cap = allocation.allocated
                released = attempt.debit.released
                if cap.max_tokens is not None and released.max_tokens is not None:
                    raise ValueError(
                        "unobservable token usage must not claim released tokens"
                    )
                if cap.max_cost is not None and released.max_cost is not None:
                    raise ValueError(
                        "unobservable cost usage must not claim released cost"
                    )
        if len(set(child_run_ids)) != len(child_run_ids):
            raise ValueError("attempt debit child run ids must be unique")
        if len(set(completion_sequences)) != len(completion_sequences):
            raise ValueError("attempt debit completion sequences must be unique")
        all_sequences = [
            item.reservation_sequence for item in self.budget_ledger.attempt_allocations
        ] + [
            item.launch_sequence
            for item in self.budget_ledger.attempt_allocations
            if item.launch_sequence is not None
        ] + completion_sequences
        if len(set(all_sequences)) != len(all_sequences):
            raise ValueError("scheduler event sequences must be globally unique")
        spent_ok = _usage_reconciles(
            self.budget_ledger.total_usage,
            (*tuple(spent_records), self.budget_ledger.parent_overhead),
        )
        if self.budget_ledger.reconciles_exactly != spent_ok:
            raise ValueError("reconciles_exactly disagrees with spend arithmetic")

        planned_by_case: dict[str, list[AttemptAllocation]] = {}
        for allocation in self.budget_ledger.attempt_allocations:
            planned_by_case.setdefault(allocation.case_id, []).append(allocation)
        for case_id, allocations in planned_by_case.items():
            if len(allocations) != self.declared_rollouts_per_case:
                raise ValueError(f"case {case_id!r} does not retain every planned rollout")
        launched_by_case = {
            case_id: [item for item in allocations if item.launched]
            for case_id, allocations in planned_by_case.items()
        }
        selected_counts = {
            case_id: sum(
                item.selected for item in self.attempts if item.case_id == case_id
            )
            for case_id in launched_by_case
        }
        cases_with_launches = {
            case_id for case_id, allocations in launched_by_case.items() if allocations
        }
        if self.schedule_valid and any(
            selected_counts[case_id] != 1 for case_id in cases_with_launches
        ):
            raise ValueError("every launched case requires exactly one selected attempt")

        expected_reset_count = {
            "per_case": len(cases_with_launches),
            "per_rollout": len(launched_ids),
            "never": 0,
        }[self.declared_state_reset]
        measured_mismatches: list[str] = []
        if self.observed_max_concurrency > len(self.attempts):
            raise ValueError("observed concurrency cannot exceed launched attempts")
        if self.observed_max_concurrency > self.declared_parallelism:
            measured_mismatches.append("observed concurrency exceeds declared parallelism")
        if self.observed_state_reset_count != expected_reset_count:
            measured_mismatches.append("observed state reset count differs from declaration")
        if self.observed_selection_policy != self.declared_candidate_selection:
            measured_mismatches.append("observed selection policy differs from declaration")
        if self.budget_ledger.reconciles_exactly is False:
            measured_mismatches.append("budget ledger does not reconcile exactly")
        if any(
            attempt.debit is not None and not attempt.debit.usage_observable
            for attempt in self.attempts
        ):
            measured_mismatches.append("budget usage is unobservable")
        if any(
            attempt.debit is not None and attempt.debit.budget_exceeded
            for attempt in self.attempts
        ):
            measured_mismatches.append("attempt exceeded its budget allocation")
        if unlaunched_ids:
            measured_mismatches.append("budget exhausted before all attempts launched")
        if self.schedule_valid and (self.mismatch_reasons or measured_mismatches):
            raise ValueError("schedule_valid=true contradicts observed schedule mismatches")
        if not self.schedule_valid and not self.mismatch_reasons:
            raise ValueError("schedule_valid=false requires mismatch_reasons")
        return self


class GateName(str, Enum):
    execution_valid = "execution_valid"
    protocol_valid = "protocol_valid"
    isolation_valid = "isolation_valid"
    scoring_valid = "scoring_valid"
    statistics_valid = "statistics_valid"
    claim_eligible = "claim_eligible"


REQUIRED_GATE_ORDER = (
    GateName.execution_valid,
    GateName.protocol_valid,
    GateName.isolation_valid,
    GateName.scoring_valid,
    GateName.statistics_valid,
)
REQUIRED_GATE_NAMES = frozenset(REQUIRED_GATE_ORDER)


class GateResult(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"valid": {"const": False}},
                        "required": ["valid"],
                    },
                    "then": {
                        "properties": {
                            "reason": {"type": "string", "minLength": 1}
                        },
                        "required": ["reason"],
                    },
                },
                {
                    "if": {
                        "properties": {"valid": {"const": True}},
                        "required": ["valid"],
                    },
                    "then": {
                        "properties": {"evidence_refs": {"minItems": 1}},
                        "required": ["evidence_refs"],
                    },
                },
            ]
        }
    )

    valid: bool
    reason: str | None = None
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_gate_has_positive_evidence(self) -> "GateResult":
        if self.valid and not self.evidence_refs:
            raise ValueError("valid gate requires positive evidence_refs")
        if any(not ref.strip() for ref in self.evidence_refs):
            raise ValueError("gate evidence_refs must be non-empty")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("gate evidence_refs must be unique")
        return self

    @model_validator(mode="after")
    def invalid_gate_has_reason(self) -> "GateResult":
        if not self.valid and not self.reason:
            raise ValueError("an invalid gate requires a reason")
        return self


class EffectEstimate(StrictModel):
    metric: str = Field(min_length=1)
    point_estimate: float
    confidence_interval: tuple[float, float]
    n_runs: int = Field(ge=1)
    n_pairs: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_interval_and_counts(self) -> "EffectEstimate":
        lower, upper = self.confidence_interval
        if lower > upper:
            raise ValueError("confidence interval lower bound exceeds upper bound")
        if self.n_pairs is not None and self.n_pairs * 2 > self.n_runs:
            raise ValueError("n_pairs cannot account for more runs than n_runs")
        return self


class StatisticalAnalysisReceipt(StrictModel):
    """Derived analysis values independently replayable from report lineage."""

    format: Literal["bmp-statistical-analysis-receipt-v1"] = (
        "bmp-statistical-analysis-receipt-v1"
    )
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    metric: str = Field(min_length=1)
    paired_unit: tuple[str, ...]
    repetition_field: Literal["repetition"]
    observed_pair_count: int = Field(ge=1, strict=True)
    observed_unit_count: int = Field(ge=1, strict=True)
    observed_min_repetitions: int = Field(ge=1, strict=True)
    pairing_digest: str = Field(pattern=SHA256_PATTERN)
    variance_method: Literal["sample_variance_v1"]
    sample_variance: float | None = Field(default=None, ge=0, strict=True)
    standard_error: float | None = Field(default=None, ge=0, strict=True)
    ci_method: Literal["normal_approximation_v1"]
    confidence_level: float = Field(gt=0.5, lt=1.0, strict=True)
    effective_alpha: float = Field(gt=0, lt=0.5, strict=True)
    point_estimate: float
    confidence_interval: tuple[float, float] | None = None
    holdout_split: str | None = Field(default=None, pattern=ID_PATTERN)
    holdout_verified: bool
    multiple_comparison_method: Literal["none", "bonferroni"]
    family_size: int = Field(ge=1, strict=True)
    family_id: str | None = Field(default=None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def receipt_shape_is_coherent(self) -> "StatisticalAnalysisReceipt":
        computed = (
            self.sample_variance,
            self.standard_error,
            self.confidence_interval,
        )
        if any(value is None for value in computed) and not all(
            value is None for value in computed
        ):
            raise ValueError(
                "variance, standard_error, and confidence_interval are all-or-none"
            )
        if self.confidence_interval is not None:
            lower, upper = self.confidence_interval
            if lower > upper:
                raise ValueError("statistical confidence interval is reversed")
        return self


class LineageRef(StrictModel):
    """Bindings from one parent plan run to its selected child attempt."""

    run_id: str = Field(pattern=ID_PATTERN)
    attempt_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    evidence_bundle_ref: ArtifactRef
    schedule_receipt_ref: ArtifactRef
    case_set_receipt_ref: ArtifactRef


class RecordIndex(StrictModel):
    """Content-addressed source index used for standalone report verification."""

    format: Literal["bmp-record-index-v1"]
    experiment_id: str = Field(pattern=ID_PATTERN)
    manifest_refs: tuple[ArtifactRef, ...]
    aggregate_path: str = Field(min_length=1, pattern=r"^/")

    @field_validator("aggregate_path")
    @classmethod
    def aggregate_path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("record index aggregate_path must be absolute")
        return value

    @model_validator(mode="after")
    def manifest_paths_are_unique(self) -> "RecordIndex":
        paths = [ref.path for ref in self.manifest_refs]
        if len(set(paths)) != len(paths):
            raise ValueError("record index manifest refs must be unique")
        return self


class Observation(StrictModel):
    metric: str = Field(min_length=1)
    value: float
    n_runs: int = Field(ge=1)


class MetricSampleDisposition(str, Enum):
    observed = "observed"
    zero_filled = "zero_filled"
    excluded = "excluded"
    missing = "missing"
    invalid = "invalid"


class MetricSample(StrictModel):
    attempt_id: str = Field(pattern=ID_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    attempt_index: int = Field(ge=0, strict=True)
    status: RunStatus
    disposition: MetricSampleDisposition
    value: float | None = None
    evidence_bundle_ref: ArtifactRef | None = None
    trajectory_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def sample_value_matches_disposition(self) -> "MetricSample":
        if self.disposition in {
            MetricSampleDisposition.observed,
            MetricSampleDisposition.zero_filled,
        } and self.value is None:
            raise ValueError("observed and zero-filled metric samples require values")
        if self.disposition == MetricSampleDisposition.zero_filled and self.value != 0:
            raise ValueError("zero-filled metric samples must equal zero")
        if self.disposition in {
            MetricSampleDisposition.excluded,
            MetricSampleDisposition.missing,
            MetricSampleDisposition.invalid,
        } and self.value is not None:
            raise ValueError("non-observed metric samples forbid values")
        if (self.evidence_bundle_ref is None) != (self.trajectory_ref is None):
            raise ValueError(
                "launched metric samples bind both evidence bundle and trajectory refs"
            )
        return self


class MetricComputationState(str, Enum):
    complete = "complete"
    unavailable = "unavailable"
    invalid = "invalid"


class MetricGroupResult(StrictModel):
    """Replayable intermediate reduction for one registered group."""

    group: Mapping[MetricGroupKey, str]
    state: MetricComputationState
    value: float | None = None
    reason: str | None = Field(default=None, min_length=1)
    attempt_ids: tuple[str, ...]
    included_count: int = Field(ge=0, strict=True)
    numerator: float | None = None
    denominator: float | None = None
    population_count: int | None = Field(default=None, ge=1, strict=True)
    success_count: int | None = Field(default=None, ge=0, strict=True)
    subset_size: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("group", mode="before")
    @classmethod
    def group_identity_is_non_empty(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("metric group identity must be a non-empty mapping")
        if any(not str(item).strip() for item in value.values()):
            raise ValueError("metric group identity values must be non-empty")
        return dict(value)

    @field_validator("attempt_ids")
    @classmethod
    def group_attempt_ids_are_closed(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("metric group attempt ids must be non-empty and unique")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("metric group attempt ids must be valid BMP ids")
        return values

    @model_validator(mode="after")
    def group_result_is_coherent(self) -> "MetricGroupResult":
        if self.included_count > len(self.attempt_ids):
            raise ValueError("metric group included_count exceeds its sample count")
        if self.state == MetricComputationState.complete:
            if self.value is None or self.reason is not None:
                raise ValueError(
                    "complete metric group requires value and forbids reason"
                )
        elif self.value is not None or self.reason is None:
            raise ValueError(
                "unavailable/invalid metric group requires reason and no value"
            )
        if (self.population_count is None) != (self.subset_size is None):
            raise ValueError(
                "metric group population_count and subset_size are all-or-none"
            )
        if self.population_count is not None:
            if self.included_count != self.population_count:
                raise ValueError(
                    "metric group population_count must equal included_count"
                )
            assert self.subset_size is not None
            if self.subset_size > self.population_count:
                raise ValueError("metric group subset_size exceeds its population")
        if self.success_count is not None:
            if self.population_count is None:
                raise ValueError(
                    "metric group success_count requires population_count"
                )
            if self.success_count > self.population_count:
                raise ValueError("metric group success_count exceeds its population")
        return self


class MetricUncertaintyResult(StrictModel):
    """Realized interval bound to a registered uncertainty specification."""

    method: MetricUncertaintyMethod
    estimand: Literal["mean"] = "mean"
    confidence_level: float = Field(gt=0.5, lt=1.0, strict=True)
    resampling_unit: Literal["rollout", "task"]
    unit_count: int = Field(ge=1, strict=True)
    lower: float
    upper: float
    standard_error: float | None = Field(default=None, ge=0, strict=True)
    resamples: int | None = Field(default=None, ge=100, strict=True)
    seed: int | None = None
    rng_algorithm: Literal["sha256_counter_v1"] | None = None
    replicate_distribution_digest: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    degenerate: bool = False

    @model_validator(mode="after")
    def interval_is_ordered_and_replayable(self) -> "MetricUncertaintyResult":
        if self.lower > self.upper:
            raise ValueError("metric uncertainty interval bounds are reversed")
        if self.method == MetricUncertaintyMethod.wilson_score_v1:
            if (
                self.resamples is not None
                or self.seed is not None
                or self.rng_algorithm is not None
                or self.replicate_distribution_digest is not None
            ):
                raise ValueError("Wilson result forbids bootstrap evidence")
        elif (
            self.resamples is None
            or self.seed is None
            or self.rng_algorithm is None
            or self.replicate_distribution_digest is None
        ):
            raise ValueError("bootstrap result requires RNG and replicate digest")
        return self


class MetricResult(StrictModel):
    """Replayable metric output over one resolved configuration/run arm."""

    metric_id: str = Field(pattern=ID_PATTERN)
    metric_digest: str = Field(pattern=SHA256_PATTERN)
    sample_ledger_digest: str = Field(pattern=SHA256_PATTERN)
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    parent_run_id: str = Field(pattern=ID_PATTERN)
    schedule_receipt_ref: ArtifactRef
    state: MetricComputationState
    value: float | None = None
    reason: str | None = Field(default=None, min_length=1)
    planned_rollout_count: int = Field(ge=1, strict=True)
    task_count: int = Field(ge=1, strict=True)
    rollouts_per_task: int = Field(ge=1, strict=True)
    observed_count: int = Field(ge=0, strict=True)
    zero_filled_count: int = Field(ge=0, strict=True)
    excluded_count: int = Field(ge=0, strict=True)
    missing_count: int = Field(ge=0, strict=True)
    invalid_count: int = Field(ge=0, strict=True)
    numerator: float | None = None
    denominator: float | None = None
    input_metric_ids: tuple[str, ...] = ()
    status_counts: Mapping[RunStatus, int]
    samples: tuple[MetricSample, ...]
    groups: tuple[MetricGroupResult, ...] = ()
    uncertainty: MetricUncertaintyResult | None = None

    def sample_ledger_identity_data(self) -> list[dict[str, Any]]:
        return [
            {
                "attempt_id": sample.attempt_id,
                "case_id": sample.case_id,
                "attempt_index": sample.attempt_index,
                "status": sample.status.value,
                "disposition": sample.disposition.value,
                "value": sample.value,
            }
            for sample in self.samples
        ]

    def canonical_sample_ledger_digest(self) -> str:
        encoded = json.dumps(
            self.sample_ledger_identity_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def metric_result_reconciles(self) -> "MetricResult":
        if self.sample_ledger_digest != self.canonical_sample_ledger_digest():
            raise ValueError("metric sample ledger digest drift")
        if self.state == MetricComputationState.complete:
            if self.reason is not None:
                raise ValueError("complete metric result forbids a failure reason")
        elif self.reason is None or self.value is not None:
            raise ValueError("unavailable/invalid metric result requires reason and no value")
        if len(self.samples) != self.planned_rollout_count:
            raise ValueError("metric samples must cover every planned rollout")
        attempt_ids = [sample.attempt_id for sample in self.samples]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("metric result attempt ids must be unique")
        counts = {
            MetricSampleDisposition.observed: self.observed_count,
            MetricSampleDisposition.zero_filled: self.zero_filled_count,
            MetricSampleDisposition.excluded: self.excluded_count,
            MetricSampleDisposition.missing: self.missing_count,
            MetricSampleDisposition.invalid: self.invalid_count,
        }
        for disposition, expected in counts.items():
            if sum(sample.disposition == disposition for sample in self.samples) != expected:
                raise ValueError("metric sample disposition counts do not reconcile")
        if sum(self.status_counts.values()) != self.planned_rollout_count:
            raise ValueError("metric status counts must cover planned rollouts")
        if self.task_count * self.rollouts_per_task != self.planned_rollout_count:
            raise ValueError("metric task and rollout counts do not match population")
        if len(set(self.input_metric_ids)) != len(self.input_metric_ids):
            raise ValueError("metric input ids must be unique")
        grouped_attempt_ids = [
            attempt_id
            for group in self.groups
            for attempt_id in group.attempt_ids
        ]
        if self.groups:
            if len(set(grouped_attempt_ids)) != len(grouped_attempt_ids):
                raise ValueError("metric groups must not overlap")
            if set(grouped_attempt_ids) != set(attempt_ids):
                raise ValueError("metric groups must partition every planned rollout")
            group_keys = [
                tuple(sorted((key.value, value) for key, value in group.group.items()))
                for group in self.groups
            ]
            if len(set(group_keys)) != len(group_keys):
                raise ValueError("metric group identities must be unique")
        if self.uncertainty is not None and self.state != MetricComputationState.complete:
            raise ValueError("metric uncertainty requires a complete aggregate")
        return self


class AuthorityDocumentRef(StrictModel):
    """One stable, tracked authority document from an external protocol."""

    id: str = Field(pattern=ID_PATTERN)
    relative_path: str = Field(min_length=1)
    artifact_ref: ArtifactRef

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_normalized(cls, value: str) -> str:
        return _validate_logical_relative_path(
            value, field_name="authority document relative_path"
        )


class ExternalProtocolAuthorityReceipt(StrictModel):
    """Read-only binding to an external protocol's tracked authority bytes."""

    format: Literal["bmp-external-protocol-authority-v1"]
    protocol_id: str = Field(pattern=ID_PATTERN)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    source_root: str = Field(min_length=1, pattern=r"^/")
    authority_documents: tuple[AuthorityDocumentRef, ...]
    contract_version: str = Field(min_length=1)
    contract_relative_path: str = Field(min_length=1)
    contract_ref: ArtifactRef
    audit_rules_ref: ArtifactRef

    @field_validator("source_root")
    @classmethod
    def source_root_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("external protocol source_root must be absolute")
        return value

    @field_validator("contract_relative_path")
    @classmethod
    def contract_path_is_normalized(cls, value: str) -> str:
        return _validate_logical_relative_path(
            value, field_name="external protocol contract_relative_path"
        )

    @model_validator(mode="after")
    def authority_refs_are_complete(self) -> "ExternalProtocolAuthorityReceipt":
        if not self.authority_documents:
            raise ValueError("external protocol authority documents must be non-empty")
        ids = [document.id for document in self.authority_documents]
        paths = [document.relative_path for document in self.authority_documents]
        if len(set(ids)) != len(ids):
            raise ValueError("external protocol authority document ids must be unique")
        if len(set(paths)) != len(paths):
            raise ValueError("external protocol authority document paths must be unique")
        refs = [
            *(document.artifact_ref for document in self.authority_documents),
            self.contract_ref,
            self.audit_rules_ref,
        ]
        identities = [(ref.path, ref.sha256, ref.size_bytes) for ref in refs]
        if len(set(identities)) != len(identities):
            raise ValueError("external protocol authority refs must be unique")
        return self


class IntegrationProbePhase(str, Enum):
    compile = "compile"
    activation = "activation"
    subject = "subject"
    verifier = "verifier"
    cleanup = "cleanup"
    complete = "complete"


class IntegrationProbeOutcome(str, Enum):
    completed = "completed"
    subject_failure = "subject_failure"
    verifier_failure = "verifier_failure"
    infrastructure_failure = "infrastructure_failure"
    blocked = "blocked"


class IntegrationProbeIdentityRole(str, Enum):
    """Closed roles for probe identity bytes retained outside a manifest."""

    benchmark_source = "benchmark_source"
    loader_implementation = "loader_implementation"
    execution_adapter_implementation = "execution_adapter_implementation"
    subject_implementation = "subject_implementation"
    backend_executable = "backend_executable"
    verifier_contract = "verifier_contract"
    container_image_receipt = "container_image_receipt"


class IntegrationProbeIdentityRef(StrictModel):
    """A probe identity role bound directly to retained, rehashable bytes."""

    role: IntegrationProbeIdentityRole
    artifact_ref: ArtifactRef


class IntegrationProbeRecord(StrictModel):
    """Content-addressed evidence for a real but non-claim integration probe.

    Probe records are intentionally weaker than ``ObservationReport``: they
    can retain evidence from a partial integration contact that never reached
    the full Pipeline.  They are always exploratory and always carry explicit
    claim blockers, so a successful case cannot be mistaken for a benchmark
    result.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "anyOf": [
                        {
                            "properties": {"manifest_ref": {"type": "object"}},
                            "required": ["manifest_ref"],
                        },
                        {
                            "properties": {"identity_refs": {"minItems": 1}},
                            "required": ["identity_refs"],
                        },
                    ]
                },
                {
                    "if": {
                        "properties": {"manifest_ref": {"type": "object"}},
                        "required": ["manifest_ref"],
                    },
                    "then": {
                        "properties": {"manifest_digest": {"type": "string"}},
                        "required": ["manifest_digest"],
                    },
                },
                {
                    "if": {
                        "properties": {"manifest_digest": {"type": "string"}},
                        "required": ["manifest_digest"],
                    },
                    "then": {
                        "properties": {"manifest_ref": {"type": "object"}},
                        "required": ["manifest_ref"],
                    },
                },
            ]
        }
    )

    format: Literal["bmp-integration-probe-v1"]
    purpose: Literal[RunPurpose.exploratory]
    probe_id: str = Field(pattern=ID_PATTERN)
    benchmark_adapter: str = Field(pattern=ADAPTER_PATTERN)
    case_id: str = Field(pattern=ID_PATTERN)
    phase: IntegrationProbePhase
    outcome: IntegrationProbeOutcome
    status: RunStatus
    manifest_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    manifest_ref: ArtifactRef | None = None
    identity_refs: tuple[IntegrationProbeIdentityRef, ...] = ()
    public_input_ref: ArtifactRef | None = None
    evidence_refs: tuple[ArtifactRef, ...]
    claim_blockers: tuple[str, ...]

    @model_validator(mode="after")
    def evidence_and_outcome_are_coherent(self) -> "IntegrationProbeRecord":
        blockers = self.claim_blockers
        if not blockers or any(not blocker.strip() for blocker in blockers):
            raise ValueError("integration probe requires non-empty claim blockers")
        if len(set(blockers)) != len(blockers):
            raise ValueError("integration probe claim blockers must be unique")
        refs = tuple(
            ref
            for ref in (
                self.manifest_ref,
                self.public_input_ref,
                *self.evidence_refs,
                *(identity.artifact_ref for identity in self.identity_refs),
            )
            if ref is not None
        )
        identities = [(ref.path, ref.sha256, ref.size_bytes) for ref in refs]
        if len(set(identities)) != len(identities):
            raise ValueError("integration probe artifact refs must be unique")
        if self.manifest_ref is None and self.manifest_digest is not None:
            raise ValueError("manifest_digest requires a manifest_ref")
        if self.manifest_ref is not None and self.manifest_digest is None:
            raise ValueError("manifest_ref requires a manifest_digest")
        if self.manifest_ref is None and not self.identity_refs:
            raise ValueError(
                "integration probe requires a manifest_ref or identity_refs"
            )
        allowed_statuses = {
            IntegrationProbeOutcome.completed: {
                RunStatus.pass_,
                RunStatus.verified_fail,
                RunStatus.scored,
            },
            IntegrationProbeOutcome.subject_failure: {
                RunStatus.no_output,
                RunStatus.invalid_output,
                RunStatus.timeout,
                RunStatus.agent_error,
                RunStatus.harness_fault,
                RunStatus.unsupported,
            },
            IntegrationProbeOutcome.verifier_failure: {
                RunStatus.verifier_error,
                RunStatus.invalid_output,
                RunStatus.timeout,
            },
            IntegrationProbeOutcome.infrastructure_failure: {
                RunStatus.infra_error,
                RunStatus.timeout,
            },
            IntegrationProbeOutcome.blocked: {
                RunStatus.unsupported,
                RunStatus.infra_error,
            },
        }
        if self.status not in allowed_statuses[self.outcome]:
            raise ValueError(
                "integration probe status does not match its outcome taxonomy"
            )
        if self.outcome == IntegrationProbeOutcome.completed and (
            self.public_input_ref is None or not self.evidence_refs
        ):
            raise ValueError(
                "completed integration probe requires public input and evidence"
            )
        return self


class ObservationReport(StrictModel):
    """Exploratory observations with no claim eligibility or causal fields."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"protocol_valid": {"const": True}},
                        "required": ["protocol_valid"],
                    },
                    "then": {
                        "properties": {"protocol_reasons": {"maxItems": 0}}
                    },
                    "else": {
                        "properties": {"protocol_reasons": {"minItems": 1}}
                    },
                },
                {
                    "if": {
                        "properties": {"isolation_valid": {"const": True}},
                        "required": ["isolation_valid"],
                    },
                    "then": {
                        "properties": {"isolation_reasons": {"maxItems": 0}}
                    },
                    "else": {
                        "properties": {"isolation_reasons": {"minItems": 1}}
                    },
                }
            ]
        }
    )

    purpose: Literal[RunPurpose.exploratory]
    comparison_kind: ComparisonKind | None = None
    subject_kinds: tuple[SubjectKind, ...]
    experiment_id: str = Field(pattern=ID_PATTERN)
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    protocol_valid: bool = True
    protocol_reasons: tuple[str, ...] = ()
    isolation_valid: bool
    isolation_reasons: tuple[str, ...]
    observations: tuple[Observation, ...] = ()
    metric_results: tuple[MetricResult, ...] = ()
    failure_breakdown: Mapping[RunStatus, int] = Field(default_factory=dict)
    lineage: tuple[LineageRef, ...] = ()
    record_index_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validity_results_are_explicit(self) -> "ObservationReport":
        if not self.subject_kinds or len(set(self.subject_kinds)) != len(
            self.subject_kinds
        ):
            raise ValueError("exploratory subject_kinds must be non-empty and unique")
        if self.comparison_kind is None and self.subject_kinds != (SubjectKind.fake,):
            raise ValueError(
                "only fake conformance reports may omit comparison_kind"
            )
        protocol_reasons = self.protocol_reasons
        if self.protocol_valid and protocol_reasons:
            raise ValueError("valid exploratory protocol cannot have failure reasons")
        if not self.protocol_valid and not protocol_reasons:
            raise ValueError("invalid exploratory protocol requires failure reasons")
        if any(not reason.strip() for reason in protocol_reasons):
            raise ValueError("exploratory protocol reasons must be non-empty")
        if len(set(protocol_reasons)) != len(protocol_reasons):
            raise ValueError("exploratory protocol reasons must be unique")
        reasons = self.isolation_reasons
        if self.isolation_valid and reasons:
            raise ValueError("valid exploratory isolation cannot have failure reasons")
        if not self.isolation_valid and not reasons:
            raise ValueError("invalid exploratory isolation requires failure reasons")
        if any(not reason.strip() for reason in reasons):
            raise ValueError("exploratory isolation reasons must be non-empty")
        if len(set(reasons)) != len(reasons):
            raise ValueError("exploratory isolation reasons must be unique")
        return self

    @field_validator("failure_breakdown")
    @classmethod
    def failure_counts_are_nonnegative(
        cls, value: Mapping[RunStatus, int]
    ) -> Mapping[RunStatus, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("failure breakdown counts must be non-negative")
        return value


class ClaimReport(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "gates": {
                                "properties": {
                                    name.value: {
                                        "properties": {"valid": {"const": True}},
                                        "required": ["valid"],
                                    }
                                    for name in REQUIRED_GATE_ORDER
                                },
                                "required": [
                                    name.value for name in REQUIRED_GATE_ORDER
                                ],
                            }
                        },
                        "required": ["gates"],
                    },
                    "then": {
                        "properties": {"claim_eligible": {"const": True}}
                    },
                    "else": {
                        "properties": {"claim_eligible": {"const": False}}
                    },
                },
                {
                    "if": {
                        "properties": {
                            "claim_eligible": {"const": True},
                            "effect": {"type": "object"},
                        },
                        "required": ["claim_eligible", "effect"],
                    },
                    "then": {
                        "properties": {
                            "effect_is_causal_claim": {"const": True}
                        }
                    },
                    "else": {
                        "properties": {
                            "effect_is_causal_claim": {"const": False}
                        }
                    },
                },
            ]
        }
    )

    purpose: Literal[RunPurpose.claim]
    comparison_kind: ComparisonKind
    subject_kinds: tuple[SubjectKind, ...]
    experiment_id: str = Field(pattern=ID_PATTERN)
    manifest_digest: str = Field(pattern=SHA256_PATTERN)
    gates: Mapping[GateName, GateResult] = Field(
        json_schema_extra={
            "propertyNames": {
                "enum": [name.value for name in REQUIRED_GATE_ORDER]
            },
            "minProperties": len(REQUIRED_GATE_NAMES),
            "maxProperties": len(REQUIRED_GATE_NAMES),
        }
    )
    effect: EffectEstimate | None = None
    statistics_receipt: StatisticalAnalysisReceipt | None = None
    metric_results: tuple[MetricResult, ...] = ()
    failure_breakdown: Mapping[RunStatus, int] = Field(default_factory=dict)
    lineage: tuple[LineageRef, ...] = ()
    record_index_ref: ArtifactRef | None = None

    @model_validator(mode="wrap")
    @classmethod
    def validate_derived_wire_fields(cls, value: Any, handler: Any) -> "ClaimReport":
        """Accept serialized computed fields only when they equal derivation.

        Production persistence includes computed fields.  They are redundant
        wire evidence, never caller-controlled overrides.
        """

        supplied: dict[str, Any] = {}
        if isinstance(value, Mapping):
            value = dict(value)
            for name in ("claim_eligible", "effect_is_causal_claim"):
                if name in value:
                    supplied[name] = value.pop(name)
        result = handler(value)
        for name, expected in (
            ("claim_eligible", result.claim_eligible),
            ("effect_is_causal_claim", result.effect_is_causal_claim),
        ):
            if name in supplied and (
                not isinstance(supplied[name], bool) or supplied[name] != expected
            ):
                raise ValueError(f"serialized {name} contradicts derived value")
        return result

    @field_validator("subject_kinds")
    @classmethod
    def claim_subject_kinds_are_unique(
        cls, values: tuple[SubjectKind, ...]
    ) -> tuple[SubjectKind, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("claim subject_kinds must be non-empty and unique")
        if SubjectKind.fake in values:
            raise ValueError("fake subjects cannot produce claims")
        return values

    @field_validator("gates")
    @classmethod
    def validate_gate_set(
        cls, value: Mapping[GateName, GateResult]
    ) -> Mapping[GateName, GateResult]:
        names = set(value)
        if GateName.claim_eligible in names:
            raise ValueError("claim_eligible is derived and must not appear in gates")
        missing = REQUIRED_GATE_NAMES - names
        unexpected = names - REQUIRED_GATE_NAMES
        if missing or unexpected:
            raise ValueError(
                f"gates must contain exactly the five validity gates; "
                f"missing={sorted(item.value for item in missing)}, "
                f"unexpected={sorted(item.value for item in unexpected)}"
            )
        return value

    @computed_field(return_type=bool)
    @property
    def claim_eligible(self) -> bool:
        """Whether every independently recorded validity gate passes."""

        return all(self.gates[name].valid for name in REQUIRED_GATE_NAMES)

    @computed_field(return_type=bool)
    @property
    def effect_is_causal_claim(self) -> bool:
        """Whether an estimate may be described using causal claim language."""

        return self.claim_eligible and self.effect is not None

    @model_validator(mode="after")
    def validate_failure_counts(self) -> "ClaimReport":
        invalid_counts = [count for count in self.failure_breakdown.values() if count < 0]
        if invalid_counts:
            raise ValueError("failure breakdown counts must be non-negative")
        return self


RunReport = Annotated[
    Union[ObservationReport, ClaimReport],
    Field(discriminator="purpose"),
]
RunReportAdapter = TypeAdapter(RunReport)


__all__ = [
    "BMP_VERSION",
    "AdapterCapability",
    "AdapterCapabilityArtifact",
    "IDENTITY_EXCLUDE",
    "ArtifactRef",
    "ConfigurationCompositionStep",
    "AttemptAllocation",
    "AttemptContext",
    "AttemptExecution",
    "AssemblySubjectArtifact",
    "AssemblySubjectSpec",
    "BackendSpec",
    "BenchmarkArtifact",
    "BenchmarkArtifactAdapter",
    "BenchmarkSpec",
    "BenchmarkSpecAdapter",
    "ConfigurationArtifact",
    "ConfigurationSelection",
    "ConfigurationSpec",
    "Budget",
    "BudgetAllocation",
    "BudgetDebit",
    "BudgetLedger",
    "CaseAllocation",
    "CaseArtifact",
    "CaseOrderArtifact",
    "CaseSetActivationReceipt",
    "CaseSetArtifact",
    "CheckpointLoadReceipt",
    "CheckpointSaveReceipt",
    "ClaimDesign",
    "ClaimReport",
    "ComparisonKind",
    "CustomBenchmarkArtifact",
    "CustomBenchmarkSpec",
    "DatasetArtifact",
    "DatasetSpec",
    "CustomCaseOrderSpec",
    "CredentialRef",
    "EffectEstimate",
    "EvolutionCandidateRecord",
    "EvolutionCandidateStatus",
    "EvolutionMethodArtifact",
    "EvolutionMethodSpec",
    "EvolutionRunEvidence",
    "EvolutionSelectionSpec",
    "EvolutionSurfacePolicy",
    "EvolutionTransitionPhase",
    "EvolutionTransitionRecord",
    "EvaluatorArtifact",
    "EvaluatorMetricBinding",
    "EvaluatorSpec",
    "EnvironmentBindingRef",
    "EnvironmentReceipt",
    "EnvironmentSpec",
    "EvidenceBundle",
    "ExecutionSpec",
    "ExperimentContrast",
    "ExperimentRegimeArtifact",
    "ExperimentRegimeKind",
    "ExperimentRegimeSpec",
    "ExperimentStageRole",
    "ExperimentStageSpec",
    "FactorActivationEvidence",
    "FactorArtifact",
    "FactorCategory",
    "FactorLevel",
    "FactorSpec",
    "FakeSubjectArtifact",
    "FakeSubjectSpec",
    "GateName",
    "GateResult",
    "AssemblySidecarRef",
    "RuntimeAssemblySidecarRef",
    "RuntimeManifestReceipt",
    "JournalRecord",
    "LineageRef",
    "MountSpec",
    "ModelActivationEvidence",
    "ModelActivationReceipt",
    "ModelActivationUsage",
    "MetricArtifact",
    "MetricAcrossGroupAggregation",
    "MetricComputationState",
    "MetricDirection",
    "MetricFormula",
    "MetricFormulaParameters",
    "MetricGroupKey",
    "MetricGroupResult",
    "MetricLevel",
    "MetricMissingDisposition",
    "MetricPopulation",
    "MetricResult",
    "MetricSample",
    "MetricSampleDisposition",
    "MetricSamplingDesign",
    "MetricSamplingSpec",
    "MetricSource",
    "MetricSpec",
    "MetricStatusDisposition",
    "MetricUncertaintyMethod",
    "MetricUncertaintyResult",
    "MetricUncertaintySpec",
    "MetricValueKind",
    "MetaEvolutionMethodArtifact",
    "MetaEvolutionMethodSpec",
    "NetworkBoundary",
    "NetworkEndpointRecord",
    "NetworkObservation",
    "NetworkObservationMode",
    "NetworkPolicySource",
    "ResolvedNetworkPolicy",
    "Observation",
    "ObservationReport",
    "PackageRecord",
    "ProtocolKind",
    "ProtocolSpec",
    "ProviderBinding",
    "ProvenanceRecord",
    "RecordIndex",
    "RegimeDependencyArtifact",
    "ResolvedBmpManifest",
    "ResolvedExecutionSpec",
    "ResolvedManifestMetadata",
    "ResourceSpec",
    "RunPurpose",
    "RunReport",
    "RunReportAdapter",
    "RunStatus",
    "ScheduleActivationReceipt",
    "ScoringKind",
    "StatisticalAnalysisPlan",
    "StatisticalAnalysisReceipt",
    "StageFeedbackVisibility",
    "StageStatePolicy",
    "SubjectKind",
    "SUBJECT_KIND_COMPARISON_MATRIX",
    "SubjectArtifact",
    "SubjectArtifactAdapter",
    "SubjectSpec",
    "SubjectSpecAdapter",
    "SystemPromptRecord",
    "TestOverrideReceipt",
    "TrajectoryCapture",
    "TrajectoryCaptureState",
    "TrajectoryEvent",
    "TrajectoryEventKind",
    "RolloutTrajectory",
    "UsageRecord",
    "VerifierEvidence",
    "WorkspaceRecord",
]
