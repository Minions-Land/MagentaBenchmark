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


def _compiled_loader_run(tmp_path: Path):
    experiment = tmp_path / "swebench-loader.toml"
    experiment.write_text(
        """[experiment]
id = "swebench-loader-conformance"
benchmark = "swebench.lite.local.v1"
subject = "fake.control"
protocol = "swebench.single.astropy-6938.v1"

[experiment.design]
scope = "conformance"
purpose = "exploratory"
vary = []

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
    return Compiler(ROOT, allow_test_override=True).compile(experiment)[0]


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
    tmp_path: Path,
) -> None:
    run = _compiled_loader_run(tmp_path)
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


def test_swebench_contract_byte_drift_is_rejected(tmp_path: Path) -> None:
    run = _compiled_loader_run(tmp_path)
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


def test_swebench_loader_rejects_missing_explicit_case(tmp_path: Path) -> None:
    run = _compiled_loader_run(tmp_path)
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


def test_swebench_loader_rejects_unpinned_image(tmp_path: Path) -> None:
    run = _compiled_loader_run(tmp_path)
    benchmark = run.manifest.benchmark
    config = dict(benchmark.config)
    config["image_digests"] = {}
    mutated_benchmark = benchmark.model_copy(update={"config": config})
    mutated = replace(
        run,
        manifest=run.manifest.model_copy(update={"benchmark": mutated_benchmark}),
    )

    with pytest.raises(AdapterRegistryError, match="lacks an immutable image digest"):
        _loader(mutated).resolve(mutated, tmp_path / "case-sets")


def test_swebench_production_compile_fails_closed_without_execution_capability(
    tmp_path: Path,
) -> None:
    experiment = tmp_path / "swebench-production.toml"
    experiment.write_text(
        """[experiment]
id = "swebench-production-guard"
benchmark = "swebench.lite.local.v1"
subject = "fake.control"
protocol = "swebench.single.astropy-6938.v1"

[experiment.design]
scope = "conformance"
purpose = "exploratory"
vary = []

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

    compiler = Compiler(ROOT)
    assert compiler._adapter_capability_artifact("swebench", "execution") is None
    with pytest.raises(CompilationError, match="PipelineAdapterActivationReceipt"):
        compiler.compile(experiment)
