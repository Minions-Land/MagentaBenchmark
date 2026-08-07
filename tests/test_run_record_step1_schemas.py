"""Schema-only coverage for RunRecord expansion Step 1 contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import (
    ArtifactRef,
    BackendSpec,
    Budget,
    CredentialRef,
    EnvironmentBindingRef,
    EvidenceBundle,
    JournalRecord,
    NetworkEndpointRecord,
    NetworkObservation,
    NetworkObservationMode,
    ProviderBinding,
    ProvenanceRecord,
    ResolvedExecutionSpec,
    ResourceSpec,
    RunStatus,
    SystemPromptRecord,
    VerifierEvidence,
    WorkspaceRecord,
)

SHA = "a" * 64
OCI_SHA = "sha256:" + SHA


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(path=f"/tmp/{name}", sha256=SHA, size_bytes=1)


def provenance() -> ProvenanceRecord:
    return ProvenanceRecord(
        manifest_digest=SHA,
        runner_digest=SHA,
        benchmark_digest=SHA,
        subject_digest=SHA,
        backend_digest="sha256:backend",
    )


def test_resource_spec_uses_digest_only_environment_bindings() -> None:
    resource = ResourceSpec(
        build_timeout_sec=600.0,
        docker_image="example/task:latest",
        docker_image_digest=OCI_SHA,
        cpus=1,
        memory_mb=2048,
        storage_mb=10240,
        gpus=0,
        allow_internet=False,
        mcp_servers=(),
        env=(
            EnvironmentBindingRef(
                name="OPENAI_API_KEY",
                value_digest=SHA,
                secret=True,
                source_name="provider-primary",
            ),
        ),
        agent_timeout_sec=1200.0,
        verifier_timeout_sec=1200.0,
    )
    payload = resource.model_dump(mode="json")
    serialized = json.dumps(payload)
    assert resource.claim_image_identity_valid is True
    assert payload["env"][0] == {
        "name": "OPENAI_API_KEY",
        "value_digest": SHA,
        "secret": True,
        "source_name": "provider-primary",
    }
    assert "plaintext-value" not in serialized
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EnvironmentBindingRef.model_validate(
            payload["env"][0] | {"value": "plaintext-value"}
        )


def test_mutable_image_is_permitted_for_exploration_but_not_claim_ready() -> None:
    payload = {
        "build_timeout_sec": 600.0,
        "docker_image": "example/task:latest",
        "cpus": 1,
        "memory_mb": 2048,
        "storage_mb": 10240,
        "gpus": 0,
        "allow_internet": True,
        "agent_timeout_sec": 1200.0,
        "verifier_timeout_sec": 1200.0,
    }
    resource = ResourceSpec.model_validate(payload)
    assert resource.docker_image_digest is None
    assert resource.claim_image_identity_valid is False
    with pytest.raises(ValidationError, match="docker_image_digest"):
        ResourceSpec.model_validate(payload | {"docker_image_digest": "latest"})


def test_credential_and_provider_binding_serialize_no_secret_value_or_fingerprint() -> None:
    credential = CredentialRef(
        name="openai-primary",
        value_sha256=SHA,
        secret=True,
        source_file="credentials/providers.toml",
    )
    binding = ProviderBinding(
        provider_id="openai",
        base_url="https://api.openai.com/v1",
        wire_api="responses",
        model_id="gpt-5",
        credential_ref=credential,
    )
    payload = binding.model_dump(mode="json")
    assert set(payload["credential_ref"]) == {
        "name",
        "value_sha256",
        "secret",
        "source_file",
    }
    assert not {"value", "length", "prefix", "suffix"}.intersection(
        payload["credential_ref"]
    )
    with pytest.raises(ValidationError, match="credentials"):
        ProviderBinding.model_validate(
            payload | {"base_url": "https://user:password@example.test/v1"}
        )
    with pytest.raises(ValidationError, match="query string"):
        ProviderBinding.model_validate(
            payload | {"base_url": "https://example.test/v1?token=secret"}
        )


def test_provider_binding_is_optional_but_identity_bearing_when_resolved() -> None:
    backend = BackendSpec(
        id="local",
        kind="local",
        adapter="subprocess",
        executable="/bin/true",
        digest=SHA,
    )
    base = ResolvedExecutionSpec(
        backend=backend,
        model="gpt-5",
        budget=Budget(max_tokens=10),
    )
    binding = ProviderBinding(
        provider_id="openai",
        base_url="https://api.openai.com/v1",
        wire_api="responses",
        model_id="gpt-5",
        credential_ref=CredentialRef(
            name="openai-primary",
            value_sha256=SHA,
            secret=True,
            source_file="credentials/providers.toml",
        ),
    )
    bound = base.model_copy(update={"provider_binding": binding})
    assert base.provider_binding is None
    assert base.model_dump_json() != bound.model_dump_json()


def test_network_observation_proves_claim_isolation_without_recording_urls() -> None:
    observation = NetworkObservation(
        policy_digest=SHA,
        declared_allow_internet=False,
        mode=NetworkObservationMode.active_probe,
        egress_attempted=True,
        egress_succeeded=False,
        reached_endpoints=(
            NetworkEndpointRecord(
                protocol="tcp",
                host="example.test",
                port=443,
                outcome="blocked",
            ),
        ),
        evidence_refs=(artifact("network-probe.json"),),
    )
    assert observation.claim_isolation_valid is True
    unobservable = NetworkObservation(
        policy_digest=SHA,
        declared_allow_internet=False,
        mode=NetworkObservationMode.unobservable,
        egress_attempted=False,
        egress_succeeded=False,
    )
    assert unobservable.claim_isolation_valid is False
    with pytest.raises(ValidationError, match="URL data or credentials"):
        NetworkEndpointRecord(
            protocol="https",
            host="https://user:secret@example.test/path?query=value",
            port=443,
            outcome="connected",
        )


def test_journal_prompt_and_workspace_are_reference_only() -> None:
    journal = JournalRecord(
        format="harnessx-journal-v2",
        session_id="session-1",
        run_ids=("run-1", "run-2"),
        segment_refs=(artifact("segment.jsonl"),),
        trace_refs=(artifact("trace.json"),),
        state_refs=(artifact("state.json"),),
        session_index_ref=artifact("index.json"),
    )
    prompt = SystemPromptRecord(step_id="step-1", prompt_ref=artifact("prompt.txt"))
    workspace = WorkspaceRecord(
        namespace="run-1/workspace",
        setup_refs=(artifact("setup.log"), prompt.prompt_ref),
        state_refs=(artifact("workspace.tar"),),
        journal=journal,
    )
    payload = workspace.model_dump(mode="json")
    assert payload["journal"]["format"] == "harnessx-journal-v2"
    assert "content" not in json.dumps(payload)
    with pytest.raises(ValidationError, match="journal run_ids must be unique"):
        JournalRecord.model_validate(
            journal.model_dump(mode="python") | {"run_ids": ("run-1", "run-1")}
        )


def test_scored_status_requires_metric_and_forbids_binary_verdict() -> None:
    bundle = EvidenceBundle(
        run_id="run-scored",
        status=RunStatus.scored,
        output_refs=(artifact("answer.txt"),),
        verifier_evidence=VerifierEvidence(
            verifier="rubric",
            passed=None,
            score=0.73,
            metrics={"overall": 0.73},
        ),
        provenance=provenance(),
    )
    assert bundle.status == RunStatus.scored
    with pytest.raises(ValidationError, match="no binary passed verdict"):
        EvidenceBundle.model_validate(
            bundle.model_dump(mode="python")
            | {
                "verifier_evidence": {
                    "verifier": "rubric",
                    "passed": False,
                    "score": 0.73,
                    "metrics": {"overall": 0.73},
                }
            }
        )
