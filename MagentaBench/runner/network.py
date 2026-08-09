"""Evidence helpers for adapter-observed network boundaries.

This module deliberately does not perform a host-side network request.  Only
the execution adapter can observe the relevant process or task-container
boundary, so it must pass the observation into these helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from MagentaBench.schemas import (
    NetworkBoundary,
    NetworkEndpointRecord,
    NetworkObservation,
    NetworkObservationMode,
    NetworkPolicySource,
    ResolvedNetworkPolicy,
    canonical_digest,
)

from .evidence import artifact_ref, atomic_write_json


class NetworkEvidenceError(ValueError):
    """An adapter supplied an incoherent network-boundary observation."""


_SUCCESS_OUTCOMES = frozenset(
    {"allowed", "connected", "connection_succeeded", "reached", "success", "ok"}
)


def _endpoint_successes(endpoints: tuple[NetworkEndpointRecord, ...]) -> bool:
    return any(
        endpoint.outcome.strip().casefold() in _SUCCESS_OUTCOMES
        for endpoint in endpoints
    )


@dataclass(frozen=True)
class ActivatedNetworkEvidence:
    """Resolved policy and observation ready to embed in an evidence bundle."""

    policy: ResolvedNetworkPolicy
    observation: NetworkObservation


def _policy(
    *,
    resolver_adapter: str,
    execution_adapter: str,
    case_id: str,
    boundary: NetworkBoundary,
    allow_internet: bool,
    required_observation: NetworkObservationMode,
    source: NetworkPolicySource,
    source_artifact_digest: str,
) -> ResolvedNetworkPolicy:
    return ResolvedNetworkPolicy(
        resolver_adapter=resolver_adapter,
        execution_adapter=execution_adapter,
        case_id=case_id,
        boundary=boundary,
        allow_internet=allow_internet,
        required_observation=required_observation,
        source=source,
        source_artifact_digest=source_artifact_digest,
    )


def record_unobservable_network(
    evidence_path: str | Path,
    *,
    resolver_adapter: str,
    execution_adapter: str,
    case_id: str,
    boundary: NetworkBoundary,
    allow_internet: bool,
    source: NetworkPolicySource,
    source_artifact_digest: str,
    reason: str,
) -> ActivatedNetworkEvidence:
    """Persist an honest negative receipt when the target boundary was unseen."""

    reason = reason.strip()
    if not reason or any(marker in reason for marker in ("=", "://", "@")):
        raise NetworkEvidenceError(
            "unobservable network reason must be non-empty and secret-free"
        )
    policy = _policy(
        resolver_adapter=resolver_adapter,
        execution_adapter=execution_adapter,
        case_id=case_id,
        boundary=boundary,
        allow_internet=allow_internet,
        required_observation=NetworkObservationMode.unobservable,
        source=source,
        source_artifact_digest=source_artifact_digest,
    )
    path = Path(evidence_path).resolve()
    atomic_write_json(
        path,
        {
            "case_id": case_id,
            "boundary": boundary.value,
            "mode": NetworkObservationMode.unobservable.value,
            "reason": reason,
        },
    )
    observation = NetworkObservation(
        policy_digest=canonical_digest(policy),
        declared_allow_internet=allow_internet,
        mode=NetworkObservationMode.unobservable,
        egress_attempted=False,
        egress_succeeded=False,
        evidence_refs=(artifact_ref(path),),
    )
    return ActivatedNetworkEvidence(policy=policy, observation=observation)


def record_active_network_probe(
    evidence_path: str | Path,
    *,
    resolver_adapter: str,
    execution_adapter: str,
    case_id: str,
    boundary: NetworkBoundary,
    allow_internet: bool,
    source: NetworkPolicySource,
    source_artifact_digest: str,
    egress_succeeded: bool,
    reached_endpoints: tuple[NetworkEndpointRecord, ...] = (),
) -> ActivatedNetworkEvidence:
    """Persist a probe result supplied by the adapter at the declared boundary."""

    if not isinstance(egress_succeeded, bool):
        raise NetworkEvidenceError("egress_succeeded must be a boolean")
    if not allow_internet and _endpoint_successes(reached_endpoints):
        raise NetworkEvidenceError(
            "a denied network policy cannot record a successful endpoint"
        )
    if not egress_succeeded and _endpoint_successes(reached_endpoints):
        raise NetworkEvidenceError(
            "network endpoints cannot succeed when egress_succeeded is false"
        )
    if not allow_internet and egress_succeeded:
        # Retain the observation because it proves a violation; callers must
        # not reinterpret it as a successful isolation activation.
        outcome = "policy_violation"
    else:
        outcome = "allowed" if egress_succeeded else "blocked"
    policy = _policy(
        resolver_adapter=resolver_adapter,
        execution_adapter=execution_adapter,
        case_id=case_id,
        boundary=boundary,
        allow_internet=allow_internet,
        required_observation=NetworkObservationMode.active_probe,
        source=source,
        source_artifact_digest=source_artifact_digest,
    )
    path = Path(evidence_path).resolve()
    atomic_write_json(
        path,
        {
            "case_id": case_id,
            "boundary": boundary.value,
            "mode": NetworkObservationMode.active_probe.value,
            "egress_attempted": True,
            "egress_succeeded": egress_succeeded,
            "outcome": outcome,
            "reached_endpoints": [
                endpoint.model_dump(mode="json") for endpoint in reached_endpoints
            ],
        },
    )
    observation = NetworkObservation(
        policy_digest=canonical_digest(policy),
        declared_allow_internet=allow_internet,
        mode=NetworkObservationMode.active_probe,
        egress_attempted=True,
        egress_succeeded=egress_succeeded,
        reached_endpoints=reached_endpoints,
        evidence_refs=(artifact_ref(path),),
    )
    return ActivatedNetworkEvidence(policy=policy, observation=observation)


__all__ = [
    "ActivatedNetworkEvidence",
    "NetworkEvidenceError",
    "record_active_network_probe",
    "record_unobservable_network",
]
