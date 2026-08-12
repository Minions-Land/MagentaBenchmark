from __future__ import annotations

from dataclasses import replace
from functools import partial
import json
from pathlib import Path
import sys

import pytest

from MagentaBench.runner.adapter_registry import AdapterRegistry, verify_resolved_case_set
from MagentaBench.runner.backend.harbor import (
    HarborBackend,
    HarborConfigurationError,
    build_job_config,
    harbor_agent_name,
)
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.evidence import artifact_ref
import plugins.terminal_bench.adapter as terminal_bench_adapter
from plugins.terminal_bench.adapter import TerminalBenchCase, TerminalBenchLoader
from MagentaBench.runner.adapter_registry import AdapterRegistryError
from MagentaBench.runner.backend.fake import CaseExecution
from MagentaBench.schemas import (
    EvidenceBundle,
    ProvenanceRecord,
    RunStatus,
    VerifierEvidence,
)


ROOT = Path(__file__).parents[1]
REGEX_EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/terminal-bench-regex-smoke.toml"
MAGENTA_EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml"


def _compiled(source: Path, bind_registry_source):
    compiler = Compiler(ROOT)
    bind_registry_source(
        compiler,
        "dataset",
        "dataset.terminal-bench-2.1",
        source,
    )
    compiled = compiler.compile(REGEX_EXPERIMENT)[0]
    assert Path(compiled.manifest.dataset.source) == source.resolve()
    return compiled


def _magenta_compiled(source: Path, bind_registry_source):
    compiler = Compiler(ROOT)
    bind_registry_source(
        compiler,
        "dataset",
        "dataset.terminal-bench-2.1",
        source,
    )
    compiled = compiler.compile(MAGENTA_EXPERIMENT)[0]
    assert Path(compiled.manifest.dataset.source) == source.resolve()
    return compiled


def _bind_harbor_executable(run, executable: Path):
    version, digest = HarborBackend._inspect_executable(str(executable))
    backend = run.manifest.execution.backend.model_copy(
        update={
            "executable": str(executable),
            "version": version,
            "digest": digest,
        }
    )
    execution = run.manifest.execution.model_copy(update={"backend": backend})
    return replace(
        run,
        manifest=run.manifest.model_copy(update={"execution": execution}),
    )


def _bind_pinned_source_path(compiler: Compiler, source: Path) -> None:
    """Relocate the external checkout while preserving its declared commit."""

    original_lookup = compiler._lookup

    def lookup(kind: str, entry_id: str):
        spec, declaration_path = original_lookup(kind, entry_id)
        if (kind, entry_id) == ("dataset", "dataset.terminal-bench-2.1"):
            spec = spec.model_copy(
                update={"source": str(source.resolve(strict=True))}
            )
        return spec, declaration_path

    compiler._lookup = lookup


def test_terminal_bench_loader_replays_explicit_case_and_contract_refs(
    tmp_path: Path, terminal_bench_source: Path, bind_registry_source
) -> None:
    run = _compiled(terminal_bench_source, bind_registry_source)
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
    assert case.verifier_completion_artifact is None
    assert len(case.task_contract_refs) >= 2
    assert len(case.verifier_contract_refs) >= 1
    assert all(Path(ref.path).is_file() for ref in case.task_contract_refs)
    assert all(Path(ref.path).is_file() for ref in case.verifier_contract_refs)


@pytest.mark.external_checkout
def test_terminal_bench_loader_accepts_complete_release_case_set(
    tmp_path: Path,
    terminal_bench_release_source: Path,
) -> None:
    """Replay the pinned 89-case release when its checkout is provisioned."""

    compiler = Compiler(ROOT)
    declared, _ = compiler._lookup("dataset", "dataset.terminal-bench-2.1")
    _bind_pinned_source_path(compiler, terminal_bench_release_source)
    compiled = compiler.compile(REGEX_EXPERIMENT)[0]
    assert (
        Path(compiled.manifest.dataset.source)
        == terminal_bench_release_source.resolve()
    )
    assert compiled.manifest.dataset.commit == declared.commit
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
    loaded = loader.load(run, resolved)
    assert len(loaded.cases) == 89
    assert all(
        case.verifier_completion_artifact == "verifier/ctrf.json"
        for case in loaded.cases
    )


