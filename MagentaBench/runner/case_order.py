"""Content-addressed custom case-order activation shared by BMP runtimes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from MagentaBench.schemas import (
    ArtifactRef,
    CaseOrderArtifact,
    CustomCaseOrderSpec,
    ProtocolSpec,
)


CUSTOM_CASE_ORDER_ADAPTER = "magentabench.case-order.json.v1"


class CaseOrderError(ValueError):
    """A custom order declaration or its content does not match its identity."""


def _stable_source(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise CaseOrderError("resolved custom order source must be absolute")
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise CaseOrderError(
                f"custom order source must not contain a symlink: {component}"
            )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CaseOrderError(
            f"custom order source is missing or unreadable: {candidate}"
        ) from exc
    if not resolved.is_file():
        raise CaseOrderError(f"custom order source is not a file: {resolved}")
    return resolved


def load_custom_case_order(
    declaration: CustomCaseOrderSpec,
) -> tuple[CaseOrderArtifact, ArtifactRef]:
    """Read one strategy input and verify its declared content address."""

    if declaration.adapter != CUSTOM_CASE_ORDER_ADAPTER:
        raise CaseOrderError(
            f"custom order adapter is not activated: {declaration.adapter!r}"
        )
    source = _stable_source(declaration.source)
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise CaseOrderError(f"cannot read custom order source: {source}") from exc
    observed_digest = hashlib.sha256(content).hexdigest()
    if len(content) != declaration.size_bytes:
        raise CaseOrderError("custom order source size differs from declaration")
    if observed_digest != declaration.sha256:
        raise CaseOrderError("custom order source digest differs from declaration")
    try:
        artifact = CaseOrderArtifact.model_validate_json(content)
    except ValueError as exc:
        raise CaseOrderError("custom order source is not a valid case-order artifact") from exc
    return artifact, ArtifactRef(
        path=str(source),
        sha256=observed_digest,
        size_bytes=len(content),
    )


def custom_order_binding(
    protocol: ProtocolSpec,
) -> tuple[tuple[str, ...], str, ArtifactRef]:
    """Return the selected ids and immutable strategy identity for a protocol."""

    if protocol.case_order != "custom" or protocol.custom_order is None:
        raise CaseOrderError("protocol does not declare a custom case order")
    artifact, ref = load_custom_case_order(protocol.custom_order)
    return artifact.ordered_case_ids, protocol.custom_order.adapter, ref


def selected_case_ids(protocol: ProtocolSpec) -> tuple[str, ...] | None:
    """Resolve selectors for explicit/custom orders; return None for full sets."""

    if protocol.case_order == "explicit":
        return tuple(protocol.explicit_case_ids)
    if protocol.case_order == "custom":
        ids, _, _ = custom_order_binding(protocol)
        return ids
    return None


__all__ = [
    "CUSTOM_CASE_ORDER_ADAPTER",
    "CaseOrderError",
    "custom_order_binding",
    "load_custom_case_order",
    "selected_case_ids",
]
