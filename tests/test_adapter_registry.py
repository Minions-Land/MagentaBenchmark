from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from MagentaBench.runner.adapter_registry import (
    AdapterRegistry,
    AdapterRegistryError,
    verify_resolved_case_set,
)
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.gates import evaluate_run_report
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import ArtifactRef, CaseArtifact, CaseSetArtifact

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance", project / "MagentaBench/conformance"
    )
    return project, project / "MagentaBench/conformance/experiments/fake-sweep.toml"


def test_fake_loader_resolves_public_and_verifier_contracts(tmp_path: Path) -> None:
    run = Compiler(ROOT).compile(EXPERIMENT)[0]
    registry = AdapterRegistry.production()
    loader = registry.benchmark_loader(run)
    resolved = loader.resolve(run, tmp_path / "case-set")
    verify_resolved_case_set(
        run,
        resolved,
        expected_loader_adapter=loader.adapter,
        expected_loader_digest=loader.digest,
    )
    loaded = loader.load(run, resolved)

    assert loaded.artifact.ordered_case_ids == ("case-001",)
    assert loaded.artifact.canonical_digest() == loaded.artifact_path.parent.name
    assert tuple(case.task_id for case in loaded.cases) == ("case-001",)
    assert not hasattr(loaded.cases[0], "expected")
    assert (
        loaded.artifact.cases[0].public_input_ref.sha256
        in Path(loaded.artifact.cases[0].public_input_ref.path).name
    )
    public = json.loads(
        Path(loaded.artifact.cases[0].public_input_ref.path).read_text(
            encoding="utf-8"
        )
    )
    assert public["instruction"] == "Emit the BMP protocol sentinel."
    assert "expected" not in public
    assert loaded.artifact.cases[0].verifier_contract_refs


def test_case_set_source_drift_is_rejected_before_runtime_load(
    tmp_path: Path,
) -> None:
    project, experiment = _project(tmp_path)
    run = Compiler(project).compile(experiment)[0]
    loader = AdapterRegistry.production().benchmark_loader(run)
    resolved = loader.resolve(run, tmp_path / "case-set")
    task_manifest = (
        project
        / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    )
    task_manifest.write_text(
        task_manifest.read_text(encoding="utf-8") + "\n# injected drift\n",
        encoding="utf-8",
    )

    with pytest.raises(AdapterRegistryError, match="content reference drift"):
        verify_resolved_case_set(
            run,
            resolved,
            expected_loader_adapter=loader.adapter,
            expected_loader_digest=loader.digest,
        )


