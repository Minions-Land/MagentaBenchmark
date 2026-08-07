"""Evidence file hashing and atomic persistence helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from MagentaBench.schemas import ArtifactRef


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_ref(path: Path) -> ArtifactRef:
    resolved = path.resolve()
    return ArtifactRef(
        path=str(resolved),
        sha256=sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
    )


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    data = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    atomic_write_bytes(path, data)


__all__ = ["artifact_ref", "atomic_write_bytes", "atomic_write_json", "sha256_file"]
