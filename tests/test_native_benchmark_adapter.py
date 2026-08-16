from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from MagentaBench.runner.adapter_registry import AdapterRegistry, AdapterRegistryError
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.evidence import artifact_ref, source_closure_digest
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.runner.scheduler import ScheduledAttempt
from MagentaBench.schemas import BudgetAllocation, RunStatus, verify_run_report
from plugins.native_benchmark.adapter import (
    NativeBenchmarkConfigurationError,
    NativeBenchmarkLoader,
    NativeProcessBackend,
)


ROOT = Path(__file__).parents[1]
EXPERIMENTS = ROOT / "MagentaBench/conformance/experiments"
FIXTURE = ROOT / "tests/fixtures/native_benchmark"


def _compiled(name: str = "native-benchmark-continuous-smoke.toml"):
    return Compiler(ROOT).compile(EXPERIMENTS / name)[0]


def _activated_case(run, artifact_root: Path):
    registry = AdapterRegistry.from_project(
        ROOT,
        required_capabilities=AdapterRegistry.required_capability_keys((run,)),
    )
    loader = registry.benchmark_loader(run)
    resolved = loader.resolve(run, artifact_root)
    return loader.load(run, resolved).cases[0]


def _attempt(attempt_id: str, *, wall_seconds: float = 2.0) -> ScheduledAttempt:
    return ScheduledAttempt(
        case_id="case-one",
        attempt_id=attempt_id,
        attempt_index=0,
        allocation=BudgetAllocation(max_tokens=0, max_cost=0.0),
        remaining_wall_seconds=wall_seconds,
    )


def _driver_run(run, driver: Path):
    source_digest = source_closure_digest(driver.parent, (artifact_ref(driver),))
    subject = run.manifest.subject.model_copy(
        update={
            "source": str(driver.parent.resolve()),
            "source_content_digest": source_digest,
            "content_globs": (driver.name,),
            "entrypoint": "/usr/bin/python3",
            "launch_argv": (
                "/usr/bin/python3",
                "{subject_source}/" + driver.name,
                "--case-id",
                "{case_id}",
                "--output-dir",
                "{output_dir}",
            ),
        }
    )
    manifest = run.manifest.model_copy(update={"subject": subject})
    return replace(run, manifest=manifest)


