from __future__ import annotations

import json
from pathlib import Path
import shutil
import stat

from jsonschema import Draft202012Validator
import pytest

from MagentaBench.collab import CollaborationError, ExperimentRepository
from MagentaBench.collab.repository import classify_changed_paths
from MagentaBench.lab import LabStore
from MagentaBench.lab.store import utc_now
from scripts.check_execution_profiles import probe_apptainer


ROOT = Path(__file__).parents[1]


def _profile_repository(tmp_path: Path) -> ExperimentRepository:
    for relative in (
        "execution-profiles",
        "registries/backends",
        "registries/adapters",
        "lab",
    ):
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
    assert modes["apptainer"]["backends"] == []
    assert modes["apptainer"]["configured"] is False
    assert modes["apptainer"]["isolation_boundary"] == "task-container"
    assert modes["apptainer"]["lab_issue"] == "apptainer-backend-adapter"
    assert modes["apptainer"]["lab_status"] not in {None, "done", "cancelled"}
    assert modes["apptainer"]["maximum_evidence_label"] == "exploratory"


def test_backend_readiness_does_not_promote_inactive_registered_adapters() -> None:
    modes = {item["mode"]: item for item in ExperimentRepository(ROOT).execution_modes()}
    local = {item["backend_id"]: item for item in modes["local-process"]["backends"]}
    docker = {item["backend_id"]: item for item in modes["docker"]["backends"]}

    assert local["fake.local"]["configured"] is True
    assert local["subprocess.echo"]["configured"] is True
    assert local["harbor.local-shim"]["configured"] is False
    assert docker["harbor.0.20.0"]["configured"] is True
    assert docker["aose.docker.immutable"]["configured"] is False


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
        (
            "e2b",
            "lab_issue",
            "appcontainer-backend-adapter",
            "linked by multiple modes",
        ),
        (
            "e2b",
            "lab_issue",
            "magenta-single-case-pilot",
            "lacks the execution label",
        ),
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


def test_open_verifier_boundary_requires_a_live_adapter_work_item(
    tmp_path: Path,
) -> None:
    repository = _profile_repository(tmp_path)
    (tmp_path / "registries/backends/e2b-test.toml").write_text(
        "[backend]\n"
        'id = "e2b.test"\n'
        'kind = "remote"\n'
        'adapter = "e2b"\n',
        encoding="utf-8",
    )
    path = tmp_path / "execution-profiles/e2b/profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["registered_backend_ids"] = ["e2b.test"]
    profile["lab_issue"] = None
    path.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(CollaborationError, match="requires a lab_issue"):
        repository.execution_modes()


def test_apptainer_registered_backend_stays_unconfigured_and_exploratory(
    tmp_path: Path,
) -> None:
    repository = _profile_repository(tmp_path)
    (tmp_path / "registries/backends/apptainer-test.toml").write_text(
        "[backend]\n"
        'id = "apptainer.test"\n'
        'kind = "container"\n'
        'adapter = "apptainer"\n',
        encoding="utf-8",
    )
    path = tmp_path / "execution-profiles/apptainer/profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["registered_backend_ids"] = ["apptainer.test"]
    path.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    modes = {item["mode"]: item for item in repository.execution_modes()}
    apptainer = modes["apptainer"]
    assert apptainer["configured"] is False
    assert apptainer["standalone_verifier_boundary_closed"] is False
    assert apptainer["maximum_evidence_label"] == "exploratory"
    assert apptainer["backends"][0]["configured"] is False
    assert apptainer["backends"][0]["standalone_verifier_boundary_closed"] is False


def test_malformed_backend_declaration_fails_strict_parsing(tmp_path: Path) -> None:
    repository = _profile_repository(tmp_path)
    (tmp_path / "registries/backends/bad.toml").write_text(
        "[backend]\n"
        'id = "bad.backend"\n'
        'kind = "container"\n'
        'adapter = "aose-docker"\n'
        'image = "not-an-image-digest"\n'
        'digest = "not-a-digest"\n',
        encoding="utf-8",
    )

    with pytest.raises(CollaborationError, match="invalid backend declaration"):
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

    trailing_newline = dict(profile, lab_issue="e2b-backend-adapter\n")
    assert list(validator.iter_errors(trailing_newline))

    apptainer = json.loads(
        (ROOT / "execution-profiles/apptainer/profile.json").read_text(
            encoding="utf-8"
        )
    )
    missing_probe = dict(apptainer)
    missing_probe.pop("readiness_probe_argv")
    assert list(validator.iter_errors(missing_probe))

    shell_probe = dict(apptainer, readiness_probe_argv=("bash", "-c", "true"))
    assert list(validator.iter_errors(shell_probe))


