from __future__ import annotations

import shutil
import hashlib
from pathlib import Path

import pytest

from MagentaBench.runner.compiler import CompilationError, Compiler, canonical_json_bytes
from MagentaBench.runner.adapter_registry import AdapterRegistry
from MagentaBench.runner.pipeline import Pipeline, ResumeDriftError
from MagentaBench.schemas.verification import (
    _verify_manifest_adapter_capabilities,
    _verify_manifest_configuration,
)


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance", project / "MagentaBench/conformance"
    )
    subject_path = project / "registries/subjects/fake-nonfake.toml"
    subject_text = subject_path.read_text(encoding="utf-8")
    if "comparison_kind" not in subject_text:
        subject_path.write_text(
            subject_text.replace(
                'kind = "opaque_agent"\n',
                'kind = "opaque_agent"\ncomparison_kind = "coding_agent"\n',
            ),
            encoding="utf-8",
        )
    (project / "registries/protocols/custom-eval.toml").write_text(
        (ROOT / "registries/protocols/benchmark-evaluation.v1.toml")
        .read_text(encoding="utf-8")
        .replace('id = "benchmark.evaluation.v1"', 'id = "custom.evaluation.v1"')
        .replace('state_reset = "per_case"', 'state_reset = "per_rollout"'),
        encoding="utf-8",
    )
    (project / "registries/datasets/external-fake.toml").write_text(
        '''[dataset]
id = "dataset.external.fake.v1"
kind = "dataset"
adapter = "external.benchmark"
bmp_version = "0.1"
source = "../../MagentaBench/conformance/fixtures/fake_benchmark"
commit = "external-fixture-v1"
content_globs = ["tasks.toml"]
format = "toml-task-suite"
split = "test"

[dataset.config]
task_manifest = "tasks.toml"
''',
        encoding="utf-8",
    )
    (project / "registries/metrics/external-quality.toml").write_text(
        '''[metric]
id = "quality.authoritative.v1"
kind = "metric"
adapter = "magentabench.measurement"
bmp_version = "0.1"
value_kind = "continuous"
level = "rollout"
direction = "maximize"
unit = "reward"
source = "evaluator"
source_field = "evaluator_binding"
formula = "direct_v1"
population = "evaluator_observations"
missing_observation = "invalidate"
''',
        encoding="utf-8",
    )
    (project / "registries/evaluators/external-quality.toml").write_text(
        '''[evaluator]
id = "evaluator.external.quality.v1"
kind = "evaluator"
adapter = "external.benchmark"
bmp_version = "0.1"
implementation = "external.verifier:v1"
scoring_kind = "continuous"

[[evaluator.metrics]]
metric_id = "quality.authoritative.v1"
source_key = "quality"
authoritative = true
''',
        encoding="utf-8",
    )
    plugin_root = project / "plugins/external-benchmark"
    plugin_root.mkdir(parents=True)
    plugin = plugin_root / "loader.py"
    plugin.write_text(
        """from hashlib import sha256
from pathlib import Path

class Loader:
    adapter = "external.benchmark"
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
""",
        encoding="utf-8",
    )
    plugin_digest = hashlib.sha256(plugin.read_bytes()).hexdigest()
    execution = plugin_root / "execution.py"
    execution.write_text(
        """from hashlib import sha256
from pathlib import Path

class Execution:
    benchmark_adapter = "external.benchmark"
    backend_adapter = "subprocess"
    subject_interface = None
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
""",
        encoding="utf-8",
    )
    execution_digest = hashlib.sha256(execution.read_bytes()).hexdigest()
    adapter_root = project / "registries/adapters"
    adapter_root.mkdir(exist_ok=True)
    (adapter_root / "external-benchmark.toml").write_text(
        f'''[adapter]
id = "external.benchmark"
kind = "adapter"
adapter = "external.benchmark"
bmp_version = "0.1"
adapter_kind = "benchmark_loader"
source = "plugins/external-benchmark"
entrypoint = "loader.py:Loader"
digest = "{plugin_digest}"
config_paths = ["agent.model"]
supported_benchmark_kinds = ["custom"]
''',
        encoding="utf-8",
    )
    (adapter_root / "external-execution.toml").write_text(
        f'''[adapter]
id = "external.execution"
kind = "adapter"
adapter = "external.benchmark"
bmp_version = "0.1"
adapter_kind = "execution"
source = "plugins/external-benchmark"
entrypoint = "execution.py:Execution"
digest = "{execution_digest}"
supported_benchmark_kinds = ["custom"]
supported_subject_kinds = ["opaque_agent"]
supported_backend_adapters = ["subprocess"]
supported_subject_adapters = ["fake"]
none_model_sentinels = ["none/echo"]
supported_state_reset_policies = ["per_rollout"]
''',
        encoding="utf-8",
    )
    experiment = project / "MagentaBench/conformance/experiments/configured.toml"
    experiment.write_text(
        EXPERIMENT.read_text(encoding="utf-8")
        + """

[experiment.configuration]
profiles = ["agent.base"]

[experiment.configuration.values.agent]
max_model_turns = 300
""",
        encoding="utf-8",
    )
    return project, experiment


