from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from dataclasses import replace

import pytest

from MagentaBench.runner.backend.apptainer import _canonical_json_bytes, _tree_digest
from MagentaBench.runner.compiler import CompiledRun, Compiler
from MagentaBench.runner.evidence import sha256_file
from MagentaBench.runner.gates import _evidence_integrity_errors
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas.models import ArtifactRef, BackendSpec, ProvenanceRecord
from MagentaBench.schemas.verification import (
    ReportVerificationError,
    _apptainer_sha256_file,
    _apptainer_tree_identity,
    _verify_bundle_provenance,
    verify_apptainer_runtime_receipt,
)

from test_apptainer_backend import _runtime


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _verified_runtime(
    tmp_path: Path,
    *,
    max_artifact_bytes: int | None = None,
    max_artifact_entries: int | None = None,
    max_capture_bytes: int | None = None,
    spec_defaults_updates: dict[str, object] | None = None,
):
    backend, _, launcher, image = _runtime(tmp_path)
    config_updates = {
        key: value
        for key, value in {
            "max_artifact_bytes": max_artifact_bytes,
            "max_artifact_entries": max_artifact_entries,
            "max_capture_bytes": max_capture_bytes,
        }.items()
        if value is not None
    }
    if config_updates:
        backend.config = replace(backend.config, **config_updates)
    config = backend.config
    defaults = {
        "binds": [],
        "fakeroot": config.fakeroot,
        "forwarded_env_names": list(config.forwarded_env_names),
        "gpu": False,
        "image_kind": config.image_kind,
        "image_sha256": config.image_digest,
        "keep_workspace_on_failure": config.keep_workspace_on_failure,
        "launcher_build_config_sha256": config.launcher_build_config_digest,
        "max_artifact_bytes": config.max_artifact_bytes,
        "max_artifact_entries": config.max_artifact_entries,
        "max_capture_bytes": config.max_capture_bytes,
        "network_mode": "none",
        "termination_grace_seconds": config.termination_grace_seconds,
    }
    defaults.update(spec_defaults_updates or {})
    spec = BackendSpec(
        id="apptainer.test",
        kind="container",
        adapter="apptainer",
        bmp_version="0.1",
        executable=str(launcher),
        digest=sha256_file(launcher),
        version=config.launcher_version,
        image=str(image),
        defaults=defaults,
    )
    base = Compiler(ROOT).compile(EXPERIMENT)[0]
    manifest = base.manifest.model_copy(
        update={
            "execution": base.manifest.execution.model_copy(update={"backend": spec})
        }
    )
    return backend, CompiledRun(manifest), launcher, image, spec


def _backend_and_provenance(backend, run, launcher: Path, image: Path, spec):
    receipt = json.loads(
        next(backend.record_root.rglob("runtime_receipt.json")).read_text()
    )
    provenance = ProvenanceRecord(
        manifest_digest=run.manifest_digest,
        runner_digest=receipt["runner_sha256"],
        benchmark_digest="b" * 64,
        subject_digest="c" * 64,
        backend_digest=sha256_file(launcher),
        executable=str(launcher),
        executable_digest=sha256_file(launcher),
        image_digest=sha256_file(image),
        backend_kind="apptainer",
        network_mode="none",
    )
    receipt_path = next(backend.record_root.rglob("runtime_receipt.json"))
    return spec, provenance, receipt_path


def _ref(path: Path) -> ArtifactRef:
    content = path.read_bytes()
    return ArtifactRef(
        path=str(path.resolve()),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _reseal(path: Path, payload: dict[str, object]) -> None:
    unsealed = {
        key: value for key, value in payload.items() if key != "receipt_payload_sha256"
    }
    payload["receipt_payload_sha256"] = hashlib.sha256(
        _canonical_json_bytes(unsealed)
    ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_standalone_apptainer_receipt_verifies_without_runtime(tmp_path: Path) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    result = backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('result.txt').write_text('ok')",
        ),
        attempt_id="verified",
        artifact_exports=("result.txt",),
    )
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    verified = verify_apptainer_runtime_receipt(
        _ref(receipt_path),
        backend=spec,
        provenance=provenance,
        manifest_digest=run.manifest_digest,
    )
    assert verified.receipt_path == receipt_path.resolve()
    assert len(verified.artifact_refs) == 1
    assert len(verified.log_refs) == 2
    assert result.status == "completed"


