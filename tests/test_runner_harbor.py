from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from MagentaBench.runner.backend.harbor import (
    HarborBackend,
    HarborConfigurationError,
    parse_harbor_result,
    build_job_config,
    harbor_agent_name,
    render_job_yaml,
)
from MagentaBench.runner.compiler import (
    CompiledRun,
    Compiler,
    canonical_manifest_json,
    sha256_bytes,
)
from MagentaBench.runner.evidence import sha256_file
from MagentaBench.schemas import RunStatus


ROOT = Path(__file__).parents[1]
EXPERIMENT = (
    ROOT / "MagentaBench" / "conformance" / "experiments" / "harbor-shim-smoke.toml"
)


def _shim(path: Path, *, fail: bool = False) -> Path:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('harbor 0.20.0')\n"
        "    raise SystemExit(0)\n"
        "if '--print-config' in args:\n"
        "    print('{}')\n"
        "    raise SystemExit(0)\n"
        + ("raise SystemExit(7)\n" if fail else "")
        + "jobs = pathlib.Path(args[args.index('--jobs-dir') + 1])\n"
        "jobs.mkdir(parents=True, exist_ok=True)\n"
        "trial = jobs / 'trial-0000'\n"
        "trial.mkdir(parents=True, exist_ok=True)\n"
        "(trial / 'answer.txt').write_text('BMP_OK', encoding='utf-8')\n"
        "native_trial = {'trial_name': 'trial-0000', 'verifier_result': {'rewards': {'score': 1.0}}}\n"
        "(trial / 'result.json').write_text('{}', encoding='utf-8')\n"
        "(jobs / 'result.json').write_text(json.dumps({'trial_results': [native_trial]}), encoding='utf-8')\n", 
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _shim_run(run: CompiledRun, shim: Path) -> CompiledRun:
    backend = run.manifest.execution.backend.model_copy(
        update={
            "executable": str(shim),
            "digest": sha256_file(shim),
            "version": "0.20.0",
        }
    )
    execution = run.manifest.execution.model_copy(update={"backend": backend})
    manifest = run.manifest.model_copy(update={"execution": execution})
    canonical = canonical_manifest_json(manifest)
    return replace(run, manifest=manifest)


def test_job_config_mapping_and_yaml_are_deterministic() -> None:
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]
    config = build_job_config(run, agent_name="echo", dataset_name="fake-task")
    assert config["agents"][0]["name"] == "echo"
    assert config["agents"][0]["model_name"] == "none/echo"
    assert config["datasets"][0] == {
        "name": "fake-task",
        "ref": run.manifest.benchmark.artifact_digest,
    }
    assert config["agent_timeout_multiplier"] == 10.0 / 3600.0
    assert render_job_yaml(config) == render_job_yaml(config)
    with pytest.raises(HarborConfigurationError, match="no Harbor"):
        harbor_agent_name(run.manifest.subject)


def test_magenta_subject_is_not_silently_substituted_by_pi() -> None:
    with pytest.raises(HarborConfigurationError, match="no Harbor"):
        harbor_agent_name(SimpleNamespace(adapter="magenta", kind="opaque_agent"))


def test_real_harbor_rejects_runtime_agent_and_dataset_overrides() -> None:
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]
    backend = run.manifest.execution.backend.model_copy(
        update={"adapter": "harbor", "defaults": {}}
    )
    execution = run.manifest.execution.model_copy(update={"backend": backend})
    real_run = run.manifest.model_copy(update={"execution": execution})
    real_run = replace(run, manifest=real_run)
    with pytest.raises(HarborConfigurationError, match="solely"):
        build_job_config(real_run, agent_name="pi")
    with pytest.raises(HarborConfigurationError, match="solely"):
        build_job_config(real_run, dataset_name="other-dataset")


def test_harbor_shim_runs_full_toml_to_evidence_path(tmp_path: Path) -> None:
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]
    shim = _shim(tmp_path / "harbor-shim")
    run = _shim_run(run, shim)
    execution = HarborBackend(
        tmp_path / "records",
        harbor_executable=shim,
        timeout_seconds=10,
        allow_test_shim=True,
    ).run(run, case_id="case-001")

    assert execution.case.bundle.status == RunStatus.pass_
    assert execution.case.bundle.verifier_evidence is not None
    assert execution.case.bundle.verifier_evidence.passed is True
    assert execution.case.bundle.output_refs
    assert Path(execution.config_path).read_text(encoding="utf-8").startswith("{")
    assert Path(execution.case.bundle.output_refs[0].path).read_text() == "BMP_OK"
    assert Path(execution.case.bundle_path).is_file()
    payload = json.loads(execution.case.bundle_path.read_text(encoding="utf-8"))
    assert payload["provenance"]["backend_kind"] == "harbor"
    assert payload["provenance"]["version"] == "0.20.0"
    assert payload["provenance"]["backend_digest"] == sha256_file(shim)


def test_harbor_nonzero_exit_emits_infra_evidence(tmp_path: Path) -> None:
    shim = _shim(tmp_path / "harbor-fail", fail=True)
    run = _shim_run(Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0], shim)
    execution = HarborBackend(
        tmp_path / "records", harbor_executable=shim, allow_test_shim=True
    ).run(run)
    assert execution.case.bundle.status == RunStatus.infra_error
    assert execution.case.bundle.log_refs


def test_harbor_rejects_relative_and_manifest_mismatched_executables(tmp_path: Path) -> None:
    with pytest.raises(HarborConfigurationError, match="absolute"):
        HarborBackend(tmp_path / "records", harbor_executable="harbor")
    shim = _shim(tmp_path / "harbor-shim")
    run = Compiler(ROOT, allow_test_override=True).compile(EXPERIMENT)[0]
    with pytest.raises(HarborConfigurationError, match="test-only"):
        HarborBackend(tmp_path / "records", harbor_executable=shim).run(
            run, agent_name="echo"
        )
    with pytest.raises(HarborConfigurationError, match="does not match"):
        HarborBackend(
            tmp_path / "records", harbor_executable=shim, allow_test_shim=True
        ).run(run, agent_name="echo")
