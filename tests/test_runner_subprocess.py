from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from MagentaBench.runner.backend.subprocess import SubprocessBackend
from MagentaBench.runner.compiler import (
    CompiledRun,
    Compiler,
    canonical_manifest_json,
    sha256_bytes,
)
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.runner.scheduler import Scheduler
from MagentaBench.schemas import (
    Budget,
    EnvironmentReceipt,
    EnvironmentSpec,
    ProvenanceRecord,
    RunStatus,
)


ROOT = Path(__file__).parents[1]
EXPERIMENT = (
    ROOT
    / "MagentaBench"
    / "conformance"
    / "experiments"
    / "subprocess-echo-smoke.toml"
)


def _with_timeout(run: CompiledRun, seconds: float) -> CompiledRun:
    budget = Budget(max_tokens=0, max_wall_seconds=seconds, max_cost=0.0)
    execution = run.manifest.execution.model_copy(update={"budget": budget})
    manifest = run.manifest.model_copy(update={"execution": execution})
    canonical = canonical_manifest_json(manifest)
    return replace(run, manifest=manifest)


def test_echo_agent_runs_through_full_subprocess_pipeline(tmp_path: Path) -> None:
    records = tmp_path / "records"
    workspaces = tmp_path / "workspaces"
    backend = SubprocessBackend(records, workspace_root=workspaces)

    with pytest.raises(ValueError, match="test override evidence"):
        Pipeline(
            ROOT, records, backend=backend, allow_test_override=True
        ).run(EXPERIMENT)

    bundle_paths = sorted(records.rglob("evidence_bundle.json"))
    assert len(bundle_paths) == 4
    statuses = []
    for bundle_path in bundle_paths:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        statuses.append(bundle["status"])
        assert bundle["provenance"]["executable"] == "/usr/bin/echo"
        assert bundle["provenance"]["test_override"]["forced_scope"] == "conformance"
        receipt = json.loads(
            Path(bundle["log_refs"][2]["path"]).read_text(encoding="utf-8")
        )
        assert receipt["workspace_kept"] is False
        assert not Path(receipt["workspace"]).exists()
        assert Path(bundle["log_refs"][0]["path"]).read_text(encoding="utf-8") in {
            "BMP_OK\n",
            "BMP_BAD\n",
        }
    assert sorted(statuses) == ["pass", "pass", "verified_fail", "verified_fail"]
    assert not list(workspaces.rglob("case-001"))


def test_subprocess_command_preserves_subject_launch_argv(tmp_path: Path) -> None:
    run = SimpleNamespace(
        manifest=SimpleNamespace(
            subject=SimpleNamespace(
                id="cli-subject",
                kind="opaque_agent",
                entrypoint="/usr/bin/echo",
                launch_argv=("/usr/bin/echo", "BMP_OK"),
            ),
            execution=SimpleNamespace(backend=SimpleNamespace()),
        )
    )
    assert SubprocessBackend._command(run) == ("/usr/bin/echo", "BMP_OK")


def test_subprocess_timeout_is_classified_and_keeps_failure_workspace(
    tmp_path: Path,
) -> None:
    run = _with_timeout(Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0], 0.01)
    backend = SubprocessBackend(
        tmp_path / "records",
        workspace_root=tmp_path / "workspaces",
        keep_workspace_on_failure=True,
        allow_test_override=True,
    )

    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "import time; time.sleep(1); print('BMP_OK')",
        ),
    )

    assert result.bundle.status == RunStatus.timeout
    assert result.bundle.output_refs == ()
    assert result.bundle.usage is not None
    assert result.bundle.usage.wall_clock_seconds is not None
    assert result.bundle.usage.wall_clock_seconds >= 0.01
    receipt = json.loads(
        Path(result.bundle.log_refs[2].path).read_text(encoding="utf-8")
    )
    assert receipt["workspace_kept"] is True
    assert Path(receipt["workspace"]).is_dir()


def test_environment_receipt_is_carried_into_evidence_provenance(
    tmp_path: Path,
) -> None:
    spec = EnvironmentSpec(id="echo-env", python_version="3.11")
    receipt = EnvironmentReceipt(
        spec_id=spec.id,
        spec_digest=spec.canonical_digest(),
        python_executable="/usr/bin/python3.11",
        python_version="3.11.13",
        installed_packages=(),
        build_duration_seconds=0.0,
        built_at="2026-08-06T16:00:00+00:00",
    )
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]
    backend = SubprocessBackend(
        tmp_path / "records",
        workspace_root=tmp_path / "workspaces",
        environment_receipt=receipt,
    )
    case = backend.execute(run)
    assert case.bundle.provenance.environment_receipt == receipt


def test_provenance_does_not_serialize_api_key_values() -> None:
    """ProvenanceRecord must not accept or serialize API-key values."""
    with pytest.raises(Exception):
        ProvenanceRecord.model_validate(
            {
                "manifest_digest": "0" * 64,
                "runner_digest": "1" * 64,
                "benchmark_digest": "2" * 64,
                "subject_digest": "3" * 64,
                "backend_digest": "backend",
                "environment": {"OPENAI_API_KEY": "sk-secret"},
            }
        )


def test_provenance_rejects_equals_in_string_fields() -> None:
    with pytest.raises(Exception, match="must not contain"):
        ProvenanceRecord(
            manifest_digest="0" * 64,
            runner_digest="1" * 64,
            benchmark_digest="2" * 64,
            subject_digest="3" * 64,
            backend_digest="backend",
            version="KEY=secret",
        )


def test_subprocess_environment_does_not_inherit_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BMP_SECRET_TOKEN", "must-not-leak")
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[1]
    backend = SubprocessBackend(
        tmp_path / "records", workspace_root=tmp_path / "workspaces",
        allow_test_override=True,
    )

    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "import os; print(os.environ.get('BMP_SECRET_TOKEN', 'BMP_OK'))",
        ),
    )

    assert result.bundle.status == RunStatus.pass_
    assert result.bundle.verifier_evidence is not None
    assert result.bundle.verifier_evidence.details["actual"] == "BMP_OK"


def test_subprocess_scheduler_uses_distinct_attempt_namespaces(tmp_path: Path) -> None:
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]
    protocol = run.manifest.execution.protocol.model_copy(
        update={
            "rollouts_per_case": 2,
            "parallelism": 2,
            "candidate_selection": "best_of_n",
            "checkpoint_policy": "disabled",
        }
    )
    execution = run.manifest.execution.model_copy(update={"protocol": protocol})
    manifest = run.manifest.model_copy(update={"execution": execution})
    canonical = canonical_manifest_json(manifest)
    run = replace(run, manifest=manifest)
    backend = SubprocessBackend(
        tmp_path / "records", workspace_root=tmp_path / "work"
    )
    task = backend._load_task(run)

    def attempt_runner(attempt):
        return backend.execute(
            run,
            task,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
            attempt_budget=attempt.allocation,
            remaining_wall_seconds=attempt.remaining_wall_seconds,
        )

    result = Scheduler(record_root=tmp_path / "records").execute(
        run,
        [task],
        attempt_runner=attempt_runner,
        reset_state=backend.reset_state,
        receipt_path=tmp_path / "schedule_activation_receipt.json",
    )
    assert result.receipt.observed_attempt_count == 2
    assert 1 <= result.receipt.observed_max_concurrency <= 2
    assert len({item.attempt_id for item in result.receipt.attempts}) == 2
    assert all(item.evidence_bundle_ref.path for item in result.receipt.attempts)
    assert len(list((tmp_path / "records").rglob("evidence_bundle.json"))) == 2
