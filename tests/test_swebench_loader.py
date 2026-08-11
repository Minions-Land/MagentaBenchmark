from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from MagentaBench.runner.adapter_registry import (
    AdapterRegistry,
    AdapterRegistryError,
    verify_resolved_case_set,
)
from MagentaBench.runner.compiler import CompilationError, Compiler


ROOT = Path(__file__).parents[1]


def _fixture_compiler(source: Path, bind_registry_source) -> Compiler:
    compiler = Compiler(ROOT)
    bind_registry_source(
        compiler,
        "dataset",
        "dataset.swebench.lite.local.v1",
        source,
    )
    return compiler


def _compiled_loader_run(source: Path, bind_registry_source):
    compiler = _fixture_compiler(source, bind_registry_source)
    base = compiler.compile(
        ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"
    )[0]
    protocol = compiler._resolved_protocol("swebench.single.astropy-6938.v1")
    manifest = base.manifest.model_copy(
        update={
            "benchmark": compiler._benchmark_artifact("swebench.lite.local.v1"),
            "dataset": compiler._dataset_artifact("dataset.swebench.lite.local.v1"),
            "evaluator": compiler._evaluator_artifact(
                "evaluator.swebench.fail-to-pass.v1"
            ),
            "metrics": (compiler._metric_artifact("reward.authoritative.v1"),),
            "execution": base.manifest.execution.model_copy(
                update={"protocol": protocol}
            ),
        }
    )
    assert Path(manifest.dataset.source) == source.resolve()
    return replace(base, manifest=manifest)


def _loader(run):
    registry = AdapterRegistry.from_project(
        ROOT,
        required_capabilities={("swebench", "benchmark_loader")},
    )
    return registry.benchmark_loader(run)


def test_swebench_loader_capability_is_source_closed() -> None:
    artifact = Compiler(ROOT)._adapter_capability_artifact(
        "swebench", "benchmark_loader"
    )

    assert artifact is not None
    assert artifact.capability.id == "swebench.loader.v1"
    assert artifact.implementation_ref.sha256 == artifact.capability.digest
    assert artifact.source_closure_digest is not None
    closure = set(artifact.source_closure_paths)
    assert "MagentaBench/adapters/benchmarks/swebench.py" in closure
    assert "MagentaBench/runner/adapter_registry.py" in closure
    assert "MagentaBench/runner/compiler.py" in closure
    assert "MagentaBench/runner/evidence.py" in closure
    assert "MagentaBench/schemas/models.py" in closure


def test_swebench_loader_activates_one_explicit_case_without_oracle(
    tmp_path: Path, swebench_source: Path, bind_registry_source
) -> None:
    run = _compiled_loader_run(swebench_source, bind_registry_source)
    loader = _loader(run)
    resolved = loader.resolve(run, tmp_path / "case-sets")
    verify_resolved_case_set(
        run,
        resolved,
        expected_loader_adapter=loader.adapter,
        expected_loader_digest=loader.digest,
    )
    loaded = loader.load(run, resolved)

    assert loaded.artifact.ordered_case_ids == ("astropy__astropy-6938",)
    assert loaded.artifact.selection_method == "explicit_case_ids"
    assert tuple(case.task_id for case in loaded.cases) == (
        "astropy__astropy-6938",
    )
    case = loaded.cases[0]
    assert case.public["repo"] == "astropy/astropy"
    assert "test_patch" not in case.public
    assert "patch" not in case.public
    assert "hints_text" not in case.public
    assert "version" not in case.public
    assert case.execution_contract["container_image"] == (
        "sweb.eval.x86_64.astropy__astropy-6938:latest"
    )
    assert case.execution_contract["container_image_digest"] == (
        "sha256:a64e48c6ff94271d86498cf991b41d40f0e3bf33537f7adc6c740c0f26e641e9"
    )
    assert case.verifier_contract["fail_to_pass"] == [
        "astropy/io/fits/tests/test_checksum.py::TestChecksumFunctions::test_ascii_table_data",
        "astropy/io/fits/tests/test_table.py::TestTableFunctions::test_ascii_table",
    ]
    assert "patch" not in case.verifier_contract
    assert case.case_set_digest == loaded.artifact.canonical_digest()


def test_swebench_contract_byte_drift_is_rejected(
    tmp_path: Path, swebench_source: Path, bind_registry_source
) -> None:
    run = _compiled_loader_run(swebench_source, bind_registry_source)
    loader = _loader(run)
    resolved = loader.resolve(run, tmp_path / "case-sets")
    public_path = Path(resolved.artifact.cases[0].public_input_ref.path)
    public = json.loads(public_path.read_text(encoding="utf-8"))
    public["problem_statement"] = "injected drift"
    public_path.write_text(json.dumps(public), encoding="utf-8")

    with pytest.raises(AdapterRegistryError, match="content reference drift"):
        verify_resolved_case_set(
            run,
            resolved,
            expected_loader_adapter=loader.adapter,
            expected_loader_digest=loader.digest,
        )


def test_swebench_loader_rejects_missing_explicit_case(
    tmp_path: Path, swebench_source: Path, bind_registry_source
) -> None:
    run = _compiled_loader_run(swebench_source, bind_registry_source)
    protocol = run.manifest.execution.protocol
    assert protocol is not None
    mutated_protocol = protocol.model_copy(
        update={"explicit_case_ids": ("missing__project-1",)}
    )
    mutated = replace(
        run,
        manifest=run.manifest.model_copy(
            update={
                "execution": run.manifest.execution.model_copy(
                    update={"protocol": mutated_protocol}
                )
            }
        ),
    )

    with pytest.raises(AdapterRegistryError, match="absent from the split"):
        _loader(mutated).resolve(mutated, tmp_path / "case-sets")


def test_swebench_loader_rejects_unpinned_image(
    tmp_path: Path, swebench_source: Path, bind_registry_source
) -> None:
    run = _compiled_loader_run(swebench_source, bind_registry_source)
    dataset = run.manifest.dataset
    config = dict(dataset.config)
    config["image_digests"] = {}
    mutated_dataset = dataset.model_copy(update={"config": config})
    mutated = replace(
        run,
        manifest=run.manifest.model_copy(update={"dataset": mutated_dataset}),
    )

    with pytest.raises(AdapterRegistryError, match="lacks an immutable image digest"):
        _loader(mutated).resolve(mutated, tmp_path / "case-sets")


def test_swebench_production_compile_fails_closed_without_execution_capability(
    tmp_path: Path, swebench_source: Path, bind_registry_source
) -> None:
    experiment = tmp_path / "swebench-production.toml"
    experiment.write_text(
        """[experiment]
id = "swebench-production-guard"
benchmark = "swebench.lite.local.v1"
dataset = "dataset.swebench.lite.local.v1"
evaluator = "evaluator.swebench.fail-to-pass.v1"
metrics = ["reward.authoritative.v1"]
subject = "fake.nonfake"
protocol = "swebench.single.astropy-6938.v1"

[experiment.design]
comparison_kind = "coding_agent"
purpose = "exploratory"

[execution]
backend = "subprocess.echo"
model = "none/echo"

[execution.budget]
max_tokens = 0
max_wall_seconds = 1.0
max_cost = 0.0
""",
        encoding="utf-8",
    )

    compiler = _fixture_compiler(swebench_source, bind_registry_source)
    assert compiler._adapter_capability_artifact("swebench", "execution") is None
    with pytest.raises(
        CompilationError,
        match=r"missing required adapter capabilities: .*swebench.*execution",
    ):
        compiler.compile(experiment)
