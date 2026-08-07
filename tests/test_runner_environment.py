from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from MagentaBench.schemas import EnvironmentSpec, ProvenanceRecord

from MagentaBench.runner.env.manager import (
    EnvManager,
    EnvManagerError,
    EnvironmentBuildError,
    EnvironmentDriftError,
)


ROOT = Path(__file__).parents[1]


def test_python_version_is_required_and_explicit() -> None:
    with pytest.raises(Exception):
        EnvironmentSpec(id="missing-python")
    with pytest.raises(Exception):
        EnvironmentSpec(id="implicit-python", python_version="latest")


def test_provenance_does_not_serialize_api_key_values() -> None:
    valid = {
        "manifest_digest": "0" * 64,
        "runner_digest": "1" * 64,
        "benchmark_digest": "2" * 64,
        "subject_digest": "3" * 64,
        "backend_digest": "backend",
        "environment": {"OPENAI_API_KEY": "sk-secret"},
    }
    with pytest.raises(Exception):
        ProvenanceRecord.model_validate(valid)


def test_environment_manager_streams_partial_output_and_surfaces_timeout(tmp_path: Path) -> None:
    manager = EnvManager(tmp_path / "envs", uv_executable="/bin/echo")
    with pytest.raises(EnvironmentBuildError) as caught:
        manager._run_streaming(
            (
                "/usr/bin/python3.11",
                "-c",
                "import time; print('partial-marker', flush=True); time.sleep(1)",
            ),
            timeout=0.02,
            cwd=tmp_path,
        )
    error = caught.value
    assert error.timeout_seconds == 0.02
    assert "partial-marker" in error.partial_output


def test_environment_manager_builds_and_reuses_explicit_python(tmp_path: Path) -> None:
    uv = shutil.which("uv") or "/root/.local/bin/uv"
    spec = EnvironmentSpec(
        id="env-build",
        python_version="3.11",
        packages=(),
        build_timeout_seconds=120.0,
    )
    manager = EnvManager(tmp_path / "envs", uv_executable=uv)
    first = manager.ensure(spec)
    second = manager.ensure(spec)

    assert first.spec_id == "env-build"
    assert first.spec_digest == manager.spec_digest(spec)
    assert first.python_version.startswith("3.11.")
    assert Path(first.python_executable).is_file()
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert (manager.environment_directory(spec) / "environment_receipt.json").is_file()


def test_environment_manager_digest_drift_is_strict(tmp_path: Path) -> None:
    spec = EnvironmentSpec(id="env-one", python_version="3.11")
    manager = EnvManager(tmp_path / "envs", uv_executable="/bin/echo")
    with pytest.raises(EnvironmentDriftError, match="digest drift"):
        manager.ensure(spec, expected_digest="0" * 64)


def test_environment_manager_rejects_bad_link_mode(tmp_path: Path) -> None:
    with pytest.raises(EnvManagerError, match="link_mode"):
        EnvManager(tmp_path, link_mode="copy-secrets")
