from __future__ import annotations

import errno
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import stat
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from MagentaBench.runner.backend.apptainer import (
    ApptainerBackend,
    ApptainerBackendFactory,
    ApptainerBind,
    ApptainerConfigurationError,
    ApptainerGpu,
    ApptainerIdentityDriftError,
    ApptainerOverlay,
    ApptainerRuntimeConfig,
    _tree_digest,
)
from MagentaBench.runner.adapter_registry import AdapterRegistry
from MagentaBench.runner.evidence import sha256_file
from MagentaBench.schemas import load_registry_lock, verify_registry_lock


ROOT = Path(__file__).parents[1]


def _fake_launcher(tmp_path: Path) -> tuple[Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "fake-apptainer"
    path.write_text(
        "#!" + sys.executable + "\n"
        "import os\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('Apptainer fake 1.0')\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:] == ['buildcfg']:\n"
        "    print('fake-build-config')\n"
        "    raise SystemExit(0)\n"
        "if not sys.argv or sys.argv[1] != 'exec':\n"
        "    raise SystemExit(2)\n"
        "args = sys.argv[2:]\n"
        "image = next(value for value in args if value.endswith('.sif'))\n"
        "index = args.index(image)\n"
        "command = args[index + 1:]\n"
        "if not command:\n"
        "    raise SystemExit(2)\n"
        "os.execvpe(command[0], command, os.environ)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    image = tmp_path / "image.sif"
    image.write_bytes(b"immutable fixture")
    version = "Apptainer fake 1.0"
    build = "fake-build-config\n"
    return path, image, version, build


def _runtime(
    tmp_path: Path,
    *,
    keep_workspace_on_failure: bool = True,
    forwarded_env_names: tuple[str, ...] = ("BMP_TEST_SECRET",),
):
    launcher, image, version, build = _fake_launcher(tmp_path)
    config = ApptainerRuntimeConfig(
        launcher=launcher,
        launcher_digest=sha256_file(launcher),
        launcher_version=version,
        launcher_build_config_digest=hashlib.sha256(build.encode()).hexdigest(),
        image=image,
        image_kind="sif",
        image_digest=sha256_file(image),
        image_size_bytes=image.stat().st_size,
        binds=(),
        overlay=None,
        network_argv=("--net", "--network", "none"),
        gpu=None,
        fakeroot=True,
        forwarded_env_names=forwarded_env_names,
        keep_workspace_on_failure=keep_workspace_on_failure,
        max_capture_bytes=1024 * 1024,
        max_artifact_bytes=1024 * 1024,
        max_artifact_entries=1024,
        termination_grace_seconds=0.05,
    )
    run = SimpleNamespace(
        manifest=SimpleNamespace(
            metadata=SimpleNamespace(experiment_id="apptainer-test")
        ),
        manifest_digest="a" * 64,
        wire_json=b"{}",
    )
    backend = ApptainerBackend(
        tmp_path / "records",
        workspace_root=tmp_path / "workspaces",
        config=config,
    )
    return backend, run, launcher, image


def _backend_spec(launcher: Path, image: Path) -> SimpleNamespace:
    return SimpleNamespace(
        adapter="apptainer",
        kind="container",
        executable=str(launcher),
        digest=sha256_file(launcher),
        version="Apptainer fake 1.0",
        image=str(image),
        defaults={
            "binds": [],
            "fakeroot": True,
            "forwarded_env_names": [],
            "gpu": False,
            "image_kind": "sif",
            "image_sha256": sha256_file(image),
            "keep_workspace_on_failure": True,
            "launcher_build_config_sha256": hashlib.sha256(
                b"fake-build-config\n"
            ).hexdigest(),
            "max_artifact_bytes": 1024 * 1024,
            "max_artifact_entries": 1024,
            "max_capture_bytes": 1024 * 1024,
            "network_mode": "none",
            "termination_grace_seconds": 0.05,
        },
    )


def test_fake_launcher_argv_is_shell_free_and_exports_before_teardown(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image = _runtime(tmp_path, keep_workspace_on_failure=False)
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('artifact.txt').write_text('ok')",
        ),
        attempt_id="attempt-0001",
        artifact_exports=("artifact.txt",),
    )

    assert result.status == "completed"
    assert result.workspace_retained is False
    assert not result.workspace.exists()
    assert result.argv[0] == str(launcher)
    assert "--network" in result.argv
    assert "none" in result.argv
    assert "--containall" in result.argv
    assert "--cleanenv" in result.argv
    assert not any(
        item in {"sh", "bash", "/bin/sh", "/bin/bash"} for item in result.argv
    )
    assert len(result.artifact_refs) == 1
    assert Path(result.artifact_refs[0].path).read_text(encoding="utf-8") == "ok"
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert receipt["shell"] is False
    assert receipt["artifact_export"]["complete"] is True
    assert receipt["teardown"]["artifact_export_before_destroy"] is True
    assert receipt["identity"]["launcher_sha256"] == sha256_file(launcher)
    assert receipt["identity"]["image_digest"] == sha256_file(image)


def test_launcher_image_and_policy_drift_fail_closed(tmp_path: Path) -> None:
    backend, run, launcher, image = _runtime(tmp_path)
    launcher.write_text(launcher.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ApptainerIdentityDriftError, match="launcher digest"):
        backend.execute(run, command=("true",), attempt_id="drift-launcher")

    backend, run, launcher, image = _runtime(tmp_path / "image-drift")
    image.write_bytes(b"changed")
    with pytest.raises(ApptainerIdentityDriftError, match="image content"):
        backend.execute(run, command=("true",), attempt_id="drift-image")


def test_factory_binds_fake_launcher_and_rejects_policy_drift(tmp_path: Path) -> None:
    _, run, launcher, image = _runtime(tmp_path)
    backend_spec = _backend_spec(launcher, image)
    config = ApptainerRuntimeConfig.from_backend(backend_spec)
    assert config.launcher_digest == sha256_file(launcher)
    factory_run = SimpleNamespace(
        manifest=SimpleNamespace(execution=SimpleNamespace(backend=backend_spec))
    )
    backend = ApptainerBackendFactory().build(
        factory_run,
        record_root=tmp_path / "factory-records",
        workspace_root=tmp_path / "factory-workspaces",
    )
    assert isinstance(backend, ApptainerBackend)

    backend_spec.defaults["network_mode"] = "host"
    with pytest.raises(ApptainerConfigurationError, match="network_mode"):
        ApptainerRuntimeConfig.from_backend(backend_spec)


def test_timeout_escalates_and_retains_workspace_for_recovery(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path)
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(2)",
        ),
        attempt_id="attempt-timeout",
        remaining_wall_seconds=0.2,
    )
    assert result.status == "timeout"
    assert result.workspace_retained is True
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert receipt["lifecycle"]["termination"]["term_sent"] is True
    assert receipt["lifecycle"]["termination"]["kill_sent"] is True
    assert receipt["lifecycle"]["termination"]["returncode"] is not None
    assert backend.verify_receipt(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )["format"] == ("magentabench-apptainer-runtime-receipt-v1")


