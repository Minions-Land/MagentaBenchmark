"""Neutral evolution/meta-evolution evidence contract tests."""

from __future__ import annotations

from pathlib import Path
import hashlib

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import (
    ArtifactRef,
    EvolutionCandidateRecord,
    EvolutionRunEvidence,
    EvolutionTransitionRecord,
    load_evolution_run_evidence,
    schema_documents,
    verify_evolution_run_evidence,
)


SHA = "a" * 64
OTHER_SHA = "b" * 64


def _ref(name: str = "artifact.json", digest: str = SHA) -> ArtifactRef:
    return ArtifactRef(path=str(Path("/tmp") / name), sha256=digest, size_bytes=4)


def _evidence(*, kind: str = "evolver", parent: ArtifactRef | None = None) -> EvolutionRunEvidence:
    seed = EvolutionCandidateRecord(
        candidate_id="seed",
        generation=0,
        status="generated",
        artifact_refs=(_ref(),),
    )
    rejected = EvolutionCandidateRecord(
        candidate_id="rejected",
        generation=1,
        parent_ids=("seed",),
        status="rejected",
        feedback_refs=(_ref("feedback.json", OTHER_SHA),),
    )
    selected = EvolutionCandidateRecord(
        candidate_id="winner",
        generation=1,
        parent_ids=("seed",),
        status="selected",
        score=0.9,
        score_metric="quality",
        evaluator_digest=SHA,
    )
    transitions = (
        EvolutionTransitionRecord(
            transition_id="seed-step",
            sequence=0,
            phase="seed",
            output_candidate_ids=("seed",),
        ),
        EvolutionTransitionRecord(
            transition_id="generate-step",
            sequence=1,
            phase="select",
            input_candidate_ids=("seed",),
            output_candidate_ids=("rejected", "winner"),
        ),
        EvolutionTransitionRecord(
            transition_id="terminate-step",
            sequence=2,
            phase="terminate",
        ),
    )
    return EvolutionRunEvidence(
        run_id="evolution-run",
        kind=kind,
        adapter_digest=SHA,
        evaluator_digest=SHA,
        budget_digest=OTHER_SHA,
        candidate_ledger=(seed, rejected, selected),
        transition_ledger=transitions,
        selected_candidate_id="winner",
        termination_reason="budget_exhausted",
        parent_evidence_ref=parent,
        attributes={"strategy": {"name": "revision", "attempts": 2}},
    )


def test_evolution_evidence_retains_rejected_candidates_and_is_content_addressed() -> None:
    evidence = _evidence()
    assert {item.status.value for item in evidence.candidate_ledger} == {
        "generated",
        "rejected",
        "selected",
    }
    assert evidence.evidence_complete is True
    assert evidence.canonical_digest() == _evidence().canonical_digest()
    relocated = evidence.model_copy(
        update={
            "candidate_ledger": tuple(
                item.model_copy(
                    update={
                        "artifact_refs": tuple(
                            ArtifactRef(
                                path=ref.path.replace("/tmp", "/relocated"),
                                sha256=ref.sha256,
                                size_bytes=ref.size_bytes,
                            )
                            for ref in item.artifact_refs
                        )
                    }
                )
                for item in evidence.candidate_ledger
            )
        }
    )
    assert relocated.canonical_digest() == evidence.canonical_digest()
    with pytest.raises(TypeError):
        evidence.attributes["new"] = True  # type: ignore[index]


def test_evolution_evidence_accepts_compatibility_aliases() -> None:
    payload = _evidence().model_dump(mode="json")
    payload["candidates"] = payload.pop("candidate_ledger")
    payload["transitions"] = payload.pop("transition_ledger")
    parsed = EvolutionRunEvidence.model_validate(payload)
    assert parsed.selected_candidate_id == "winner"


def test_evolution_evidence_requires_complete_binding() -> None:
    with pytest.raises(ValidationError, match="unknown parent"):
        EvolutionRunEvidence.model_validate(
            _evidence()
            .model_copy(
                update={
                    "candidate_ledger": (
                        _evidence().candidate_ledger[0].model_copy(
                            update={"parent_ids": ("missing",)}
                        ),
                        *_evidence().candidate_ledger[1:],
                    )
                }
            )
            .model_dump(mode="python")
        )

    with pytest.raises(ValidationError, match="references unknown candidates"):
        EvolutionRunEvidence.model_validate(
            _evidence()
            .model_copy(
                update={
                    "transition_ledger": (
                        EvolutionTransitionRecord(
                            transition_id="bad-step",
                            sequence=0,
                            phase="generate",
                            output_candidate_ids=("missing",),
                        ),
                        EvolutionTransitionRecord(
                            transition_id="done-step",
                            sequence=1,
                            phase="terminate",
                        ),
                    )
                }
            )
            .model_dump(mode="python")
        )


def test_meta_evolver_requires_a_parent_evidence_reference() -> None:
    with pytest.raises(ValidationError, match="requires parent_evidence_ref"):
        _evidence(kind="meta_evolver")
    parent = _ref("parent-evolution.json", OTHER_SHA)
    nested = _evidence(kind="meta_evolver", parent=parent)
    assert nested.parent_evidence_ref == parent
    with pytest.raises(ValidationError, match="cannot carry parent_evidence_ref"):
        _evidence(parent=parent)


def test_evolution_schemas_are_public_documents() -> None:
    documents = schema_documents()
    assert {
        "evolution-candidate-record",
        "evolution-transition-record",
        "evolution-run-evidence",
    }.issubset(documents)
    assert "candidate_ledger" in documents["evolution-run-evidence"]["properties"]
    assert "transition_ledger" in documents["evolution-run-evidence"]["properties"]


def test_evolution_evidence_loader_replays_json(tmp_path: Path) -> None:
    path = tmp_path / "evolution.json"
    path.write_bytes(_evidence().model_dump_json().encode("utf-8"))
    loaded = load_evolution_run_evidence(path)
    assert loaded.canonical_digest() == _evidence().canonical_digest()


def test_evolution_verifier_replays_nested_parent_evidence(tmp_path: Path) -> None:
    def without_artifact_refs(evidence: EvolutionRunEvidence) -> EvolutionRunEvidence:
        return evidence.model_copy(
            update={
                "candidate_ledger": tuple(
                    candidate.model_copy(
                        update={"artifact_refs": (), "feedback_refs": ()}
                    )
                    for candidate in evidence.candidate_ledger
                )
            }
        )

    parent_path = tmp_path / "parent.json"
    parent_bytes = without_artifact_refs(_evidence()).model_dump_json().encode("utf-8")
    parent_path.write_bytes(parent_bytes)
    parent_ref = ArtifactRef(
        path=str(parent_path),
        sha256=hashlib.sha256(parent_bytes).hexdigest(),
        size_bytes=len(parent_bytes),
    )
    child = without_artifact_refs(_evidence(kind="meta_evolver", parent=parent_ref))
    child_path = tmp_path / "child.json"
    child_path.write_bytes(child.model_dump_json().encode("utf-8"))

    verified = verify_evolution_run_evidence(child_path)
    assert verified.evidence.kind == "meta_evolver"
    assert verified.nested_parent is not None
    assert verified.nested_parent.evidence.kind == "evolver"

    child_path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid evolution evidence"):
        verify_evolution_run_evidence(child_path)