def test_apptainer_readiness_probe_is_host_only_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "apptainer"
    launcher.write_bytes(b"read-only launcher fixture\n")
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    cache = tmp_path / "cache"
    temporary = tmp_path / "tmp"
    artifacts = tmp_path / "artifacts"
    image = tmp_path / "task.sif"
    for directory in (cache, temporary, artifacts):
        directory.mkdir()
    image.write_bytes(b"image path fixture\n")

    commands: list[tuple[str, ...]] = []

    def fake_run(
        argv: tuple[str, ...], *, timeout: float = 15.0
    ) -> tuple[int, str]:
        commands.append(tuple(argv))
        if argv[-1] == "--version":
            return 0, "apptainer version 1.5.3"
        if argv[-1] == "buildcfg":
            return 0, "PACKAGE_NAME=apptainer\nPACKAGE_VERSION=1.5.3"
        if "--user" in argv:
            return 0, ""
        if argv[0].endswith("findmnt"):
            return 0, "ext4"
        raise AssertionError(f"unexpected probe command: {argv!r}")

    monkeypatch.setattr("scripts.check_execution_profiles._run", fake_run)
    monkeypatch.setattr(
        "scripts.check_execution_profiles.shutil.which",
        lambda name: (
            "/usr/bin/unshare"
            if name == "unshare"
            else "/usr/bin/findmnt" if name == "findmnt" else None
        ),
    )

    report = probe_apptainer(
        launcher_value=str(launcher),
        cache_dir=str(cache),
        tmp_dir=str(temporary),
        artifact_root=str(artifacts),
        image=str(image),
        require_fakeroot=False,
        require_cgroup_v2=False,
        require_gpu=False,
    )

    assert report["launcher"]["installed"] is True
    assert report["launcher"]["identity"]["path"] == str(launcher.resolve())
    assert report["image"] == {
        "configured": True,
        "exists": True,
        "path": str(image.resolve()),
        "kind": "sif-or-file",
        "identity_verified": False,
    }
    assert report["host_ready"] is False
    assert report["required_checks"]["rootless_principal"] is False
    assert "uidmap_helpers" not in report["required_checks"]
    assert "subordinate_ids" not in report["required_checks"]
    assert "cgroup_v2" not in report["required_checks"]
    assert all(
        "exec" not in command and "pull" not in command for command in commands
    )


def test_apptainer_readiness_only_gates_optional_capabilities_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "apptainer"
    launcher.write_bytes(b"read-only launcher fixture\n")
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    for name in ("cache", "tmp", "artifacts"):
        (tmp_path / name).mkdir()

    def fake_run(
        argv: tuple[str, ...], *, timeout: float = 15.0
    ) -> tuple[int, str]:
        if argv[-1] == "--version":
            return 0, "apptainer version 1.5.3"
        if argv[-1] == "buildcfg":
            return 0, "PACKAGE_NAME=apptainer\nPACKAGE_VERSION=1.5.3"
        if "--user" in argv:
            return 0, ""
        if argv[0].endswith("findmnt"):
            return 0, "ext4"
        raise AssertionError(f"unexpected probe command: {argv!r}")

    monkeypatch.setattr("scripts.check_execution_profiles._run", fake_run)
    monkeypatch.setattr("scripts.check_execution_profiles.os.geteuid", lambda: 1000)
    monkeypatch.setattr(
        "scripts.check_execution_profiles.pwd.getpwuid",
        lambda uid: type("Passwd", (), {"pw_name": "fixture-user"})(),
    )
    monkeypatch.setattr(
        "scripts.check_execution_profiles._subordinate_id_entry",
        lambda path, username: False,
    )
    original_exists = Path.exists
    original_stat = Path.stat
    monkeypatch.setattr(
        "scripts.check_execution_profiles.Path.exists",
        lambda path: True if str(path) == "/dev/fuse" else original_exists(path),
    )
    monkeypatch.setattr(
        "scripts.check_execution_profiles.Path.stat",
        lambda path: (
            type("FuseStat", (), {"st_mode": stat.S_IFCHR})()
            if str(path) == "/dev/fuse"
            else original_stat(path)
        ),
    )
    monkeypatch.setattr(
        "scripts.check_execution_profiles.Path.is_file",
        lambda path: (
            False
            if str(path).endswith("cgroup.controllers")
            else True
            if str(path) == "/usr/bin/fusermount3"
            else original_exists(path)
        ),
    )
    monkeypatch.setattr(
        "scripts.check_execution_profiles.os.access", lambda path, mode: True
    )
    monkeypatch.setattr(
        "scripts.check_execution_profiles.shutil.which",
        lambda name: (
            "/usr/bin/unshare"
            if name == "unshare"
            else "/usr/bin/findmnt"
            if name == "findmnt"
            else "/usr/bin/newuidmap"
            if name == "newuidmap"
            else "/usr/bin/newgidmap"
            if name == "newgidmap"
            else "/usr/bin/fusermount3"
            if name == "fusermount3"
            else None
        ),
    )

    common = {
        "launcher_value": str(launcher),
        "cache_dir": str(tmp_path / "cache"),
        "tmp_dir": str(tmp_path / "tmp"),
        "artifact_root": str(tmp_path / "artifacts"),
        "image": None,
        "require_gpu": False,
    }
    baseline = probe_apptainer(
        **common, require_fakeroot=False, require_cgroup_v2=False
    )
    strict = probe_apptainer(
        **common, require_fakeroot=True, require_cgroup_v2=True
    )

    assert baseline["required_checks"] == {
        "installed": True,
        "rootless_principal": True,
        "user_namespace": True,
        "fuse_device": True,
        "fuse_helper": True,
        "persistent_storage": True,
    }, baseline
    assert baseline["host_ready"] is True
    assert baseline["rootless"]["username"] == "fixture-user"
    assert strict["host_ready"] is False
    assert strict["required_checks"]["subordinate_ids"] is False
    assert strict["required_checks"]["cgroup_v2"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("network_policy", "disabled"),
        ("workspace_lifecycle", "ephemeral"),
    ),
)
def test_bundle_placement_must_match_its_execution_profile(
    field: str, value: str
) -> None:
    repository = ExperimentRepository(ROOT)
    path = ROOT / "experiments/terminal-bench-magenta-smoke/bundle.json"
    bundle = repository.load_bundle(path)
    mutated_execution = bundle.execution.model_copy(update={field: value})
    mutated = bundle.model_copy(update={"execution": mutated_execution})
    lab = LabStore(ROOT)
    states = {state.issue.issue_id: state for state in lab.list()}

    with pytest.raises(CollaborationError, match=field):
        repository._validate_bundle(
            path,
            mutated,
            protocols=repository._protocols(),
            backends=repository._backends(),
            lab=lab,
            states=states,
            at=utc_now(),
        )


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
