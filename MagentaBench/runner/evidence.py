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


def source_closure_digest(
    source_root: Path, refs: tuple[ArtifactRef, ...]
) -> str:
    source = source_root.resolve(strict=True)
    entries = []
    for ref in refs:
        path = Path(ref.path).resolve(strict=True)
        try:
            relative = path.relative_to(source)
        except ValueError as exc:
            raise ValueError(
                f"source content ref escapes declared source: {ref.path}"
            ) from exc
        entries.append(
            {
                "path": relative.as_posix(),
                "size_bytes": ref.size_bytes,
                "sha256": ref.sha256,
            }
        )
    payload = json.dumps(
        sorted(entries, key=lambda item: item["path"]),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
