"""Replay the closed native evidence contract for provider/model activation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from .models import (
    ArtifactRef,
    ModelActivationEvidence,
    ModelActivationReceipt,
    ModelActivationUsage,
    ProviderBinding,
    UsageRecord,
)


ACTIVATION_SOURCES = frozenset(
    {
        "provider_response",
        "runtime_manifest",
        "native_result",
        "adapter_receipt",
    }
)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "cost",
)


class ModelActivationEvidenceError(ValueError):
    """Native activation evidence is missing, malformed, or internally false."""


class _UnsubstantiatedModelActivationEvidence(ModelActivationEvidenceError):
    """Well-addressed evidence bytes that cannot substantiate activation."""


@dataclass(frozen=True)
class ReplayedModelActivation:
    provider_id: str | None
    model_id: str | None
    binding_digest: str | None
    usage: ModelActivationUsage | None
    observed: bool
    reason: str | None = None


def _strict_nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelActivationEvidenceError(f"native evidence {field} is missing")
    return value.strip()


def _harbor_native_result(document: Mapping[str, Any]) -> ReplayedModelActivation:
    """Project the closed Harbor TrialResult fields used by the backend adapter."""

    agent_result = document.get("agent_result")
    if not isinstance(agent_result, Mapping):
        raise ModelActivationEvidenceError(
            "native_result is neither ModelActivationEvidence nor a Harbor agent_result"
        )
    # Harbor's nop/failed trials have no provider identity. Preserve them as
    # unobserved instead of allowing a fabricated receipt from surrounding
    # result metadata.
    try:
        provider_id = _strict_nonempty_string(
            agent_result.get("provider_id"), field="agent_result.provider_id"
        )
        model_id = _strict_nonempty_string(
            agent_result.get("model_id"), field="agent_result.model_id"
        )
    except ModelActivationEvidenceError as exc:
        return ReplayedModelActivation(
            provider_id=None,
            model_id=None,
            binding_digest=None,
            usage=None,
            observed=False,
            reason=str(exc),
        )
    if "usage" in agent_result:
        try:
            usage = ModelActivationUsage.model_validate(agent_result["usage"])
        except ValidationError:
            raise _UnsubstantiatedModelActivationEvidence(
                "native usage is invalid"
            ) from None
    else:
        input_tokens = agent_result.get("n_input_tokens")
        output_tokens = agent_result.get("n_output_tokens")
        cache_read_tokens = agent_result.get("n_cache_read_tokens")
        cache_write_tokens = agent_result.get("n_cache_write_tokens")
        cost = agent_result.get("cost_usd")
        supplied = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            cost,
        )
        usage = None
        if any(value is not None for value in supplied):
            total_tokens = (
                input_tokens + output_tokens
                if type(input_tokens) is int and type(output_tokens) is int
                else None
            )
            try:
                usage = ModelActivationUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    total_tokens=total_tokens,
                    cost=cost,
                )
            except ValidationError:
                raise _UnsubstantiatedModelActivationEvidence(
                    "native Harbor usage is invalid"
                ) from None
    try:
        evidence = ModelActivationEvidence(
            activation_source="native_result",
            provider_id=provider_id,
            base_url=agent_result.get("base_url"),
            wire_api=agent_result.get("wire_api"),
            model_id=model_id,
            credential_name=agent_result.get("credential_name"),
            credential_value_sha256=agent_result.get(
                "credential_value_sha256"
            ),
            usage=usage,
        )
    except ValidationError:
        return ReplayedModelActivation(
            provider_id=None,
            model_id=None,
            binding_digest=None,
            usage=None,
            observed=False,
            reason="native Harbor binding evidence is incomplete",
        )
    return ReplayedModelActivation(
        provider_id=evidence.provider_id,
        model_id=evidence.model_id,
        binding_digest=evidence.binding_digest(),
        usage=evidence.usage,
        observed=True,
    )


def _json_evidence(
    content: bytes, *, activation_source: str
) -> ReplayedModelActivation:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _UnsubstantiatedModelActivationEvidence(
            "model activation evidence is not valid JSON"
        ) from None
    try:
        evidence = ModelActivationEvidence.model_validate(document)
    except ValidationError:
        if (
            activation_source == "native_result"
            and isinstance(document, Mapping)
            and isinstance(document.get("agent_result"), Mapping)
        ):
            return _harbor_native_result(document)
        raise _UnsubstantiatedModelActivationEvidence(
            "model activation evidence violates the closed schema"
        ) from None
    if evidence.activation_source != activation_source:
        raise _UnsubstantiatedModelActivationEvidence(
            "model activation evidence source differs from the selected capability"
        )
    return ReplayedModelActivation(
        provider_id=evidence.provider_id,
        model_id=evidence.model_id,
        binding_digest=evidence.binding_digest(),
        usage=evidence.usage,
        observed=True,
    )


def _runtime_manifest_evidence(content: bytes) -> ReplayedModelActivation:
    """Retain Magenta configuration without treating assembly as a model call."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _UnsubstantiatedModelActivationEvidence(
            "runtime_manifest evidence is not UTF-8 JSONL"
        ) from None
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise _UnsubstantiatedModelActivationEvidence(
                f"runtime_manifest evidence line {line_number} is blank"
            )
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise _UnsubstantiatedModelActivationEvidence(
                f"runtime_manifest evidence line {line_number} is invalid JSON"
            ) from None
        if not isinstance(record, Mapping):
            raise _UnsubstantiatedModelActivationEvidence(
                f"runtime_manifest evidence line {line_number} is not an object"
            )
        records.append(record)
    manifests = [record for record in records if record.get("type") == "runtime_manifest"]
    if not manifests:
        raise _UnsubstantiatedModelActivationEvidence(
            "runtime_manifest evidence contains no runtime_manifest record"
        )
    manifest = manifests[-1]
    model = manifest.get("model")
    if not isinstance(model, Mapping):
        return ReplayedModelActivation(
            provider_id=None,
            model_id=None,
            binding_digest=None,
            usage=None,
            observed=False,
            reason="runtime manifest did not expose provider/model",
        )
    try:
        provider_id = _strict_nonempty_string(model.get("provider"), field="model.provider")
        model_id = _strict_nonempty_string(model.get("id"), field="model.id")
    except ModelActivationEvidenceError as exc:
        return ReplayedModelActivation(
            provider_id=None,
            model_id=None,
            binding_digest=None,
            usage=None,
            observed=False,
            reason=str(exc),
        )
    return ReplayedModelActivation(
        provider_id=None,
        model_id=None,
        binding_digest=None,
        usage=None,
        observed=False,
        reason=(
            "runtime manifest declares provider/model configuration but contains "
            "no provider-call binding evidence"
        ),
    )


