from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from MagentaBench.runner.adapter_registry import (
    AdapterRegistry,
    AdapterRegistryError,
    verify_resolved_case_set,
)
from MagentaBench.runner.adapter_source import (
    AdapterSourceError,
    closure_digest,
    import_closure,
    resolve_entrypoint,
    resolve_source_root,
)
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.gates import evaluate_run_report
from MagentaBench.runner.pipeline import Pipeline, ResumeDriftError
from MagentaBench.schemas import (
    AdapterCapability,
    ArtifactRef,
    CaseArtifact,
    CaseSetArtifact,
    verify_observation_report,
)

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


class _ExternalLoader:
    adapter = "external.benchmark"
    digest = "a" * 64


class _ExternalBackendFactory:
    adapter = "external.backend"
    digest = "a" * 64


class _ExternalExecutionAdapter:
    benchmark_adapter = "external.execution"
    backend_adapter = "external.backend"
    subject_interface = None
    digest = "a" * 64


class _ExternalBenchmarkExecutionAdapter:
    benchmark_adapter = "external.benchmark"
    backend_adapter = "external.backend"
    subject_interface = None
    digest = "b" * 64


class _ExternalBenchmarkOtherExecutionAdapter:
    benchmark_adapter = "external.benchmark"
    backend_adapter = "other.backend"
    subject_interface = "chat"
    digest = "c" * 64


def test_adapter_capability_config_paths_match_resolved_configuration_tree() -> None:
    base = AdapterCapability(
        id="external.configured",
        kind="adapter",
        adapter="external.configured",
        adapter_kind="benchmark_loader",
        entrypoint="package.module:loader",
        digest="a" * 64,
    )
    values = {
        "agent": {"model": "gpt-5.4", "limits": {"max_turns": 300}},
        "debugger": {"model": "gpt-5.4-mini"},
    }

    assert base.owns_configuration(values)
    assert base.model_copy(update={"config_paths": ("*",)}).owns_configuration(
        values
    )
    assert base.model_copy(update={"config_paths": ("agent",)}).owns_configuration(
        values
    )
    assert base.model_copy(
        update={"config_paths": ("agent.limits.max_turns",)}
    ).owns_configuration(values)
    assert not base.model_copy(
        update={"config_paths": ("agent.missing", "meta_agent")}
    ).owns_configuration(values)


def test_project_adapter_registry_loads_digest_bound_toml_plugin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "plugins/demo"
    declarations = project / "registries/adapters"
    source.mkdir(parents=True)
    declarations.mkdir(parents=True)
    module = source / "loader.py"
    # A file cannot embed its own digest. The plugin may compute it from its
    # source bytes at import time; BMP independently compares the same bytes.
    module.write_text(
        """from hashlib import sha256
from pathlib import Path

class Loader:
    adapter = "external.demo"
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
""",
        encoding="utf-8",
    )
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    declaration = declarations / "external-demo.toml"
    declaration.write_text(
        f'''[adapter]
id = "external.demo"
kind = "adapter"
adapter = "external.demo"
bmp_version = "0.1"
adapter_kind = "benchmark_loader"
source = "plugins/demo"
entrypoint = "loader.py:Loader"
digest = "{digest}"
supported_benchmark_kinds = ["custom"]
''',
        encoding="utf-8",
    )

    registry = AdapterRegistry.from_project(project)
    assert registry.capability("external.demo").digest == digest

    declaration.write_text(
        declaration.read_text(encoding="utf-8").replace(digest, "b" * 64),
        encoding="utf-8",
    )
    with pytest.raises(AdapterRegistryError, match="source digest mismatch"):
        AdapterRegistry.from_project(project)