def test_cancellation_is_idempotent_and_credentials_are_not_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    backend, run, _, _ = _runtime(tmp_path)
    monkeypatch.setenv("BMP_TEST_SECRET", "do-not-record")
    cancelled = threading.Event()

    def cancel() -> None:
        time.sleep(0.05)
        cancelled.set()

    threading.Thread(target=cancel, daemon=True).start()
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "import time; print('do-not-record'); time.sleep(1)",
        ),
        attempt_id="attempt-cancel",
        cancellation_event=cancelled,
    )
    assert result.status == "cancelled"
    assert b"do-not-record" not in result.receipt_path.read_bytes()
    assert (
        b"do-not-record"
        not in Path(result.receipt_path.parent / "stdout.log").read_bytes()
    )
    assert not (result.workspace / "stdout.raw").exists()
    assert not (result.workspace / "stderr.raw").exists()
    assert all(
        b"do-not-record" not in path.read_bytes()
        for path in result.workspace.rglob("*")
        if path.is_file()
    )
    environment, _ = backend._child_environment()
    assert "BMP_TEST_SECRET" not in environment
    assert environment["APPTAINERENV_BMP_TEST_SECRET"] == "do-not-record"


def test_writable_bind_and_overlay_retain_post_run_identity(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    bind_source = tmp_path / "bind.txt"
    bind_source.write_text("before-bind", encoding="utf-8")
    overlay_source = tmp_path / "overlay.img"
    overlay_source.write_text("before-overlay", encoding="utf-8")
    bind = ApptainerBind.from_mapping(
        {
            "source": str(bind_source),
            "destination": "/data/input",
            "read_only": False,
        }
    )
    overlay = ApptainerOverlay.from_mapping(
        {
            "path": str(overlay_source),
            "mode": "rw",
            "sha256": sha256_file(overlay_source),
        }
    )
    assert overlay is not None
    backend.config = replace(backend.config, binds=(bind,), overlay=overlay)
    command = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"Path({str(bind_source)!r}).write_text('after-bind'); "
            f"Path({str(overlay_source)!r}).write_text('after-overlay')"
        ),
    )
    result = backend.execute(run, command=command, attempt_id="attempt-mutable")
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert receipt["identity"]["binds"][0]["post_run_digest"] == sha256_file(
        bind_source
    )
    assert receipt["identity"]["overlay"]["post_run_sha256"] == sha256_file(
        overlay_source
    )
    backend.verify_receipt(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )

    bind_source.write_text("later-drift", encoding="utf-8")
    with pytest.raises(ApptainerIdentityDriftError, match="post-run content drift"):
        backend.verify_receipt(
            result.receipt_path,
            expected_receipt_sha256=result.receipt_sha256,
        )


