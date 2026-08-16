from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from typing import Any, Sequence

import pytest

from tools.mirror_acquisition.cli import main
from tools.mirror_acquisition.mirror import (
    CANONICAL_GIT_URL,
    DEFAULT_OCI_MIRROR,
    GIT_MIRROR_PUSH_URL,
    GIT_MIRROR_REFSPEC,
    GIT_MIRROR_URL,
    PYTHON_INDEX_URL,
    AcquisitionError,
    CommandResult,
    DockerClient,
    PinnedExecutable,
    SubprocessRunner,
    _canonical_tag_lock,
    _receipt_path_lock,
    acquire_image,
    acquisition_plan,
    acquisition_ref,
    configure_git_mirror,
    mirror_doctor,
    validate_mirror_registry,
    verify_cached_image,
)
from tools.mirror_acquisition.models import ImageSpecError, load_image_spec


ROOT = Path(__file__).resolve().parents[1]
OCI_SPECS = ROOT / "acquisition" / "oci"
CONFIG_DIGEST = "sha256:" + hashlib.sha256(b"config fixture").hexdigest()
LAYER_DIGEST = "sha256:" + hashlib.sha256(b"compressed layer").hexdigest()
OTHER_LAYER_DIGEST = "sha256:" + hashlib.sha256(b"different layer").hexdigest()
DIFF_ID = "sha256:" + hashlib.sha256(b"uncompressed layer").hexdigest()
MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"


