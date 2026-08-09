from __future__ import annotations

import json
from pathlib import Path

import pytest

from MagentaBench.runner.evolution import (
    DeterministicLocalEvolutionAdapter,
    DeterministicTargetEvaluator,
    EvolutionRuntime,
)
from MagentaBench.runner.evidence import artifact_ref
from MagentaBench.schemas import Budget, EvolutionEvaluationStage, EvolutionRuntimeReceipt


def _run(root: Path):
    def evaluator(name: str, stage: EvolutionEvaluationStage, target: int):
        contract = root / f"{name}-evaluator.json"
        split = root / f"{name}-split.json"
        contract.write_text(
            json.dumps(
                {
                    "name": name,
                    "stage": stage.value,
                    "metric": "search_quality" if stage == EvolutionEvaluationStage.search else "holdout_quality",
                }
            ),
            encoding="utf-8",
        )
        split.write_text(json.dumps({"split": name, "target": target}), encoding="utf-8")
        return DeterministicTargetEvaluator.from_files(
            stage=stage,
            evaluator_path=contract,
            split_manifest_path=split,
        )

    search = evaluator("search", EvolutionEvaluationStage.search, 3)
    holdout = evaluator("holdout", EvolutionEvaluationStage.sealed_holdout, 5)
    return EvolutionRuntime(root / "records").execute(
        run_id="archive-receipt-run",
        kind="evolver",
        adapter=DeterministicLocalEvolutionAdapter(generation_step=2),
        search_evaluator=search,
        holdout_evaluator=holdout,
        budget=Budget(max_tokens=6, max_cost=0.0, max_wall_seconds=10.0),
        public_input=b'{"seed":0}',
    )


def test_archive_selection_and_promotion_receipts_are_complete(tmp_path: Path) -> None:
    result = _run(tmp_path)
    receipt = result.runtime_receipt
    assert receipt.archive_ledger is not None
    assert receipt.parent_selection is not None
    assert receipt.promotion_gate is not None

    archive = receipt.archive_ledger
    assert len(archive.transitions) == 8
    assert archive.transitions[0].previous_transition_digest is None
    assert archive.transitions[-1].after_archive_digest == archive.final_archive_digest
    assert receipt.parent_selection.selected_candidate_id == receipt.selected_candidate_id
    assert {item.candidate_id for item in receipt.parent_selection.candidate_set} == {
        "seed",
        "generated",
        "revised",
    }
    assert sum(item.probability for item in receipt.parent_selection.candidate_set) == pytest.approx(1.0)
    assert receipt.promotion_gate.full_evaluation_id == receipt.sealed_holdout.holdout_evaluation_id


def test_archive_receipt_rejects_state_digest_mutation(tmp_path: Path) -> None:
    result = _run(tmp_path)
    receipt = result.runtime_receipt
    assert receipt.archive_ledger is not None
    transition = receipt.archive_ledger.transitions[1]
    mutated_after = transition.after.model_copy(
        update={"metric": "forged-metric"}
    )
    mutated_transition = transition.model_copy(update={"after": mutated_after})
    mutated_transitions = list(receipt.archive_ledger.transitions)
    mutated_transitions[1] = mutated_transition
    with pytest.raises(ValueError, match="archive transition after digest drift"):
        EvolutionRuntimeReceipt.model_validate(
            receipt.model_copy(
                update={
                    "archive_ledger": receipt.archive_ledger.model_copy(
                        update={"transitions": tuple(mutated_transitions)}
                    )
                }
            ).model_dump(mode="json")
        )


def test_archive_receipt_keeps_candidate_artifacts_content_addressed(tmp_path: Path) -> None:
    result = _run(tmp_path)
    archive = result.runtime_receipt.archive_ledger
    assert archive is not None
    final_entries = archive.transitions[-1].after.entries
    assert final_entries
    for entry in final_entries:
        path = Path(entry.candidate_ref.path)
        assert path.is_file()
        assert artifact_ref(path) == entry.candidate_ref