def test_terminal_bench_staging_is_allowlisted_and_toctou_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_bench_source: Path,
    bind_registry_source,
) -> None:
    run = _compiled(terminal_bench_source, bind_registry_source)
    executable = Path(sys.executable).resolve(strict=True)
    monkeypatch.setattr(
        terminal_bench_adapter,
        "HarborBackend",
        partial(HarborBackend, harbor_executable=executable),
    )
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
        verifier_completion_artifact=None,
    )
    backend = terminal_bench_adapter.TerminalBenchHarborBackend(
        tmp_path / "records",
        timeout_seconds=run.manifest.execution.budget.max_wall_seconds,
    )
    staged, _ = backend._stage_task(run, case, "attempt-0000")
    assert not (staged / "solution").exists()
    assert not (staged / "README.md").exists()
    assert (staged / "environment/Dockerfile").is_file()
    (task / "instruction.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="byte drift"):
        backend._stage_task(run, case, "attempt-0001")
    with pytest.raises(ValueError, match="already exists"):
        backend._stage_task(run, case, "attempt-0000")


def test_terminal_bench_subject_selects_native_harbor_agent(
    terminal_bench_source: Path, bind_registry_source
) -> None:
    run = _compiled(terminal_bench_source, bind_registry_source)
    assert harbor_agent_name(run.manifest.subject) == "nop"
    config = build_job_config(run, task_path=ROOT)
    assert config["agents"][0]["name"] == "nop"
    assert "tasks" in config and "datasets" not in config


def test_magenta_subject_selects_pinned_custom_agent_and_provider_env(
    terminal_bench_source: Path, bind_registry_source
) -> None:
    run = _magenta_compiled(terminal_bench_source, bind_registry_source)
    config = build_job_config(run)
    agent = config["agents"][0]
    assert agent["import_path"] == "plugins.terminal_bench.magenta_agent:MagentaAgent"
    assert agent["kwargs"] == {
        "release_version": "0.1.23",
        "github_mirror": "https://ghfast.top",
    }
    assert agent["env"] == {
        "OPENAI_API_KEY": "${OPENAI_API_KEY:-}",
        "OPENAI_BASE_URL": "${OPENAI_BASE_URL:-}",
    }
    assert "name" not in agent


def test_harbor_task_path_and_execution_identity_fail_closed(
    tmp_path: Path, terminal_bench_source: Path, bind_registry_source
) -> None:
    run = _compiled(terminal_bench_source, bind_registry_source)
    executable = Path(sys.executable).resolve(strict=True)
    run = _bind_harbor_executable(run, executable)
    backend = HarborBackend(
        tmp_path / "records",
        harbor_executable=executable,
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


def test_terminal_bench_network_fields_are_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_bench_source: Path,
    bind_registry_source,
) -> None:
    run = _compiled(terminal_bench_source, bind_registry_source)
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


def _terminal_native_case(
    tmp_path: Path,
    *,
    status: RunStatus,
    include_ctrf: bool,
    valid_ctrf: bool = True,
    verifier_evidence: bool = True,
) -> CaseExecution:
    case_dir = tmp_path / "native"
    case_dir.mkdir()
    result_path = case_dir / "result.json"
    result_path.write_text("{}\n", encoding="utf-8")
    refs = [artifact_ref(result_path)]
    verifier_dir = case_dir / "harbor_artifacts" / "trial" / "verifier"
    verifier_dir.mkdir(parents=True)
    stdout = verifier_dir / "test-stdout.txt"
    summary = (
        "============================== 1 passed in 0.12s ===============================\n"
        if status == RunStatus.pass_
        else "============================== 1 failed in 0.12s ===============================\n"
    )
    stdout.write_text(summary, encoding="utf-8")
    refs.append(artifact_ref(stdout))
    if include_ctrf:
        ctrf = verifier_dir / "ctrf.json"
        payload = (
            {
                "results": {
                    "tool": {"name": "pytest", "version": "8.4.1"},
                    "summary": {
                        "tests": 1,
                        "passed": 1 if status == RunStatus.pass_ else 0,
                        "failed": 0 if status == RunStatus.pass_ else 1,
                        "skipped": 0,
                        "pending": 0,
                        "other": 0,
                    },
                    "tests": [
                        {
                            "name": "test_output",
                            "status": (
                                "passed"
                                if status == RunStatus.pass_
                                else "failed"
                            ),
                        }
                    ],
                }
            }
            if valid_ctrf
            else {"results": {"tool": {"name": "pytest"}}}
        )
        ctrf.write_text(json.dumps(payload), encoding="utf-8")
        refs.append(artifact_ref(ctrf))
    bundle = EvidenceBundle(
        run_id="native",
        status=status,
        output_refs=(artifact_ref(result_path),),
        log_refs=tuple(refs),
        verifier_evidence=(
            VerifierEvidence(
                verifier="harbor.native",
                passed=status == RunStatus.pass_,
                score=1.0 if status == RunStatus.pass_ else 0.0,
                metrics={"reward": 1.0 if status == RunStatus.pass_ else 0.0},
                artifact_refs=(artifact_ref(result_path),),
            )
            if verifier_evidence
            else None
        ),
        provenance=ProvenanceRecord(
            manifest_digest="a" * 64,
            runner_digest="b" * 64,
            benchmark_digest="c" * 64,
            subject_digest="d" * 64,
            backend_digest="e" * 64,
        ),
    )
    bundle_path = case_dir / "evidence_bundle.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    return CaseExecution(
        case_id="native",
        bundle=bundle,
        bundle_path=bundle_path,
        bundle_digest="f" * 64,
    )