def test_adapter_registry_accepts_only_digest_bound_extensions() -> None:
    capability = AdapterCapability(
        id="external.benchmark",
        kind="adapter",
        adapter="external.benchmark",
        adapter_kind="benchmark_loader",
        entrypoint="package.module:loader",
        digest=_ExternalLoader.digest,
        supported_benchmark_kinds=("custom",),
    )
    registry = AdapterRegistry.production().extend(
        capability=capability,
        benchmark_loader=_ExternalLoader(),
    )
    assert registry.capability("external.benchmark") == capability
    assert registry.capabilities[-1] == capability
    with pytest.raises(AdapterRegistryError, match="digest"):
        AdapterRegistry.production().extend(
            capability=capability.model_copy(update={"digest": "b" * 64}),
            benchmark_loader=_ExternalLoader(),
        )

    backend_capability = capability.model_copy(
        update={
            "id": "external.backend",
            "adapter": "external.backend",
            "adapter_kind": "backend_factory",
        }
    )
    with pytest.raises(AdapterRegistryError, match="digest"):
        AdapterRegistry.production().extend(
            capability=backend_capability.model_copy(update={"digest": "b" * 64}),
            backend_factory=_ExternalBackendFactory(),
        )

    execution_capability = capability.model_copy(
        update={
            "id": "external.execution",
            "adapter": "external.execution",
            "adapter_kind": "execution",
        }
    )
    with pytest.raises(AdapterRegistryError, match="digest"):
        AdapterRegistry.production().extend(
            capability=execution_capability.model_copy(update={"digest": "b" * 64}),
            execution_adapter=_ExternalExecutionAdapter(),
        )


def test_adapter_registry_separates_capabilities_by_kind() -> None:
    loader_capability = AdapterCapability(
        id="external.benchmark",
        kind="adapter",
        adapter="external.benchmark",
        adapter_kind="benchmark_loader",
        entrypoint="package.module:loader",
        digest=_ExternalLoader.digest,
        supported_benchmark_kinds=("custom",),
    )
    execution_capability = loader_capability.model_copy(
        update={
            "adapter_kind": "execution",
            "digest": _ExternalBenchmarkExecutionAdapter.digest,
        }
    )

    registry = AdapterRegistry.production().extend(
        capability=loader_capability,
        benchmark_loader=_ExternalLoader(),
    )
    registry = registry.extend(
        capability=execution_capability,
        execution_adapter=_ExternalBenchmarkExecutionAdapter(),
    )

    assert registry.capability("external.benchmark", "benchmark_loader") == (
        loader_capability
    )
    assert registry.capability("external.benchmark", "execution") == (
        execution_capability
    )
    with pytest.raises(AdapterRegistryError, match="multiple kinds"):
        registry.capability("external.benchmark")
    assert {
        capability.adapter_kind for capability in registry.capabilities
        if capability.adapter == "external.benchmark"
    } == {"benchmark_loader", "execution"}


def test_execution_capabilities_are_keyed_by_full_compatibility_tuple() -> None:
    base = AdapterCapability(
        id="external.execution",
        kind="adapter",
        adapter="external.benchmark",
        adapter_kind="execution",
        entrypoint="package.module:execution",
        digest=_ExternalBenchmarkExecutionAdapter.digest,
    )
    registry = AdapterRegistry.production().extend(
        capability=base,
        execution_adapter=_ExternalBenchmarkExecutionAdapter(),
    )
    other = base.model_copy(
        update={
            "id": "external.execution.other",
            "digest": _ExternalBenchmarkOtherExecutionAdapter.digest,
        }
    )
    registry = registry.extend(
        capability=other,
        execution_adapter=_ExternalBenchmarkOtherExecutionAdapter(),
    )

    with pytest.raises(AdapterRegistryError, match="multiple compatibility tuples"):
        registry.capability("external.benchmark", "execution")
    assert sum(
        item.adapter_kind == "execution" and item.adapter == "external.benchmark"
        for item in registry.capabilities
    ) == 2


