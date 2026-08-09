from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from MagentaBench.runner.adapter_registry import AdapterRegistry, verify_resolved_case_set
from MagentaBench.runner.backend.harbor import (
    DEFAULT_HARBOR_EXECUTABLE,
    HarborBackend,
    HarborConfigurationError,
    build_job_config,
    harbor_agent_name,
)
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.evidence import artifact_ref
from plugins.terminal_bench.adapter import TerminalBenchCase, TerminalBenchLoader
from MagentaBench.runner.adapter_registry import AdapterRegistryError


ROOT = Path(__file__).parents[1]
REGEX_EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/terminal-bench-regex-smoke.toml"


def _compiled():
    return Compiler(ROOT).compile(REGEX_EXPERIMENT)[0]


def test_terminal_bench_loader_replays_explicit_case_and_contract_refs(tmp_path: Path) -> None:
    run = _compiled()
    registry = AdapterRegistry.from_project(
        ROOT,
        required_capabilities=AdapterRegistry.required_capability_keys((run,)),
    )
    loader = registry.benchmark_loader(run)
    resolved = loader.resolve(run, tmp_path / "case-set")
    verify_resolved_case_set(
        run,
        resolved,
        expected_loader_adapter=loader.adapter,
        expected_loader_digest=loader.digest,
    )
    loaded = loader.load(run, resolved)
    assert loaded.artifact.ordered_case_ids == ("regex-log",)
    case = loaded.cases[0]
    assert case.task_name == "terminal-bench/regex-log"
    assert len(case.task_contract_refs) >= 2
    assert len(case.verifier_contract_refs) >= 1
    assert all(Path(ref.path).is_file() for ref in case.task_contract_refs)
    assert all(Path(ref.path).is_file() for ref in case.verifier_contract_refs)


def test_terminal_bench_loader_accepts_complete_case_set(tmp_path: Path) -> None:
    compiled = _compiled()
    protocol = compiled.manifest.execution.protocol.model_copy(
        update={"case_order": "fixed", "explicit_case_ids": ()}
    )
    run = replace(
        compiled,
        manifest=compiled.manifest.model_copy(
            update={
                "execution": compiled.manifest.execution.model_copy(
                    update={"protocol": protocol}
                )
            }
        ),
    )
    registry = AdapterRegistry.from_project(
        ROOT,
        required_capabilities=AdapterRegistry.required_capability_keys((run,)),
    )
    loader = registry.benchmark_loader(run)
    resolved = loader.resolve(run, tmp_path / "case-set")
    assert len(resolved.artifact.cases) == 89
    assert len(loader.load(run, resolved).cases) == 89


def test_terminal_bench_staging_is_allowlisted_and_toctou_checked(tmp_path: Path) -> None:
    run = _compiled()
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\nname='demo/task'\n", encoding="utf-8")
    (task / "instruction.md").write_text("do it\n", encoding="utf-8")
    (task / "environment/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (task / "tests/test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (task / "solution/solve.sh").write_text("secret\n", encoding="utf-8")
    (task / "README.md").write_text("private\n", encoding="utf-8")
    task_refs = tuple(
        artifact_ref(path)
        for path in (task / "task.toml", task / "instruction.md", task / "environment/Dockerfile")
    )
    verifier_refs = (artifact_ref(task / "tests/test.sh"),)
    case = TerminalBenchCase(
        task_id="demo-task",
        task_name="demo/task",
        task_path=str(task),
        task_manifest_ref=task_refs[0],
        task_contract_refs=task_refs,
        verifier_contract_refs=verifier_refs,
        case_set_digest="a" * 64,
        allow_internet=False,
    )
    backend = AdapterRegistry.from_project(
        ROOT,
        required_capabilities=AdapterRegistry.required_capability_keys((run,)),
    ).backend_factory(run).build(run, record_root=tmp_path / "records", workspace_root=tmp_path / "work")
    staged, _ = backend._stage_task(run, case, "attempt-0000")
    assert not (staged / "solution").exists()
    assert not (staged / "README.md").exists()
    assert (staged / "environment/Dockerfile").is_file()
    (task / "instruction.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="byte drift"):
        backend._stage_task(run, case, "attempt-0001")
    with pytest.raises(ValueError, match="already exists"):
        backend._stage_task(run, case, "attempt-0000")


def test_terminal_bench_subject_selects_native_harbor_agent() -> None:
    run = _compiled()
    assert harbor_agent_name(run.manifest.subject) == "nop"
    config = build_job_config(run, task_path=ROOT)
    assert config["agents"][0]["name"] == "nop"
    assert "tasks" in config and "datasets" not in config


def test_harbor_task_path_and_execution_identity_fail_closed(tmp_path: Path) -> None:
    run = _compiled()
    backend = HarborBackend(
        tmp_path / "records",
        harbor_executable=DEFAULT_HARBOR_EXECUTABLE,
        timeout_seconds=1,
    )
    with pytest.raises(HarborConfigurationError, match="invalid execution id"):
        backend.run(run, task_path=tmp_path, execution_id="bad/id")

    existing = backend.run_directory(run) / "harbor_runs" / "attempt-0000"
    existing.mkdir(parents=True)
    with pytest.raises(HarborConfigurationError, match="already exists"):
        backend.run(run, task_path=tmp_path, execution_id="attempt-0000")

    with pytest.raises(HarborConfigurationError, match="escapes"):
        backend.run(run, task_path=tmp_path, execution_id="attempt-0001")


def test_terminal_bench_network_fields_are_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _compiled()
    source = tmp_path / "bench"
    task = source / "tasks" / "demo"
    task.mkdir(parents=True)
    (task / "instruction.md").write_text("do\n", encoding="utf-8")
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (task / "task.toml").write_text(
        "[task]\nname='terminal-bench/demo'\n\n[environment]\nallow_internet='false'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(TerminalBenchLoader, "_source", staticmethod(lambda run: source))
    with pytest.raises(AdapterRegistryError, match="allow_internet must be boolean"):
        TerminalBenchLoader._tasks(run)
    (task / "task.toml").write_text(
        "[task]\nname='terminal-bench/demo'\n\n[environment]\nnetwork_mode='unknown'\n",
        encoding="utf-8",
    )
    with pytest.raises(AdapterRegistryError, match="unsupported.*network_mode"):
        TerminalBenchLoader._tasks(run)