def test_terminal_bench_reward_zero_without_ctrf_is_verifier_error(tmp_path: Path) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=False,
    )
    case = TerminalBenchCase(
        task_id="regex-log",
        task_name="terminal-bench/regex-log",
        task_path=str(tmp_path),
        task_manifest_ref=artifact_ref(native.bundle_path),
        task_contract_refs=(),
        verifier_contract_refs=(),
        case_set_digest="a" * 64,
        allow_internet=True,
        verifier_completion_artifact="verifier/ctrf.json",
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(native, case)
    assert bundle.status == RunStatus.verifier_error
    assert not bundle.output_refs
    assert bundle.verifier_evidence is not None
    assert bundle.verifier_evidence.details["completion_evidence"]["valid"] is False
    status_ref = next(
        ref
        for ref in bundle.log_refs
        if Path(ref.path).name == "verifier_completion_status.json"
    )
    assert status_ref in bundle.verifier_evidence.artifact_refs
    status = json.loads(Path(status_ref.path).read_text(encoding="utf-8"))
    assert status["case_id"] == "regex-log"
    assert status["status"] == RunStatus.verifier_error.value


def test_terminal_bench_valid_ctrf_preserves_verified_failure(tmp_path: Path) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    case = TerminalBenchCase(
        task_id="regex-log",
        task_name="terminal-bench/regex-log",
        task_path=str(tmp_path),
        task_manifest_ref=artifact_ref(native.bundle_path),
        task_contract_refs=(),
        verifier_contract_refs=(),
        case_set_digest="a" * 64,
        allow_internet=True,
        verifier_completion_artifact="verifier/ctrf.json",
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(native, case)
    assert bundle.status == RunStatus.verified_fail
    assert bundle.verifier_evidence is not None
    completion = bundle.verifier_evidence.details["completion_evidence"]
    assert completion["tests"] == 1
    assert completion["kind"] == "ctrf+pytest-stdout"
    assert {Path(ref.path).name for ref in bundle.verifier_evidence.artifact_refs} >= {
        "ctrf.json",
        "test-stdout.txt",
    }


@pytest.mark.parametrize(
    ("status", "passed", "failed"),
    [
        (RunStatus.pass_, 1, 0),
        (RunStatus.verified_fail, 0, 1),
    ],
)
def test_terminal_bench_valid_ctrf_preserves_native_outcome(
    tmp_path: Path,
    status: RunStatus,
    passed: int,
    failed: int,
) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=status,
        include_ctrf=True,
    )
    ctrf = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "ctrf.json"
    )
    payload = json.loads(ctrf.read_text(encoding="utf-8"))
    payload["results"]["summary"].update({"passed": passed, "failed": failed})
    payload["results"]["tests"][0]["status"] = (
        "passed" if status == RunStatus.pass_ else "failed"
    )
    ctrf.write_text(json.dumps(payload), encoding="utf-8")
    refreshed = artifact_ref(ctrf)
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed if Path(ref.path) == ctrf else ref
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    case = _terminal_case(tmp_path, native)
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(native, case)
    assert bundle.status == status
    assert bundle.verifier_evidence is not None
    assert bundle.verifier_evidence.details["completion_evidence"]["passed"] == passed
    assert bundle.verifier_evidence.details["completion_evidence"]["failed"] == failed


