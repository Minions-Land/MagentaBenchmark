"""Executable evolution/meta-evolution runtime and audit replay tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from MagentaBench.runner.evidence import artifact_ref, atomic_write_json
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.runner.evolution import (
    DeterministicLocalEvolutionAdapter,
    DeterministicTargetEvaluator,
    EvolutionBudgetExceeded,
    EvolutionRuntime,
    EvolutionRuntimeError,
)
from MagentaBench.schemas import (
    Budget,
    EvolutionEvaluationStage,
    EvolutionRuntimeReceipt,
    ReportVerificationError,
    schema_documents,
    verify_evolution_run_evidence,
    verify_observation_report,
)


ROOT = Path(__file__).parents[1]


def _evaluator(
    root: Path,
    *,
    name: str,
    stage: EvolutionEvaluationStage,
    target: int,
    metric: str,
) -> DeterministicTargetEvaluator:
    contract = root / f"{name}-evaluator.json"
    split = root / f"{name}-split.json"
    contract.write_text(
        json.dumps({"name": name, "stage": stage.value, "metric": metric}),
        encoding="utf-8",
    )
    split.write_text(json.dumps({"split": name, "target": target}), encoding="utf-8")
    return DeterministicTargetEvaluator.from_files(
        stage=stage,
        evaluator_path=contract,
        split_manifest_path=split,
    )


def _run(root: Path, *, run_id: str = "evolution-run"):
    search = _evaluator(
        root,
        name=f"{run_id}-search",
        stage=EvolutionEvaluationStage.search,
        target=3,
        metric="search_quality",
    )
    holdout = _evaluator(
        root,
        name=f"{run_id}-holdout",
        stage=EvolutionEvaluationStage.sealed_holdout,
        target=5,
        metric="holdout_quality",
    )
    result = EvolutionRuntime(root / "records").execute(
        run_id=run_id,
        kind="evolver",
        adapter=DeterministicLocalEvolutionAdapter(generation_step=2),
        search_evaluator=search,
        holdout_evaluator=holdout,
        budget=Budget(max_tokens=6, max_cost=0.0, max_wall_seconds=10.0),
        public_input=b'{"seed":0}',
    )
    return result, search, holdout


def _meta_run(root: Path):
    parent, _, _ = _run(root, run_id="parent-evolver")
    search = _evaluator(
        root,
        name="meta-search",
        stage=EvolutionEvaluationStage.search,
        target=2,
        metric="meta_search_quality",
    )
    holdout = _evaluator(
        root,
        name="meta-holdout",
        stage=EvolutionEvaluationStage.sealed_holdout,
        target=4,
        metric="meta_holdout_quality",
    )
    result = EvolutionRuntime(root / "records").execute(
        run_id="meta-evolution-run",
        kind="meta_evolver",
        adapter=DeterministicLocalEvolutionAdapter(generation_step=1),
        search_evaluator=search,
        holdout_evaluator=holdout,
        budget=Budget(max_tokens=12, max_cost=0.0, max_wall_seconds=10.0),
        public_input=b'{"seed":0}',
        parent_evidence_ref=artifact_ref(parent.evidence_path),
    )
    return parent, result


def test_deterministic_runtime_executes_full_lifecycle_and_replays(tmp_path: Path) -> None:
    result, search, holdout = _run(tmp_path)

    assert result.evidence.claim_ready is True
    assert result.evidence.selected_candidate_id == "revised"
    assert [item.status.value for item in result.evidence.candidate_ledger] == [
        "rejected",
        "rejected",
        "selected",
    ]
    assert [item.phase.value for item in result.evidence.transition_ledger] == [
        "seed",
        "generate",
        "feedback",
        "revise",
        "feedback",
        "select",
        "terminate",
    ]

    receipt = result.runtime_receipt
    assert [item.stage.value for item in receipt.evaluations] == [
        "search",
        "search",
        "sealed_holdout",
    ]
    assert receipt.evaluations[-1].after_transition_sequence == 5
    assert receipt.sealed_holdout.selection_transition_sequence == 5
    assert receipt.sealed_holdout.search_evaluator_digest == search.digest
    assert receipt.sealed_holdout.holdout_evaluator_digest == holdout.digest
    assert holdout.split_access_count == 1
    assert receipt.budget_ledger.total_usage.total_tokens == 6
    assert receipt.budget_ledger.reconciles_exactly is True
    assert receipt.budget_ledger.events[-1].remaining_after.max_tokens == 0

    verified = verify_evolution_run_evidence(result.evidence_path)
    assert verified.runtime_receipt == receipt


def test_runtime_rejects_shared_search_and_holdout_authority(tmp_path: Path) -> None:
    search = _evaluator(
        tmp_path,
        name="shared",
        stage=EvolutionEvaluationStage.search,
        target=3,
        metric="quality",
    )
    holdout = DeterministicTargetEvaluator(
        stage=EvolutionEvaluationStage.sealed_holdout,
        evaluator_ref=search.evaluator_ref,
        split_manifest_ref=search.split_manifest_ref,
        target=None,
        metric="quality",
    )
    with pytest.raises(EvolutionRuntimeError, match="authorities must differ"):
        EvolutionRuntime(tmp_path / "records").execute(
            run_id="shared-authority",
            kind="evolver",
            adapter=DeterministicLocalEvolutionAdapter(),
            search_evaluator=search,
            holdout_evaluator=holdout,
            budget=Budget(max_tokens=6, max_cost=0.0),
            public_input=b'{"seed":0}',
        )


def test_runtime_rejects_holdout_opened_before_selection(tmp_path: Path) -> None:
    search = _evaluator(
        tmp_path,
        name="early-search",
        stage=EvolutionEvaluationStage.search,
        target=3,
        metric="search_quality",
    )
    holdout = _evaluator(
        tmp_path,
        name="early-holdout",
        stage=EvolutionEvaluationStage.sealed_holdout,
        target=5,
        metric="holdout_quality",
    )
    holdout.evaluate(b'{"value":0}')
    assert holdout.split_access_count == 1

    with pytest.raises(EvolutionRuntimeError, match="opened before selection"):
        EvolutionRuntime(tmp_path / "records").execute(
            run_id="early-holdout",
            kind="evolver",
            adapter=DeterministicLocalEvolutionAdapter(),
            search_evaluator=search,
            holdout_evaluator=holdout,
            budget=Budget(max_tokens=6, max_cost=0.0),
            public_input=b'{"seed":0}',
        )


def test_runtime_rejects_run_path_escape_before_writing(tmp_path: Path) -> None:
    search = _evaluator(
        tmp_path,
        name="path-search",
        stage=EvolutionEvaluationStage.search,
        target=3,
        metric="search_quality",
    )
    holdout = _evaluator(
        tmp_path,
        name="path-holdout",
        stage=EvolutionEvaluationStage.sealed_holdout,
        target=5,
        metric="holdout_quality",
    )
    runtime = EvolutionRuntime(tmp_path / "records")
    with pytest.raises(EvolutionRuntimeError, match="run_id"):
        runtime.execute(
            run_id="../escape",
            kind="evolver",
            adapter=DeterministicLocalEvolutionAdapter(),
            search_evaluator=search,
            holdout_evaluator=holdout,
            budget=Budget(max_tokens=6, max_cost=0.0),
            public_input=b'{"seed":0}',
        )
    assert not (tmp_path / "escape").exists()

    (tmp_path / "records").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "records" / "linked-run").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(EvolutionRuntimeError, match="not stable"):
        runtime.execute(
            run_id="linked-run",
            kind="evolver",
            adapter=DeterministicLocalEvolutionAdapter(),
            search_evaluator=search,
            holdout_evaluator=holdout,
            budget=Budget(max_tokens=6, max_cost=0.0),
            public_input=b'{"seed":0}',
        )
    assert tuple(outside.iterdir()) == ()


def test_runtime_enforces_root_budget(tmp_path: Path) -> None:
    search = _evaluator(
        tmp_path,
        name="budget-search",
        stage=EvolutionEvaluationStage.search,
        target=3,
        metric="search_quality",
    )
    holdout = _evaluator(
        tmp_path,
        name="budget-holdout",
        stage=EvolutionEvaluationStage.sealed_holdout,
        target=5,
        metric="holdout_quality",
    )
    with pytest.raises(EvolutionBudgetExceeded, match="holdout_evaluate"):
        EvolutionRuntime(tmp_path / "records").execute(
            run_id="budget-exhaustion",
            kind="evolver",
            adapter=DeterministicLocalEvolutionAdapter(),
            search_evaluator=search,
            holdout_evaluator=holdout,
            budget=Budget(max_tokens=5, max_cost=0.0),
            public_input=b'{"seed":0}',
        )
    assert not (
        tmp_path
        / "records"
        / "budget-exhaustion"
        / "evolution-runtime-receipt.json"
    ).exists()
    assert not (
        tmp_path
        / "records"
        / "budget-exhaustion"
        / "evaluations"
        / "evaluation-0002-request.json"
    ).exists()
    assert holdout.split_access_count == 0


def test_standalone_verifier_rejects_rebound_selection_lineage(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path)
    payload = json.loads(result.runtime_receipt_path.read_text(encoding="utf-8"))
    payload["sealed_holdout"]["selection_transition_id"] = "transition-generate"
    rebound_receipt = EvolutionRuntimeReceipt.model_validate(payload)
    atomic_write_json(result.runtime_receipt_path, rebound_receipt)
    rebound_evidence = result.evidence.model_copy(
        update={"runtime_receipt_ref": artifact_ref(result.runtime_receipt_path)}
    )
    atomic_write_json(result.evidence_path, rebound_evidence)

    with pytest.raises(ReportVerificationError, match="selection transition drift"):
        verify_evolution_run_evidence(result.evidence_path)


def test_standalone_verifier_rejects_sealed_split_byte_drift(tmp_path: Path) -> None:
    result, _, holdout = _run(tmp_path)
    Path(holdout.split_manifest_ref.path).write_text(
        json.dumps({"split": "changed", "target": 100}), encoding="utf-8"
    )
    with pytest.raises(ReportVerificationError, match="split_manifest_ref"):
        verify_evolution_run_evidence(result.evidence_path)


def test_meta_evolution_executes_with_recursively_verified_parent(tmp_path: Path) -> None:
    _, result = _meta_run(tmp_path)

    verified = verify_evolution_run_evidence(result.evidence_path)
    assert verified.evidence.kind == "meta_evolver"
    assert verified.runtime_receipt is not None
    assert (
        verified.runtime_receipt.budget_ledger.events[0].operation.value
        == "parent_evolution"
    )
    assert verified.runtime_receipt.budget_ledger.total_usage.total_tokens == 12
    assert verified.nested_parent is not None
    assert verified.nested_parent.evidence.run_id == "parent-evolver"


def test_verifier_replays_recursive_parent_budget_usage(tmp_path: Path) -> None:
    _, result = _meta_run(tmp_path)
    payload = json.loads(result.runtime_receipt_path.read_text(encoding="utf-8"))
    ledger = payload["budget_ledger"]
    ledger["events"][0]["spent"]["total_tokens"] = 5
    for event in ledger["events"]:
        event["remaining_after"]["max_tokens"] += 1
    ledger["total_usage"]["total_tokens"] = 11
    rebound_receipt = EvolutionRuntimeReceipt.model_validate(payload)
    atomic_write_json(result.runtime_receipt_path, rebound_receipt)
    rebound_evidence = result.evidence.model_copy(
        update={"runtime_receipt_ref": artifact_ref(result.runtime_receipt_path)}
    )
    atomic_write_json(result.evidence_path, rebound_evidence)

    with pytest.raises(ReportVerificationError, match="parent budget usage drift"):
        verify_evolution_run_evidence(result.evidence_path)


def test_runtime_receipt_schemas_are_public() -> None:
    assert {
        "evolution-budget-event",
        "evolution-budget-ledger",
        "evolution-evaluation-record",
        "evolution-runtime-receipt",
        "evolution-sealed-holdout-receipt",
    }.issubset(schema_documents())


@pytest.mark.parametrize(
    ("experiment_name", "experiment_id", "kind", "total_tokens"),
    (
        (
            "deterministic-evolution-smoke.toml",
            "deterministic-evolution-smoke",
            "evolver",
            6,
        ),
        (
            "deterministic-meta-evolution-smoke.toml",
            "deterministic-meta-evolution-smoke",
            "meta_evolver",
            12,
        ),
    ),
)
def test_registered_evolution_adapter_runs_through_bmp_pipeline(
    tmp_path: Path,
    experiment_name: str,
    experiment_id: str,
    kind: str,
    total_tokens: int,
) -> None:
    experiment = (
        ROOT
        / "MagentaBench/conformance/experiments"
        / experiment_name
    )

    result = Pipeline(ROOT, tmp_path / "records").run(experiment)
    verified = verify_observation_report(result.report_path)

    assert verified.report.experiment_id == experiment_id
    assert verified.report.subject_kind.value == kind
    assert verified.report.protocol_valid is True
    assert verified.report.isolation_valid is False
    assert len(result.runs) == 1
    evolution_ref = result.runs[0].case.bundle.provenance.evolution_evidence_ref
    assert evolution_ref is not None
    evolution = verify_evolution_run_evidence(evolution_ref.path)
    assert evolution.evidence.kind == kind
    assert evolution.evidence.claim_ready
    assert evolution.runtime_receipt is not None
    assert evolution.runtime_receipt.budget_ledger.total_usage.total_tokens == total_tokens
    assert (evolution.nested_parent is not None) == (kind == "meta_evolver")