def test_gpu_identity_and_symlink_drift_fail_closed(tmp_path: Path) -> None:
    backend, run, _, image = _runtime(tmp_path)
    gpu_identity = tmp_path / "gpu-identity.json"
    gpu_identity.write_text('{"driver":"fake","libraries":[]}', encoding="utf-8")
    backend.config = replace(
        backend.config,
        gpu=ApptainerGpu(gpu_identity, sha256_file(gpu_identity)),
    )
    workspace = backend._workspace(run, "gpu-argv")
    workspace.mkdir(parents=True)
    assert "--nv" in backend.build_argv(workspace, ("true",))
    gpu_identity.write_text("drift", encoding="utf-8")
    with pytest.raises(ApptainerIdentityDriftError, match="GPU library digest"):
        backend.config.verify()

    other_image = tmp_path / "other.sif"
    other_image.write_bytes(b"other")
    image.unlink()
    image.symlink_to(other_image)
    with pytest.raises(ApptainerIdentityDriftError, match="symlink"):
        backend.config.verify()


def test_export_and_teardown_retries_are_idempotent(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    result = backend.execute(
        run,
        command=("true",),
        attempt_id="attempt-retry",
        artifact_exports=("late.txt",),
    )
    assert result.status == "export_error"
    assert result.workspace_retained is True
    (result.workspace / "late.txt").write_text("durable", encoding="utf-8")

    first = backend.retry_export(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )
    export_retry_sha256 = sha256_file(
        result.receipt_path.parent / "artifact_export_retry.json"
    )
    second = backend.retry_export(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )
    assert first == second
    assert Path(first[0].path).read_text(encoding="utf-8") == "durable"
    teardown = backend.retry_teardown(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
        expected_export_retry_sha256=export_retry_sha256,
    )
    repeated = backend.retry_teardown(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
        expected_export_retry_sha256=export_retry_sha256,
    )
    assert teardown == repeated
    assert teardown["result"] == "removed"
    assert not result.workspace.exists()


def test_receipt_verification_rejects_exported_byte_drift(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('result.txt').write_text('original')",
        ),
        attempt_id="attempt-ref-drift",
        artifact_exports=("result.txt",),
    )
    exported = Path(result.artifact_refs[0].path)
    exported.write_text("changed", encoding="utf-8")
    with pytest.raises(ApptainerIdentityDriftError, match="artifact ref"):
        backend.verify_receipt(
            result.receipt_path,
            expected_receipt_sha256=result.receipt_sha256,
        )


def test_factory_rejects_unpinned_backend_and_profile_lock_is_synchronized() -> None:
    lock = verify_registry_lock(ROOT / "registries")
    assert isinstance(
        lock, load_registry_lock(ROOT / "registries/registry.lock.toml").__class__
    )
    profile = json.loads(
        (ROOT / "execution-profiles/apptainer/profile.json").read_text(encoding="utf-8")
    )
    assert profile["registered_backend_ids"] == ["apptainer.rootless.exploratory"]
    assert profile["evidence_ceiling"] == "exploratory"


