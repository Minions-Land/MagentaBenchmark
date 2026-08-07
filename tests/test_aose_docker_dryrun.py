from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from MagentaBench.adapters.benchmarks.aosebench import load_task
from MagentaBench.runner.backend.aose_docker import AoseDockerBackend
from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas import RunStatus

ROOT = Path(__file__).parents[1]
IMAGE = "sha256:8b54d62b3ff7fb4521b4291d5c0622d54c25333786996e8d807c66d2d748c222"
AOSE = Path("/mnt/aliyunsb/BioAgent/AOSEBench")
DATA = Path("/mnt/aliyunsb/BioAgent/BiomniBench-DA/Data")


def _receipt(execution) -> dict[str, object]:
    path = next(
        Path(ref.path)
        for ref in execution.case.bundle.log_refs
        if Path(ref.path).name == "container_receipt.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_real_zero_cost_aose_docker_contract(tmp_path: Path) -> None:
    if not (AOSE.is_dir() and (DATA / "da-1-3/environment/data").is_dir()):
        pytest.skip("local AOSEBench task data is not installed")
    inspected = subprocess.run(
        ["/usr/bin/docker", "image", "inspect", IMAGE],
        capture_output=True,
        check=False,
    )
    if inspected.returncode != 0:
        pytest.skip("pinned BiomniBench-DA image is not cached")

    compiler = Compiler(ROOT, allow_test_override=True)
    experiments = ROOT / "MagentaBench/conformance/experiments"
    run_a = compiler.compile(experiments / "aose-zero-cost-run-a.toml")[0]
    run_b = compiler.compile(experiments / "aose-zero-cost-run-b.toml")[0]
    task = load_task(AOSE, "da-1-3", data_root=DATA)
    backend = AoseDockerBackend(
        tmp_path / "records", workspace_root=tmp_path / "workspaces"
    )

    a = backend.execute(run_a, task)
    b = backend.execute(run_b, task)
    receipt_a = _receipt(a)
    receipt_b = _receipt(b)

    assert a.case.bundle.provenance.image_digest == IMAGE
    assert receipt_a["image_id"] == IMAGE
    assert receipt_a["instruction_sha256"] == (
        "1f3d6be0015cb118289667d3c8fcd44b825e4d0fe19b4bfcaacda7bf918e61fa"
    )
    assert receipt_a["instruction_probe_returncode"] == 0
    assert receipt_a["data_readonly_probe_returncode"] != 0
    assert a.case.bundle.status == RunStatus.no_output
    assert not a.case.bundle.output_refs
    assert receipt_a["judge_invocations"] == 0
    assert receipt_a["provider_environment_names"] == []
    assert receipt_a["network_mode"] == "none"
    assert {Path(ref.path).name for ref in a.case.bundle.log_refs} == {
        "container.stdout.log",
        "container.stderr.log",
        "container_receipt.json",
        "status.json",
    }
    assert receipt_a["docker_argv"][0] == "/usr/bin/docker"
    assert receipt_a["agent_executable_sha256"] == (
        "4b5a5694e3c0e8b1d58fc52ac6ef076e55e72c2f53195243ac86d5ff517cc2f6"
    )
    assert a.case.bundle.provenance.executable_digest == receipt_a["agent_executable_sha256"]

    assert b.case.bundle.status == RunStatus.unsupported
    assert run_b.manifest.benchmark.authoritative_reward_metric == "overall"
    assert receipt_b["judge_invocations"] == 0
    assert len(b.case.bundle.output_refs) == 2
    assert {Path(ref.path).name for ref in b.case.bundle.output_refs} == {
        "trace.md",
        "answer.txt",
    }
    assert all(len(ref.sha256) == 64 for ref in b.case.bundle.output_refs)
    assert receipt_b["agent_executable_sha256"] == (
        "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
    )
    assert b.case.bundle.provenance.executable_digest == receipt_b["agent_executable_sha256"]
    assert a.workspace_kept and a.workspace.is_dir()
    assert not b.workspace_kept and not b.workspace.exists()
    assert receipt_a["container_removed"] is True
    assert receipt_b["container_removed"] is True