def _write_driver(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_native_process_rejects_subject_source_drift(tmp_path: Path) -> None:
    driver = _write_driver(
        tmp_path / "source-drift/driver.py",
        "raise SystemExit('original driver should not execute')\n",
    )
    run = _driver_run(_compiled(), driver)
    case = _activated_case(run, tmp_path / "case-set")
    driver.write_text("raise SystemExit('changed')\n", encoding="utf-8")
    backend = NativeProcessBackend(
        tmp_path / "records", workspace_root=tmp_path / "workspaces"
    )
    with pytest.raises(
        NativeBenchmarkConfigurationError, match="subject source drift"
    ):
        backend.execute(run, case, _attempt("attempt-source-drift"))


def test_native_pipeline_retains_all_metrics_trace_and_standalone_verifies(
    tmp_path: Path,
) -> None:
    continuous = Pipeline(ROOT, tmp_path / "continuous").run(
        EXPERIMENTS / "native-benchmark-continuous-smoke.toml"
    )
    continuous_bundle = continuous.runs[0].case.bundle
    assert continuous_bundle.status == RunStatus.scored
    assert continuous_bundle.verifier_evidence is not None
    assert continuous_bundle.verifier_evidence.passed is None
    assert dict(continuous_bundle.verifier_evidence.metrics) == {
        "native_score": 0.75,
        "secondary_error": 0.25,
        "secondary_exact": 1.0,
    }
    assert continuous_bundle.trace_ref is not None
    assert Path(continuous_bundle.trace_ref.path).read_text(encoding="utf-8")
    verified = verify_run_report(continuous.report_path).report
    assert verified.protocol_valid is True
    assert verified.isolation_valid is False
    assert any(
        "cannot substantiate isolation" in reason
        for reason in verified.isolation_reasons
    )

    binary = Pipeline(ROOT, tmp_path / "binary").run(
        EXPERIMENTS / "native-benchmark-binary-smoke.toml"
    )
    binary_bundle = binary.runs[0].case.bundle
    assert binary_bundle.status == RunStatus.pass_
    assert binary_bundle.verifier_evidence is not None
    assert binary_bundle.verifier_evidence.passed is True
    assert binary_bundle.verifier_evidence.score == 0.75
    verify_run_report(binary.report_path)


def test_native_loader_rejects_path_escape_symlink_and_content_drift(
    tmp_path: Path, bind_registry_source
) -> None:
    escaped_source = tmp_path / "escaped-source"
    shutil.copytree(FIXTURE, escaped_source)
    escape = tmp_path / "escape.json"
    escape.write_text("{}\n", encoding="utf-8")
    manifest_path = escaped_source / "cases.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["cases"][0]["public_input"] = "../escape.json"
    manifest_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    compiler = Compiler(ROOT)
    bind_registry_source(
        compiler,
        "dataset",
        "dataset.native-benchmark.conformance.v1",
        escaped_source,
    )
    escaped_run = compiler.compile(
        EXPERIMENTS / "native-benchmark-continuous-smoke.toml"
    )[0]
    with pytest.raises(NativeBenchmarkConfigurationError, match="normalized relative"):
        NativeBenchmarkLoader().resolve(escaped_run, tmp_path / "escaped-artifacts")

    symlink_source = tmp_path / "symlink-source"
    shutil.copytree(FIXTURE, symlink_source)
    (symlink_source / "public/case-one.json").unlink()
    (symlink_source / "public/case-one.json").symlink_to(escape)
    compiler = Compiler(ROOT)
    bind_registry_source(
        compiler,
        "dataset",
        "dataset.native-benchmark.conformance.v1",
        symlink_source,
    )
    with pytest.raises(ValueError, match="symlink"):
        compiler.compile(EXPERIMENTS / "native-benchmark-continuous-smoke.toml")

    drift_source = tmp_path / "drift-source"
    shutil.copytree(FIXTURE, drift_source)
    compiler = Compiler(ROOT)
    bind_registry_source(
        compiler,
        "dataset",
        "dataset.native-benchmark.conformance.v1",
        drift_source,
    )
    drift_run = compiler.compile(
        EXPERIMENTS / "native-benchmark-continuous-smoke.toml"
    )[0]
    (drift_source / "contracts/task.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(AdapterRegistryError, match="source closure"):
        NativeBenchmarkLoader().resolve(drift_run, tmp_path / "drift-artifacts")


def test_native_process_redacts_forwarded_values(tmp_path: Path, monkeypatch) -> None:
    secret = "native-secret-value-927461"
    monkeypatch.setenv("NATIVE_BENCH_SECRET", secret)
    driver = _write_driver(
        tmp_path / "secret-driver/driver.py",
        """import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--case-id'); p.add_argument('--output-dir'); a=p.parse_args()
print(os.environ['NATIVE_BENCH_SECRET'])
o=Path(a.output_dir); o.mkdir(parents=True, exist_ok=True)
r={'schema_version':'magentabench.native-result.v1','case_id':a.case_id,'metrics':{'native_score':1.0},'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0,'cost':0.0},'artifacts':[],'trace':None,'model_activation':None}
(o/'result.json').write_text(json.dumps(r)+'\\n')
""",
    )
    run = _driver_run(_compiled(), driver)
    case = replace(
        _activated_case(run, tmp_path / "case-set"),
        subject_source=str(driver.parent),
    )
    records = tmp_path / "records"
    workspace = tmp_path / "workspaces"
    backend = NativeProcessBackend(
        records,
        workspace_root=workspace,
        environment_variable_names=("NATIVE_BENCH_SECRET",),
    )
    result = backend.execute(run, case, _attempt("attempt-secret"))
    assert result.bundle.status == RunStatus.scored
    assert not backend.workspace_directory(run, "attempt-secret").exists()
    for path in records.rglob("*"):
        if path.is_file():
            assert secret.encode("utf-8") not in path.read_bytes()
    stdout = next(records.rglob("stdout.log")).read_text(encoding="utf-8")
    assert "[REDACTED:NATIVE_BENCH_SECRET]" in stdout
    receipt = json.loads(next(records.rglob("subject_receipt.json")).read_text())
    assert receipt["environment_variable_names"] == ["NATIVE_BENCH_SECRET"]


@pytest.mark.parametrize(
    ("name", "body", "wall_seconds", "expected"),
    (
        (
            "nonzero",
            "import sys\nsys.exit(7)\n",
            2.0,
            RunStatus.agent_error,
        ),
        (
            "malformed",
            """import argparse
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--case-id'); p.add_argument('--output-dir'); a=p.parse_args()
o=Path(a.output_dir); o.mkdir(parents=True, exist_ok=True); (o/'result.json').write_text('{bad')
""",
            2.0,
            RunStatus.invalid_output,
        ),
        (
            "timeout",
            "import time\ntime.sleep(5)\n",
            0.05,
            RunStatus.timeout,
        ),
    ),
)
def test_native_process_classifies_process_and_result_failures(
    tmp_path: Path,
    name: str,
    body: str,
    wall_seconds: float,
    expected: RunStatus,
) -> None:
    driver = _write_driver(tmp_path / name / "driver.py", body)
    run = _driver_run(_compiled(), driver)
    case = replace(
        _activated_case(run, tmp_path / (name + "-case-set")),
        subject_source=str(driver.parent),
    )
    backend = NativeProcessBackend(
        tmp_path / (name + "-records"),
        workspace_root=tmp_path / (name + "-workspaces"),
    )
    result = backend.execute(
        run,
        case,
        _attempt("attempt-" + name, wall_seconds=wall_seconds),
    )
    assert result.bundle.status == expected
    assert result.bundle.verifier_evidence is None
    if expected == RunStatus.invalid_output:
        assert not backend.workspace_directory(run, "attempt-" + name).exists()


def test_native_process_rejects_unknown_launch_placeholder(tmp_path: Path) -> None:
    run = _compiled()
    subject = run.manifest.subject.model_copy(
        update={
            "launch_argv": ("/usr/bin/python3", "{unknown}"),
        }
    )
    run = replace(
        run,
        manifest=run.manifest.model_copy(update={"subject": subject}),
    )
    case = _activated_case(run, tmp_path / "case-set")
    backend = NativeProcessBackend(
        tmp_path / "records", workspace_root=tmp_path / "workspaces"
    )
    with pytest.raises(
        NativeBenchmarkConfigurationError, match="unsupported placeholder"
    ):
        backend.execute(run, case, _attempt("attempt-placeholder"))
