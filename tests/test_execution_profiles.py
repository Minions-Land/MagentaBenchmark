from __future__ import annotations

import json
from pathlib import Path
import shutil

from jsonschema import Draft202012Validator
import pytest

from MagentaBench.collab import CollaborationError, ExperimentRepository
from MagentaBench.collab.repository import classify_changed_paths


ROOT = Path(__file__).parents[1]


def _profile_repository(tmp_path: Path) -> ExperimentRepository:
    for relative in ("execution-profiles", "registries/backends", "lab"):
        shutil.copytree(ROOT / relative, tmp_path / relative)
    return ExperimentRepository(tmp_path)


def test_execution_modes_join_profiles_backends_and_lab_work() -> None:
    modes = {
        item["mode"]: item for item in ExperimentRepository(ROOT).execution_modes()
    }

    assert modes["local-process"]["isolation_boundary"] == "process"
    assert [item["backend_id"] for item in modes["local-process"]["backends"]] == [
        "fake.local",
        "harbor.local-shim",
        "subprocess.echo",
    ]
    assert "harbor.local-shim" not in {
        item["backend_id"] for item in modes["docker"]["backends"]
    }
    assert modes["e2b"]["lab_issue"] == "e2b-backend-adapter"
    assert modes["e2b"]["lab_status"] not in {None, "done", "cancelled"}
    assert modes["e2b"]["maximum_evidence_label"] == "exploratory"


@pytest.mark.parametrize(
    ("mode", "field", "value", "message"),
    (
        (
            "docker",
            "registered_backend_ids",
            ["aose.docker.immutable"],
            "backend ids drift",
        ),
        ("e2b", "isolation_boundary", "process", "isolation boundary drift"),
        ("e2b", "lab_issue", "missing-adapter-work", "missing lab issue"),
    ),
)
def test_execution_modes_reject_cross_repository_profile_drift(
    tmp_path: Path,
    mode: str,
    field: str,
    value: object,
    message: str,
) -> None:
    repository = _profile_repository(tmp_path)
    path = tmp_path / "execution-profiles" / mode / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(CollaborationError, match=message):
        repository.execution_modes()


def test_execution_profile_schema_rejects_unrecoverable_or_unsafe_metadata() -> None:
    schema = json.loads(
        (ROOT / "execution-profiles/schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    profile = json.loads(
        (ROOT / "execution-profiles/e2b/profile.json").read_text(encoding="utf-8")
    )

    missing_lifecycle = dict(profile)
    missing_lifecycle.pop("workspace_lifecycle")
    assert list(validator.iter_errors(missing_lifecycle))

    invalid_ceiling = dict(profile, evidence_ceiling="bmp-gated")
    assert list(validator.iter_errors(invalid_ceiling))

    unsafe_evidence = dict(
        profile,
        required_identity_evidence=("template identity\ncredential text",),
    )
    assert list(validator.iter_errors(unsafe_evidence))


def test_execution_profile_changes_have_an_explicit_non_protocol_class() -> None:
    report = classify_changed_paths(("execution-profiles/e2b/profile.json",))

    assert report.ok
    assert report.classes == {
        "execution-target": ("execution-profiles/e2b/profile.json",)
    }


def test_collaboration_validation_surfaces_execution_profile_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ExperimentRepository(ROOT)

    def fail() -> tuple[dict[str, object], ...]:
        raise CollaborationError("synthetic execution profile drift")

    monkeypatch.setattr(repository, "execution_modes", fail)
    report = repository.validate()

    assert any(item.code == "execution-profiles" for item in report.errors)