def _terminal_case(tmp_path: Path, native: CaseExecution) -> TerminalBenchCase:
    return TerminalBenchCase(
        task_id="regex-log",
        task_name="terminal-bench/regex-log",
        task_path=str(tmp_path),
        task_manifest_ref=artifact_ref(native.bundle_path),
        task_contract_refs=(),
        verifier_contract_refs=(),
        case_set_digest="a" * 64,
        allow_internet=True,
        verifier_completion_artifact="verifier/ctrf.json",
    )


def test_terminal_bench_ctrf_reference_drift_is_verifier_error(tmp_path: Path) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    ctrf = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "ctrf.json"
    )
    ctrf.write_text("{}\n", encoding="utf-8")
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verifier_error
    assert bundle.verifier_evidence is not None
    assert "digest drift" in bundle.verifier_evidence.details["completion_evidence"]["reason"]


def test_terminal_bench_invalid_ctrf_is_verifier_error(tmp_path: Path) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
        valid_ctrf=False,
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verifier_error
    assert bundle.verifier_evidence is not None
    assert "summary or tests" in bundle.verifier_evidence.details["completion_evidence"]["reason"]


def test_terminal_bench_non_object_ctrf_is_verifier_error(tmp_path: Path) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    ctrf = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "ctrf.json"
    )
    ctrf.write_text("[]\n", encoding="utf-8")
    refreshed = artifact_ref(ctrf)
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed if Path(ref.path) == ctrf else ref
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verifier_error
    assert bundle.verifier_evidence is not None
    assert (
        bundle.verifier_evidence.details["completion_evidence"]["reason"]
        == "CTRF document must be an object"
    )


def test_terminal_bench_duplicate_ctrf_is_verifier_error(tmp_path: Path) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    ctrf_ref = next(
        ref for ref in native.bundle.log_refs if Path(ref.path).name == "ctrf.json"
    )
    duplicate = tmp_path / "duplicate" / "verifier" / "ctrf.json"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(Path(ctrf_ref.path).read_bytes())
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={"log_refs": (*native.bundle.log_refs, artifact_ref(duplicate))}
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verifier_error
    assert bundle.verifier_evidence is not None
    assert "found 2" in bundle.verifier_evidence.details["completion_evidence"]["reason"]


def test_terminal_bench_stale_ctrf_with_bootstrap_stdout_is_verifier_error(
    tmp_path: Path,
) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    stdout = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "test-stdout.txt"
    )
    stdout.write_text("uvx: command not found\n", encoding="utf-8")
    refreshed = artifact_ref(stdout)
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed if Path(ref.path) == stdout else ref
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verifier_error
    assert bundle.verifier_evidence is not None
    assert (
        "completed pytest summary"
        in bundle.verifier_evidence.details["completion_evidence"]["reason"]
    )


def test_terminal_bench_stdout_counts_must_match_ctrf(tmp_path: Path) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    stdout = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "test-stdout.txt"
    )
    stdout.write_text(
        "============================== 2 failed in 0.12s ===============================\n",
        encoding="utf-8",
    )
    refreshed = artifact_ref(stdout)
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed if Path(ref.path) == stdout else ref
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verifier_error
    assert bundle.verifier_evidence is not None
    assert "counts disagree" in bundle.verifier_evidence.details["completion_evidence"]["reason"]