def test_standalone_apptainer_receipt_rejects_seal_and_image_drift(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="tampered")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    payload = json.loads(receipt_path.read_text())
    payload["argv_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReportVerificationError, match="content seal"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_normalizes_deep_invalid_json(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="deep-invalid")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    receipt_path.write_text(
        "{" + '"nested":[' * 2500 + "!]" + "]" * 2500 + "}",
        encoding="utf-8",
    )
    with pytest.raises(
        ReportVerificationError, match="malformed receipt|malformed JSON"
    ):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_rejects_image_drift(tmp_path: Path) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="image-drift")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    image.write_bytes(b"drift")
    with pytest.raises(ReportVerificationError, match="image"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_rejects_nonterminal_and_incomplete_export(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    result = backend.execute(
        run,
        command=("true",),
        attempt_id="missing-export",
        artifact_exports=("not-created.txt",),
    )
    assert result.status == "export_error"
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    with pytest.raises(ReportVerificationError, match="artifact export"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_rejects_scratch_runs_root(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path / ".runs")
    backend.execute(run, command=("true",), attempt_id="scratch-root")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    with pytest.raises(ReportVerificationError, match="scratch .runs"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_rejects_nonterminal_lifecycle(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="nonterminal")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    payload = json.loads(receipt_path.read_text())
    payload["lifecycle"]["status"] = "running"
    _reseal(receipt_path, payload)

    with pytest.raises(ReportVerificationError, match="not terminal|inconsistent"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are not available")
def test_standalone_apptainer_receipt_rejects_symlink_refs(tmp_path: Path) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="symlink")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    target = receipt_path.parent / "stdout-link.log"
    target.symlink_to(receipt_path.parent / "stdout.log")
    payload = json.loads(receipt_path.read_text())
    payload["log_refs"][0] = {
        **payload["log_refs"][0],
        "path": str(target),
    }
    _reseal(receipt_path, payload)

    with pytest.raises(ReportVerificationError, match="symlink"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not available")
def test_standalone_apptainer_receipt_rejects_fifo_and_hardlink_refs(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="fifo")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    fifo = receipt_path.parent / "fifo"
    os.mkfifo(fifo)
    payload = json.loads(receipt_path.read_text())
    payload["log_refs"][0]["path"] = str(fifo)
    payload["log_refs"][0]["sha256"] = "0" * 64
    payload["log_refs"][0]["size_bytes"] = 0
    payload["receipt_payload_sha256"] = hashlib.sha256(
        _canonical_json_bytes(
            {k: v for k, v in payload.items() if k != "receipt_payload_sha256"}
        )
    ).hexdigest()
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReportVerificationError, match="regular file|FIFO"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )

    # The verifier also rejects a hard-linked receipt itself.
    linked = receipt_path.parent / "receipt-hardlink.json"
    linked.hardlink_to(receipt_path)
    with pytest.raises(ReportVerificationError, match="hard-linked"):
        verify_apptainer_runtime_receipt(
            _ref(linked),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_rejects_manifest_semantic_drift(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="manifest-drift")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    manifest_path = receipt_path.parent.parent.parent / "resolved_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["execution"]["backend"]["defaults"]["network_mode"] = "isolated"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    payload = json.loads(receipt_path.read_text())
    payload["resolved_manifest_sha256"] = sha256_file(manifest_path)
    payload["resolved_manifest_size_bytes"] = manifest_path.stat().st_size
    _reseal(receipt_path, payload)

    with pytest.raises(
        ReportVerificationError,
        match="canonical digest|backend binding",
    ):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


@pytest.mark.parametrize("limit_kind", ["bytes", "entries", "log"])
def test_standalone_apptainer_receipt_enforces_backend_limits(
    tmp_path: Path,
    limit_kind: str,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(
        tmp_path,
        max_artifact_bytes=32,
        max_artifact_entries=1,
        max_capture_bytes=16,
    )
    backend.execute(run, command=("true",), attempt_id=f"limit-{limit_kind}")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    payload = json.loads(receipt_path.read_text())
    if limit_kind == "log":
        log_path = receipt_path.parent / "stdout.log"
        log_path.write_bytes(b"x" * 30)
        payload["log_refs"][0] = _ref(log_path).model_dump(mode="json")
    else:
        artifact_root = receipt_path.parent / "artifacts"
        artifact_root.mkdir()
        count = 1 if limit_kind == "bytes" else 2
        paths = []
        for index in range(count):
            path = artifact_root / f"artifact-{index}.bin"
            path.write_bytes(b"x" * (33 if limit_kind == "bytes" else 1))
            paths.append(path)
        payload["artifact_export"] = {
            "complete": True,
            "error_type": None,
            "refs": [_ref(path).model_dump(mode="json") for path in paths],
            "requested": [path.name for path in paths],
        }
    _reseal(receipt_path, payload)

    with pytest.raises(ReportVerificationError, match="exceed|byte limit"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_rejects_incomplete_directory_export(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(
        run,
        command=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('bundle').mkdir(); "
            "Path('bundle/a.txt').write_text('a'); "
            "Path('bundle/b.txt').write_text('b')",
        ),
        attempt_id="directory-export",
        artifact_exports=("bundle",),
    )
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    payload = json.loads(receipt_path.read_text())
    payload["artifact_export"]["refs"] = payload["artifact_export"]["refs"][:1]
    _reseal(receipt_path, payload)

    with pytest.raises(ReportVerificationError, match="exactly cover"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links are not available")
def test_standalone_apptainer_runtime_inputs_reject_hardlinks(tmp_path: Path) -> None:
    source = tmp_path / "launcher"
    source.write_bytes(b"launcher")
    linked = tmp_path / "launcher-link"
    linked.hardlink_to(source)
    mismatches: list[str] = []

    assert (
        _apptainer_sha256_file(
            linked,
            label="launcher",
            mismatches=mismatches,
        )
        is None
    )
    assert any("hard-linked" in mismatch for mismatch in mismatches)


def test_sandbox_tree_digest_matches_backend_global_lexical_order(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    (sandbox / "a").mkdir(parents=True)
    (sandbox / "a" / "zz").write_text("nested", encoding="utf-8")
    (sandbox / "a-b").mkdir()
    (sandbox / "a-b" / "x").write_text("sibling", encoding="utf-8")
    expected = _tree_digest(sandbox)
    mismatches: list[str] = []

    observed = _apptainer_tree_identity(
        sandbox,
        label="sandbox",
        mismatches=mismatches,
        max_bytes=1024,
        max_entries=32,
    )

    assert observed == expected
    assert mismatches == []

    limit_mismatches: list[str] = []
    assert (
        _apptainer_tree_identity(
            sandbox,
            label="sandbox",
            mismatches=limit_mismatches,
            max_bytes=1024,
            max_entries=2,
        )
        is None
    )
    assert any("entry limit" in mismatch for mismatch in limit_mismatches)

    byte_limit_mismatches: list[str] = []
    assert (
        _apptainer_tree_identity(
            sandbox,
            label="sandbox",
            mismatches=byte_limit_mismatches,
            max_bytes=1,
            max_entries=32,
        )
        is None
    )
    assert any("byte limit" in mismatch for mismatch in byte_limit_mismatches)


def test_standalone_apptainer_receipt_rejects_malformed_overlay_contract(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(
        tmp_path,
        spec_defaults_updates={"overlay": "invalid"},
    )
    backend.execute(run, command=("true",), attempt_id="bad-overlay")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    payload = json.loads(receipt_path.read_text())
    payload["identity"]["overlay"] = "invalid"
    payload["policy_sha256"] = hashlib.sha256(
        _canonical_json_bytes(payload["identity"])
    ).hexdigest()
    _reseal(receipt_path, payload)

    with pytest.raises(ReportVerificationError, match="overlay identity is malformed"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_normalizes_locator_and_signal_errors(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="invalid-locator")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    invalid_spec = spec.model_copy(update={"executable": "relative/apptainer"})
    with pytest.raises(ReportVerificationError, match="absolute normalized path"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=invalid_spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )

    payload = json.loads(receipt_path.read_text())
    payload["lifecycle"]["termination"]["kill_sent"] = True
    payload["lifecycle"]["termination"]["term_sent"] = False
    _reseal(receipt_path, payload)
    with pytest.raises(ReportVerificationError, match="termination escalation"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )

    payload["lifecycle"]["termination"]["kill_sent"] = False
    payload["lifecycle"]["termination"]["term_sent"] = True
    _reseal(receipt_path, payload)
    with pytest.raises(ReportVerificationError, match="natural process termination"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_rejects_unbounded_duration_and_strict_ref_drift(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="bounded-fields")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    payload = json.loads(receipt_path.read_text())
    payload["lifecycle"]["wall_clock_seconds"] = 10**1000
    _reseal(receipt_path, payload)
    with pytest.raises(ReportVerificationError, match="lifecycle duration"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )

    payload = json.loads(receipt_path.read_text())
    payload["lifecycle"]["wall_clock_seconds"] = 0.01
    payload["log_refs"][0]["size_bytes"] = "0"
    _reseal(receipt_path, payload)
    with pytest.raises(ReportVerificationError, match="size_bytes must be an integer"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_standalone_apptainer_receipt_normalizes_overflowing_policy(
    tmp_path: Path,
) -> None:
    backend, run, launcher, image, spec = _verified_runtime(tmp_path)
    backend.execute(run, command=("true",), attempt_id="overflow-policy")
    spec, provenance, receipt_path = _backend_and_provenance(
        backend, run, launcher, image, spec
    )
    invalid_spec = spec.model_copy(
        update={
            "defaults": {
                **spec.defaults,
                "termination_grace_seconds": 10**1000,
            }
        }
    )
    with pytest.raises(ReportVerificationError, match="malformed receipt"):
        verify_apptainer_runtime_receipt(
            _ref(receipt_path),
            backend=invalid_spec,
            provenance=provenance,
            manifest_digest=run.manifest_digest,
        )


def test_apptainer_receipt_is_required_by_runner_and_standalone_gates(
    tmp_path: Path,
) -> None:
    item = Pipeline(ROOT, tmp_path / "pipeline").run(EXPERIMENT).runs[0]
    backend = item.plan.manifest.execution.backend.model_copy(
        update={"adapter": "apptainer", "kind": "container"}
    )
    manifest = item.plan.manifest.model_copy(
        update={
            "execution": item.plan.manifest.execution.model_copy(
                update={"backend": backend}
            )
        }
    )
    rebound = replace(item, plan=replace(item.plan, manifest=manifest))
    assert "Apptainer runtime receipt reference missing" in _evidence_integrity_errors(
        rebound
    )

    mismatches: list[str] = []
    _verify_bundle_provenance(
        item.case.bundle,
        manifest,
        label="case",
        path_map={},
        mismatches=mismatches,
    )
    assert "case: Apptainer runtime receipt is missing" in mismatches


def test_apptainer_receipt_verifies_through_bundle_provenance(
    tmp_path: Path,
) -> None:
    item = Pipeline(ROOT, tmp_path / "pipeline").run(EXPERIMENT).runs[0]
    backend, _, launcher, image, spec = _verified_runtime(tmp_path / "runtime")
    manifest = item.plan.manifest.model_copy(
        update={
            "execution": item.plan.manifest.execution.model_copy(
                update={"backend": spec}
            )
        }
    )
    run = CompiledRun(manifest)
    execution = backend.execute(
        run,
        command=("true",),
        attempt_id=item.case.bundle.run_id,
    )
    receipt = json.loads(execution.receipt_path.read_text())
    provenance = item.case.bundle.provenance.model_copy(
        update={
            "manifest_digest": run.manifest_digest,
            "runner_digest": receipt["runner_sha256"],
            "backend_digest": spec.digest,
            "executable": spec.executable,
            "executable_digest": spec.digest,
            "image_digest": spec.defaults["image_sha256"],
            "backend_kind": "apptainer",
            "network_mode": spec.defaults["network_mode"],
            "container_receipt_ref": _ref(execution.receipt_path),
        }
    )
    bundle = item.case.bundle.model_copy(update={"provenance": provenance})

    mismatches: list[str] = []
    _verify_bundle_provenance(
        bundle,
        manifest,
        label="case",
        path_map={},
        mismatches=mismatches,
    )
    assert mismatches == []