def test_project_adapter_registry_rejects_duplicate_capability_kind(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "plugins/demo"
    declarations = project / "registries/adapters"
    source.mkdir(parents=True)
    declarations.mkdir(parents=True)
    module = source / "loader.py"
    module.write_text(
        """from hashlib import sha256
from pathlib import Path

class Loader:
    adapter = "external.demo"
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
""",
        encoding="utf-8",
    )
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    declaration = f'''[adapter]
id = "external.demo"
kind = "adapter"
adapter = "external.demo"
bmp_version = "0.1"
adapter_kind = "benchmark_loader"
source = "plugins/demo"
entrypoint = "loader.py:Loader"
digest = "{digest}"
'''
    (declarations / "one.toml").write_text(declaration, encoding="utf-8")
    (declarations / "two.toml").write_text(declaration, encoding="utf-8")

    with pytest.raises(AdapterRegistryError, match="already registered"):
        AdapterRegistry.from_project(project)


def test_project_adapter_registry_only_imports_required_plugins(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "plugins/demo"
    declarations = project / "registries/adapters"
    source.mkdir(parents=True)
    declarations.mkdir(parents=True)
    used = source / "used.py"
    used.write_text(
        """from hashlib import sha256
from pathlib import Path

class Loader:
    adapter = "external.used"
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
""",
        encoding="utf-8",
    )
    used_digest = hashlib.sha256(used.read_bytes()).hexdigest()
    (declarations / "used.toml").write_text(
        f'''[adapter]
id = "external.used"
kind = "adapter"
adapter = "external.used"
bmp_version = "0.1"
adapter_kind = "benchmark_loader"
source = "plugins/demo"
entrypoint = "used.py:Loader"
digest = "{used_digest}"
''',
        encoding="utf-8",
    )
    unused = source / "unused.py"
    unused.write_text("raise RuntimeError('unused plugin must not load')\n", encoding="utf-8")
    unused_digest = hashlib.sha256(unused.read_bytes()).hexdigest()
    (declarations / "unused.toml").write_text(
        f'''[adapter]
id = "external.unused"
kind = "adapter"
adapter = "external.unused"
bmp_version = "0.1"
adapter_kind = "benchmark_loader"
source = "plugins/demo"
entrypoint = "unused.py:Unused"
digest = "{unused_digest}"
''',
        encoding="utf-8",
    )

    registry = AdapterRegistry.from_project(
        project,
        required_capabilities={("external.used", "benchmark_loader")},
    )
    assert registry.capability("external.used").adapter == "external.used"
    with pytest.raises(AdapterRegistryError, match="cannot instantiate"):
        AdapterRegistry.from_project(project)


def test_unselected_malformed_adapter_declaration_is_inert(tmp_path: Path) -> None:
    project = tmp_path / "project"
    declarations = project / "registries/adapters"
    declarations.mkdir(parents=True)
    (declarations / "unselected.toml").write_text(
        '''[adapter]
id = "broken.unselected"
kind = "adapter"
adapter = "broken"
bmp_version = "0.1"
adapter_kind = "backend_factory"
entrypoint = "missing.py:Factory"
digest = "not-a-sha256"

[unrelated]
value = ["malformed plugin metadata is inert"]
''',
        encoding="utf-8",
    )

    registry = AdapterRegistry.from_project(
        project,
        required_capabilities={("wanted", "benchmark_loader")},
    )

    assert registry.capabilities == ()


def test_unselected_non_table_adapter_declaration_is_inert(tmp_path: Path) -> None:
    project = tmp_path / "project"
    declarations = project / "registries/adapters"
    declarations.mkdir(parents=True)
    (declarations / "unselected.toml").write_text(
        '''[unrelated]
adapter = ["not", "a", "table"]
''',
        encoding="utf-8",
    )

    registry = AdapterRegistry.from_project(
        project,
        required_capabilities={("wanted", "benchmark_loader")},
    )

    assert registry.capabilities == ()


def test_selected_malformed_adapter_declaration_fails_closed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    declarations = project / "registries/adapters"
    declarations.mkdir(parents=True)
    (declarations / "selected.toml").write_text(
        '''[adapter]
id = "broken.selected"
kind = "adapter"
adapter = "broken"
bmp_version = "0.1"
adapter_kind = "backend_factory"
entrypoint = "missing.py:Factory"
digest = "not-a-sha256"
''',
        encoding="utf-8",
    )

    with pytest.raises(AdapterRegistryError, match="invalid adapter declaration"):
        AdapterRegistry.from_project(
            project,
            required_capabilities={("broken", "backend_factory")},
        )


def test_adapter_source_closure_binds_local_helpers(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "plugins/demo"
    source.mkdir(parents=True)
    entrypoint = source / "loader.py"
    helper = source / "helper.py"
    helper.write_text("VALUE = 'one'\n", encoding="utf-8")
    entrypoint.write_text(
        "from helper import VALUE\nclass Loader:\n    value = VALUE\n",
        encoding="utf-8",
    )
    root = resolve_source_root(project, "plugins/demo")
    first_closure = import_closure(root, resolve_entrypoint(root, "loader.py:Loader"))
    assert tuple(path.name for path in first_closure) == ("helper.py", "loader.py")
    first_digest = closure_digest(root, first_closure)

    helper.write_text("VALUE = 'two'\n", encoding="utf-8")
    second_closure = import_closure(root, resolve_entrypoint(root, "loader.py:Loader"))
    assert closure_digest(root, second_closure) != first_digest


def test_adapter_source_closure_binds_package_initializers(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "plugins/demo"
    package = source / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("PREFIX = 'one'\n", encoding="utf-8")
    (package / "helper.py").write_text(
        "import pkg\nVALUE = pkg.PREFIX\n", encoding="utf-8"
    )
    entrypoint = source / "loader.py"
    entrypoint.write_text(
        "import pkg.helper\nclass Loader:\n    value = pkg.helper.VALUE\n",
        encoding="utf-8",
    )
    root = resolve_source_root(project, "plugins/demo")
    closure = import_closure(root, resolve_entrypoint(root, "loader.py:Loader"))
    assert tuple(path.relative_to(root).as_posix() for path in closure) == (
        "loader.py",
        "pkg/__init__.py",
        "pkg/helper.py",
    )


def test_adapter_source_rejects_in_root_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    real_source = project / "plugins/real"
    real_source.mkdir(parents=True)
    (real_source / "loader.py").write_text(
        "from helper import VALUE\nclass Loader:\n    value = VALUE\n",
        encoding="utf-8",
    )
    (real_source / "helper-target.py").write_text("VALUE = 1\n", encoding="utf-8")

    source_link = project / "plugins/source-link"
    source_link.symlink_to(real_source, target_is_directory=True)
    with pytest.raises(AdapterSourceError, match="adapter source contains symlink"):
        resolve_source_root(project, "plugins/source-link")

    root = resolve_source_root(project, "plugins/real")
    entrypoint_link = real_source / "entrypoint.py"
    entrypoint_link.symlink_to(real_source / "loader.py")
    with pytest.raises(AdapterSourceError, match="adapter entrypoint contains symlink"):
        resolve_entrypoint(root, "entrypoint.py:Loader")

    helper_link = real_source / "helper.py"
    helper_link.symlink_to(real_source / "helper-target.py")
    entrypoint = resolve_entrypoint(root, "loader.py:Loader")
    with pytest.raises(AdapterSourceError, match="adapter import contains symlink"):
        import_closure(root, entrypoint)


def test_pipeline_registry_injection_is_never_implicit_production(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="adapter-registry injection"):
        Pipeline(
            ROOT,
            tmp_path / "records",
            adapter_registry=AdapterRegistry.production(),
        )


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
        match="source closure differs from compiled dataset",
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
            completed=result.runs,
            expected_run_ids=tuple(
                item.plan.manifest.metadata.run_id for item in result.runs
            ),
        )
    with pytest.raises(ResumeDriftError, match="execution instance"):
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


def test_pipeline_materializes_multi_case_lineage(
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
    result = Pipeline(project, records).run(experiment)
    assert len(result.runs) == 16
    assert {item.case.case_id for item in result.runs} == {"case-001", "case-002"}
    assert len(result.report.lineage) == 16
    assert all(item.run_id for item in result.report.lineage)
    assert len({item.case_id for item in result.report.lineage}) == 2
    assert tuple(records.rglob("schedule_activation_receipt.json"))
    assert tuple(records.rglob("evidence_bundle.json"))
    # Standalone verification must use the parent/case lineage identity for
    # each selected bundle and shared schedule receipt.
    assert verify_observation_report(result.report_path).report.experiment_id == (
        result.report.experiment_id
    )


def test_pipeline_rejects_multi_case_checkpoint_until_ledger_is_widened(
    tmp_path: Path,
) -> None:
    project, experiment = _project(tmp_path)
    protocol = project / "registries/protocols/fake-deterministic.toml"
    protocol.write_text(
        protocol.read_text(encoding="utf-8").replace(
            'checkpoint_policy = "disabled"', 'checkpoint_policy = "save"'
        ),
        encoding="utf-8",
    )
    task_manifest = (
        project / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    )
    task_manifest.write_text(
        task_manifest.read_text(encoding="utf-8")
        + '\n[[tasks]]\nid = "case-002"\ninstruction = "second"\nexpected = "BMP_OK"\noutput = "answer.txt"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="multi-case checkpoint identity"):
        Pipeline(project, tmp_path / "records").run(experiment)


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
