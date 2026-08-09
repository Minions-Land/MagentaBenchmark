"""Provider/model activation receipts for execution adapters and Pipeline."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Iterable

from MagentaBench.schemas import ArtifactRef, ModelActivationReceipt
from MagentaBench.schemas.model_activation import derive_model_activation_receipt

from .compiler import CompiledRun
from .evidence import atomic_write_json, sha256_file

if TYPE_CHECKING:
    from .backend.fake import CaseExecution


NONE_MODELS = frozenset({"none", "none/deterministic", "none/echo"})


def is_none_model(model: str) -> bool:
    """Return whether ``model`` selects a provider-free execution sentinel."""

    return model in NONE_MODELS


def declared_model_activation_source(run: CompiledRun) -> str | None:
    """Return the single digest-bound activation source selected at compile time."""

    sources = {
        artifact.capability.model_activation_source
        for artifact in run.manifest.metadata.adapter_capabilities
        if artifact.capability.adapter_kind == "execution"
        and artifact.capability.model_activation_source is not None
    }
    if len(sources) > 1:
        raise ValueError("resolved execution capabilities disagree on model activation")
    return next(iter(sources), None)


def make_model_activation_receipt(
    run: CompiledRun,
    *,
    activated_provider_id: str | None = None,
    activated_model_id: str | None = None,
    evidence_refs: Iterable[ArtifactRef] = (),
    activation_source: str | None = None,
    reason: str | None = None,
) -> ModelActivationReceipt:
    """Build a receipt by replaying one adapter-native evidence artifact.

    Provider/model scalars are accepted only as a compatibility cross-check;
    the selected capability parser remains authoritative and derives identity
    from the retained evidence bytes.
    """

    execution = run.manifest.execution
    if is_none_model(execution.model):
        raise ValueError("none-model sentinels do not use ModelActivationReceipt")
    declared_source = declared_model_activation_source(run)
    if (
        activation_source is not None
        and declared_source is not None
        and activation_source != declared_source
    ):
        raise ValueError("model activation source differs from selected capability")
    source = activation_source or declared_source
    if source not in {
        "provider_response",
        "runtime_manifest",
        "native_result",
        "adapter_receipt",
    }:
        raise ValueError("real-model execution lacks a declared activation source")
    refs = tuple(evidence_refs)
    if len(refs) > 1:
        raise ValueError(
            "model activation requires exactly one replayable native evidence ref"
        )
    receipt = derive_model_activation_receipt(
        requested_model=execution.model,
        binding=execution.provider_binding,
        activation_source=source,
        evidence_ref=(None if not refs else refs[0]),
        reason=reason,
    )
    if activated_provider_id is not None and receipt.activated_provider_id != activated_provider_id:
        raise ValueError("caller activation provider disagrees with native evidence")
    if activated_model_id is not None and receipt.activated_model_id != activated_model_id:
        raise ValueError("caller activation model disagrees with native evidence")
    return receipt


def bind_model_activation(
    execution: CaseExecution,
    receipt: ModelActivationReceipt,
) -> CaseExecution:
    """Persist ``receipt`` into one already-materialized evidence bundle."""

    provenance = execution.bundle.provenance.model_copy(
        update={"model_activation": receipt}
    )
    bundle = execution.bundle.model_copy(update={"provenance": provenance})
    atomic_write_json(execution.bundle_path, bundle)
    return replace(
        execution,
        bundle=bundle,
        bundle_digest=sha256_file(execution.bundle_path),
    )


def ensure_model_activation_receipt(
    run: CompiledRun,
    execution: CaseExecution,
) -> CaseExecution:
    """Fail visibly when a real-model adapter omitted activation evidence.

    Exploratory Pipeline execution is intentionally preserved: an adapter that
    does not expose a native receipt gets an explicit ``unobserved`` record and
    reaches report generation.  Claim/isolation gates then fail closed.  This
    is preferable to losing the provider call and its failure artifacts.
    """

    if is_none_model(run.manifest.execution.model):
        return execution
    if execution.bundle.provenance.model_activation is not None:
        return execution
    receipt = make_model_activation_receipt(
        run,
        reason="execution adapter omitted ModelActivationReceipt",
    )
    return bind_model_activation(execution, receipt)


__all__ = [
    "NONE_MODELS",
    "bind_model_activation",
    "declared_model_activation_source",
    "ensure_model_activation_receipt",
    "is_none_model",
    "make_model_activation_receipt",
]
