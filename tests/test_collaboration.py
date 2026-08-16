from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from MagentaBench.collab import (
    BundleExecution,
    ExperimentRepository,
    classify_changed_paths,
)
from MagentaBench.collab.models import BundleEvidence, ExecutionMode, portable_path


ROOT = Path(__file__).parents[1]


def test_checked_in_bundle_is_pinned_to_lab_and_bmp() -> None:
    report = ExperimentRepository(ROOT).validate()
    assert report.ok, report.as_dict()
    bundles = {item.id: item for item in report.bundles}
    assert bundles
    assert list(bundles) == sorted(bundles)
    assert all(item.bmp_spec and item.protocol_id and item.lab_issue for item in bundles.values())

    magenta_smoke = bundles["terminal-bench-magenta-smoke"]
    assert magenta_smoke.lab_issue == "magenta-single-case-pilot"
    assert magenta_smoke.lab_status == "blocked"
    assert not magenta_smoke.available



def test_execution_modes_keep_unregistered_cloud_targets_exploratory() -> None:
    modes = {item["mode"]: item for item in ExperimentRepository(ROOT).execution_modes()}
    assert modes["docker"]["configured"] is True
    assert modes["docker"]["standalone_verifier_boundary_closed"] is True
    assert modes["appcontainer"]["configured"] is False
    assert modes["e2b"]["maximum_evidence_label"] == "exploratory"
    assert not ExperimentRepository._verifier_boundary_closed(ExecutionMode.e2b)


def test_bundle_commands_reject_secret_options_and_opaque_shells() -> None:
    for argv in (
        ("bash", "-c", "echo unsafe"),
        ("/bin/bash", "-ec", "echo unsafe"),
        ("uv", "run", "python", "-c", "print('unsafe')"),
    ):
        with pytest.raises(ValidationError, match="opaque shell or Python"):
            BundleExecution(
                mode=ExecutionMode.docker,
                backend_id="harbor.test",
                isolation_boundary="task-container",
                workspace_lifecycle="persist-on-failure",
                network_policy="benchmark-defined",
                preflight_argv=argv,
                run_argv=(
                    "uv",
                    "run",
                    "bmp-run",
                    "spec.toml",
                    "--record-root",
                    "{record_root}",
                ),
                record_root_template="{artifact_root}/demo/{run_id}",
            )
    with pytest.raises(ValidationError, match="credential-bearing"):
        BundleEvidence(
            classification="exploratory",
            required_files=("record_index.json",),
            verifier_argv=("uv", "run", "bmp-verify-report", "--api-key=secret", "{report}"),
            retention_policy="retain bytes",
        )
    with pytest.raises(ValidationError, match="credential fields"):
        BundleEvidence(
            classification="exploratory",
            required_files=("record_index.json",),
            verifier_argv=(
                "uv",
                "run",
                "bmp-verify-report",
                "https://example.test/report?apikey=secret",
                "{report}",
            ),
            retention_policy="retain bytes",
        )


@pytest.mark.parametrize("value", ("./a", "a//b", "a/./b", "a/../b", "a/"))
def test_collaboration_paths_reject_noncanonical_spellings(value: str) -> None:
    with pytest.raises(ValueError, match="normalized"):
        portable_path(value, label="test path")
    with pytest.raises(ValueError, match="normalized"):
        classify_changed_paths((value,))


def test_change_scope_requires_explicit_protocol_review_and_registry_lock() -> None:
    report = classify_changed_paths(
        ["experiments/new/bundle.json", "MagentaBench/schemas/models.py"]
    )
    assert not report.ok
    assert any(item.code == "protocol-review-required" for item in report.errors)
    allowed = classify_changed_paths(
        ["registries/metrics/new.toml", "registries/registry.lock.toml"],
        allow_protocol_change=True,
    )
    assert allowed.ok
    missing_lock = classify_changed_paths(
        ["registries/metrics/new.toml"], allow_protocol_change=True
    )
    assert any(item.code == "registry-lock-not-updated" for item in missing_lock.errors)


def test_scaffold_is_idempotent_without_mutating_bmp(tmp_path: Path) -> None:
    # Keep this test independent of sibling benchmark checkouts by copying only
    # the declarations and the already-verified lab ledger it needs.
    for relative in (
        "MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml",
        "registries/protocols/terminal-bench-probe.toml",
        "registries/backends/harbor-020-terminal-bench.toml",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    shutil.copytree(ROOT / "lab", tmp_path / "lab")
    repository = ExperimentRepository(tmp_path)
    before = (tmp_path / "MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml").read_bytes()
    path, changed = repository.scaffold(
        experiment_id="terminal-bench-magenta-smoke",
        bmp_spec="MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml",
        lab_issue="magenta-single-case-pilot",
        related_issues=("tb-pinned-images", "tb-container-verifier-uvx", "magenta-activation-usage"),
        question="Does the pinned case run?",
        hypothesis="It can run with retained evidence.",
        stop_conditions=("Stop on infrastructure failure.",),
        required_env=("OPENAI_API_KEY",),
    )
    assert changed and path.is_file()
    after = (tmp_path / "MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml").read_bytes()
    assert before == after
    second, changed_again = repository.scaffold(
        experiment_id="terminal-bench-magenta-smoke",
        bmp_spec="MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml",
        lab_issue="magenta-single-case-pilot",
        related_issues=("tb-pinned-images", "tb-container-verifier-uvx", "magenta-activation-usage"),
        question="Does the pinned case run?",
        hypothesis="It can run with retained evidence.",
        stop_conditions=("Stop on infrastructure failure.",),
        required_env=("OPENAI_API_KEY",),
    )
    assert second == path
    assert not changed_again


def test_bundle_json_has_no_duplicate_keys() -> None:
    path = ROOT / "experiments/terminal-bench-magenta-smoke/bundle.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "magentabench-experiment-bundle-v1"
    assert payload["execution"]["artifact_export_required"] is True