def test_terminal_bench_last_of_multiple_pytest_summaries_must_match_ctrf(
    tmp_path: Path,
) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    stdout = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "test-stdout.txt"
    )
    stdout.write_text(
        "============================== 3 passed in 0.08s ===============================\n"
        "============================== 1 failed in 0.12s ===============================\n",
        encoding="utf-8",
    )
    refreshed = artifact_ref(stdout)
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed if Path(ref.path) == stdout else ref
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verified_fail
    assert bundle.verifier_evidence is not None
    completion = bundle.verifier_evidence.details["completion_evidence"]
    assert completion["kind"] == "ctrf+pytest-stdout"
    assert completion["stdout_completed_pytest_runs"] == 2
    assert completion["stdout_summary"] == {
        "passed": 0,
        "failed": 1,
        "skipped": 0,
    }


def test_terminal_bench_native_failure_may_come_from_an_earlier_pytest_run(
    tmp_path: Path,
) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.verified_fail,
        include_ctrf=True,
    )
    ctrf = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "ctrf.json"
    )
    payload = json.loads(ctrf.read_text(encoding="utf-8"))
    payload["results"]["summary"].update({"passed": 1, "failed": 0})
    payload["results"]["tests"][0]["status"] = "passed"
    ctrf.write_text(json.dumps(payload), encoding="utf-8")
    stdout = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "test-stdout.txt"
    )
    stdout.write_text(
        "============================== 2 failed in 0.08s ===============================\n"
        "============================== 1 passed in 0.12s ===============================\n",
        encoding="utf-8",
    )
    refreshed = {ctrf: artifact_ref(ctrf), stdout: artifact_ref(stdout)}
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed.get(Path(ref.path), ref)
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verified_fail
    assert bundle.verifier_evidence is not None
    completion = bundle.verifier_evidence.details["completion_evidence"]
    assert completion["passed"] == 1
    assert completion["failed"] == 0
    assert completion["stdout_completed_pytest_runs"] == 2


def test_terminal_bench_native_pass_requires_every_pytest_run_to_pass(
    tmp_path: Path,
) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.pass_,
        include_ctrf=True,
    )
    stdout = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "test-stdout.txt"
    )
    stdout.write_text(
        "============================== 1 failed in 0.08s ===============================\n"
        "============================== 1 passed in 0.12s ===============================\n",
        encoding="utf-8",
    )
    refreshed = artifact_ref(stdout)
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed if Path(ref.path) == stdout else ref
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.verifier_error
    assert bundle.verifier_evidence is not None
    assert (
        "native reward"
        in bundle.verifier_evidence.details["completion_evidence"]["reason"]
    )


def test_terminal_bench_skipped_count_is_bound_across_ctrf_and_stdout(
    tmp_path: Path,
) -> None:
    native = _terminal_native_case(
        tmp_path,
        status=RunStatus.pass_,
        include_ctrf=True,
    )
    ctrf = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "ctrf.json"
    )
    payload = json.loads(ctrf.read_text(encoding="utf-8"))
    payload["results"]["summary"].update(
        {"passed": 0, "failed": 0, "skipped": 1}
    )
    payload["results"]["tests"][0]["status"] = "skipped"
    ctrf.write_text(json.dumps(payload), encoding="utf-8")
    stdout = next(
        Path(ref.path)
        for ref in native.bundle.log_refs
        if Path(ref.path).name == "test-stdout.txt"
    )
    stdout.write_text(
        "============================== 1 skipped in 0.12s ===============================\n",
        encoding="utf-8",
    )
    refreshed = {ctrf: artifact_ref(ctrf), stdout: artifact_ref(stdout)}
    native = replace(
        native,
        bundle=native.bundle.model_copy(
            update={
                "log_refs": tuple(
                    refreshed.get(Path(ref.path), ref)
                    for ref in native.bundle.log_refs
                )
            }
        ),
    )
    backend = object.__new__(terminal_bench_adapter.TerminalBenchHarborBackend)
    bundle = backend._validate_verifier_completion(
        native,
        _terminal_case(tmp_path, native),
    )
    assert bundle.status == RunStatus.pass_
    assert bundle.verifier_evidence is not None
    completion = bundle.verifier_evidence.details["completion_evidence"]
    assert completion["skipped"] == 1
    assert completion["stdout_summary"]["skipped"] == 1
