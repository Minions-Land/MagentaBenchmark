from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from MagentaBench.runner.compiler import CompilationError, Compiler
from MagentaBench.runner.configuration import ConfigurationRegistry
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import ReportVerificationError, verify_run_report


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance", project / "MagentaBench/conformance"
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
    registry = ConfigurationRegistry(project / "registries/configurations")
    registry.upsert(
        "agent.base",
        {
            "agent": {
                "model": "claude-opus-4.6",
                "reasoning_effort": "high",
                "max_context_tokens": 200_000,
                "max_generation_tokens": 128_000,
            },
            "debugger": {"max_model_turns": 25},
            "meta_agent": {"max_model_turns": 500},
        },
    )

    first = Compiler(project).compile(experiment)
    config = first[0].manifest.metadata.configuration
    assert config is not None
    assert config.adapter == "generic"
    assert config.values["agent"]["model"] == "claude-opus-4.6"
    assert config.values["agent"]["max_model_turns"] == 300
    assert config.artifact_digest == config.canonical_digest()
    assert config.source_refs

    registry.upsert(
        "agent.base",
        {
            "agent": {
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "max_context_tokens": 200_000,
                "max_generation_tokens": 128_000,
            }
        },
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


def test_standalone_verifier_rejects_configuration_source_drift(tmp_path: Path) -> None:
    project, experiment = _project(tmp_path)
    registry = ConfigurationRegistry(project / "registries/configurations")
    record = registry.upsert("agent.base", {"agent": {"model": "gpt-5.4-mini"}})
    result = Pipeline(project, tmp_path / "records").run(experiment)
    record.path.write_bytes(record.toml_bytes + b"\n")
    try:
        verify_run_report(result.report_path)
    except ReportVerificationError as exc:
        assert "configuration.source_refs" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("configuration source drift was accepted")


def test_custom_benchmark_can_compile_exploratory_with_external_adapter(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance", project / "MagentaBench/conformance"
    )
    (project / "registries/benchmarks/custom-demo.toml").write_text(
        """[benchmark]
id = "custom.demo"
kind = "custom"
adapter = "external.benchmark"
bmp_version = "0.1"
source = "../../MagentaBench/conformance/fixtures/fake_benchmark"
content_globs = ["tasks.toml"]
verifier = "external.verifier:v1"
scoring_kind = "continuous"
authoritative_reward_metric = "quality"

[benchmark.config]
split = "test"
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
subject = "fake.nonfake"
protocol = "custom.evaluation.v1"

[experiment.design]
scope = "whole_harness"
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
    run = Compiler(project).compile(experiment)[0]
    assert run.manifest.benchmark.kind == "custom"
    assert run.manifest.benchmark.adapter == "external.benchmark"
    assert run.manifest.claim_design.purpose.value == "exploratory"