def parse_model_activation_evidence(
    content: bytes, *, activation_source: str
) -> ReplayedModelActivation:
    """Parse the exact evidence bytes selected by ``activation_source``."""

    if activation_source not in ACTIVATION_SOURCES:
        raise ModelActivationEvidenceError(
            f"unknown model activation source {activation_source!r}"
        )
    if activation_source == "runtime_manifest":
        return _runtime_manifest_evidence(content)
    return _json_evidence(content, activation_source=activation_source)


def load_model_activation_evidence(
    ref: ArtifactRef,
    *,
    activation_source: str,
    path: str | Path | None = None,
) -> ReplayedModelActivation:
    """Read, rehash, and replay one content-addressed activation artifact."""

    evidence_path = Path(ref.path if path is None else path)
    try:
        content = evidence_path.read_bytes()
    except OSError as exc:
        raise ModelActivationEvidenceError(
            f"model activation evidence is unreadable: {evidence_path}"
        ) from exc
    if len(content) != ref.size_bytes or hashlib.sha256(content).hexdigest() != ref.sha256:
        raise ModelActivationEvidenceError(
            f"model activation evidence digest drift: {evidence_path}"
        )
    return parse_model_activation_evidence(
        content,
        activation_source=activation_source,
    )


def derive_model_activation_receipt(
    *,
    requested_model: str,
    binding: ProviderBinding | None,
    activation_source: str,
    evidence_ref: ArtifactRef | None,
    reason: str | None = None,
) -> ModelActivationReceipt:
    """Derive receipt identity and status exclusively from replayed evidence."""

    if activation_source not in ACTIVATION_SOURCES:
        raise ValueError("real-model execution lacks a declared activation source")
    requested_provider_id = None if binding is None else binding.provider_id
    requested_model_id = requested_model if binding is None else binding.model_id
    binding_digest = None if binding is None else binding.canonical_digest()
    refs = () if evidence_ref is None else (evidence_ref,)
    if evidence_ref is None:
        missing = reason or "runtime provider/model activation was not observed"
        return ModelActivationReceipt(
            requested_model=requested_model,
            requested_provider_id=requested_provider_id,
            requested_model_id=requested_model_id,
            binding_digest=binding_digest,
            activation_source=activation_source,
            status="unobserved",
            reason=(missing,),
        )
    try:
        observation = load_model_activation_evidence(
            evidence_ref,
            activation_source=activation_source,
        )
    except ModelActivationEvidenceError as exc:
        return ModelActivationReceipt(
            requested_model=requested_model,
            requested_provider_id=requested_provider_id,
            requested_model_id=requested_model_id,
            binding_digest=binding_digest,
            activation_source=activation_source,
            status="unobserved",
            reason=(reason or str(exc),),
            evidence_refs=refs,
        )
    if (
        binding is None
        or not observation.observed
        or observation.binding_digest is None
    ):
        missing = reason or observation.reason or (
            "provider binding is absent"
            if binding is None
            else "runtime provider/model activation was not observed"
        )
        return ModelActivationReceipt(
            requested_model=requested_model,
            requested_provider_id=requested_provider_id,
            requested_model_id=requested_model_id,
            binding_digest=binding_digest,
            activation_source=activation_source,
            status="unobserved",
            reason=(missing,),
            evidence_refs=refs,
        )
    observed = (observation.provider_id, observation.model_id)
    expected = (binding.provider_id, binding.model_id)
    expected_binding_digest = binding.canonical_digest()
    status = (
        "matched"
        if observed == expected
        and observation.binding_digest == expected_binding_digest
        else "mismatch"
    )
    return ModelActivationReceipt(
        requested_model=requested_model,
        requested_provider_id=binding.provider_id,
        requested_model_id=binding.model_id,
        activated_provider_id=observation.provider_id,
        activated_model_id=observation.model_id,
        binding_digest=expected_binding_digest,
        activated_binding_digest=observation.binding_digest,
        activation_source=activation_source,
        status=status,
        reason=(
            ()
            if status == "matched"
            else (
                reason
                or "runtime provider/model binding differs from the resolved binding",
            )
        ),
        evidence_refs=refs,
    )


