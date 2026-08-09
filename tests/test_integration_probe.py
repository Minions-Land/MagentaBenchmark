from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.evidence import artifact_ref, atomic_write_json
from MagentaBench.schemas import (
    IntegrationProbeIdentityRef,
    IntegrationProbeIdentityRole,
    IntegrationProbeOutcome,
    IntegrationProbePhase,
    IntegrationProbeRecord,
    ReportVerificationError,
    RunPurpose,
    RunStatus,
    verify_integration_probe,
)


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


LEGACY_IDENTITY_FIELDS = (
    "benchmark_digest",
    "loader_digest",
    "execution_adapter_digest",
    "subject_digest",
    "backend_digest",
    "verifier_contract_digest",
    "container_image_digest",
)


def _record(tmp_path: Path) -> tuple[IntegrationProbeRecord, Path]:
    public = tmp_path / "public.json"
    evidence = tmp_path / "candidate.patch"
    public.write_text('{"case_id":"case-1"}\n', encoding="utf-8")
    evidence.write_text("diff --git a/a b/a\n", encoding="utf-8")
    identity_refs: list[IntegrationProbeIdentityRef] = []
    for role in IntegrationProbeIdentityRole:
        identity_path = tmp_path / f"{role.value}.identity"
        identity_path.write_text(f"retained {role.value} bytes\n", encoding="utf-8")
        identity_refs.append(
            IntegrationProbeIdentityRef(
                role=role,
                artifact_ref=artifact_ref(identity_path),
            )
        )
    record = IntegrationProbeRecord(
        format="bmp-integration-probe-v1",
        purpose=RunPurpose.exploratory,
        probe_id="probe-1",
        benchmark_adapter="swebench",
        case_id="case-1",
        phase=IntegrationProbePhase.complete,
        outcome=IntegrationProbeOutcome.completed,
        status=RunStatus.pass_,
        identity_refs=tuple(identity_refs),
        public_input_ref=artifact_ref(public),
        evidence_refs=(artifact_ref(evidence),),
        claim_blockers=("execution adapter was not activated by Pipeline",),
    )
    path = tmp_path / "integration_probe.json"
    atomic_write_json(path, record)
    return record, path


def test_integration_probe_round_trips_and_rehashes_all_refs(tmp_path: Path) -> None:
    record, path = _record(tmp_path)

    verified = verify_integration_probe(path)

    assert verified.record == record
    assert verified.record_path == path.resolve()
    Path(record.evidence_refs[0].path).write_text("drift\n", encoding="utf-8")
    with pytest.raises(ReportVerificationError, match="sha256 mismatch"):
        verify_integration_probe(path)


@pytest.mark.parametrize("role", tuple(IntegrationProbeIdentityRole))
def test_each_probe_identity_role_fails_closed_on_digest_mutation(
    tmp_path: Path,
    role: IntegrationProbeIdentityRole,
) -> None:
    record, path = _record(tmp_path)
    payload = record.model_dump(mode="json")
    identity = next(
        item for item in payload["identity_refs"] if item["role"] == role.value
    )
    identity["artifact_ref"]["sha256"] = "f" * 64
    atomic_write_json(path, payload)

    with pytest.raises(ReportVerificationError, match="sha256 mismatch"):
        verify_integration_probe(path)


@pytest.mark.parametrize("field", LEGACY_IDENTITY_FIELDS)
def test_unbound_legacy_probe_identity_digests_are_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    record, _ = _record(tmp_path)
    payload = record.model_dump(mode="json")
    payload[field] = (
        "sha256:" + "f" * 64 if field == "container_image_digest" else "f" * 64
    )

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, IntegrationProbeRecord.model_json_schema())
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IntegrationProbeRecord.model_validate(payload)


def test_integration_probe_outcome_and_unretained_metadata_fail_closed(
    tmp_path: Path,
) -> None:
    record, _ = _record(tmp_path)
    payload = record.model_dump(mode="python")
    payload["outcome"] = IntegrationProbeOutcome.infrastructure_failure
    with pytest.raises(ValidationError, match="outcome taxonomy"):
        IntegrationProbeRecord.model_validate(payload)

    payload = record.model_dump(mode="python")
    payload["details"] = {"model": "unretained-model"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IntegrationProbeRecord.model_validate(payload)

    payload = record.model_dump(mode="python")
    payload["usage"] = {"total_tokens": 999}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IntegrationProbeRecord.model_validate(payload)

    payload = record.model_dump(mode="python")
    payload["network_policy"] = {"source_artifact_digest": "f" * 64}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IntegrationProbeRecord.model_validate(payload)

    payload = record.model_dump(mode="python")
    payload["network_observation"] = {"policy_digest": "f" * 64}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IntegrationProbeRecord.model_validate(payload)


def test_manifest_identity_cannot_be_asserted_without_manifest_bytes(
    tmp_path: Path,
) -> None:
    record, _ = _record(tmp_path)
    payload = record.model_dump(mode="python")
    payload["manifest_digest"] = "f" * 64
    with pytest.raises(ValidationError, match="requires a manifest_ref"):
        IntegrationProbeRecord.model_validate(payload)


def test_probe_manifest_digest_is_replayed_from_retained_manifest(
    tmp_path: Path,
) -> None:
    record, path = _record(tmp_path)
    manifest = Compiler(ROOT).compile(EXPERIMENT)[0].manifest
    manifest_path = tmp_path / "resolved_manifest.json"
    atomic_write_json(manifest_path, manifest)
    payload = record.model_dump(mode="json")
    payload.update(
        {
            "benchmark_adapter": manifest.benchmark.adapter,
            "identity_refs": [],
            "manifest_digest": manifest.canonical_digest(),
            "manifest_ref": artifact_ref(manifest_path).model_dump(mode="json"),
        }
    )
    atomic_write_json(path, payload)

    assert verify_integration_probe(path).record.manifest_digest == (
        manifest.canonical_digest()
    )

    payload["manifest_digest"] = "f" * 64
    atomic_write_json(path, payload)
    with pytest.raises(ReportVerificationError, match="manifest digest drift"):
        verify_integration_probe(path)


def test_partial_probe_requires_at_least_one_retained_identity(
    tmp_path: Path,
) -> None:
    record, _ = _record(tmp_path)
    payload = record.model_dump(mode="json")
    payload["identity_refs"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, IntegrationProbeRecord.model_json_schema())
    with pytest.raises(ValidationError, match="manifest_ref or identity_refs"):
        IntegrationProbeRecord.model_validate(payload)


def test_probe_case_id_is_bound_to_retained_public_input(tmp_path: Path) -> None:
    record, path = _record(tmp_path)
    payload = record.model_dump(mode="json")
    payload["case_id"] = "case-2"
    atomic_write_json(path, payload)

    with pytest.raises(ReportVerificationError, match="case id drift"):
        verify_integration_probe(path)
