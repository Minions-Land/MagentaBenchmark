from __future__ import annotations

import json
from pathlib import Path

import pytest

from MagentaBench.runner.network import (
    NetworkEvidenceError,
    record_active_network_probe,
    record_unobservable_network,
)
from MagentaBench.schemas import (
    NetworkBoundary,
    NetworkEndpointRecord,
    NetworkObservationMode,
    NetworkPolicySource,
    canonical_digest,
)


def _arguments(tmp_path: Path) -> dict[str, object]:
    return {
        "evidence_path": tmp_path / "network.json",
        "resolver_adapter": "terminal-bench",
        "execution_adapter": "harbor",
        "case_id": "regex-log",
        "boundary": NetworkBoundary.task_container,
        "allow_internet": False,
        "source": NetworkPolicySource.case_set_artifact,
        "source_artifact_digest": "a" * 64,
    }


def test_unobservable_receipt_is_evidence_but_not_isolation(tmp_path: Path) -> None:
    activated = record_unobservable_network(
        **_arguments(tmp_path),
        reason="Harbor emitted no boundary probe or connection log",
    )

    assert activated.policy.required_observation == NetworkObservationMode.unobservable
    assert activated.observation.policy_digest == canonical_digest(activated.policy)
    assert activated.observation.claim_isolation_valid is False
    assert activated.observation.evidence_refs
    persisted = json.loads(
        Path(activated.observation.evidence_refs[0].path).read_text(encoding="utf-8")
    )
    assert persisted["mode"] == "unobservable"


def test_failed_deny_probe_substantiates_isolation(tmp_path: Path) -> None:
    activated = record_active_network_probe(
        **_arguments(tmp_path),
        egress_succeeded=False,
    )

    assert activated.policy.required_observation == NetworkObservationMode.active_probe
    assert activated.observation.claim_isolation_valid is True


def test_successful_deny_probe_records_policy_violation(tmp_path: Path) -> None:
    activated = record_active_network_probe(
        **_arguments(tmp_path),
        egress_succeeded=True,
    )

    assert activated.observation.claim_isolation_valid is False
    persisted = json.loads(
        Path(activated.observation.evidence_refs[0].path).read_text(encoding="utf-8")
    )
    assert persisted["outcome"] == "policy_violation"


def test_probe_rejects_successful_endpoint_under_deny_policy(tmp_path: Path) -> None:
    with pytest.raises(NetworkEvidenceError, match="successful endpoint"):
        record_active_network_probe(
            **_arguments(tmp_path),
            egress_succeeded=False,
            reached_endpoints=(
                NetworkEndpointRecord(
                    protocol="tcp",
                    host="example.invalid",
                    port=443,
                    outcome="connected",
                ),
            ),
        )


def test_unobservable_reason_rejects_url_or_key_value_text(tmp_path: Path) -> None:
    for reason in ("", "target=https://example.invalid", "user@example.invalid"):
        with pytest.raises(NetworkEvidenceError, match="secret-free"):
            record_unobservable_network(
                **_arguments(tmp_path),
                reason=reason,
            )
