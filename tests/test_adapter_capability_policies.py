from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from MagentaBench.runner.adapter_registry import (
    AdapterRegistry,
    AdapterRegistryError,
)
from MagentaBench.runner.compiler import CompilationError, Compiler
from MagentaBench.schemas.verification import (
    _verify_manifest_adapter_capabilities,
)


def _declarative_adapter_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "declarative-adapter"
    for directory in (
        "registries/adapters",
        "registries/backends",
        "registries/benchmarks",
        "registries/protocols",
        "registries/subjects",
        "plugins",
        "fixture",
        "subject",
    ):
        (project / directory).mkdir(parents=True, exist_ok=True)
    (project / "fixture/case.txt").write_text("case\n", encoding="utf-8")

    modules = {
        "loader.py": '''from hashlib import sha256
from pathlib import Path

class Loader:
    adapter = "novel-benchmark"
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
''',
        "backend.py": '''from hashlib import sha256
from pathlib import Path

class BackendFactory:
    adapter = "novel-backend"
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
''',
        "execution.py": '''from hashlib import sha256
from pathlib import Path

class Execution:
    benchmark_adapter = "novel-benchmark"
    backend_adapter = "novel-backend"
    subject_interface = "novel-wire-v1"
    digest = sha256(Path(__file__).read_bytes()).hexdigest()
''',
    }
    digests: dict[str, str] = {}
    for name, source in modules.items():
        path = project / "plugins" / name
        path.write_text(source, encoding="utf-8")
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    (project / "registries/benchmarks/novel.toml").write_text(
        '''[benchmark]
id = "novel.benchmark.v1"
kind = "custom"
adapter = "novel-benchmark"
bmp_version = "0.1"
source = "../../fixture"
content_globs = ["case.txt"]
verifier = "novel.verifier:v1"
scoring_kind = "binary"
authoritative_reward_metric = "reward"
reward_pass_value = 1.0
''',
        encoding="utf-8",
    )
    (project / "registries/subjects/novel.toml").write_text(
        '''[subject]
id = "novel.subject.v1"
kind = "opaque_agent"
adapter = "novel-subject"
bmp_version = "0.1"
source = "../../subject"
entrypoint = "/bin/true"
interface = "novel-wire-v1"
emits_trace = false
''',
        encoding="utf-8",
    )
    (project / "registries/backends/novel.toml").write_text(
        '''[backend]
id = "novel.backend.v1"
kind = "local"
adapter = "novel-backend"
bmp_version = "0.1"

[backend.defaults]
mode = "safe"
''',
        encoding="utf-8",
    )
    (project / "registries/protocols/novel.toml").write_text(
        '''[protocol]
id = "novel.protocol.v1"
kind = "benchmark_evaluation"
adapter = "magentabench.scheduler"
bmp_version = "0.1"
rollouts_per_case = 1
parallelism = 1
case_order = "fixed"
adaptive_budget = false
candidate_selection = "single"
state_reset = "per_case"
checkpoint_policy = "disabled"
deterministic_conformance = false
''',
        encoding="utf-8",
    )
    (project / "registries/adapters/novel-loader.toml").write_text(
        f'''[adapter]
id = "novel.loader.v1"
kind = "adapter"
adapter = "novel-benchmark"
bmp_version = "0.1"
adapter_kind = "benchmark_loader"
source = "."
entrypoint = "plugins/loader.py:Loader"
digest = "{digests['loader.py']}"
supported_benchmark_kinds = ["custom"]
''',
        encoding="utf-8",
    )
    (project / "registries/adapters/novel-backend.toml").write_text(
        f'''[adapter]
id = "novel.backend-factory.v1"
kind = "adapter"
adapter = "novel-backend"
bmp_version = "0.1"
adapter_kind = "backend_factory"
source = "."
entrypoint = "plugins/backend.py:BackendFactory"
digest = "{digests['backend.py']}"
supported_backend_kinds = ["local"]
supported_backend_adapters = ["novel-backend"]
backend_default_read_set = ["mode"]
''',
        encoding="utf-8",
    )
    (project / "registries/adapters/novel-execution.toml").write_text(
        f'''[adapter]
id = "novel.execution.v1"
kind = "adapter"
adapter = "novel-benchmark"
bmp_version = "0.1"
adapter_kind = "execution"
source = "."
entrypoint = "plugins/execution.py:Execution"
digest = "{digests['execution.py']}"
supported_benchmark_kinds = ["custom"]
supported_subject_kinds = ["opaque_agent"]
supported_subject_adapters = ["novel-subject"]
supported_backend_kinds = ["local"]
supported_backend_adapters = ["novel-backend"]
supported_subject_interfaces = ["novel-wire-v1"]
none_model_sentinels = ["none"]
supported_state_reset_policies = ["per_case"]
''',
        encoding="utf-8",
    )
    experiment = project / "experiment.toml"
    experiment.write_text(
        '''[experiment]
id = "novel-adapter"
benchmark = "novel.benchmark.v1"
subject = "novel.subject.v1"
protocol = "novel.protocol.v1"

[experiment.design]
scope = "whole_harness"
purpose = "exploratory"
vary = []

[execution]
backend = "novel.backend.v1"
model = "none"

[execution.budget]
max_tokens = 0
max_wall_seconds = 1.0
max_cost = 0.0
''',
        encoding="utf-8",
    )
    return project, experiment