def test_configuration_profile_is_resolved_into_manifest_identity(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    profile = project / "registries/configurations/agent-base.toml"
    profile.write_text(
        '''[configuration]
id = "agent.base"
kind = "configuration"
adapter = "generic"
bmp_version = "0.1"

[configuration.values.agent]
model = "claude-opus-4.6"
reasoning_effort = "high"
max_context_tokens = 200000
max_generation_tokens = 128000

[configuration.values.debugger]
max_model_turns = 25

[configuration.values.meta_agent]
max_model_turns = 500
''',
        encoding="utf-8",
    )

    first = Compiler(project).compile(experiment)
    config = first[0].manifest.metadata.configuration
    assert config is not None
    assert config.adapter == "generic"
    assert config.values["agent"]["model"] == "claude-opus-4.6"
    assert config.values["agent"]["max_model_turns"] == 300
    assert config.artifact_digest == config.canonical_digest()
    assert config.source_refs

    profile.write_text(
        '''[configuration]
id = "agent.base"
kind = "configuration"
adapter = "generic"
bmp_version = "0.1"

[configuration.values.agent]
model = "gpt-5.4"
reasoning_effort = "high"
max_context_tokens = 200000
max_generation_tokens = 128000
''',
        encoding="utf-8",
    )
    second = Compiler(project).compile(experiment)
    assert first[0].manifest_digest != second[0].manifest_digest
    assert (
        second[0].manifest.metadata.configuration.values["agent"]["model"]
        == "gpt-5.4"
    )


def test_external_configuration_file_is_content_bound(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    external = project / "MagentaBench/conformance/experiments/agent.toml"
    external.write_text(
        """[configuration]
id = "agent.external"
kind = "configuration"
adapter = "cli-agent"

[configuration.values.agent]
model = "gpt-5.4-mini"
reasoning_effort = "high"
""",
        encoding="utf-8",
    )
    text = experiment.read_text(encoding="utf-8")
    text = text.replace(
        '[experiment.configuration]\nprofiles = ["agent.base"]',
        '[experiment.configuration]\nfiles = ["agent.toml"]',
    )
    experiment.write_text(text, encoding="utf-8")

    run = Compiler(project).compile(experiment)[0]
    config = run.manifest.metadata.configuration
    assert config is not None
    assert config.adapter == "cli-agent"
    assert config.values["agent"]["model"] == "gpt-5.4-mini"
    assert config.source_refs[0].path == str(external.resolve())


def test_configuration_profiles_can_compose_multiple_adapter_namespaces(
    tmp_path: Path,
) -> None:
    project, experiment = _project(tmp_path)
    registry = project / "registries/configurations"
    (registry / "agent-layer.toml").write_text(
        '''[configuration]
id = "agent.layer"
kind = "configuration"
adapter = "agent"
bmp_version = "0.1"

[configuration.values.agent]
model = "gpt-5.4"

[configuration.schema]
type = "object"

[configuration.schema.properties.agent]
type = "object"
''',
        encoding="utf-8",
    )
    (registry / "harness-layer.toml").write_text(
        '''[configuration]
id = "harness.layer"
kind = "configuration"
adapter = "harness"
bmp_version = "0.1"

[configuration.values.harness]
max_turns = 25

[configuration.schema]
type = "object"

[configuration.schema.properties.harness]
type = "object"
''',
        encoding="utf-8",
    )
    text = experiment.read_text(encoding="utf-8")
    text = text.replace(
        'profiles = ["agent.base"]',
        'profiles = ["agent.layer", "harness.layer"]',
    )
    experiment.write_text(text, encoding="utf-8")

    run = Compiler(project).compile(experiment)[0]
    config = run.manifest.metadata.configuration
    assert config is not None
    assert config.adapter == "composite"
    assert config.values["agent"]["model"] == "gpt-5.4"
    assert config.values["harness"]["max_turns"] == 25
    assert config.ownership["agent.model"] == "agent"
    assert config.ownership["harness.max_turns"] == "harness"
    assert config.json_schema["properties"]["harness"]["type"] == "object"
    assert config.schema_digest == hashlib.sha256(
        canonical_json_bytes(config.json_schema)
    ).hexdigest()


def test_external_configuration_symlink_is_rejected(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    experiment_dir = experiment.parent
    target = experiment_dir / "agent-target.toml"
    target.write_text(
        """[configuration]
id = "agent.external"
kind = "configuration"
adapter = "cli-agent"
""",
        encoding="utf-8",
    )
    linked = experiment_dir / "agent.toml"
    linked.symlink_to(target)
    experiment.write_text(
        experiment.read_text(encoding="utf-8").replace(
            '[experiment.configuration]\nprofiles = ["agent.base"]',
            '[experiment.configuration]\nfiles = ["agent.toml"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(CompilationError, match="must not contain a symlink"):
        Compiler(project).compile(experiment)


def test_raw_external_configuration_requires_explicit_mode(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    raw = experiment.parent / "raw-agent.toml"
    raw.write_text('[agent]\nmodel = "gpt-5.4"\n', encoding="utf-8")
    text = experiment.read_text(encoding="utf-8")
    text = text.replace(
        '[experiment.configuration]\nprofiles = ["agent.base"]',
        '[experiment.configuration]\nfiles = ["raw-agent.toml"]',
    )
    experiment.write_text(text, encoding="utf-8")
    with pytest.raises(CompilationError, match=r"requires a \[configuration\] envelope"):
        Compiler(project).compile(experiment)

    experiment.write_text(
        text.replace(
            'files = ["raw-agent.toml"]',
            'raw_files = ["raw-agent.toml"]',
        ),
        encoding="utf-8",
    )
    run = Compiler(project).compile(experiment)[0]
    assert run.manifest.metadata.configuration is not None
    assert run.manifest.metadata.configuration.values["agent"]["model"] == "gpt-5.4"


def test_configuration_json_schema_is_checked_and_replayed(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    (project / "registries/configurations/agent-schema.toml").write_text(
        '''[configuration]
id = "agent.schema"
kind = "configuration"
adapter = "generic"
bmp_version = "0.1"

[configuration.values.agent]
model = "gpt-5.4"

[configuration.schema]
type = "object"
required = ["agent"]

[configuration.schema.properties.agent]
type = "object"
required = ["model"]

[configuration.schema.properties.agent.properties.model]
type = "string"
''',
        encoding="utf-8",
    )
    text = experiment.read_text(encoding="utf-8").replace(
        'profiles = ["agent.base"]',
        'profiles = ["agent.schema"]',
    )
    experiment.write_text(text, encoding="utf-8")
    run = Compiler(project).compile(experiment)[0]
    assert run.manifest.metadata.configuration is not None
    assert run.manifest.metadata.configuration.composition

    bad = project / "MagentaBench/conformance/experiments/bad-schema.toml"
    bad.write_text(
        text.replace(
            "max_model_turns = 300",
            "model = 42\nmax_model_turns = 300",
        ),
        encoding="utf-8",
    )
    with pytest.raises(CompilationError, match="fails its JSON Schema"):
        Compiler(project).compile(bad)


def test_standalone_verifier_rejects_configuration_source_drift(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    profile = project / "registries/configurations/agent-base.toml"
    profile.write_text(
        '''[configuration]
id = "agent.base"
kind = "configuration"
adapter = "generic"
bmp_version = "0.1"

[configuration.values.agent]
model = "gpt-5.4-mini"
''',
        encoding="utf-8",
    )
    run = Compiler(project).compile(experiment)[0]
    profile.write_bytes(profile.read_bytes() + b"\n")
    mismatches: list[str] = []
    _verify_manifest_configuration(
        run.manifest,
        label="manifest.configuration",
        path_map={},
        mismatches=mismatches,
    )
    assert any("configuration.source_refs" in mismatch for mismatch in mismatches)


def test_custom_benchmark_can_compile_exploratory_with_external_adapter(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    plugin = project / "plugins/external-benchmark/loader.py"
    plugin_digest = hashlib.sha256(plugin.read_bytes()).hexdigest()
    benchmark_path = project / "registries/benchmarks/custom-demo.toml"
    benchmark_path.write_text(
        """[benchmark]
id = "custom.demo"
kind = "custom"
adapter = "external.benchmark"
bmp_version = "0.1"
""",
        encoding="utf-8",
    )
    (project / "registries/protocols/custom-eval.toml").write_text(
        (ROOT / "registries/protocols/benchmark-evaluation.v1.toml")
        .read_text(encoding="utf-8")
        .replace('id = "benchmark.evaluation.v1"', 'id = "custom.evaluation.v1"')
        .replace('state_reset = "per_case"', 'state_reset = "per_rollout"'),
        encoding="utf-8",
    )
    experiment = project / "custom.toml"
    experiment.write_text(
        """[experiment]
id = "custom-exploratory"
benchmark = "custom.demo"
dataset = "dataset.external.fake.v1"
evaluator = "evaluator.external.quality.v1"
metrics = ["quality.authoritative.v1"]
subject = "fake.nonfake"
protocol = "custom.evaluation.v1"

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

[experiment.configuration.values.agent]
model = "gpt-5.4-mini"
""",
        encoding="utf-8",
    )
    run = Compiler(project).compile(experiment)[0]
    assert run.manifest.benchmark.kind == "custom"
    assert run.manifest.benchmark.adapter == "external.benchmark"
    assert run.manifest.claim_design.purpose.value == "exploratory"
    assert run.manifest.metadata.configuration is not None
    assert run.manifest.metadata.adapter_capabilities[0].implementation_ref.sha256 == (
        plugin_digest
    )
    assert {
        item.capability.adapter_kind
        for item in run.manifest.metadata.adapter_capabilities
    } == {"benchmark_loader", "execution"}


def test_runtime_rejects_adapter_helper_closure_drift(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    plugin_root = project / "plugins/external-benchmark"
    helper = plugin_root / "helper.py"
    helper.write_text("VALUE = 'one'\n", encoding="utf-8")
    loader = plugin_root / "loader.py"
    original_loader_digest = hashlib.sha256(loader.read_bytes()).hexdigest()
    loader.write_text(
        """from hashlib import sha256
from pathlib import Path
from helper import VALUE

class Loader:
    adapter = "external.benchmark"
    value = VALUE
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
""",
        encoding="utf-8",
    )
    adapter_decl = project / "registries/adapters/external-benchmark.toml"
    adapter_decl.write_text(
        adapter_decl.read_text(encoding="utf-8").replace(
            original_loader_digest,
            hashlib.sha256(loader.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )
    execution = plugin_root / "execution.py"
    original_execution_digest = hashlib.sha256(execution.read_bytes()).hexdigest()
    execution.write_text(
        execution.read_text(encoding="utf-8").replace(
            "subject_interface = None", 'subject_interface = "task_to_output"'
        ),
        encoding="utf-8",
    )
    execution_decl = project / "registries/adapters/external-execution.toml"
    execution_decl.write_text(
        execution_decl.read_text(encoding="utf-8")
        .replace(original_execution_digest, hashlib.sha256(execution.read_bytes()).hexdigest())
        .replace(
            'supported_subject_kinds = ["opaque_agent"]',
            'supported_subject_kinds = ["opaque_agent"]\nsupported_subject_interfaces = ["task_to_output"]',
        ),
        encoding="utf-8",
    )
    benchmark_path = project / "registries/benchmarks/custom-drift.toml"
    benchmark_path.write_text(
        """[benchmark]
id = "custom.drift"
kind = "custom"
adapter = "external.benchmark"
bmp_version = "0.1"
""",
        encoding="utf-8",
    )
    experiment = project / "custom-drift.toml"
    experiment.write_text(
        """[experiment]
id = "custom-drift"
benchmark = "custom.drift"
dataset = "dataset.external.fake.v1"
evaluator = "evaluator.external.quality.v1"
metrics = ["quality.authoritative.v1"]
subject = "fake.nonfake"
protocol = "custom.evaluation.v1"

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

[experiment.configuration.values.agent]
model = "gpt-5.4-mini"
""",
        encoding="utf-8",
    )
    run = Compiler(project).compile(experiment)[0]
    helper.write_text("VALUE = 'two'\n", encoding="utf-8")
    registry = AdapterRegistry.from_project(
        project,
        required_capabilities=AdapterRegistry.required_capability_keys((run,)),
    )
    pipeline = Pipeline(
        project,
        tmp_path / "records",
        adapter_registry=registry,
        allow_test_override=True,
    )
    with pytest.raises(ResumeDriftError, match="source closure drift"):
        pipeline._verify_adapter_activation(run)


def test_standalone_adapter_closure_refs_reject_byte_mutation(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    plugin_root = project / "plugins/external-benchmark"
    helper = plugin_root / "helper.py"
    helper.write_text("VALUE = 'one'\n", encoding="utf-8")
    loader = plugin_root / "loader.py"
    original_loader_digest = hashlib.sha256(loader.read_bytes()).hexdigest()
    loader.write_text(
        """from hashlib import sha256
from pathlib import Path
from helper import VALUE

class Loader:
    adapter = "external.benchmark"
    value = VALUE
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
""",
        encoding="utf-8",
    )
    adapter_decl = project / "registries/adapters/external-benchmark.toml"
    adapter_decl.write_text(
        adapter_decl.read_text(encoding="utf-8").replace(
            original_loader_digest,
            hashlib.sha256(loader.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )
    benchmark_path = project / "registries/benchmarks/custom-standalone.toml"
    benchmark_path.write_text(
        """[benchmark]
id = "custom.standalone"
kind = "custom"
adapter = "external.benchmark"
bmp_version = "0.1"
""",
        encoding="utf-8",
    )
    experiment = project / "custom-standalone.toml"
    experiment.write_text(
        """[experiment]
id = "custom-standalone"
benchmark = "custom.standalone"
dataset = "dataset.external.fake.v1"
evaluator = "evaluator.external.quality.v1"
metrics = ["quality.authoritative.v1"]
subject = "fake.nonfake"
protocol = "custom.evaluation.v1"

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

[experiment.configuration.values.agent]
model = "gpt-5.4-mini"
""",
        encoding="utf-8",
    )
    run = Compiler(project).compile(experiment)[0]
    helper.write_text("VALUE = 'mutated'\n", encoding="utf-8")
    mismatches: list[str] = []
    _verify_manifest_adapter_capabilities(
        run.manifest,
        label="manifest.adapter_capabilities",
        path_map={},
        mismatches=mismatches,
    )
    assert any("source_closure_refs" in mismatch for mismatch in mismatches)


def test_custom_benchmark_requires_backend_and_execution_capabilities(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    benchmark_path = project / "registries/benchmarks/custom-demo.toml"
    benchmark_path.write_text(
        """[benchmark]
id = "custom.demo"
kind = "custom"
adapter = "external.benchmark"
bmp_version = "0.1"
""",
        encoding="utf-8",
    )
    # The loader declaration remains, while removing execution proves that a
    # custom benchmark cannot silently reuse the built-in fake tuple.
    (project / "registries/adapters/external-execution.toml").unlink()
    experiment = project / "custom-missing-capabilities.toml"
    experiment.write_text(
        """[experiment]
id = "custom-missing-capabilities"
benchmark = "custom.demo"
dataset = "dataset.external.fake.v1"
evaluator = "evaluator.external.quality.v1"
metrics = ["quality.authoritative.v1"]
subject = "fake.nonfake"
protocol = "custom.evaluation.v1"

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
    with pytest.raises(
        CompilationError,
        match=r"missing required adapter capabilities:.*\('external\.benchmark', 'execution'\)",
    ):
        Compiler(project).compile(experiment)


def test_adapter_capability_rejects_missing_resolved_configuration_path(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    benchmark_path = project / "registries/benchmarks/custom-demo.toml"
    benchmark_path.write_text(
        """[benchmark]
id = "custom.demo"
kind = "custom"
adapter = "external.benchmark"
bmp_version = "0.1"
""",
        encoding="utf-8",
    )
    experiment = project / "custom-missing-config-path.toml"
    experiment.write_text(
        """[experiment]
id = "custom-missing-config-path"
benchmark = "custom.demo"
dataset = "dataset.external.fake.v1"
evaluator = "evaluator.external.quality.v1"
metrics = ["quality.authoritative.v1"]
subject = "fake.nonfake"
protocol = "custom.evaluation.v1"

[experiment.design]
comparison_kind = "coding_agent"
purpose = "exploratory"

[experiment.configuration.values.debugger]
model = "gpt-5.4-mini"

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

    with pytest.raises(
        CompilationError, match="does not own any resolved configuration path"
    ):
        Compiler(project).compile(experiment)
