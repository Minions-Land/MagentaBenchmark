"""Content-addressed lock catalogs for TOML registry declarations.

The compiler already binds declarations used by one manifest.  This module
adds a repository-level catalog so CI or a release process can assert that the
whole registry tree is the one that was reviewed.  The lock file itself is
excluded from the catalog and can therefore be regenerated deterministically.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import SHA256_PATTERN, StrictModel

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_REGISTRY_SECTIONS = frozenset(
    {
        "adapter",
        "backend",
        "benchmark",
        "configuration",
        "dataset",
        "evaluator",
        "evolver",
        "factor",
        "meta_evolver",
        "metric",
        "protocol",
        "subject",
    }
)


def _entries_digest(entries: tuple["RegistryLockEntry", ...]) -> str:
    payload = [entry.model_dump(mode="json") for entry in entries]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RegistryLockError(ValueError):
    """The registry tree does not match its lock catalog."""


class RegistryLockEntry(StrictModel):
    """One raw TOML declaration in a lock catalog."""

    path: str = Field(min_length=1)
    section: str = Field(min_length=1)
    id: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def path_and_identity_are_canonical(self) -> "RegistryLockEntry":
        normalized = self.path.replace("\\", "/")
        if normalized != self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("registry lock paths must be relative POSIX paths")
        if self.section not in _REGISTRY_SECTIONS:
            raise ValueError(f"unknown registry section: {self.section!r}")
        if _ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError(f"invalid registry id: {self.id!r}")
        return self


class RegistryLockCatalog(StrictModel):
    """Immutable catalog for every declaration below one registry root."""

    format: Literal["magentabench-registry-lock-v1"] = (
        "magentabench-registry-lock-v1"
    )
    entries: tuple[RegistryLockEntry, ...]
    catalog_digest: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def catalog_is_sorted_and_bound(self) -> "RegistryLockCatalog":
        keys = [(entry.path, entry.section, entry.id) for entry in self.entries]
        if keys != sorted(keys):
            raise ValueError("registry lock entries must be sorted")
        if len({entry.path for entry in self.entries}) != len(self.entries):
            raise ValueError("registry lock paths must be unique")
        if self.catalog_digest != _entries_digest(self.entries):
            raise ValueError("registry lock catalog digest drift")
        return self


def _parse_declaration(path: Path) -> tuple[str, str]:
    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RegistryLockError(f"cannot parse registry declaration {path}: {exc}") from exc
    roots = set(document)
    if len(roots) != 1 or next(iter(roots), None) not in _REGISTRY_SECTIONS:
        raise RegistryLockError(
            f"registry declaration {path} must contain exactly one known section"
        )
    section = next(iter(roots))
    value = document[section]
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise RegistryLockError(f"registry declaration {path} has no string id")
    return section, value["id"]


def build_registry_lock(registry_root: str | Path) -> RegistryLockCatalog:
    """Scan and digest all ``*.toml`` declarations under ``registry_root``."""

    root = Path(registry_root).resolve(strict=True)
    entries: list[RegistryLockEntry] = []
    for path in sorted(root.rglob("*.toml")):
        if path.name == "registry.lock.toml":
            continue
        if not path.is_file() or path.is_symlink():
            raise RegistryLockError(f"registry declaration is not a regular file: {path}")
        section, entry_id = _parse_declaration(path)
        data = path.read_bytes()
        entries.append(
            RegistryLockEntry(
                path=path.relative_to(root).as_posix(),
                section=section,
                id=entry_id,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    ordered = tuple(sorted(entries, key=lambda entry: (entry.path, entry.section, entry.id)))
    return RegistryLockCatalog(entries=ordered, catalog_digest=_entries_digest(ordered))


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_registry_lock(
    registry_root: str | Path,
    lock_path: str | Path | None = None,
) -> Path:
    """Write a deterministic TOML lock catalog and return its path."""

    root = Path(registry_root).resolve(strict=True)
    destination = (
        root / "registry.lock.toml"
        if lock_path is None
        else Path(lock_path).resolve()
    )
    catalog = build_registry_lock(root)
    lines = [
        "[registry]",
        f"format = {_toml_string(catalog.format)}",
        f"catalog_digest = {_toml_string(catalog.catalog_digest)}",
        "",
    ]
    for entry in catalog.entries:
        lines.extend(
            [
                "[[registry.entries]]",
                f"path = {_toml_string(entry.path)}",
                f"section = {_toml_string(entry.section)}",
                f"id = {_toml_string(entry.id)}",
                f"sha256 = {_toml_string(entry.sha256)}",
                f"size_bytes = {entry.size_bytes}",
                "",
            ]
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def load_registry_lock(path: str | Path) -> RegistryLockCatalog:
    """Parse a strict ``registry.lock.toml`` file."""

    source = Path(path)
    try:
        document = tomllib.loads(source.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RegistryLockError(f"cannot parse registry lock {source}: {exc}") from exc
    if set(document) != {"registry"} or not isinstance(document["registry"], dict):
        raise RegistryLockError("registry lock must contain only [registry]")
    table = document["registry"]
    try:
        return RegistryLockCatalog.model_validate(table)
    except ValueError as exc:
        raise RegistryLockError(f"invalid registry lock {source}: {exc}") from exc


def verify_registry_lock(
    registry_root: str | Path,
    lock_path: str | Path | None = None,
) -> RegistryLockCatalog:
    """Fail closed when the registry tree differs from its lock catalog."""

    root = Path(registry_root).resolve(strict=True)
    source = root / "registry.lock.toml" if lock_path is None else Path(lock_path)
    expected = load_registry_lock(source)
    observed = build_registry_lock(root)
    if observed != expected:
        expected_map = {entry.path: entry for entry in expected.entries}
        observed_map = {entry.path: entry for entry in observed.entries}
        missing = sorted(set(expected_map) - set(observed_map))
        extra = sorted(set(observed_map) - set(expected_map))
        changed = sorted(
            path
            for path in set(expected_map) & set(observed_map)
            if expected_map[path] != observed_map[path]
        )
        details = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        if changed:
            details.append(f"changed={changed}")
        if expected.catalog_digest != observed.catalog_digest:
            details.append("catalog_digest drift")
        raise RegistryLockError("registry lock verification failed: " + "; ".join(details))
    return expected


__all__ = [
    "RegistryLockCatalog",
    "RegistryLockEntry",
    "RegistryLockError",
    "build_registry_lock",
    "load_registry_lock",
    "verify_registry_lock",
    "write_registry_lock",
]