def test_project_registry_loads_the_digest_bound_apptainer_factory() -> None:
    registry = AdapterRegistry.from_project(
        ROOT,
        required_capabilities={("apptainer", "backend_factory")},
    )
    capability = registry.capability("apptainer", "backend_factory")
    assert capability.adapter == "apptainer"
    assert capability.backend_default_read_set is not None


def test_receipt_seal_and_expected_digest_reject_lifecycle_tampering(
    tmp_path: Path,
) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    result = backend.execute(
        run,
        command=(sys.executable, "-c", "raise SystemExit(7)"),
        attempt_id="receipt-tamper",
    )
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    receipt["lifecycle"]["status"] = "completed"
    result.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ApptainerIdentityDriftError, match="seal drift"):
        backend.verify_receipt(
            result.receipt_path,
            expected_receipt_sha256=sha256_file(result.receipt_path),
        )

    receipt.pop("receipt_payload_sha256")
    receipt["lifecycle"].update(
        {
            "process_returncode": 0,
            "process_status": "completed",
            "status": "completed",
        }
    )
    receipt["lifecycle"]["termination"]["returncode"] = 0
    receipt["teardown"].update({"result": "removed", "workspace_retained": False})
    receipt["argv_sha256"] = "b" * 64
    receipt["receipt_payload_sha256"] = hashlib.sha256(
        json.dumps(
            receipt,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ApptainerIdentityDriftError, match="content-address drift"):
        backend.verify_receipt(
            result.receipt_path,
            expected_receipt_sha256=result.receipt_sha256,
        )


def test_policy_digest_binds_recovery_and_resource_limits(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    result = backend.execute(run, command=("true",), attempt_id="policy-receipt")
    original = backend.config
    original_digest = original.policy_digest()
    replacements = (
        {"keep_workspace_on_failure": not original.keep_workspace_on_failure},
        {"max_capture_bytes": original.max_capture_bytes + 1},
        {"max_artifact_bytes": original.max_artifact_bytes + 1},
        {"max_artifact_entries": original.max_artifact_entries + 1},
        {"termination_grace_seconds": original.termination_grace_seconds + 0.01},
    )
    for changes in replacements:
        changed = replace(original, **changes)
        assert changed.policy_digest() != original_digest
        backend.config = changed
        with pytest.raises(ApptainerIdentityDriftError, match="policy drift"):
            backend.verify_receipt(
                result.receipt_path,
                expected_receipt_sha256=result.receipt_sha256,
            )
    backend.config = original


def test_bind_paths_reject_traversal_and_bind_clause_injection(tmp_path: Path) -> None:
    source = tmp_path / "source,extra"
    source.write_text("content", encoding="utf-8")
    with pytest.raises(ApptainerConfigurationError, match="bind separator"):
        ApptainerBind.from_mapping(
            {
                "source": str(source),
                "destination": "/data/input",
                "read_only": True,
            }
        )

    nested = tmp_path / "nested"
    nested.mkdir()
    traversal = f"{nested}/../safe-source"
    with pytest.raises(ApptainerConfigurationError, match="path traversal"):
        ApptainerBind.from_mapping(
            {
                "source": traversal,
                "destination": "/data/input",
                "read_only": True,
            }
        )


@pytest.mark.parametrize(
    "suffix", (":bad", ",bad", "\\bad", "\rbad", "\nbad", "\x00bad")
)
def test_bind_source_rejects_every_apptainer_clause_separator(
    tmp_path: Path, suffix: str
) -> None:
    with pytest.raises(ApptainerConfigurationError, match="bind separator"):
        ApptainerBind.from_mapping(
            {
                "source": str(tmp_path / "source") + suffix,
                "destination": "/data/input",
                "read_only": True,
            }
        )

    safe_source = tmp_path / "safe-source"
    safe_source.write_text("content", encoding="utf-8")
    with pytest.raises(ApptainerConfigurationError, match="normalized path"):
        ApptainerBind.from_mapping(
            {
                "source": str(safe_source),
                "destination": "/data/../workspace",
                "read_only": True,
            }
        )


def test_invalid_export_is_rejected_before_attempt_or_command_creation(
    tmp_path: Path,
) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    marker = tmp_path / "command-ran"
    attempt_id = "invalid-export"
    with pytest.raises(ApptainerConfigurationError, match="normalized relative"):
        backend.execute(
            run,
            command=(
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ),
            attempt_id=attempt_id,
            artifact_exports=("../escape",),
        )
    assert not marker.exists()
    assert not backend._workspace(run, attempt_id).exists()
    assert not backend._case_root(run, attempt_id).exists()


def test_teardown_retry_recovers_after_delete_before_final_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    result = backend.execute(
        run,
        command=("true",),
        attempt_id="teardown-crash",
        artifact_exports=("late.txt",),
    )
    (result.workspace / "late.txt").write_text("durable", encoding="utf-8")
    backend.retry_export(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )
    export_retry_sha256 = sha256_file(
        result.receipt_path.parent / "artifact_export_retry.json"
    )
    original_remove = backend._remove_workspace

    def remove_then_crash(workspace: Path) -> str:
        original_remove(workspace)
        raise RuntimeError("simulated crash after deletion")

    monkeypatch.setattr(backend, "_remove_workspace", remove_then_crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        backend.retry_teardown(
            result.receipt_path,
            expected_receipt_sha256=result.receipt_sha256,
            expected_export_retry_sha256=export_retry_sha256,
        )
    assert not result.workspace.exists()
    assert (result.receipt_path.parent / "teardown_intent.json").is_file()
    assert not (result.receipt_path.parent / "teardown_retry.json").exists()

    monkeypatch.setattr(backend, "_remove_workspace", original_remove)
    completed = backend.retry_teardown(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
        expected_export_retry_sha256=export_retry_sha256,
    )
    assert completed["result"] == "already_absent"


def test_export_retry_allows_only_the_original_forwarded_credential_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BMP_TEST_SECRET", "original-secret")
    backend, run, _, _ = _runtime(tmp_path)
    result = backend.execute(
        run,
        command=("true",),
        attempt_id="credential-retry",
        artifact_exports=("late.txt",),
    )
    (result.workspace / "late.txt").write_text("safe artifact", encoding="utf-8")
    refs = backend.retry_export(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )
    assert len(refs) == 1

    drifted, drifted_run, _, _ = _runtime(tmp_path / "drifted")
    drifted_result = drifted.execute(
        drifted_run,
        command=("true",),
        attempt_id="credential-drift",
        artifact_exports=("late.txt",),
    )
    (drifted_result.workspace / "late.txt").write_text(
        "safe artifact", encoding="utf-8"
    )
    monkeypatch.setenv("BMP_TEST_SECRET", "changed-secret")
    with pytest.raises(ApptainerIdentityDriftError, match="environment value drift"):
        drifted.retry_export(
            drifted_result.receipt_path,
            expected_receipt_sha256=drifted_result.receipt_sha256,
        )


def test_sandbox_tree_digest_binds_modes_and_empty_directories(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    executable = sandbox / "tool"
    executable.write_text("tool", encoding="utf-8")
    executable.chmod(0o755)
    first, _ = _tree_digest(sandbox)
    executable.chmod(0o644)
    second, _ = _tree_digest(sandbox)
    assert second != first
    (sandbox / "empty").mkdir()
    third, _ = _tree_digest(sandbox)
    assert third != second


def test_sandbox_xattr_unsupported_errors_have_one_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "file").write_text("content", encoding="utf-8")

    def unsupported(_path: Path, *, follow_symlinks: bool) -> list[str]:
        assert follow_symlinks is False
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(
        "MagentaBench.runner.backend.apptainer.os.listxattr", unsupported
    )
    enotsup_digest, _ = _tree_digest(sandbox)

    def operation_not_supported(_path: Path, *, follow_symlinks: bool) -> list[str]:
        assert follow_symlinks is False
        raise OSError(errno.EOPNOTSUPP, "operation not supported")

    monkeypatch.setattr(
        "MagentaBench.runner.backend.apptainer.os.listxattr",
        operation_not_supported,
    )
    eopnotsupp_digest, _ = _tree_digest(sandbox)
    assert eopnotsupp_digest == enotsup_digest


def test_symlink_roots_are_rejected_before_resolution(tmp_path: Path) -> None:
    backend, _, _, _ = _runtime(tmp_path / "fixture", forwarded_env_names=())
    real_record_root = tmp_path / "real-records"
    real_workspace_root = tmp_path / "real-workspaces"
    real_record_root.mkdir()
    real_workspace_root.mkdir()
    record_link = tmp_path / "record-link"
    workspace_link = tmp_path / "workspace-link"
    record_link.symlink_to(real_record_root, target_is_directory=True)
    workspace_link.symlink_to(real_workspace_root, target_is_directory=True)

    with pytest.raises(ApptainerIdentityDriftError, match="symlink"):
        ApptainerBackend(
            record_link,
            workspace_root=tmp_path / "safe-workspaces",
            config=backend.config,
        )
    with pytest.raises(ApptainerIdentityDriftError, match="symlink"):
        ApptainerBackend(
            tmp_path / "safe-records",
            workspace_root=workspace_link,
            config=backend.config,
        )


def test_existing_resolved_manifest_must_match_before_attempt_creation(
    tmp_path: Path,
) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    manifest_path = backend.run_directory(run) / "resolved_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b'{"drift":true}\n')
    marker = tmp_path / "command-ran"

    with pytest.raises(ApptainerIdentityDriftError, match="manifest content drift"):
        backend.execute(
            run,
            command=(
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ),
            attempt_id="manifest-drift",
        )
    assert not marker.exists()
    assert not backend._case_root(run, "manifest-drift").exists()
    assert not backend._workspace(run, "manifest-drift").exists()


def test_capture_redacts_secret_crossing_the_persisted_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BMP_TEST_SECRET", "supersecret")
    backend, run, _, _ = _runtime(tmp_path)
    backend.config = replace(backend.config, max_capture_bytes=5)
    result = backend.execute(
        run,
        command=(sys.executable, "-c", "print('xxsupersecret', end='')"),
        attempt_id="secret-boundary",
    )
    stdout = (result.receipt_path.parent / "stdout.log").read_bytes()
    assert b"supersecret" not in stdout
    assert b"sup" not in stdout
    assert stdout.endswith(b"[TRUNCATED]\n")


def test_capture_and_artifact_limits_bound_persisted_bytes(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    backend.config = replace(
        backend.config,
        max_capture_bytes=64,
        max_artifact_bytes=32,
        max_artifact_entries=4,
    )
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys; "
                "sys.stdout.write('x' * 100000); "
                "sys.stderr.write('y' * 100000); "
                "Path('large.bin').write_bytes(b'z' * 100000)"
            ),
        ),
        attempt_id="bounded-output",
        artifact_exports=("large.bin",),
    )
    assert result.status == "export_error"
    assert result.workspace_retained is True
    for name in ("stdout.log", "stderr.log"):
        content = (result.receipt_path.parent / name).read_bytes()
        assert content.endswith(b"[TRUNCATED]\n")
        assert len(content) <= 64 + len(b"\n[TRUNCATED]\n")
    assert not (result.workspace / "stdout.raw").exists()
    assert not (result.workspace / "stderr.raw").exists()
    backend.verify_receipt(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )


def test_artifact_limits_apply_across_the_complete_export_set(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    backend.config = replace(
        backend.config,
        max_artifact_bytes=32,
        max_artifact_entries=8,
    )
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "Path('first.bin').write_bytes(b'a' * 20); "
                "Path('second.bin').write_bytes(b'b' * 20)"
            ),
        ),
        attempt_id="aggregate-byte-limit",
        artifact_exports=("first.bin", "second.bin"),
    )
    assert result.status == "export_error"
    assert result.workspace_retained is True
    assert result.artifact_refs == ()

    backend.config = replace(
        backend.config,
        max_artifact_bytes=1024,
        max_artifact_entries=2,
    )
    entry_result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; Path('many').mkdir(); "
                "Path('many/a').touch(); Path('many/b').touch()"
            ),
        ),
        attempt_id="aggregate-entry-limit",
        artifact_exports=("many",),
    )
    assert entry_result.status == "export_error"
    assert entry_result.workspace_retained is True
    assert entry_result.artifact_refs == ()


def test_successful_parent_terminates_residual_process_group(tmp_path: Path) -> None:
    backend, run, _, _ = _runtime(tmp_path, forwarded_env_names=())
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            (
                "import subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
                "print(child.pid)"
            ),
        ),
        attempt_id="residual-process",
    )
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    termination = receipt["lifecycle"]["termination"]
    assert termination["residual_term_sent"] is True
    assert result.status == "completed"
    backend.verify_receipt(
        result.receipt_path,
        expected_receipt_sha256=result.receipt_sha256,
    )