def _usage_projection(usage: UsageRecord | None) -> dict[str, int | float | None] | None:
    if usage is None:
        return None
    return {field: getattr(usage, field) for field in _USAGE_FIELDS}


def replay_model_activation_receipt(
    receipt: ModelActivationReceipt,
    *,
    requested_model: str,
    binding: ProviderBinding | None,
    bundle_usage: UsageRecord | None,
    require_usage: bool,
    evidence_path: str | Path | None = None,
) -> tuple[str, ...]:
    """Independently derive activation/status/usage and report every mismatch."""

    errors: list[str] = []
    if not receipt.evidence_refs:
        if receipt.status != "unobserved":
            errors.append("observed model activation lacks replayable native evidence")
        if require_usage:
            errors.append("real-model claim native usage evidence is missing")
        return tuple(errors)
    if len(receipt.evidence_refs) != 1:
        return ("model activation must reference exactly one native evidence artifact",)
    ref = receipt.evidence_refs[0]
    try:
        observation = load_model_activation_evidence(
            ref,
            activation_source=receipt.activation_source,
            path=evidence_path,
        )
    except _UnsubstantiatedModelActivationEvidence as exc:
        observation = ReplayedModelActivation(
            provider_id=None,
            model_id=None,
            binding_digest=None,
            usage=None,
            observed=False,
            reason=str(exc),
        )
    except ModelActivationEvidenceError as exc:
        return (str(exc),)

    if (
        binding is None
        or not observation.observed
        or observation.binding_digest is None
    ):
        expected_status = "unobserved"
        expected_provider_id = None
        expected_model_id = None
        expected_binding_digest = None
    else:
        expected_provider_id = observation.provider_id
        expected_model_id = observation.model_id
        expected_binding_digest = observation.binding_digest
        expected_status = (
            "matched"
            if (expected_provider_id, expected_model_id)
            == (binding.provider_id, binding.model_id)
            and expected_binding_digest == binding.canonical_digest()
            else "mismatch"
        )
    if receipt.status != expected_status:
        errors.append("model activation status is not derived from native evidence")
    if receipt.activated_provider_id != expected_provider_id:
        errors.append("model activation provider is not derived from native evidence")
    if receipt.activated_model_id != expected_model_id:
        errors.append("model activation model is not derived from native evidence")
    if receipt.activated_binding_digest != expected_binding_digest:
        errors.append("model activation binding is not derived from native evidence")
    if receipt.requested_model != requested_model:
        errors.append("model activation requested model drift")

    if require_usage:
        native_usage = observation.usage
        if native_usage is None:
            errors.append("real-model claim native usage evidence is missing")
        else:
            if native_usage.total_tokens is None:
                errors.append("real-model claim native token usage is unobservable")
            if native_usage.cost is None:
                errors.append("real-model claim native cost usage is unobservable")
            expected_usage = native_usage.model_dump(mode="python")
            if _usage_projection(bundle_usage) != expected_usage:
                errors.append("real-model claim usage differs from native activation evidence")
    return tuple(errors)


__all__ = [
    "ACTIVATION_SOURCES",
    "ModelActivationEvidenceError",
    "ReplayedModelActivation",
    "derive_model_activation_receipt",
    "load_model_activation_evidence",
    "parse_model_activation_evidence",
    "replay_model_activation_receipt",
]