def _manifest_bytes(
    *, layer_digest: str = LAYER_DIGEST, optional_metadata: bool = False
) -> bytes:
    config = {
        "mediaType": CONFIG_MEDIA_TYPE,
        "size": 123,
        "digest": CONFIG_DIGEST,
    }
    layer = {
        "mediaType": LAYER_MEDIA_TYPE,
        "size": 456,
        "digest": layer_digest,
    }
    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE,
        "config": config,
        "layers": [layer],
    }
    if optional_metadata:
        config["annotations"] = {"org.opencontainers.image.title": "fixture"}
        layer["urls"] = ["https://mirror.example.invalid/layer"]
        manifest.update(
            {
                "annotations": {"org.opencontainers.image.ref.name": "v1"},
                "artifactType": "application/vnd.example.fixture",
                "subject": {
                    "mediaType": MEDIA_TYPE,
                    "size": 123,
                    "digest": CONFIG_DIGEST,
                },
            }
        )
    return json.dumps(
        manifest,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _write_spec(
    tmp_path: Path,
    *,
    manifest_layer_digest: str = LAYER_DIGEST,
    spec_layer_digest: str = LAYER_DIGEST,
    optional_manifest_metadata: bool = False,
) -> tuple[Any, bytes]:
    raw = _manifest_bytes(
        layer_digest=manifest_layer_digest,
        optional_metadata=optional_manifest_metadata,
    )
    manifest_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    payload = {
        "format": "magentabench-oci-image-spec-v1",
        "spec_id": "fixture-image-v1",
        "canonical_repository": "docker.io/example/image",
        "canonical_tag": "v1",
        "platform": {"os": "linux", "architecture": "amd64", "variant": None},
        "manifest": {
            "media_type": MEDIA_TYPE,
            "size_bytes": len(raw),
            "digest": manifest_digest,
        },
        "config": {
            "media_type": CONFIG_MEDIA_TYPE,
            "size_bytes": 123,
            "digest": CONFIG_DIGEST,
        },
        "layers": [
            {
                "media_type": LAYER_MEDIA_TYPE,
                "size_bytes": 456,
                "digest": spec_layer_digest,
            }
        ],
        "rootfs_diff_ids": [DIFF_ID],
    }
    path = tmp_path / "fixture-image.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="ascii")
    return load_image_spec(path), raw


def _version_output() -> str:
    return json.dumps(
        {
            "Client": {"Version": "26.1.3", "ApiVersion": "1.45"},
            "Server": {
                "Version": "26.1.3",
                "ApiVersion": "1.45",
                "Os": "linux",
                "Arch": "amd64",
            },
        }
    )


class FakeDockerRunner:
    def __init__(
        self,
        loaded: Any,
        raw_manifest: bytes,
        *,
        source_present: bool = False,
        canonical_present: bool = False,
        canonical_conflict: bool = False,
        source_config_mismatch: bool = False,
        inspect_failure: bool = False,
        descriptor_platform: dict[str, str] | None = None,
        manifest_returncode: int = 0,
    ) -> None:
        self.loaded = loaded
        self.spec = loaded.spec
        self.source_ref = acquisition_ref(self.spec, DEFAULT_OCI_MIRROR)
        self.source_present = source_present
        self.canonical_present = canonical_present
        self.canonical_conflict = canonical_conflict
        self.source_config_mismatch = source_config_mismatch
        self.inspect_failure = inspect_failure
        self.descriptor_platform = descriptor_platform or {
            "architecture": "amd64",
            "os": "linux",
        }
        self.manifest_returncode = manifest_returncode
        self.commands: list[tuple[str, ...]] = []
        self.manifest_output = json.dumps(
            {
                "Ref": self.source_ref,
                "Descriptor": {
                    "mediaType": MEDIA_TYPE,
                    "digest": self.spec.manifest.digest,
                    "size": len(raw_manifest),
                    "platform": self.descriptor_platform,
                },
                "Raw": base64.b64encode(raw_manifest).decode("ascii"),
            }
        )

    def _image(self, *, conflict: bool = False, source: bool = False) -> str:
        image_id = (
            "sha256:" + "f" * 64
            if conflict or (source and self.source_config_mismatch)
            else self.spec.config.digest
        )
        return json.dumps(
            [
                {
                    "Id": image_id,
                    "Os": self.spec.platform.os,
                    "Architecture": self.spec.platform.architecture,
                    "RootFS": {"Layers": list(self.spec.rootfs_diff_ids)},
                    "RepoDigests": [self.source_ref] if source else [],
                }
            ]
        )

    def __call__(self, argv: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        command = tuple(argv)
        self.commands.append(command)
        if command[1:3] == ("version", "--format"):
            return CommandResult(0, _version_output())
        if command[1:4] == ("manifest", "inspect", "--verbose"):
            return CommandResult(self.manifest_returncode, self.manifest_output)
        if command[1:3] == ("image", "inspect"):
            reference = command[3]
            if self.inspect_failure:
                return CommandResult(1, "[]\n", "permission denied\n")
            if reference == self.source_ref and self.source_present:
                return CommandResult(0, self._image(source=True))
            if reference == self.spec.canonical_tag_ref and self.canonical_present:
                return CommandResult(0, self._image(conflict=self.canonical_conflict))
            return CommandResult(
                1,
                "[]\n",
                f"Error response from daemon: No such image: {reference}\n",
            )
        if command[1:3] == ("image", "pull"):
            self.source_present = True
            return CommandResult(0, "pulled")
        if command[1:3] == ("image", "tag"):
            self.canonical_present = True
            return CommandResult(0)
        raise AssertionError(f"unexpected Docker command shape: {command!r}")


class DoctorRunner:
    def __init__(self, values: dict[str, tuple[str, ...]]) -> None:
        self.values = values
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        command = tuple(argv)
        self.commands.append(command)
        if command[1:3] == ("version", "--format"):
            return CommandResult(0, _version_output())
        if "--get-regexp" in command:
            return CommandResult(1)
        if "remote" in command and "get-url" in command:
            remote = command[-1]
            push = "--push" in command
            if remote == "origin":
                values = self.values.get("remote.origin.pushurl", ()) if push else ()
                values = values or self.values.get("remote.origin.url", ())
            else:
                values = self.values.get("remote.mirror.pushurl", ()) if push else ()
                values = values or self.values.get("remote.mirror.url", ())
            return CommandResult(
                0 if values else 2,
                "\n".join(values) + ("\n" if values else ""),
            )
        if "--get-all" in command:
            key = command[-1]
            values = self.values.get(key, ())
            return CommandResult(
                0 if values else 1, "\n".join(values) + ("\n" if values else "")
            )
        raise AssertionError(f"doctor attempted a mutating command: {command!r}")


def _docker(runner: FakeDockerRunner) -> DockerClient:
    return DockerClient(runner, "/usr/bin/docker")


def test_tracked_specs_are_strict_and_preserve_canonical_identity() -> None:
    specs = [load_image_spec(path) for path in sorted(OCI_SPECS.glob("*.json"))]

    assert {item.spec.spec_id for item in specs} == {
        "terminal-bench-headless-terminal-20251031",
        "terminal-bench-regex-log-20251031",
    }
    for loaded in specs:
        plan = acquisition_plan(loaded)
        assert (
            plan["identity"]["manifest_digest"] in plan["transport"]["acquisition_ref"]
        )
        assert plan["identity"]["canonical_digest_ref"].endswith(
            plan["identity"]["manifest_digest"]
        )
        assert plan["transport"]["mirror_is_experiment_identity"] is False


def test_spec_loader_rejects_unknown_duplicate_and_symlink_inputs(
    tmp_path: Path,
) -> None:
    loaded, _ = _write_spec(tmp_path)
    payload = json.loads((tmp_path / "fixture-image.json").read_text(encoding="ascii"))
    payload["unexpected"] = "value"
    (tmp_path / "unknown.json").write_text(json.dumps(payload), encoding="ascii")
    (tmp_path / "duplicate.json").write_text(
        '{"format":"magentabench-oci-image-spec-v1","format":"duplicate"}',
        encoding="ascii",
    )
    (tmp_path / "linked.json").symlink_to(tmp_path / "fixture-image.json")

    assert loaded.spec.spec_id == "fixture-image-v1"
    for name in ("unknown.json", "duplicate.json", "linked.json"):
        with pytest.raises(ImageSpecError, match="image spec"):
            load_image_spec(tmp_path / name)


def test_spec_loader_rejects_secret_material_without_echo(tmp_path: Path) -> None:
    _write_spec(tmp_path)
    payload = json.loads((tmp_path / "fixture-image.json").read_text(encoding="ascii"))
    sentinel = "ghp_" + "a" * 24
    payload["spec_id"] = sentinel
    path = tmp_path / "secret.json"
    path.write_text(json.dumps(payload), encoding="ascii")

    with pytest.raises(ImageSpecError) as captured:
        load_image_spec(path)

    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "registry",
    (
        "docker.io",
        "https://mirror.example",
        "user:password@mirror.example",
        "mirror.example/repository",
        "MIRROR.EXAMPLE",
        "mirror.example:70000",
        "mirror.example\ninvalid",
    ),
)
def test_registry_validation_rejects_identity_ambiguity_and_credentials(
    registry: str,
) -> None:
    with pytest.raises(AcquisitionError) as captured:
        validate_mirror_registry(registry)
    assert registry not in str(captured.value)


def test_doctor_is_secret_safe_sorted_and_read_only(tmp_path: Path) -> None:
    sentinel = "secret-sentinel-value"
    runner = DoctorRunner(
        {
            "remote.origin.url": (CANONICAL_GIT_URL,),
            "remote.origin.pushurl": (),
            "remote.mirror.url": (GIT_MIRROR_URL,),
            "remote.mirror.pushurl": (GIT_MIRROR_PUSH_URL,),
            "remote.mirror.fetch": (GIT_MIRROR_REFSPEC,),
        }
    )
    report = mirror_doctor(
        tmp_path,
        environment={"UV_DEFAULT_INDEX": PYTHON_INDEX_URL, "SECRET": sentinel},
        git_runner=runner,
        docker_runner=runner,
        git_executable="/usr/bin/git",
        docker_executable="/usr/bin/docker",
    )

    assert report["ok"] is True
    assert [item["id"] for item in report["checks"]] == sorted(
        item["id"] for item in report["checks"]
    )
    assert sentinel not in json.dumps(report)
    assert not any(
        token in command
        for command in runner.commands
        for token in ("fetch", "push", "pull", "tag", "run")
    )


def test_doctor_does_not_echo_mismatched_observed_values(tmp_path: Path) -> None:
    sentinel = "secret-sentinel-value"
    runner = DoctorRunner(
        {
            "remote.origin.url": (f"https://user:{sentinel}@example.invalid/repo",),
            "remote.mirror.url": (f"https://{sentinel}.invalid/repo",),
        }
    )
    report = mirror_doctor(
        tmp_path,
        environment={"UV_DEFAULT_INDEX": sentinel},
        git_runner=runner,
        docker_runner=runner,
        git_executable="/usr/bin/git",
        docker_executable="/usr/bin/docker",
    )

    assert report["ok"] is False
    assert sentinel not in json.dumps(report)


def test_doctor_rejects_untrusted_runtime_text_without_echo(tmp_path: Path) -> None:
    sentinel = "secret-sentinel-value"
    baseline = DoctorRunner(
        {
            "remote.origin.url": (CANONICAL_GIT_URL,),
            "remote.mirror.url": (GIT_MIRROR_URL,),
            "remote.mirror.pushurl": (GIT_MIRROR_PUSH_URL,),
            "remote.mirror.fetch": (GIT_MIRROR_REFSPEC,),
        }
    )

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        if tuple(argv)[1:3] == ("version", "--format"):
            payload = json.loads(_version_output())
            payload["Server"]["Version"] = sentinel
            return CommandResult(0, json.dumps(payload))
        return baseline(argv, timeout)

    report = mirror_doctor(
        tmp_path,
        environment={"UV_DEFAULT_INDEX": PYTHON_INDEX_URL},
        git_runner=runner,
        docker_runner=runner,
        git_executable="/usr/bin/git",
        docker_executable="/usr/bin/docker",
    )

    assert report["ok"] is False
    assert report["docker"] is None
    assert sentinel not in json.dumps(report)


def test_doctor_normalizes_version_qualifiers_without_echo(tmp_path: Path) -> None:
    sentinel = "sk-" + "A" * 24
    git_runner = DoctorRunner(
        {
            "remote.origin.url": (CANONICAL_GIT_URL,),
            "remote.mirror.url": (GIT_MIRROR_URL,),
            "remote.mirror.pushurl": (GIT_MIRROR_PUSH_URL,),
            "remote.mirror.fetch": (GIT_MIRROR_REFSPEC,),
        }
    )

    def docker_runner(argv: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        assert tuple(argv)[1:3] == ("version", "--format")
        payload = json.loads(_version_output())
        payload["Client"]["Version"] = f"26.1.3+{sentinel}"
        payload["Server"]["Version"] = f"26.1.3+{sentinel}"
        return CommandResult(0, json.dumps(payload))

    report = mirror_doctor(
        tmp_path,
        environment={"UV_DEFAULT_INDEX": PYTHON_INDEX_URL},
        git_runner=git_runner,
        docker_runner=docker_runner,
        git_executable="/usr/bin/git",
        docker_executable="/usr/bin/docker",
    )

    assert report["ok"] is True
    assert report["docker"]["client_version"] == "26.1.3"
    assert report["docker"]["server_version"] == "26.1.3"
    assert sentinel not in json.dumps(report)
    assert not any(command[1] == "version" for command in git_runner.commands)


def test_git_configuration_is_fetch_only_and_idempotent(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run((git, "init", "-q", str(tmp_path)), check=True)
    subprocess.run(
        (git, "-C", str(tmp_path), "remote", "add", "origin", CANONICAL_GIT_URL),
        check=True,
    )

    first = configure_git_mirror(tmp_path, git_executable=git)
    second = configure_git_mirror(tmp_path, git_executable=git)

    assert first["changed"] is True
    assert second["changed"] is False
    assert (
        subprocess.run(
            (git, "-C", str(tmp_path), "config", "--get", "remote.origin.url"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == CANONICAL_GIT_URL
    )
    assert (
        subprocess.run(
            (git, "-C", str(tmp_path), "config", "--get", "remote.mirror.url"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == GIT_MIRROR_URL
    )
    assert (
        subprocess.run(
            (git, "-C", str(tmp_path), "config", "--get", "remote.mirror.pushurl"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == GIT_MIRROR_PUSH_URL
    )


def test_git_configuration_refuses_noncanonical_origin_without_writes(
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    assert git is not None
    sentinel = "secret-sentinel-value"
    subprocess.run((git, "init", "-q", str(tmp_path)), check=True)
    subprocess.run(
        (
            git,
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            f"https://user:{sentinel}@example.invalid/repo",
        ),
        check=True,
    )

    with pytest.raises(AcquisitionError) as captured:
        configure_git_mirror(tmp_path, git_executable=git)

    assert captured.value.code == "ORIGIN_NOT_CANONICAL"
    assert sentinel not in str(captured.value)
    missing = subprocess.run(
        (git, "-C", str(tmp_path), "config", "--get", "remote.mirror.url"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1


def test_git_configuration_refuses_local_url_rewrite_rules(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run((git, "init", "-q", str(tmp_path)), check=True)
    subprocess.run(
        (git, "-C", str(tmp_path), "remote", "add", "origin", CANONICAL_GIT_URL),
        check=True,
    )
    subprocess.run(
        (
            git,
            "-C",
            str(tmp_path),
            "config",
            "--local",
            "url.https://redirect.invalid/.insteadOf",
            "https://github.com/",
        ),
        check=True,
    )

    with pytest.raises(AcquisitionError) as captured:
        configure_git_mirror(tmp_path, git_executable=git)

    assert captured.value.code == "ORIGIN_NOT_CANONICAL"
    missing = subprocess.run(
        (git, "-C", str(tmp_path), "config", "--get", "remote.mirror.url"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1


def test_git_configuration_refuses_global_url_rewrite_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    assert git is not None
    repository = tmp_path / "repository"
    git_home = tmp_path / "home"
    git_home.mkdir()
    subprocess.run((git, "init", "-q", str(repository)), check=True)
    subprocess.run(
        (git, "-C", str(repository), "remote", "add", "origin", CANONICAL_GIT_URL),
        check=True,
    )
    subprocess.run(
        (
            git,
            "config",
            "--file",
            str(git_home / ".gitconfig"),
            "url.https://redirect.invalid/.insteadOf",
            "https://github.com/",
        ),
        check=True,
    )
    monkeypatch.setenv("HOME", str(git_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    with pytest.raises(AcquisitionError) as captured:
        configure_git_mirror(repository, git_executable=git)

    assert captured.value.code == "ORIGIN_NOT_CANONICAL"
    missing = subprocess.run(
        (git, "-C", str(repository), "config", "--get", "remote.mirror.url"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1


def test_git_configuration_refuses_system_url_rewrite_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    assert git is not None
    repository = tmp_path / "repository"
    system_config = tmp_path / "system.gitconfig"
    subprocess.run((git, "init", "-q", str(repository)), check=True)
    subprocess.run(
        (git, "-C", str(repository), "remote", "add", "origin", CANONICAL_GIT_URL),
        check=True,
    )
    subprocess.run(
        (
            git,
            "config",
            "--file",
            str(system_config),
            "url.https://redirect.invalid/.insteadOf",
            "https://github.com/",
        ),
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    with pytest.raises(AcquisitionError) as captured:
        configure_git_mirror(repository, git_executable=git)

    assert captured.value.code == "ORIGIN_NOT_CANONICAL"
    missing = subprocess.run(
        (git, "-C", str(repository), "config", "--get", "remote.mirror.url"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1


def test_git_configuration_refuses_command_scope_url_rewrite_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run((git, "init", "-q", str(tmp_path)), check=True)
    subprocess.run(
        (git, "-C", str(tmp_path), "remote", "add", "origin", CANONICAL_GIT_URL),
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://redirect.invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")

    with pytest.raises(AcquisitionError) as captured:
        configure_git_mirror(tmp_path, git_executable=git)

    assert captured.value.code == "ORIGIN_NOT_CANONICAL"


def test_acquire_verifies_before_pull_tag_and_receipt(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)
    receipt = tmp_path / "receipts" / "fixture.json"

    result = acquire_image(
        loaded,
        DEFAULT_OCI_MIRROR,
        receipt,
        _docker(runner),
        now=datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
    )

    payload = json.loads(receipt.read_text(encoding="ascii"))
    assert result["status"] == "verified"
    assert result["receipt_reused"] is False
    assert payload["claim_eligible"] is False
    assert payload["transport"]["pull_action"] == "pulled"
    assert payload["transport"]["tag_action"] == "tagged"
    assert payload["verification"]["manifest"]["raw_sha256"] == (
        loaded.spec.manifest.digest.removeprefix("sha256:")
    )
    actions = [command[1:3] for command in runner.commands]
    assert actions.index(("manifest", "inspect")) < actions.index(("image", "pull"))
    assert actions.index(("image", "pull")) < actions.index(("image", "tag"))
    pull = next(
        command for command in runner.commands if command[1:3] == ("image", "pull")
    )
    assert pull[-1] == acquisition_ref(loaded.spec, DEFAULT_OCI_MIRROR)
    assert "@sha256:" in pull[-1]


def test_acquire_records_one_stable_docker_executable_identity(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)
    executable = tmp_path / "docker"
    executable_bytes = b"fixture docker executable\n"
    executable.write_bytes(executable_bytes)
    executable.chmod(0o755)
    receipt = tmp_path / "receipt.json"

    with PinnedExecutable.open(executable) as pinned:
        docker = DockerClient(
            runner,
            pinned.invocation_path,
            pinned_executable=pinned,
        )
        first = acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, docker)
        second = acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, docker)

    payload = json.loads(receipt.read_text(encoding="ascii"))
    assert first["receipt_reused"] is False
    assert second["receipt_reused"] is True
    assert payload["runtime"]["docker_executable"] == {
        "sha256": hashlib.sha256(executable_bytes).hexdigest(),
        "size_bytes": len(executable_bytes),
    }


def test_invalid_docker_executable_identity_fails_before_docker(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)

    with pytest.raises(AcquisitionError) as captured:
        PinnedExecutable.open(tmp_path)

    assert captured.value.code == "DOCKER_IDENTITY_FAILED"
    assert runner.commands == []


def test_changed_docker_executable_identity_emits_no_receipt(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    base_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    executable = tmp_path / "docker"
    executable.write_bytes(b"initial executable\n")
    executable.chmod(0o755)
    changed = False

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        nonlocal changed
        result = base_runner(argv, timeout)
        if not changed:
            executable.write_bytes(b"changed executable\n")
            executable.chmod(0o755)
            changed = True
        return result

    receipt = tmp_path / "receipt.json"
    with PinnedExecutable.open(executable) as pinned:
        with pytest.raises(AcquisitionError) as captured:
            acquire_image(
                loaded,
                DEFAULT_OCI_MIRROR,
                receipt,
                DockerClient(
                    runner,
                    pinned.invocation_path,
                    pinned_executable=pinned,
                ),
            )

    assert captured.value.code == "DOCKER_IDENTITY_CHANGED"
    assert not receipt.exists()
    assert not any(
        command[1:3] in {("image", "pull"), ("image", "tag")}
        for command in base_runner.commands
    )


def test_atomic_path_replacement_cannot_change_pinned_docker_invocation(
    tmp_path: Path,
) -> None:
    loaded, raw = _write_spec(tmp_path)
    base_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    executable = tmp_path / "docker"
    original = b"original docker executable\n"
    executable.write_bytes(original)
    executable.chmod(0o755)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement docker executable\n")
    replacement.chmod(0o755)
    replaced = False

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        nonlocal replaced
        if not replaced:
            os.replace(replacement, executable)
            replaced = True
        return base_runner(argv, timeout)

    receipt = tmp_path / "receipt.json"
    with PinnedExecutable.open(executable) as pinned:
        result = acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            receipt,
            DockerClient(
                runner,
                pinned.invocation_path,
                pinned_executable=pinned,
            ),
        )

    payload = json.loads(receipt.read_text(encoding="ascii"))
    assert result["status"] == "verified"
    assert payload["runtime"]["docker_executable"] == {
        "sha256": hashlib.sha256(original).hexdigest(),
        "size_bytes": len(original),
    }


def test_manifest_digest_mismatch_emits_no_pull_tag_or_receipt(
    tmp_path: Path,
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw + b" ")
    receipt = tmp_path / "receipt.json"

    with pytest.raises(AcquisitionError, match="manifest digest"):
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert not receipt.exists()
    assert not any(
        command[1:3] in {("image", "pull"), ("image", "tag")}
        for command in runner.commands
    )


def test_valid_oci_optional_metadata_does_not_change_core_descriptor_checks(
    tmp_path: Path,
) -> None:
    loaded, raw = _write_spec(tmp_path, optional_manifest_metadata=True)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)

    result = acquire_image(
        loaded,
        DEFAULT_OCI_MIRROR,
        tmp_path / "receipt.json",
        _docker(runner),
    )

    assert result["status"] == "verified"
    assert not any(
        command[1:3] in {("image", "pull"), ("image", "tag")}
        for command in runner.commands
    )


def test_layer_descriptor_mismatch_emits_no_pull_or_tag(tmp_path: Path) -> None:
    loaded, raw = _write_spec(
        tmp_path,
        manifest_layer_digest=OTHER_LAYER_DIGEST,
        spec_layer_digest=LAYER_DIGEST,
    )
    runner = FakeDockerRunner(loaded, raw)

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            tmp_path / "receipt.json",
            _docker(runner),
        )

    assert captured.value.code == "LAYER_DESCRIPTOR_MISMATCH"
    assert not any(command[1:3] == ("image", "pull") for command in runner.commands)
    assert not any(command[1:3] == ("image", "tag") for command in runner.commands)


def test_platform_mismatch_emits_no_pull_or_tag(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(
        loaded,
        raw,
        descriptor_platform={"architecture": "arm64", "os": "linux"},
    )

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            tmp_path / "receipt.json",
            _docker(runner),
        )

    assert captured.value.code == "MANIFEST_DESCRIPTOR_MISMATCH"
    assert not any(command[1:3] == ("image", "pull") for command in runner.commands)
    assert not any(command[1:3] == ("image", "tag") for command in runner.commands)


def test_local_config_mismatch_after_pull_never_tags_or_receipts(
    tmp_path: Path,
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_config_mismatch=True)
    receipt = tmp_path / "receipt.json"

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "LOCAL_CONFIG_MISMATCH"
    assert not receipt.exists()
    assert not any(command[1:3] == ("image", "tag") for command in runner.commands)


def test_canonical_conflict_fails_before_registry_or_pull(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(
        loaded, raw, canonical_present=True, canonical_conflict=True
    )

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            tmp_path / "receipt.json",
            _docker(runner),
        )

    assert captured.value.code == "CANONICAL_TAG_CONFLICT"
    assert not any(command[1] == "manifest" for command in runner.commands)
    assert not any(command[1:3] == ("image", "pull") for command in runner.commands)
    assert not any(command[1:3] == ("image", "tag") for command in runner.commands)


def test_canonical_tag_lock_contention_fails_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "alternate-temp"))

    with _canonical_tag_lock(loaded.spec):
        with pytest.raises(AcquisitionError) as captured:
            acquire_image(
                loaded,
                DEFAULT_OCI_MIRROR,
                tmp_path / "receipt.json",
                _docker(runner),
            )

    assert captured.value.code == "ACQUISITION_BUSY"
    assert runner.commands == []


def test_host_lock_has_no_replaceable_filesystem_entry(tmp_path: Path) -> None:
    loaded, _ = _write_spec(tmp_path)

    with _canonical_tag_lock(loaded.spec):
        assert list(tmp_path.glob("magentabench-*.lock")) == []

    with _canonical_tag_lock(loaded.spec):
        assert list(tmp_path.glob("magentabench-*.lock")) == []


def test_receipt_path_contention_fails_before_docker(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)
    receipt = tmp_path / "receipt.json"

    with _receipt_path_lock(receipt):
        with pytest.raises(AcquisitionError) as captured:
            acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "RECEIPT_BUSY"
    assert runner.commands == []


def test_receipt_destination_is_prepared_before_docker(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            Path("/proc/magentabench-missing/receipt.json"),
            _docker(runner),
        )

    assert captured.value.code == "RECEIPT_PREPARATION_FAILED"
    assert runner.commands == []


def test_receipt_parent_replacement_is_detected_before_image_mutation(
    tmp_path: Path,
) -> None:
    loaded, raw = _write_spec(tmp_path)
    base_runner = FakeDockerRunner(loaded, raw)
    parent = tmp_path / "receipts"
    moved_parent = tmp_path / "receipts-moved"
    changed = False

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        nonlocal changed
        result = base_runner(argv, timeout)
        if not changed:
            parent.rename(moved_parent)
            parent.mkdir()
            changed = True
        return result

    receipt = parent / "receipt.json"
    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded, DEFAULT_OCI_MIRROR, receipt, DockerClient(runner, "docker")
        )

    assert captured.value.code == "RECEIPT_PATH_CHANGED"
    assert not receipt.exists()
    assert not any(
        command[1:3] in {("image", "pull"), ("image", "tag")}
        for command in base_runner.commands
    )


def test_receipt_writer_enforces_its_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr("tools.mirror_acquisition.mirror._MAX_RECEIPT_BYTES", 128)

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "RECEIPT_TOO_LARGE"
    assert not receipt.exists()
    assert not any(
        command[1:3] in {("image", "pull"), ("image", "tag")}
        for command in runner.commands
    )


def test_directory_fsync_failure_invalidates_linked_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)
    receipt = tmp_path / "receipt.json"
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        real_fsync(descriptor)

    monkeypatch.setattr(
        "tools.mirror_acquisition.mirror.os.fsync", fail_directory_fsync
    )

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "RECEIPT_WRITE_FAILED"
    assert receipt.read_bytes() == b""


def test_receipt_invalidation_corrupts_success_even_when_truncate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)
    receipt = tmp_path / "receipt.json"
    real_fsync = os.fsync
    real_ftruncate = os.ftruncate
    truncate_calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        real_fsync(descriptor)

    def fail_second_truncate(descriptor: int, length: int) -> None:
        nonlocal truncate_calls
        truncate_calls += 1
        if truncate_calls > 1:
            raise OSError
        real_ftruncate(descriptor, length)

    monkeypatch.setattr(
        "tools.mirror_acquisition.mirror.os.fsync", fail_directory_fsync
    )
    monkeypatch.setattr(
        "tools.mirror_acquisition.mirror.os.ftruncate", fail_second_truncate
    )

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "RECEIPT_WRITE_FAILED"
    assert receipt.read_bytes().startswith(b"!")
    with pytest.raises(json.JSONDecodeError):
        json.loads(receipt.read_bytes())


def test_invalid_failed_receipt_is_not_auto_deleted_and_new_path_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)
    receipt = tmp_path / "receipt.json"
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        real_fsync(descriptor)

    with monkeypatch.context() as context:
        context.setattr(
            "tools.mirror_acquisition.mirror.os.fsync", fail_directory_fsync
        )
        with pytest.raises(AcquisitionError) as captured:
            acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "RECEIPT_WRITE_FAILED"
    assert receipt.read_bytes() == b""
    command_count = len(runner.commands)

    with pytest.raises(AcquisitionError) as retry_error:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert retry_error.value.code == "RECEIPT_CONFLICT"
    assert receipt.read_bytes() == b""
    assert len(runner.commands) == command_count

    retry_receipt = tmp_path / "receipt-retry.json"
    result = acquire_image(loaded, DEFAULT_OCI_MIRROR, retry_receipt, _docker(runner))

    assert result["status"] == "verified"
    assert json.loads(retry_receipt.read_text(encoding="ascii"))["status"] == "verified"


def test_invalidation_never_deletes_a_concurrent_receipt_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)
    receipt = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    replacement_bytes = b'{"external":"must-survive"}\n'
    replacement.write_bytes(replacement_bytes)
    real_fsync = os.fsync
    replaced = False

    def replace_then_fail_directory_fsync(descriptor: int) -> None:
        nonlocal replaced
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            if not replaced:
                os.replace(replacement, receipt)
                replaced = True
            raise OSError
        real_fsync(descriptor)

    monkeypatch.setattr(
        "tools.mirror_acquisition.mirror.os.fsync",
        replace_then_fail_directory_fsync,
    )

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "RECEIPT_WRITE_FAILED"
    assert receipt.read_bytes() == replacement_bytes


def test_inspect_failure_is_not_treated_as_image_absence(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, inspect_failure=True)
    receipt = tmp_path / "receipt.json"

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "DOCKER_INSPECT_FAILED"
    assert not receipt.exists()
    assert not any(command[1] == "manifest" for command in runner.commands)
    assert not any(command[1:3] == ("image", "pull") for command in runner.commands)
    assert not any(command[1:3] == ("image", "tag") for command in runner.commands)


def test_manifest_command_failure_emits_no_success_receipt(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, manifest_returncode=1)
    receipt = tmp_path / "receipt.json"

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "DOCKER_MANIFEST_FAILED"
    assert not receipt.exists()
    assert not any(command[1:3] == ("image", "tag") for command in runner.commands)


def test_cached_acquisition_and_receipt_retry_are_idempotent(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)
    receipt = tmp_path / "receipt.json"
    first = acquire_image(
        loaded,
        DEFAULT_OCI_MIRROR,
        receipt,
        _docker(runner),
        now=datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
    )
    first_bytes = receipt.read_bytes()
    command_count = len(runner.commands)

    second = acquire_image(
        loaded,
        DEFAULT_OCI_MIRROR,
        receipt,
        _docker(runner),
        now=datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc),
    )

    assert first["receipt_reused"] is False
    assert second["receipt_reused"] is True
    assert receipt.read_bytes() == first_bytes
    first_commands = runner.commands[:command_count]
    retry_commands = runner.commands[command_count:]
    assert not any(command[1:3] == ("image", "pull") for command in first_commands)
    assert not any(command[1:3] == ("image", "tag") for command in first_commands)
    assert all(command[1:3] == ("image", "inspect") for command in retry_commands)


def test_receipt_reuse_detects_parent_replacement(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    receipt = tmp_path / "receipts" / "receipt.json"
    initial_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(initial_runner))
    moved_parent = tmp_path / "receipts-moved"
    base_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    changed = False

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        nonlocal changed
        result = base_runner(argv, timeout)
        if not changed:
            receipt.parent.rename(moved_parent)
            receipt.parent.mkdir()
            changed = True
        return result

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            receipt,
            DockerClient(runner, "docker"),
        )

    assert captured.value.code == "RECEIPT_PATH_CHANGED"
    assert not receipt.exists()
    assert (moved_parent / receipt.name).exists()


def test_receipt_reuse_detects_atomic_file_replacement(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    receipt = tmp_path / "receipt.json"
    initial_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(initial_runner))
    base_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"tampered":true}\n', encoding="ascii")
    changed = False

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        nonlocal changed
        result = base_runner(argv, timeout)
        if not changed:
            os.replace(replacement, receipt)
            changed = True
        return result

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            receipt,
            DockerClient(runner, "docker"),
        )

    assert captured.value.code == "RECEIPT_PATH_CHANGED"
    assert receipt.read_text(encoding="ascii") == '{"tampered":true}\n'


def test_receipt_reuse_detects_in_place_rewrite(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    receipt = tmp_path / "receipt.json"
    initial_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(initial_runner))
    base_runner = FakeDockerRunner(
        loaded, raw, source_present=True, canonical_present=True
    )
    changed = False

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        nonlocal changed
        result = base_runner(argv, timeout)
        if not changed:
            receipt.write_text("tampered\n", encoding="ascii")
            changed = True
        return result

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            receipt,
            DockerClient(runner, "docker"),
        )

    assert captured.value.code == "RECEIPT_PATH_CHANGED"
    assert receipt.read_text(encoding="ascii") == "tampered\n"


def test_verify_cached_is_read_only_and_reports_layer_limit(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw, source_present=True, canonical_present=True)

    report = verify_cached_image(loaded, DEFAULT_OCI_MIRROR, _docker(runner))

    assert report["verified"] is True
    assert report["claim_eligible"] is False
    assert report["verification"]["compressed_layer_descriptors_verified"] is False
    assert all(command[1:3] == ("image", "inspect") for command in runner.commands)


def test_unsafe_or_conflicting_receipt_is_rejected_before_docker(
    tmp_path: Path,
) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="ascii")
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, linked, _docker(runner))

    assert captured.value.code == "UNSAFE_RECEIPT_PATH"
    assert target.read_text(encoding="ascii") == "{}\n"
    assert runner.commands == []


def test_incomplete_existing_receipt_is_not_reused(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n", encoding="ascii")

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(loaded, DEFAULT_OCI_MIRROR, receipt, _docker(runner))

    assert captured.value.code == "RECEIPT_CONFLICT"
    assert receipt.read_text(encoding="ascii") == "{}\n"
    assert runner.commands == []


def test_invalid_receipt_timestamp_fails_before_docker_mutation(tmp_path: Path) -> None:
    loaded, raw = _write_spec(tmp_path)
    runner = FakeDockerRunner(loaded, raw)

    with pytest.raises(AcquisitionError) as captured:
        acquire_image(
            loaded,
            DEFAULT_OCI_MIRROR,
            tmp_path / "receipt.json",
            _docker(runner),
            now=datetime(2026, 8, 14, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        )

    assert captured.value.code == "INVALID_TIMESTAMP"
    assert runner.commands == []


def test_cli_rejects_secret_bearing_registry_without_echo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded, _ = _write_spec(tmp_path)
    sentinel = "secret-sentinel-value"

    returncode = main(
        (
            "plan",
            str(tmp_path / "fixture-image.json"),
            "--mirror-registry",
            f"https://user:{sentinel}@mirror.example",
        )
    )
    output = capsys.readouterr().out

    assert loaded.spec.spec_id == "fixture-image-v1"
    assert returncode == 2
    assert "INVALID_MIRROR_REGISTRY" in output
    assert sentinel not in output


def test_cli_argument_errors_do_not_echo_untrusted_argv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "secret-sentinel-value"

    returncode = main(("plan", "--unknown-option", sentinel))
    captured = capsys.readouterr()

    assert returncode == 2
    assert "INVALID_ARGUMENTS" in captured.out
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_subprocess_runner_drops_ambient_secret_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "secret-sentinel-value"
    monkeypatch.setenv("MIRROR_SECRET_SENTINEL", sentinel)
    observed_environment: dict[str, str] = {}

    def fake_popen(*args: Any, **kwargs: Any) -> Any:
        del args
        observed_environment.update(kwargs["env"])
        raise OSError

    monkeypatch.setattr("tools.mirror_acquisition.mirror.subprocess.Popen", fake_popen)
    runner = SubprocessRunner(docker_config=tmp_path)

    assert runner(("/usr/bin/true",), 1.0).returncode == 127
    assert "MIRROR_SECRET_SENTINEL" not in observed_environment
    assert sentinel not in json.dumps(observed_environment)


def test_subprocess_runner_stops_at_bounded_combined_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = shutil.which("printf")
    assert executable is not None
    monkeypatch.setattr("tools.mirror_acquisition.mirror._MAX_COMMAND_OUTPUT", 32)

    result = SubprocessRunner()((executable, "%s", "x" * 64), 2.0)

    assert result.returncode == 125
    assert result.stdout == ""
    assert result.stderr == ""


def test_subprocess_runner_executes_the_opened_inode_after_path_replacement(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    executable.chmod(0o755)
    replacement = tmp_path / "replacement"
    replacement.write_text("#!/bin/sh\nexit 9\n", encoding="ascii")
    replacement.chmod(0o755)

    with PinnedExecutable.open(executable) as pinned:
        os.replace(replacement, executable)
        result = SubprocessRunner(pass_fds=(pinned.descriptor,))(
            (pinned.invocation_path,), 2.0
        )

    assert result.returncode == 0


def test_subprocess_timeout_terminates_the_entire_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = 'sleep 30 & child="$!"; echo "$child" > "$1"; wait "$child"'

    result = SubprocessRunner()(
        ("/bin/sh", "-c", script, "magentabench-timeout", str(pid_file)), 0.2
    )

    assert result.returncode == 124
    child_pid = int(pid_file.read_text(encoding="ascii").strip())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            state = (
                Path(f"/proc/{child_pid}/stat").read_text(encoding="ascii").split()[2]
            )
        except FileNotFoundError:
            break
        if state == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("timed-out subprocess descendant is still running")


def test_subprocess_timeout_still_applies_after_output_pipes_close() -> None:
    started = time.monotonic()

    result = SubprocessRunner()(("/bin/sh", "-c", "exec >/dev/null 2>&1; sleep 5"), 0.2)

    assert result.returncode == 124
    assert time.monotonic() - started < 2.0