def test_runtime_uses_activated_snapshot_not_live_source(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    run = next(
        item
        for item in Compiler(project).compile(experiment)
        if item.manifest.subject.id == "fake.treatment"
    )
    registry = AdapterRegistry.production()
    loader = registry.benchmark_loader(run)
    resolved = loader.resolve(run, tmp_path / "case-set")
    verify_resolved_case_set(
        run,
        resolved,
        expected_loader_adapter=loader.adapter,
        expected_loader_digest=loader.digest,
    )
    loaded = loader.load(run, resolved)
    task_manifest = (
        project
        / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    )
    task_manifest.write_text("not valid TOML [", encoding="utf-8")
    backend = registry.backend_factory(run).build(
        run,
        record_root=tmp_path / "records",
        workspace_root=tmp_path / "workspaces",
    )
    execution = registry.execution_adapter(run).execute(
        backend,
        run,
        loaded.cases[0],
        SimpleNamespace(
            attempt_id="activated-snapshot-attempt",
            allocation=None,
            remaining_wall_seconds=None,
        ),
    )
    assert execution.bundle.verifier_evidence is not None
    assert execution.bundle.verifier_evidence.score == 1.0


def test_pipeline_rejects_source_change_after_compilation(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    pipeline = Pipeline(project, tmp_path / "records")
    original_compile = pipeline.compiler.compile
    task_manifest = (
        project
        / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    )

    def compile_then_mutate(path: Path, **kwargs):
        compiled = original_compile(path, **kwargs)
        task_manifest.write_text(
            task_manifest.read_text(encoding="utf-8")
            + "\n# post-compile drift\n",
            encoding="utf-8",
        )
        return compiled

    pipeline.compiler.compile = compile_then_mutate  # type: ignore[method-assign]
    with pytest.raises(
        AdapterRegistryError,
        match="source closure differs from compiled benchmark",
    ):
        pipeline.run(experiment)


def test_report_rehashes_case_set_content_refs(tmp_path: Path) -> None:
    result = Pipeline(ROOT, tmp_path).run(EXPERIMENT)
    artifact = CaseSetArtifact.model_validate_json(
        Path(result.runs[0].case_set_receipt.case_set_ref.path).read_bytes()
    )
    public_input = Path(artifact.cases[0].public_input_ref.path)
    public_input.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="case-set content reference drift"):
        evaluate_run_report(
            experiment_id="fake-conformance-sweep",
            experiment_digest="0" * 64,
            completed=result.runs,
            expected_run_ids=tuple(
                item.plan.manifest.metadata.run_id for item in result.runs
            ),
            control_id="fake.control",
            treatment_id="fake.treatment",
            deterministic_conformance=True,
            counterbalanced=True,
        )
    with pytest.raises(
        AdapterRegistryError,
        match="existing case-set public input byte drift",
    ):
        Pipeline(ROOT, tmp_path).run(EXPERIMENT)


def test_case_set_identity_excludes_artifact_locations(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text('{"case":"same"}\n', encoding="utf-8")
    second.write_bytes(first.read_bytes())
    first_ref = ArtifactRef(
        path=str(first.resolve()), sha256="f" * 64, size_bytes=first.stat().st_size
    )
    second_ref = first_ref.model_copy(update={"path": str(second.resolve())})
    common = {
        "benchmark_id": "fake.exact.v1",
        "benchmark_digest": "a" * 64,
        "loader_adapter": "fake",
        "loader_digest": "b" * 64,
        "source_content_digest": "c" * 64,
        "ordered_case_ids": ("case-001",),
    }
    left = CaseSetArtifact(
        **common,
        source_content_refs=(first_ref,),
        cases=(CaseArtifact(case_id="case-001", public_input_ref=first_ref),),
    )
    right = CaseSetArtifact(
        **common,
        source_content_refs=(second_ref,),
        cases=(CaseArtifact(case_id="case-001", public_input_ref=second_ref),),
    )
    assert left.canonical_digest() == right.canonical_digest()


def test_pipeline_rejects_multi_case_identity_at_activation(
    tmp_path: Path,
) -> None:
    project, experiment = _project(tmp_path)
    task_manifest = (
        project
        / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    )
    task_manifest.write_text(
        task_manifest.read_text(encoding="utf-8")
        + """

[[tasks]]
id = "case-002"
instruction = "Emit the BMP protocol sentinel again."
expected = "BMP_OK"
output = "answer.txt"
""",
        encoding="utf-8",
    )
    records = tmp_path / "records"
    with pytest.raises(
        RuntimeError,
        match=(
            "multi-case execution identity is unimplemented: .* "
            "resolved 2 selected cases"
        ),
    ):
        Pipeline(project, records).run(experiment)
    assert not tuple(records.rglob("schedule_activation_receipt.json"))
    assert not tuple(records.rglob("evidence_bundle.json"))


def test_registry_has_no_loader_or_compatibility_fallback() -> None:
    run = Compiler(ROOT).compile(EXPERIMENT)[0]
    registry = AdapterRegistry.production()
    with pytest.raises(AdapterRegistryError, match="registry key mismatch"):
        AdapterRegistry(
            benchmark_loaders={
                "substituted": registry.benchmark_loader(run)
            },
            backend_factories={},
            execution_adapters={},
        )

    benchmark = run.manifest.benchmark.model_copy(
        update={"adapter": "unregistered-loader"}
    )
    loader_manifest = run.manifest.model_copy(update={"benchmark": benchmark})
    loader_mutation = run.__class__(manifest=loader_manifest)
    with pytest.raises(AdapterRegistryError, match="BenchmarkLoader"):
        registry.benchmark_loader(loader_mutation)

    backend = run.manifest.execution.backend.model_copy(
        update={"adapter": "harbor-shim"}
    )
    execution = run.manifest.execution.model_copy(update={"backend": backend})
    manifest = run.manifest.model_copy(update={"execution": execution})
    mutated = run.__class__(manifest=manifest)

    with pytest.raises(AdapterRegistryError, match="backend factory"):
        registry.backend_factory(mutated)
    with pytest.raises(AdapterRegistryError, match="compatibility tuple"):
        registry.execution_adapter(mutated)


def test_pipeline_core_does_not_call_backend_private_task_or_execute() -> None:
    source = (ROOT / "MagentaBench/runner/pipeline.py").read_text(encoding="utf-8")
    assert "_load_task" not in source
    assert "_verifier_digest" not in source
    assert "_task_manifest_digest" not in source
    assert "self.backend.execute" not in source
    assert "FakeBackend" not in source
    assert "FakeTask" not in source
    assert "verifier.py" not in source
