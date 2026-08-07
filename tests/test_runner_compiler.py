from __future__ import annotations

import json
from pathlib import Path

import pytest

from MagentaBench.runner.compiler import (
    CompilationError,
    Compiler,
    IsolationViolation,
    canonical_manifest_json,
    enforce_allowed_diff,
)


ROOT = Path(__file__).parents[1]
EXPERIMENTS = ROOT / "MagentaBench" / "conformance" / "experiments"


def test_same_toml_compiles_to_byte_identical_manifests_and_digests() -> None:
    compiler = Compiler(ROOT)
    first = compiler.compile(EXPERIMENTS / "fake-sweep.toml")
    second = compiler.compile(EXPERIMENTS / "fake-sweep.toml")

    assert len(first) == 8
    assert [run.canonical_json for run in first] == [run.canonical_json for run in second]
    assert [run.manifest_digest for run in first] == [
        run.manifest_digest for run in second
    ]
    assert len({run.manifest_digest for run in first}) == 8
    assert all(
        run.manifest_digest == run.manifest.canonical_digest() for run in first
    )
    assert [run.factor_values for run in first] == [
        {"execution.seed": seed, "repetition": repetition, "subject": subject}
        for seed in (11, 29)
        for repetition in (0, 1)
        for subject in ("fake.control", "fake.treatment")
    ]
    assert [run.manifest.metadata.run_id for run in first] == [
        f"fake-conformance-sweep__run{index:04d}" for index in range(8)
    ]


def test_manifest_identity_excludes_only_schema_declared_observation_fields() -> None:
    run = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")[0]
    observed = run.manifest.model_copy(
        update={
            "created_at": "2099-01-01T00:00:00Z",
            "wall_clock_start": "2099-01-01T00:00:01Z",
            "wall_clock_end": "2099-01-01T00:00:02Z",
            "record_root": "/different/root",
            "resume_count": 99,
            "runner_invocation_id": "different-invocation",
        }
    )
    assert canonical_manifest_json(run.manifest) == canonical_manifest_json(observed)


def test_allowed_diff_accepts_declared_subject_intervention() -> None:
    runs = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")
    pair = [run for run in runs if run.factor_values["execution.seed"] == 11 and run.factor_values["repetition"] == 0]
    control = next(run for run in pair if run.manifest.subject.id == "fake.control")
    treatment = next(run for run in pair if run.manifest.subject.id == "fake.treatment")

    allowed = (
        "subject.artifact_digest",
        "subject.fixed_answer",
        "subject.id",
    )
    paths = enforce_allowed_diff(control.manifest, treatment.manifest, allowed)
    assert paths == allowed


def test_forbidden_diff_is_rejected_and_audited_before_execution(tmp_path: Path) -> None:
    compiler = Compiler(ROOT)
    with pytest.raises(IsolationViolation) as caught:
        compiler.compile(
            EXPERIMENTS / "fake-isolation-violation.toml",
            record_root=tmp_path,
        )

    assert "subject.id" in caught.value.forbidden_paths
    audits = list(
        (tmp_path / "fake-isolation-violation").glob(
            "REJECTED_*/isolation_violation.json"
        )
    )
    assert len(audits) == 1
    report = json.loads(audits[0].read_text(encoding="utf-8"))
    assert report["claim_eligible"] is False
    assert report["gates"]["isolation_valid"]["valid"] is False
    assert "subject.id" in report["gates"]["isolation_valid"]["reason"]
    assert not list(tmp_path.rglob("evidence_bundle.json"))


def test_deterministic_conformance_protocol_rejects_non_fake_subject() -> None:
    with pytest.raises(CompilationError, match="only for fake subjects"):
        Compiler(ROOT).compile(EXPERIMENTS / "fake-protocol-real-subject.toml")
