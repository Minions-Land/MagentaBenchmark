"""BMP-owned execution boundary for evolution and meta-evolution adapters.

The runtime deliberately owns ordering, evaluator access and evidence writes.
An evolution strategy sees public input plus search feedback; it never receives
the sealed evaluator or its split manifest.  The deterministic local strategy
and evaluator are a reference implementation suitable for conformance tests
and zero-provider smoke runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from MagentaBench.schemas import (
    ArtifactRef,
    Budget,
    BudgetAllocation,
    EvolutionCandidateRecord,
    EvolutionRunEvidence,
    EvolutionTransitionRecord,
    UsageRecord,
)
from MagentaBench.schemas.compiler import canonical_digest, canonical_json
from MagentaBench.schemas.evolution import (
    EvolutionBudgetEvent,
    EvolutionBudgetLedger,
    EvolutionBudgetOperation,
    EvolutionEvaluationRecord,
    EvolutionEvaluationStage,
    EvolutionRuntimeReceipt,
    EvolutionSealedHoldoutReceipt,
)

from .evidence import artifact_ref, atomic_write_bytes


class EvolutionRuntimeError(RuntimeError):
    """An evolution adapter violated the runtime contract."""


class EvolutionBudgetExceeded(EvolutionRuntimeError):
    """A deterministic operation exhausted its declared root budget."""


@dataclass(frozen=True)
class EvolutionAction:
    """Opaque candidate bytes plus measured adapter usage."""

    content: bytes
    usage: UsageRecord
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class EvolutionEvaluation:
    """One evaluator result returned to the BMP runtime."""

    score: float
    score_metric: str
    feedback: bytes
    usage: UsageRecord
    attributes: Mapping[str, Any]


class EvolutionAdapter(Protocol):
    """Candidate producer that has no handle to the sealed evaluator."""

    adapter_ref: ArtifactRef
    digest: str

    def usage_bound(self, operation: EvolutionBudgetOperation) -> UsageRecord: ...

    def seed(self, public_input: bytes) -> EvolutionAction: ...

    def generate(self, parent: bytes) -> EvolutionAction: ...

    def revise(self, parent: bytes, search_feedback: bytes) -> EvolutionAction: ...


class EvolutionEvaluator(Protocol):
    """Evaluator authority injected separately from the strategy."""

    stage: EvolutionEvaluationStage
    evaluator_ref: ArtifactRef
    split_manifest_ref: ArtifactRef
    digest: str
    metric: str
    split_access_count: int

    def usage_bound(self) -> UsageRecord: ...

    def evaluate(self, candidate: bytes) -> EvolutionEvaluation: ...


@dataclass(frozen=True)
class EvolutionRuntimeResult:
    """Persisted runtime receipt and public evolution evidence."""

    evidence: EvolutionRunEvidence
    evidence_path: Path
    runtime_receipt: EvolutionRuntimeReceipt
    runtime_receipt_path: Path


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _write_immutable(path: Path, content: bytes, *, label: str) -> None:
    current = path.parent
    while current != current.parent:
        if current.is_symlink():
            raise EvolutionRuntimeError(f"{label} path contains a symlink: {current}")
        current = current.parent
    if path.is_symlink():
        raise EvolutionRuntimeError(f"{label} path is a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise EvolutionRuntimeError(f"existing {label} byte drift: {path}")
        return
    atomic_write_bytes(path, content)


def _observed_usage(tokens: int = 1, cost: float = 0.0) -> UsageRecord:
    return UsageRecord(total_tokens=tokens, cost=cost)


def _parse_value(content: bytes, *, label: str) -> int:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvolutionRuntimeError(f"{label} is not JSON") from exc
    if isinstance(value, Mapping):
        value = value.get("value", value.get("seed"))
    if not isinstance(value, int) or isinstance(value, bool):
        raise EvolutionRuntimeError(f"{label} requires an integer value")
    return value


class DeterministicLocalEvolutionAdapter:
    """Provider-free generate/feedback/revise strategy.

    The strategy performs integer search.  It only learns a direction from
    search feedback and therefore cannot infer the sealed holdout target.
    """

    def __init__(self, *, generation_step: int = 2) -> None:
        if generation_step == 0:
            raise ValueError("generation_step must be non-zero")
        self.generation_step = generation_step
        self.adapter_ref = artifact_ref(Path(__file__))
        self.digest = self.adapter_ref.sha256

    def usage_bound(self, operation: EvolutionBudgetOperation) -> UsageRecord:
        if operation not in {
            EvolutionBudgetOperation.seed,
            EvolutionBudgetOperation.generate,
            EvolutionBudgetOperation.revise,
        }:
            raise EvolutionRuntimeError(
                f"deterministic adapter cannot execute {operation.value}"
            )
        return _observed_usage()

    def seed(self, public_input: bytes) -> EvolutionAction:
        value = _parse_value(public_input, label="evolution public input")
        return EvolutionAction(
            content=_json_bytes({"value": value}),
            usage=_observed_usage(),
            attributes={"operation": "seed"},
        )

    def generate(self, parent: bytes) -> EvolutionAction:
        value = _parse_value(parent, label="parent candidate")
        return EvolutionAction(
            content=_json_bytes({"value": value + self.generation_step}),
            usage=_observed_usage(),
            attributes={"operation": "generate", "step": self.generation_step},
        )

    def revise(self, parent: bytes, search_feedback: bytes) -> EvolutionAction:
        value = _parse_value(parent, label="parent candidate")
        try:
            feedback = json.loads(search_feedback)
            direction = feedback["direction"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise EvolutionRuntimeError("search feedback lacks a direction") from exc
        if direction not in {-1, 0, 1}:
            raise EvolutionRuntimeError("search feedback direction must be -1, 0 or 1")
        return EvolutionAction(
            content=_json_bytes({"value": value + direction}),
            usage=_observed_usage(),
            attributes={"operation": "revise", "direction": direction},
        )


class DeterministicTargetEvaluator:
    """Provider-free evaluator backed by separate contract and split bytes."""

    def __init__(
        self,
        *,
        stage: EvolutionEvaluationStage | str,
        evaluator_ref: ArtifactRef,
        split_manifest_ref: ArtifactRef,
        target: int | None,
        metric: str,
    ) -> None:
        self.stage = EvolutionEvaluationStage(stage)
        self.evaluator_ref = evaluator_ref
        self.split_manifest_ref = split_manifest_ref
        if target is not None and (
            not isinstance(target, int) or isinstance(target, bool)
        ):
            raise ValueError("evaluator target must be an integer")
        if self.stage == EvolutionEvaluationStage.sealed_holdout and target is not None:
            raise ValueError("sealed-holdout target must be loaded lazily from its split")
        self._target = target
        self.split_access_count = 0
        if not metric:
            raise ValueError("evaluator metric must be non-empty")
        self.metric = metric
        self.digest = evaluator_ref.sha256

    def usage_bound(self) -> UsageRecord:
        return _observed_usage()

    def _sealed_target(self) -> int:
        if self._target is not None:
            return self._target
        source = Path(self.split_manifest_ref.path).resolve(strict=True)
        content = source.read_bytes()
        observed = ArtifactRef(
            path=str(source),
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
        if observed != self.split_manifest_ref:
            raise EvolutionRuntimeError("evaluator split manifest byte drift")
        try:
            value = json.loads(content)["target"]
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            raise EvolutionRuntimeError("deterministic evaluator split is malformed") from exc
        if not isinstance(value, int) or isinstance(value, bool):
            raise EvolutionRuntimeError("evaluator split target is invalid")
        self._target = value
        self.split_access_count += 1
        return value

    @classmethod
    def from_files(
        cls,
        *,
        stage: EvolutionEvaluationStage | str,
        evaluator_path: str | Path,
        split_manifest_path: str | Path,
    ) -> "DeterministicTargetEvaluator":
        evaluator_source = Path(evaluator_path).resolve(strict=True)
        split_source = Path(split_manifest_path).resolve(strict=True)
        try:
            evaluator_data = json.loads(evaluator_source.read_bytes())
            metric = evaluator_data["metric"]
            declared_stage = evaluator_data["stage"]
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            raise EvolutionRuntimeError("deterministic evaluator files are malformed") from exc
        requested_stage = EvolutionEvaluationStage(stage)
        if declared_stage != requested_stage.value:
            raise EvolutionRuntimeError("evaluator contract stage drift")
        if not isinstance(metric, str) or not metric:
            raise EvolutionRuntimeError("evaluator contract metric is invalid")
        return cls(
            stage=requested_stage,
            evaluator_ref=artifact_ref(evaluator_source),
            split_manifest_ref=artifact_ref(split_source),
            target=None,
            metric=metric,
        )

    def evaluate(self, candidate: bytes) -> EvolutionEvaluation:
        value = _parse_value(candidate, label="evaluated candidate")
        target = self._sealed_target()
        distance = abs(target - value)
        direction = 0 if distance == 0 else (1 if value < target else -1)
        return EvolutionEvaluation(
            score=-float(distance),
            score_metric=self.metric,
            feedback=_json_bytes(
                {
                    "direction": direction,
                    "distance": distance,
                    "stage": self.stage.value,
                }
            ),
            usage=_observed_usage(),
            attributes={"distance": distance},
        )


class _BudgetTracker:
    def __init__(self, budget: Budget, budget_ref: ArtifactRef) -> None:
        self.budget = budget
        self.budget_ref = budget_ref
        self.events: list[EvolutionBudgetEvent] = []
        self.tokens = 0
        self.cost = 0.0

    @staticmethod
    def _observable(usage: UsageRecord) -> tuple[int, float]:
        if usage.total_tokens is None or usage.cost is None:
            raise EvolutionRuntimeError(
                "deterministic evolution requires observable token and cost usage"
            )
        return usage.total_tokens, usage.cost

    def require_available(
        self,
        operation: EvolutionBudgetOperation,
        usage_bound: UsageRecord,
    ) -> None:
        tokens, cost = self._observable(usage_bound)
        token_cap = self.budget.max_tokens
        cost_cap = self.budget.max_cost
        if (
            (token_cap is not None and self.tokens + tokens > token_cap)
            or (cost_cap is not None and self.cost + cost > cost_cap + 1e-15)
        ):
            raise EvolutionBudgetExceeded(
                f"evolution budget exhausted before {operation.value}"
            )

    @staticmethod
    def validate_bound(actual: UsageRecord, bound: UsageRecord) -> None:
        actual_tokens, actual_cost = _BudgetTracker._observable(actual)
        bound_tokens, bound_cost = _BudgetTracker._observable(bound)
        if actual_tokens > bound_tokens or actual_cost > bound_cost + 1e-15:
            raise EvolutionRuntimeError(
                "evolution operation exceeded its declared usage bound"
            )

    def charge(
        self,
        operation: EvolutionBudgetOperation,
        candidate_ids: tuple[str, ...],
        usage: UsageRecord,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = f"budget-{len(self.events):04d}"
        usage_tokens, usage_cost = self._observable(usage)
        self.tokens += usage_tokens
        self.cost += usage_cost
        token_cap = self.budget.max_tokens
        cost_cap = self.budget.max_cost
        exceeded = (
            (token_cap is not None and self.tokens > token_cap)
            or (cost_cap is not None and self.cost > cost_cap + 1e-15)
        )
        event = EvolutionBudgetEvent(
            event_id=event_id,
            sequence=len(self.events),
            operation=operation,
            candidate_ids=candidate_ids,
            spent=usage,
            usage_observable=True,
            remaining_after=BudgetAllocation(
                max_tokens=(
                    None if token_cap is None else max(0, token_cap - self.tokens)
                ),
                max_cost=(
                    None if cost_cap is None else max(0.0, cost_cap - self.cost)
                ),
            ),
            budget_exceeded=exceeded,
            attributes={} if attributes is None else attributes,
        )
        self.events.append(event)
        if exceeded:
            raise EvolutionBudgetExceeded(
                f"evolution budget exhausted by {operation.value}"
            )
        return event_id

    def ledger(self, elapsed: float) -> EvolutionBudgetLedger:
        wall_cap = self.budget.max_wall_seconds
        if wall_cap is not None and elapsed > wall_cap:
            raise EvolutionBudgetExceeded("evolution wall budget exhausted")
        return EvolutionBudgetLedger(
            budget_digest=self.budget_ref.sha256,
            budget_ref=self.budget_ref,
            declared_budget=self.budget,
            events=tuple(self.events),
            total_usage=UsageRecord(total_tokens=self.tokens, cost=self.cost),
            elapsed_wall_seconds=elapsed,
            usage_observable=True,
            reconciles_exactly=True,
            budget_exceeded=False,
        )


class EvolutionRuntime:
    """Execute one deterministic candidate lifecycle under BMP governance."""

    def __init__(self, record_root: str | Path) -> None:
        self.record_root = Path(record_root).resolve()

    @staticmethod
    def _ledger_digest(items: tuple[Any, ...]) -> str:
        identity = [
            item.identity_data()
            if hasattr(item, "identity_data")
            else item.model_dump(mode="json")
            for item in items
        ]
        return canonical_digest(identity)

    @staticmethod
    def _validate_authorities(
        adapter: EvolutionAdapter,
        search_evaluator: EvolutionEvaluator,
        holdout_evaluator: EvolutionEvaluator,
    ) -> None:
        if adapter.adapter_ref.sha256 != adapter.digest:
            raise EvolutionRuntimeError("evolution adapter digest drift")
        if search_evaluator.stage != EvolutionEvaluationStage.search:
            raise EvolutionRuntimeError("search evaluator has the wrong authority")
        if holdout_evaluator.stage != EvolutionEvaluationStage.sealed_holdout:
            raise EvolutionRuntimeError("holdout evaluator has the wrong authority")
        if search_evaluator.evaluator_ref.sha256 != search_evaluator.digest:
            raise EvolutionRuntimeError("search evaluator digest drift")
        if holdout_evaluator.evaluator_ref.sha256 != holdout_evaluator.digest:
            raise EvolutionRuntimeError("holdout evaluator digest drift")
        if search_evaluator.digest == holdout_evaluator.digest:
            raise EvolutionRuntimeError(
                "search and sealed-holdout evaluator authorities must differ"
            )
        if (
            search_evaluator.split_manifest_ref.sha256
            == holdout_evaluator.split_manifest_ref.sha256
        ):
            raise EvolutionRuntimeError(
                "search and sealed-holdout split authorities must differ"
            )

    def _evaluate(
        self,
        *,
        run_dir: Path,
        evaluator: EvolutionEvaluator,
        candidate_id: str,
        candidate_ref: ArtifactRef,
        candidate_content: bytes,
        evaluation_sequence: int,
        after_transition_sequence: int,
        budget: _BudgetTracker,
    ) -> tuple[EvolutionEvaluationRecord, EvolutionEvaluation]:
        stage = evaluator.stage
        evaluation_id = f"evaluation-{evaluation_sequence:04d}"
        request_path = run_dir / "evaluations" / f"{evaluation_id}-request.json"
        request = {
            "evaluation_id": evaluation_id,
            "stage": stage.value,
            "candidate_id": candidate_id,
            "candidate": candidate_ref.identity_data(),
            "evaluator": evaluator.evaluator_ref.identity_data(),
        }
        operation = (
            EvolutionBudgetOperation.search_evaluate
            if stage == EvolutionEvaluationStage.search
            else EvolutionBudgetOperation.holdout_evaluate
        )
        usage_bound = evaluator.usage_bound()
        budget.require_available(operation, usage_bound)
        if (
            stage == EvolutionEvaluationStage.sealed_holdout
            and evaluator.split_access_count != 0
        ):
            raise EvolutionRuntimeError("sealed holdout opened before selection")
        _write_immutable(
            request_path,
            _json_bytes(request),
            label="evolution evaluation request",
        )
        result = evaluator.evaluate(candidate_content)
        if (
            stage == EvolutionEvaluationStage.sealed_holdout
            and evaluator.split_access_count != 1
        ):
            raise EvolutionRuntimeError(
                "sealed holdout access count is not exactly one"
            )
        if result.score_metric != evaluator.metric:
            raise EvolutionRuntimeError("evaluator returned an undeclared metric")
        budget.validate_bound(result.usage, usage_bound)
        result_path = run_dir / "evaluations" / f"{evaluation_id}-result.json"
        _write_immutable(
            result_path,
            _json_bytes(
                {
                    "evaluation_id": evaluation_id,
                    "stage": stage.value,
                    "candidate_id": candidate_id,
                    "score": result.score,
                    "score_metric": result.score_metric,
                    "feedback": json.loads(result.feedback),
                    "attributes": dict(result.attributes),
                }
            ),
            label="evolution evaluation result",
        )
        budget_event_id = budget.charge(
            operation,
            (candidate_id,),
            result.usage,
            attributes={"evaluation_id": evaluation_id, "stage": stage.value},
        )
        record = EvolutionEvaluationRecord(
            evaluation_id=evaluation_id,
            sequence=evaluation_sequence,
            stage=stage,
            candidate_id=candidate_id,
            evaluator_digest=evaluator.digest,
            evaluator_ref=evaluator.evaluator_ref,
            split_manifest_ref=evaluator.split_manifest_ref,
            candidate_ref=candidate_ref,
            request_ref=artifact_ref(request_path),
            result_ref=artifact_ref(result_path),
            score=result.score,
            score_metric=result.score_metric,
            budget_event_id=budget_event_id,
            after_transition_sequence=after_transition_sequence,
        )
        return record, result

    def execute(
        self,
        *,
        run_id: str,
        kind: Literal["evolver", "meta_evolver"],
        adapter: EvolutionAdapter,
        search_evaluator: EvolutionEvaluator,
        holdout_evaluator: EvolutionEvaluator,
        budget: Budget,
        public_input: bytes,
        parent_evidence_ref: ArtifactRef | None = None,
    ) -> EvolutionRuntimeResult:
        if re.fullmatch(
            r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$", run_id
        ) is None:
            raise EvolutionRuntimeError("evolution run_id is not a valid BMP id")
        if kind not in {"evolver", "meta_evolver"}:
            raise EvolutionRuntimeError("unsupported evolution runtime kind")
        self._validate_authorities(adapter, search_evaluator, holdout_evaluator)
        parent_usage: UsageRecord | None = None
        parent_elapsed = 0.0
        if kind == "meta_evolver":
            if parent_evidence_ref is None:
                raise EvolutionRuntimeError(
                    "meta-evolution requires content-addressed parent evidence"
                )
            from MagentaBench.schemas import verify_evolution_run_evidence

            parent = verify_evolution_run_evidence(parent_evidence_ref.path)
            if parent.evidence.kind != "evolver":
                raise EvolutionRuntimeError(
                    "meta-evolution parent must be evolver evidence"
                )
            if artifact_ref(Path(parent_evidence_ref.path)) != parent_evidence_ref:
                raise EvolutionRuntimeError("meta-evolution parent evidence drift")
            if parent.runtime_receipt is None:
                raise EvolutionRuntimeError(
                    "meta-evolution parent lacks an auditable runtime receipt"
                )
            parent_ledger = parent.runtime_receipt.budget_ledger
            if not parent_ledger.usage_observable or not parent_ledger.reconciles_exactly:
                raise EvolutionRuntimeError(
                    "meta-evolution parent budget is not exactly observable"
                )
            parent_usage = parent_ledger.total_usage
            parent_elapsed = parent_ledger.elapsed_wall_seconds
        elif parent_evidence_ref is not None:
            raise EvolutionRuntimeError("evolver cannot carry parent evidence")

        run_dir = self.record_root / run_id
        if run_dir.is_symlink() or (run_dir.exists() and not run_dir.is_dir()):
            raise EvolutionRuntimeError("evolution run directory is not stable")
        run_dir.mkdir(parents=True, exist_ok=True)
        if run_dir.resolve() != run_dir:
            raise EvolutionRuntimeError("evolution run directory escapes record root")
        input_path = run_dir / "public-input.json"
        _write_immutable(input_path, public_input, label="evolution public input")

        budget_path = run_dir / "budget.json"
        _write_immutable(
            budget_path,
            canonical_json(budget).encode("utf-8"),
            label="evolution budget",
        )
        budget_ref = artifact_ref(budget_path)
        if budget_ref.sha256 != canonical_digest(budget):
            raise EvolutionRuntimeError("persisted evolution budget identity drift")
        tracker = _BudgetTracker(budget, budget_ref)
        started = time.monotonic()
        if parent_usage is not None:
            tracker.charge(
                EvolutionBudgetOperation.parent_evolution,
                (),
                parent_usage,
                attributes={
                    "parent_evidence_sha256": parent_evidence_ref.sha256,
                },
            )

        candidate_content: dict[str, bytes] = {}
        candidate_refs: dict[str, ArtifactRef] = {}
        candidate_generations = {"seed": 0, "generated": 1, "revised": 2}
        candidate_parents = {
            "seed": (),
            "generated": ("seed",),
            "revised": ("generated",),
        }
        candidate_attributes: dict[str, Mapping[str, Any]] = {}
        feedback_refs: dict[str, list[ArtifactRef]] = {
            "seed": [],
            "generated": [],
            "revised": [],
        }
        transitions: list[EvolutionTransitionRecord] = []
        evaluations: list[EvolutionEvaluationRecord] = []
        search_results: dict[str, EvolutionEvaluation] = {}

        seed_bound = adapter.usage_bound(EvolutionBudgetOperation.seed)
        tracker.require_available(EvolutionBudgetOperation.seed, seed_bound)
        seed = adapter.seed(public_input)
        tracker.validate_bound(seed.usage, seed_bound)
        seed_path = run_dir / "candidates" / "seed.json"
        _write_immutable(seed_path, seed.content, label="seed candidate")
        candidate_content["seed"] = seed.content
        candidate_refs["seed"] = artifact_ref(seed_path)
        candidate_attributes["seed"] = seed.attributes
        tracker.charge(
            EvolutionBudgetOperation.seed,
            ("seed",),
            seed.usage,
            attributes=seed.attributes,
        )
        transitions.append(
            EvolutionTransitionRecord(
                transition_id="transition-seed",
                sequence=0,
                phase="seed",
                output_candidate_ids=("seed",),
            )
        )

        generate_bound = adapter.usage_bound(EvolutionBudgetOperation.generate)
        tracker.require_available(EvolutionBudgetOperation.generate, generate_bound)
        generated = adapter.generate(seed.content)
        tracker.validate_bound(generated.usage, generate_bound)
        generated_path = run_dir / "candidates" / "generated.json"
        _write_immutable(
            generated_path, generated.content, label="generated candidate"
        )
        candidate_content["generated"] = generated.content
        candidate_refs["generated"] = artifact_ref(generated_path)
        candidate_attributes["generated"] = generated.attributes
        tracker.charge(
            EvolutionBudgetOperation.generate,
            ("seed", "generated"),
            generated.usage,
            attributes=generated.attributes,
        )
        transitions.append(
            EvolutionTransitionRecord(
                transition_id="transition-generate",
                sequence=1,
                phase="generate",
                input_candidate_ids=("seed",),
                output_candidate_ids=("generated",),
            )
        )
        generated_evaluation, generated_result = self._evaluate(
            run_dir=run_dir,
            evaluator=search_evaluator,
            candidate_id="generated",
            candidate_ref=candidate_refs["generated"],
            candidate_content=generated.content,
            evaluation_sequence=0,
            after_transition_sequence=1,
            budget=tracker,
        )
        evaluations.append(generated_evaluation)
        search_results["generated"] = generated_result
        feedback_refs["generated"].append(generated_evaluation.result_ref)
        transitions.append(
            EvolutionTransitionRecord(
                transition_id="transition-feedback-generated",
                sequence=2,
                phase="feedback",
                input_candidate_ids=("generated",),
                feedback_refs=(generated_evaluation.result_ref,),
            )
        )

        revise_bound = adapter.usage_bound(EvolutionBudgetOperation.revise)
        tracker.require_available(EvolutionBudgetOperation.revise, revise_bound)
        revised = adapter.revise(generated.content, generated_result.feedback)
        tracker.validate_bound(revised.usage, revise_bound)
        revised_path = run_dir / "candidates" / "revised.json"
        _write_immutable(revised_path, revised.content, label="revised candidate")
        candidate_content["revised"] = revised.content
        candidate_refs["revised"] = artifact_ref(revised_path)
        candidate_attributes["revised"] = revised.attributes
        tracker.charge(
            EvolutionBudgetOperation.revise,
            ("generated", "revised"),
            revised.usage,
            attributes=revised.attributes,
        )
        transitions.append(
            EvolutionTransitionRecord(
                transition_id="transition-revise",
                sequence=3,
                phase="revise",
                input_candidate_ids=("generated",),
                output_candidate_ids=("revised",),
                feedback_refs=(generated_evaluation.result_ref,),
            )
        )
        revised_evaluation, revised_result = self._evaluate(
            run_dir=run_dir,
            evaluator=search_evaluator,
            candidate_id="revised",
            candidate_ref=candidate_refs["revised"],
            candidate_content=revised.content,
            evaluation_sequence=1,
            after_transition_sequence=3,
            budget=tracker,
        )
        evaluations.append(revised_evaluation)
        search_results["revised"] = revised_result
        feedback_refs["revised"].append(revised_evaluation.result_ref)
        transitions.append(
            EvolutionTransitionRecord(
                transition_id="transition-feedback-revised",
                sequence=4,
                phase="feedback",
                input_candidate_ids=("revised",),
                feedback_refs=(revised_evaluation.result_ref,),
            )
        )

        selected_id = max(
            ("generated", "revised"),
            key=lambda candidate_id: (
                search_results[candidate_id].score,
                candidate_id == "revised",
            ),
        )
        transitions.append(
            EvolutionTransitionRecord(
                transition_id="transition-select",
                sequence=5,
                phase="select",
                input_candidate_ids=("generated", "revised"),
                output_candidate_ids=(selected_id,),
                attributes={"selection_metric": search_evaluator.metric},
            )
        )

        # This is the first point at which the runtime invokes or exposes any
        # sealed-holdout authority.  The strategy has already returned and the
        # selected candidate id is immutable in the transition ledger.
        holdout_evaluation, holdout_result = self._evaluate(
            run_dir=run_dir,
            evaluator=holdout_evaluator,
            candidate_id=selected_id,
            candidate_ref=candidate_refs[selected_id],
            candidate_content=candidate_content[selected_id],
            evaluation_sequence=2,
            after_transition_sequence=5,
            budget=tracker,
        )
        evaluations.append(holdout_evaluation)
        feedback_refs[selected_id].append(holdout_evaluation.result_ref)
        transitions.append(
            EvolutionTransitionRecord(
                transition_id="transition-terminate",
                sequence=6,
                phase="terminate",
                input_candidate_ids=(selected_id,),
                attributes={"reason": "completed"},
            )
        )

        candidate_records = tuple(
            EvolutionCandidateRecord(
                candidate_id=candidate_id,
                generation=candidate_generations[candidate_id],
                parent_ids=candidate_parents[candidate_id],
                artifact_refs=(candidate_refs[candidate_id],),
                feedback_refs=tuple(feedback_refs[candidate_id]),
                status=("selected" if candidate_id == selected_id else "rejected"),
                score=(
                    holdout_result.score if candidate_id == selected_id else None
                ),
                score_metric=(
                    holdout_result.score_metric
                    if candidate_id == selected_id
                    else None
                ),
                evaluator_digest=(
                    holdout_evaluator.digest
                    if candidate_id == selected_id
                    else None
                ),
                attributes=candidate_attributes[candidate_id],
            )
            for candidate_id in ("seed", "generated", "revised")
        )
        transition_records = tuple(transitions)
        elapsed = parent_elapsed + (time.monotonic() - started)
        budget_ledger = tracker.ledger(elapsed)
        sealed_holdout = EvolutionSealedHoldoutReceipt(
            split_manifest_ref=holdout_evaluator.split_manifest_ref,
            search_evaluator_digest=search_evaluator.digest,
            holdout_evaluator_digest=holdout_evaluator.digest,
            selected_candidate_id=selected_id,
            selection_transition_id="transition-select",
            selection_transition_sequence=5,
            holdout_evaluation_id=holdout_evaluation.evaluation_id,
            holdout_evaluation_sequence=holdout_evaluation.sequence,
            access_count=holdout_evaluator.split_access_count,
        )
        runtime_receipt = EvolutionRuntimeReceipt(
            run_id=run_id,
            kind=kind,
            adapter_digest=adapter.digest,
            evaluator_digest=holdout_evaluator.digest,
            budget_digest=budget_ref.sha256,
            candidate_ledger_digest=self._ledger_digest(candidate_records),
            transition_ledger_digest=self._ledger_digest(transition_records),
            selected_candidate_id=selected_id,
            evaluations=tuple(evaluations),
            budget_ledger=budget_ledger,
            sealed_holdout=sealed_holdout,
            parent_evidence_ref=parent_evidence_ref,
        )
        runtime_receipt_path = run_dir / "evolution-runtime-receipt.json"
        _write_immutable(
            runtime_receipt_path,
            _json_bytes(runtime_receipt.model_dump(mode="json")),
            label="evolution runtime receipt",
        )

        evidence = EvolutionRunEvidence(
            run_id=run_id,
            kind=kind,
            adapter_digest=adapter.digest,
            evaluator_digest=holdout_evaluator.digest,
            budget_digest=budget_ref.sha256,
            adapter_ref=adapter.adapter_ref,
            evaluator_ref=holdout_evaluator.evaluator_ref,
            budget_ref=budget_ref,
            authoritative_metric=holdout_evaluator.metric,
            candidate_ledger=candidate_records,
            transition_ledger=transition_records,
            selected_candidate_id=selected_id,
            termination_reason="completed",
            search_state_refs=(artifact_ref(input_path),),
            parent_evidence_ref=parent_evidence_ref,
            runtime_receipt_ref=artifact_ref(runtime_receipt_path),
            attributes={
                "runtime": "deterministic_local",
                "search_evaluator_digest": search_evaluator.digest,
                "sealed_holdout_access": "after_selection",
            },
        )
        evidence_path = run_dir / "evolution-evidence.json"
        _write_immutable(
            evidence_path,
            _json_bytes(evidence.model_dump(mode="json")),
            label="evolution evidence",
        )
        return EvolutionRuntimeResult(
            evidence=evidence,
            evidence_path=evidence_path,
            runtime_receipt=runtime_receipt,
            runtime_receipt_path=runtime_receipt_path,
        )


__all__ = [
    "DeterministicLocalEvolutionAdapter",
    "DeterministicTargetEvaluator",
    "EvolutionAction",
    "EvolutionAdapter",
    "EvolutionBudgetExceeded",
    "EvolutionEvaluation",
    "EvolutionEvaluator",
    "EvolutionRuntime",
    "EvolutionRuntimeError",
    "EvolutionRuntimeResult",
]