def test_new_adapter_compiles_from_declared_policies_without_core_tuple(
    tmp_path: Path,
) -> None:
    project, experiment = _declarative_adapter_project(tmp_path)

    run = Compiler(project).compile(experiment)[0]

    capabilities = {
        artifact.capability.adapter_kind: artifact.capability
        for artifact in run.manifest.metadata.adapter_capabilities
    }
    assert set(capabilities) == {
        "benchmark_loader",
        "backend_factory",
        "execution",
    }
    assert capabilities["backend_factory"].backend_default_read_set == ("mode",)
    assert capabilities["execution"].supported_subject_adapters == (
        "novel-subject",
    )
    assert capabilities["execution"].none_model_sentinels == ("none",)
    assert capabilities["execution"].supported_state_reset_policies == (
        "per_case",
    )


@pytest.mark.parametrize(
    ("declaration", "old", "new", "message"),
    [
        (
            "registries/adapters/novel-execution.toml",
            'supported_subject_adapters = ["novel-subject"]',
            'supported_subject_adapters = ["other-subject"]',
            "rejects the resolved benchmark/subject/backend tuple",
        ),
        (
            "experiment.toml",
            'model = "none"',
            'model = "none/echo"',
            "ModelActivationReceipt missing",
        ),
        (
            "registries/protocols/novel.toml",
            'state_reset = "per_case"',
            'state_reset = "per_rollout"',
            "StateResetReceipt missing",
        ),
        (
            "registries/adapters/novel-backend.toml",
            'backend_default_read_set = ["mode"]',
            "backend_default_read_set = []",
            "backend defaults contain keys not read",
        ),
    ],
)
def test_declared_adapter_policy_mismatches_fail_closed(
    tmp_path: Path,
    declaration: str,
    old: str,
    new: str,
    message: str,
) -> None:
    project, experiment = _declarative_adapter_project(tmp_path)
    path = project / declaration
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new), encoding="utf-8")

    with pytest.raises(CompilationError, match=message):
        Compiler(project).compile(experiment)


def test_real_model_cannot_be_declared_as_a_none_model_sentinel(
    tmp_path: Path,
) -> None:
    project, experiment = _declarative_adapter_project(tmp_path)
    capability = project / "registries/adapters/novel-execution.toml"
    capability.write_text(
        capability.read_text(encoding="utf-8").replace(
            'none_model_sentinels = ["none"]',
            'none_model_sentinels = ["provider/model"]',
        ),
        encoding="utf-8",
    )
    experiment.write_text(
        experiment.read_text(encoding="utf-8").replace(
            'model = "none"', 'model = "provider/model"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(CompilationError, match="ModelActivationReceipt"):
        Compiler(project).compile(experiment)


def test_real_model_compiles_with_declared_activation_and_provider_binding(
    tmp_path: Path,
) -> None:
    project, experiment = _declarative_adapter_project(tmp_path)
    capability = project / "registries/adapters/novel-execution.toml"
    capability.write_text(
        capability.read_text(encoding="utf-8").replace(
            'none_model_sentinels = ["none"]',
            'model_activation_source = "native_result"',
        ),
        encoding="utf-8",
    )
    experiment.write_text(
        experiment.read_text(encoding="utf-8")
        .replace('model = "none"', 'model = "provider/model-v1"')
        .replace(
            "[execution.budget]",
            '''[execution.provider_binding]
provider_id = "provider"
base_url = "https://provider.example/v1"
wire_api = "responses"
model_id = "provider/model-v1"

[execution.provider_binding.credential_ref]
name = "provider-primary"
value_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
secret = true
source_file = "credentials/providers.toml"

[execution.budget]''',
        ),
        encoding="utf-8",
    )

    run = Compiler(project).compile(experiment)[0]

    binding = run.manifest.execution.provider_binding
    assert binding is not None
    assert binding.provider_id == "provider"
    assert binding.model_id == run.manifest.execution.model
    execution_capability = next(
        artifact.capability
        for artifact in run.manifest.metadata.adapter_capabilities
        if artifact.capability.adapter_kind == "execution"
    )
    assert execution_capability.model_activation_source == "native_result"
    registry = AdapterRegistry.from_project(
        project,
        required_capabilities=AdapterRegistry.required_capability_keys((run,)),
    )
    assert registry.execution_adapter(run).digest == execution_capability.digest


def test_runtime_and_standalone_checks_replay_subject_and_default_policies(
    tmp_path: Path,
) -> None:
    project, experiment = _declarative_adapter_project(tmp_path)
    run = Compiler(project).compile(experiment)[0]
    registry = AdapterRegistry.from_project(
        project,
        required_capabilities=AdapterRegistry.required_capability_keys((run,)),
    )

    changed_subject = run.manifest.subject.model_copy(
        update={"adapter": "undeclared-subject"}
    )
    changed_manifest = run.manifest.model_copy(update={"subject": changed_subject})
    changed_run = replace(run, manifest=changed_manifest)
    with pytest.raises(AdapterRegistryError, match="rejects compatibility tuple"):
        registry.execution_adapter(changed_run)

    changed_backend = run.manifest.execution.backend.model_copy(
        update={"defaults": {"mode": "safe", "undeclared": True}}
    )
    changed_execution = run.manifest.execution.model_copy(
        update={"backend": changed_backend}
    )
    changed_manifest = run.manifest.model_copy(
        update={"execution": changed_execution}
    )
    mismatches: list[str] = []
    _verify_manifest_adapter_capabilities(
        changed_manifest,
        label="manifest.adapter_capabilities",
        path_map={},
        mismatches=mismatches,
    )
    assert any("outside declared read-set" in item for item in mismatches)
