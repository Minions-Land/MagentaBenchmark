"""Pipeline adapter for the provider-free deterministic evolution runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from MagentaBench.runner.adapter_registry import (
    AdapterRegistryError,
    LoadedCaseSet,
    ResolvedCaseSet,
    _write_immutable,
    write_immutable_json,
)
from MagentaBench.runner.backend.fake import CaseExecution, FakeBackend
from MagentaBench.runner.compiler import CompiledRun
from MagentaBench.runner.evidence import (
    artifact_ref,
    atomic_write_json,
    sha256_file,
    source_closure_digest,
)
from MagentaBench.runner.evolution import (
    DeterministicLocalEvolutionAdapter,
    DeterministicTargetEvaluator,
    EvolutionRuntime,
)
from MagentaBench.runner.network import record_unobservable_network
from MagentaBench.schemas import (
    ArtifactRef,
    Budget,
    CaseArtifact,
    CaseSetArtifact,
    EvidenceBundle,
    NetworkBoundary,
    NetworkPolicySource,
    ProvenanceRecord,
    RunStatus,
    UsageRecord,
    VerifierEvidence,
)
from MagentaBench.schemas.evolution import EvolutionEvaluationStage
from MagentaBench.schemas.compiler import canonical_json


_MODULE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class DeterministicEvolutionCase:
    task_id: str
    public_input_ref: ArtifactRef
    case_set_digest: str


class DeterministicEvolutionLoader:
    """Activate one public-input case and its evaluator split declarations."""

    adapter = "deterministic_evolution"
    digest = _MODULE_DIGEST

    @staticmethod
    def _source(run: CompiledRun) -> Path:
        source = getattr(run.manifest.benchmark, "source", None)
        if not source:
            raise AdapterRegistryError("deterministic evolution source is missing")
        root = Path(source).resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise AdapterRegistryError("deterministic evolution source is not a directory")
        return root

    @classmethod
    def _content_refs(cls, run: CompiledRun) -> tuple[ArtifactRef, ...]:
        source = cls._source(run)
        benchmark = run.manifest.benchmark
        paths: set[Path] = set()
        for pattern in tuple(getattr(benchmark, "content_globs", ())):
            matches = tuple(source.glob(pattern))
            if not matches or any(not path.is_file() for path in matches):
                raise AdapterRegistryError(
                    f"deterministic evolution content pattern matched no regular files: {pattern!r}"
                )
            paths.update(path.resolve(strict=True) for path in matches)
        refs = tuple(artifact_ref(path) for path in sorted(paths))
        if source_closure_digest(source, refs) != benchmark.source_content_digest:
            raise AdapterRegistryError("deterministic evolution source closure drift")
        return refs

    @classmethod
    def _config_path(cls, run: CompiledRun, key: str) -> Path:
        config = getattr(run.manifest.benchmark, "config", {})
        relative = config.get(key)
        if not isinstance(relative, str) or not relative:
            raise AdapterRegistryError(f"deterministic evolution config lacks {key!r}")
        path = (cls._source(run) / relative).resolve(strict=True)
        try:
            path.relative_to(cls._source(run))
        except ValueError as exc:
            raise AdapterRegistryError(f"deterministic evolution config escapes source: {key!r}") from exc
        if path.is_symlink() or not path.is_file():
            raise AdapterRegistryError(f"deterministic evolution config is not a regular file: {key!r}")
        return path

    def resolve(self, run: CompiledRun, artifact_root: Path) -> ResolvedCaseSet:
        source_refs = self._content_refs(run)
        for key in (
            "public_input",
            "search_evaluator",
            "search_split",
            "holdout_split",
        ):
            if artifact_ref(self._config_path(run, key)) not in source_refs:
                raise AdapterRegistryError(
                    f"deterministic evolution config {key!r} is outside the declared content closure"
                )
        public_path = self._config_path(run, "public_input")
        public_ref = artifact_ref(public_path)
        case = CaseArtifact(case_id="evolution-case", public_input_ref=public_ref)
        protocol = run.manifest.execution.protocol
        assert protocol is not None
        artifact = CaseSetArtifact(
            benchmark_id=run.manifest.benchmark.id,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            loader_adapter=self.adapter,
            loader_digest=self.digest,
            selection_method="all_cases",
            case_order=protocol.case_order,
            order_seed=(
                run.manifest.execution.seed
                if protocol.case_order == "seeded_random"
                else None
            ),
            source_content_digest=run.manifest.benchmark.source_content_digest,
            source_content_refs=source_refs,
            ordered_case_ids=(case.case_id,),
            cases=(case,),
        )
        artifact_path = artifact_root / artifact.canonical_digest() / "case_set.json"
        write_immutable_json(artifact_path, artifact, label="evolution case-set artifact")
        return ResolvedCaseSet(
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_sha256=sha256_file(artifact_path),
        )

    def load(self, run: CompiledRun, resolved: ResolvedCaseSet) -> LoadedCaseSet:
        case = resolved.artifact.cases[0]
        return LoadedCaseSet(
            artifact=resolved.artifact,
            artifact_path=resolved.artifact_path,
            artifact_sha256=resolved.artifact_sha256,
            cases=(
                DeterministicEvolutionCase(
                    task_id=case.case_id,
                    public_input_ref=case.public_input_ref,
                    case_set_digest=resolved.artifact.canonical_digest(),
                ),
            ),
        )


class DeterministicEvolutionExecutionAdapter:
    """Turn one runtime result into a normal BMP ``EvidenceBundle``."""

    benchmark_adapter = "deterministic_evolution"
    backend_adapter = "fake"
    subject_interface = None
    digest = _MODULE_DIGEST

    @staticmethod
    def _evaluator_identity(run: CompiledRun, directory: Path) -> ArtifactRef:
        benchmark = run.manifest.benchmark
        identity = benchmark.model_dump(
            mode="json", exclude={"source", "artifact_digest"}
        )
        path = directory / "holdout-evaluator-identity.json"
        _write_immutable(
            path,
            canonical_json(identity).encode("utf-8"),
            label="evolution evaluator identity",
        )
        ref = artifact_ref(path)
        if ref.sha256 != benchmark.artifact_digest:
            raise AdapterRegistryError("evolution evaluator identity digest drift")
        return ref

    def execute(
        self,
        backend: FakeBackend,
        run: CompiledRun,
        case: DeterministicEvolutionCase,
        attempt: Any,
    ) -> CaseExecution:
        search_evaluator_path = DeterministicEvolutionLoader._config_path(
            run, "search_evaluator"
        )
        search_split_path = DeterministicEvolutionLoader._config_path(
            run, "search_split"
        )
        holdout_split_path = DeterministicEvolutionLoader._config_path(
            run, "holdout_split"
        )
        search = DeterministicTargetEvaluator.from_files(
            stage=EvolutionEvaluationStage.search,
            evaluator_path=search_evaluator_path,
            split_manifest_path=search_split_path,
        )
        evaluator_directory = backend.run_directory(run)
        holdout_ref = self._evaluator_identity(run, evaluator_directory)
        holdout_split_ref = artifact_ref(holdout_split_path)

        def make_holdout() -> DeterministicTargetEvaluator:
            return DeterministicTargetEvaluator(
                stage=EvolutionEvaluationStage.sealed_holdout,
                evaluator_ref=holdout_ref,
                split_manifest_ref=holdout_split_ref,
                target=None,
                metric=run.manifest.benchmark.authoritative_reward_metric,
            )

        holdout = make_holdout()
        public_input = Path(case.public_input_ref.path).read_bytes()
        generation_step = getattr(run.manifest.benchmark, "config", {}).get(
            "generation_step", 2
        )
        if (
            not isinstance(generation_step, int)
            or isinstance(generation_step, bool)
            or generation_step == 0
        ):
            raise AdapterRegistryError(
                "deterministic evolution generation_step must be a non-zero integer"
            )
        strategy = DeterministicLocalEvolutionAdapter(
            generation_step=generation_step
        )
        # The registry capability identifies this pipeline adapter.  Its
        # import-closure receipt separately binds the BMP runtime module that
        # implements the delegated deterministic strategy methods.
        strategy.adapter_ref = artifact_ref(Path(__file__))
        strategy.digest = self.digest
        runtime = EvolutionRuntime(evaluator_directory)
        parent_evidence_ref = None
        if run.manifest.subject.kind == "meta_evolver":
            root_budget = run.manifest.execution.budget
            if root_budget.max_tokens is not None and root_budget.max_tokens < 12:
                raise AdapterRegistryError(
                    "deterministic meta-evolution requires 12 root tokens before parent launch"
                )
            parent = runtime.execute(
                run_id=f"{attempt.attempt_id}__parent",
                kind="evolver",
                adapter=strategy,
                search_evaluator=search,
                holdout_evaluator=holdout,
                budget=Budget(
                    max_tokens=6,
                    max_wall_seconds=root_budget.max_wall_seconds,
                    max_cost=(0.0 if root_budget.max_cost is not None else None),
                ),
                public_input=public_input,
            )
            parent_evidence_ref = artifact_ref(parent.evidence_path)
            holdout = make_holdout()
        result = runtime.execute(
            run_id=attempt.attempt_id,
            kind=run.manifest.subject.kind,
            adapter=strategy,
            search_evaluator=search,
            holdout_evaluator=holdout,
            budget=run.manifest.execution.budget,
            public_input=public_input,
            parent_evidence_ref=parent_evidence_ref,
        )
        selected = next(
            candidate
            for candidate in result.evidence.candidate_ledger
            if candidate.candidate_id == result.evidence.selected_candidate_id
        )
        bundle_directory = evaluator_directory / "cases" / attempt.attempt_id
        bundle_path = bundle_directory / "bundle.json"
        status_path = bundle_directory / "status.json"
        network = record_unobservable_network(
            bundle_directory / "network-observation.json",
            resolver_adapter=self.benchmark_adapter,
            execution_adapter=self.backend_adapter,
            case_id=case.task_id,
            boundary=NetworkBoundary.process,
            allow_internet=False,
            source=NetworkPolicySource.case_set_artifact,
            source_artifact_digest=case.case_set_digest,
            reason="deterministic local runtime does not expose a provider boundary",
        )
        provenance = ProvenanceRecord(
            manifest_digest=run.manifest_digest,
            runner_digest=backend.runner_digest,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            subject_digest=run.manifest.subject.artifact_digest,
            backend_digest=run.manifest.execution.backend.digest or backend.runner_digest,
            trace_emission_claimed=False,
            distribution="magentabench",
            version="0.1.0",
            backend_kind="fake",
            network_mode="none",
            evolution_evidence_ref=artifact_ref(result.evidence_path),
        )
        score = selected.score
        if score is None:
            raise AdapterRegistryError("evolution selected candidate has no holdout score")
        bundle = EvidenceBundle(
            run_id=attempt.attempt_id,
            status=RunStatus.scored,
            output_refs=selected.artifact_refs,
            log_refs=(artifact_ref(result.runtime_receipt_path),),
            verifier_evidence=VerifierEvidence(
                verifier=run.manifest.benchmark.verifier,
                passed=None,
                score=score,
                metrics={run.manifest.benchmark.authoritative_reward_metric: score},
                artifact_refs=(artifact_ref(result.evidence_path),),
                details={"selected_candidate_id": result.evidence.selected_candidate_id},
            ),
            usage=UsageRecord(
                total_tokens=result.runtime_receipt.budget_ledger.total_usage.total_tokens,
                cost=result.runtime_receipt.budget_ledger.total_usage.cost,
                wall_clock_seconds=result.runtime_receipt.budget_ledger.elapsed_wall_seconds,
            ),
            network_policy=network.policy,
            network_observation=network.observation,
            provenance=provenance,
        )
        atomic_write_json(bundle_directory / "status.json", {"status": bundle.status.value})
        atomic_write_json(bundle_path, bundle)
        return CaseExecution(
            case_id=case.task_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=sha256_file(bundle_path),
        )

    def reset_state(self, backend: FakeBackend, case_id: str, policy: str) -> Any:
        return backend.reset_state(case_id, policy)


__all__ = [
    "DeterministicEvolutionCase",
    "DeterministicEvolutionExecutionAdapter",
    "DeterministicEvolutionLoader",
]
