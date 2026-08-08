"""Content-addressed, secret-free TOML configuration registry.

Named entries are mutable references stored in an atomically replaced index.
The referenced TOML objects are immutable and addressed by their canonical
SHA-256 digest, so updating or deleting a name never rewrites prior content.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


_REGISTRY_FORMAT = "magentabench-configuration-registry-v1"
_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_PARTS = frozenset(
    {
        "key",
        "keys",
        "token",
        "secret",
        "secrets",
        "password",
        "credential",
        "credentials",
    }
)
_NON_SECRET_TOKEN_KEY_PATTERN = re.compile(
    r"^(?:(?:cache|completion|context|generation|input|max|max_context|"
    r"max_generation|output|prompt|total)_)?tokens$"
)
_MIN_TOML_INTEGER = -(2**63)
_MAX_TOML_INTEGER = 2**63 - 1


class ConfigurationRegistryError(ValueError):
    """A configuration declaration or registry state is invalid."""


class ConfigurationNotFoundError(ConfigurationRegistryError):
    """A requested named configuration does not exist."""


class ConfigurationDriftError(ConfigurationRegistryError):
    """Persisted registry paths or bytes no longer match their identity."""


@dataclass(frozen=True)
class ConfigurationRecord:
    """One verified named reference to an immutable TOML object."""

    name: str
    sha256: str
    size_bytes: int
    path: Path
    _toml_bytes: bytes = field(repr=False, compare=False)

    @property
    def digest(self) -> str:
        return self.sha256

    @property
    def data(self) -> dict[str, Any]:
        return tomllib.loads(self._toml_bytes.decode("utf-8"))

    @property
    def configuration(self) -> dict[str, Any]:
        return self.data

    @property
    def toml_bytes(self) -> bytes:
        return bytes(self._toml_bytes)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or _NAME_PATTERN.fullmatch(name) is None:
        raise ConfigurationRegistryError(
            "configuration name must be a normalized identifier"
        )
    return name


def _validate_key(key: object, *, path: str) -> str:
    if not isinstance(key, str) or not key:
        raise ConfigurationRegistryError(
            f"configuration key at {path} must be a non-empty string"
        )
    normalized_key = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key)
    normalized_key = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized_key
    ).replace("-", "_")
    normalized_key = normalized_key.lower()
    key_parts = frozenset(part for part in normalized_key.split("_") if part)
    secret_token_key = (
        "tokens" in key_parts
        and _NON_SECRET_TOKEN_KEY_PATTERN.fullmatch(normalized_key) is None
    )
    if key_parts.intersection(_SECRET_KEY_PARTS) or secret_token_key:
        raise ConfigurationRegistryError(
            f"configuration must not contain secret-like key {key!r} at {path}"
        )
    return key


def _normalize_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = _validate_key(raw_key, path=path)
            child_path = f"{path}.{key}" if path else key
            result[key] = _normalize_value(item, path=child_path)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _normalize_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not _MIN_TOML_INTEGER <= value <= _MAX_TOML_INTEGER:
            raise ConfigurationRegistryError(
                f"integer at {path} is outside the TOML 64-bit range"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigurationRegistryError(
                f"non-finite float at {path} is not accepted"
            )
        return value
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    if isinstance(value, time):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ConfigurationRegistryError(
                f"time at {path} must not contain an offset"
            )
        return value
    if isinstance(value, str):
        return value
    raise ConfigurationRegistryError(
        f"unsupported TOML value at {path}: {type(value).__name__}"
    )


def validate_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached TOML-compatible value after recursive validation."""

    if not isinstance(configuration, Mapping):
        raise ConfigurationRegistryError("configuration root must be a mapping")
    normalized = _normalize_value(configuration, path="")
    assert isinstance(normalized, dict)
    return normalized


def _toml_key(key: str) -> str:
    return json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, Mapping):
        items = ", ".join(
            f"{_toml_key(key)} = {_toml_value(value[key])}"
            for key in sorted(value)
        )
        return "{}" if not items else "{ " + items + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise AssertionError(f"unvalidated TOML value: {type(value).__name__}")


def canonical_toml_bytes(configuration: Mapping[str, Any]) -> bytes:
    """Encode one mapping into deterministic, round-trippable TOML bytes."""

    normalized = validate_configuration(configuration)
    lines = [
        f"{_toml_key(key)} = {_toml_value(normalized[key])}"
        for key in sorted(normalized)
    ]
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    try:
        restored = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationRegistryError(
            "configuration cannot be encoded as canonical TOML"
        ) from exc
    if restored != normalized:
        raise ConfigurationRegistryError(
            "canonical TOML round-trip changed the configuration"
        )
    return payload


def _document_bytes(document: Mapping[str, Any] | str | bytes) -> bytes:
    if isinstance(document, Mapping):
        return canonical_toml_bytes(document)
    if isinstance(document, bytes):
        try:
            text = document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigurationRegistryError(
                "configuration TOML must be UTF-8"
            ) from exc
    elif isinstance(document, str):
        text = document
    else:
        raise ConfigurationRegistryError(
            "configuration must be a mapping, TOML string, or TOML bytes"
        )
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationRegistryError("configuration TOML is malformed") from exc
    return canonical_toml_bytes(parsed)


def deep_merge(
    base: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, Any]:
    """Recursively merge mappings without mutating either input."""

    left = validate_configuration(base)
    right = validate_configuration(override)

    def merge(
        left_value: dict[str, Any], right_value: dict[str, Any]
    ) -> dict[str, Any]:
        result = {
            key: _normalize_value(value, path=key)
            for key, value in left_value.items()
        }
        for key, value in right_value.items():
            current = result.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                result[key] = merge(current, value)
            else:
                result[key] = _normalize_value(value, path=key)
        return result

    return merge(left, right)


def _parse_dotted_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path:
        raise ConfigurationRegistryError("override path must be non-empty")
    parts = tuple(path.split("."))
    if any(_PATH_SEGMENT_PATTERN.fullmatch(part) is None for part in parts):
        raise ConfigurationRegistryError(
            f"override path must contain normalized dotted segments: {path!r}"
        )
    for part in parts:
        _validate_key(part, path=path)
    return parts


def apply_dotted_overrides(
    configuration: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply non-overlapping dotted-path replacements to a detached mapping."""

    result = validate_configuration(configuration)
    if not isinstance(overrides, Mapping):
        raise ConfigurationRegistryError("overrides must be a dotted-path mapping")
    parsed: list[tuple[tuple[str, ...], Any]] = []
    for path, value in overrides.items():
        parts = _parse_dotted_path(path)
        parsed.append((parts, _normalize_value(value, path=path)))
    parsed.sort(key=lambda item: item[0])
    for index, (parts, _) in enumerate(parsed):
        for other_parts, _ in parsed[index + 1 :]:
            if other_parts[: len(parts)] == parts:
                raise ConfigurationRegistryError(
                    "override paths must not overlap: "
                    f"{'.'.join(parts)!r} and {'.'.join(other_parts)!r}"
                )
    for parts, value in parsed:
        current = result
        for position, part in enumerate(parts[:-1]):
            child = current.get(part)
            if child is None:
                child = {}
                current[part] = child
            if not isinstance(child, dict):
                prefix = ".".join(parts[: position + 1])
                raise ConfigurationRegistryError(
                    f"override path traverses non-table value at {prefix!r}"
                )
            current = child
        current[parts[-1]] = value
    return validate_configuration(result)


def apply_dotted_override(
    configuration: Mapping[str, Any], path: str, value: Any
) -> dict[str, Any]:
    return apply_dotted_overrides(configuration, {path: value})


def merge_configurations(
    base: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, Any]:
    return deep_merge(base, override)


def apply_overrides(
    configuration: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    return apply_dotted_overrides(configuration, overrides)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ConfigurationDriftError(f"refusing to replace symlink: {path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ConfigurationDriftError(
            f"atomic write parent is not a stable directory: {path.parent}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _path_identity(path: Path) -> tuple[int, int]:
    stat = path.stat(follow_symlinks=False)
    return stat.st_dev, stat.st_ino


def _reject_symlink_ancestors(path: Path) -> None:
    candidates = (path, *path.parents)
    for candidate in candidates:
        if candidate.is_symlink():
            raise ConfigurationDriftError(
                f"configuration registry path contains a symlink: {candidate}"
            )


class ConfigurationRegistry:
    """Named references over immutable canonical TOML objects."""

    def __init__(self, root: str | Path) -> None:
        expanded = Path(root).expanduser()
        self._root = Path(os.path.abspath(os.fspath(expanded)))
        self._objects = self._root / "objects"
        self._index = self._root / "index.json"
        self._lock = threading.RLock()

        _reject_symlink_ancestors(self._root)
        if self._root.exists() and not self._root.is_dir():
            raise ConfigurationDriftError(
                f"configuration registry root is not a directory: {self._root}"
            )
        self._root.mkdir(parents=True, exist_ok=True)
        if self._objects.is_symlink():
            raise ConfigurationDriftError(
                f"configuration object path is a symlink: {self._objects}"
            )
        if self._objects.exists() and not self._objects.is_dir():
            raise ConfigurationDriftError(
                f"configuration object path is not a directory: {self._objects}"
            )
        self._objects.mkdir(exist_ok=True)
        self._root_identity = _path_identity(self._root)
        self._objects_identity = _path_identity(self._objects)
        self._assert_layout()

    @property
    def root(self) -> Path:
        return self._root

    def _assert_layout(self) -> None:
        if self._root.is_symlink() or not self._root.is_dir():
            raise ConfigurationDriftError("configuration registry root path drift")
        if _path_identity(self._root) != self._root_identity:
            raise ConfigurationDriftError("configuration registry root path drift")
        if self._objects.is_symlink() or not self._objects.is_dir():
            raise ConfigurationDriftError("configuration object directory path drift")
        if _path_identity(self._objects) != self._objects_identity:
            raise ConfigurationDriftError("configuration object directory path drift")
        if self._index.is_symlink():
            raise ConfigurationDriftError("configuration index is a symlink")
        if self._index.exists() and not self._index.is_file():
            raise ConfigurationDriftError("configuration index path drift")

    def _load_index(self) -> dict[str, dict[str, int | str]]:
        self._assert_layout()
        if not self._index.exists():
            return {}

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ConfigurationDriftError(
                        f"configuration index contains duplicate key {key!r}"
                    )
                result[key] = value
            return result

        try:
            payload = json.loads(
                self._index.read_text(encoding="utf-8"),
                object_pairs_hook=reject_duplicates,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationDriftError(
                "configuration index is unreadable or malformed"
            ) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"format", "entries"}
            or payload.get("format") != _REGISTRY_FORMAT
            or not isinstance(payload.get("entries"), dict)
        ):
            raise ConfigurationDriftError("configuration index contract drift")
        result: dict[str, dict[str, int | str]] = {}
        for raw_name, raw_entry in payload["entries"].items():
            try:
                name = _validate_name(raw_name)
            except ConfigurationRegistryError as exc:
                raise ConfigurationDriftError(
                    "configuration index contains an invalid name"
                ) from exc
            if not isinstance(raw_entry, dict) or set(raw_entry) != {
                "sha256",
                "size_bytes",
            }:
                raise ConfigurationDriftError(
                    f"configuration index entry contract drift: {name}"
                )
            digest = raw_entry["sha256"]
            size = raw_entry["size_bytes"]
            if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
                raise ConfigurationDriftError(
                    f"configuration index digest drift: {name}"
                )
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ConfigurationDriftError(
                    f"configuration index size drift: {name}"
                )
            result[name] = {"sha256": digest, "size_bytes": size}
        return result

    def _write_index(self, entries: Mapping[str, Mapping[str, int | str]]) -> None:
        payload = {
            "format": _REGISTRY_FORMAT,
            "entries": {
                name: {
                    "sha256": entries[name]["sha256"],
                    "size_bytes": entries[name]["size_bytes"],
                }
                for name in sorted(entries)
            },
        }
        data = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        self._assert_layout()
        _atomic_write_bytes(self._index, data)
        self._assert_layout()

    def _object_path(self, digest: str) -> Path:
        return self._objects / f"{digest}.toml"

    def _verify_object(
        self, name: str, entry: Mapping[str, int | str]
    ) -> ConfigurationRecord:
        digest = str(entry["sha256"])
        size = int(entry["size_bytes"])
        path = self._object_path(digest)
        if path.is_symlink():
            raise ConfigurationDriftError(
                f"configuration object is a symlink: {name}"
            )
        if not path.is_file():
            raise ConfigurationDriftError(
                f"configuration object is missing or not a file: {name}"
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ConfigurationDriftError(
                f"configuration object is unreadable: {name}"
            ) from exc
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise ConfigurationDriftError(
                f"configuration object content drift: {name}"
            )
        try:
            parsed = tomllib.loads(content.decode("utf-8"))
            canonical = canonical_toml_bytes(parsed)
        except (UnicodeDecodeError, ConfigurationRegistryError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationDriftError(
                f"configuration object TOML drift: {name}"
            ) from exc
        if canonical != content:
            raise ConfigurationDriftError(
                f"configuration object is not canonical TOML: {name}"
            )
        return ConfigurationRecord(
            name=name,
            sha256=digest,
            size_bytes=size,
            path=path,
            _toml_bytes=content,
        )

    def list(self) -> tuple[ConfigurationRecord, ...]:
        with self._lock:
            entries = self._load_index()
            return tuple(
                self._verify_object(name, entries[name]) for name in sorted(entries)
            )

    def get(self, name: str) -> ConfigurationRecord:
        normalized_name = _validate_name(name)
        with self._lock:
            entries = self._load_index()
            entry = entries.get(normalized_name)
            if entry is None:
                raise ConfigurationNotFoundError(
                    f"configuration does not exist: {normalized_name}"
                )
            return self._verify_object(normalized_name, entry)

    def upsert(
        self,
        name: str,
        configuration: Mapping[str, Any] | str | bytes,
    ) -> ConfigurationRecord:
        normalized_name = _validate_name(name)
        content = _document_bytes(configuration)
        digest = hashlib.sha256(content).hexdigest()
        entry: dict[str, int | str] = {
            "sha256": digest,
            "size_bytes": len(content),
        }
        with self._lock:
            entries = self._load_index()
            object_path = self._object_path(digest)
            if object_path.is_symlink():
                raise ConfigurationDriftError(
                    f"configuration object is a symlink: {normalized_name}"
                )
            if object_path.exists():
                try:
                    observed_content = object_path.read_bytes()
                except OSError as exc:
                    raise ConfigurationDriftError(
                        "existing content-addressed configuration object is unreadable"
                    ) from exc
                if not object_path.is_file() or observed_content != content:
                    raise ConfigurationDriftError(
                        "existing content-addressed configuration object drift"
                    )
            else:
                _atomic_write_bytes(object_path, content)
            current = entries.get(normalized_name)
            if current != entry:
                entries[normalized_name] = entry
                self._write_index(entries)
            return self._verify_object(normalized_name, entry)

    def delete(self, name: str) -> bool:
        normalized_name = _validate_name(name)
        with self._lock:
            entries = self._load_index()
            entry = entries.get(normalized_name)
            if entry is None:
                return False
            self._verify_object(normalized_name, entry)
            del entries[normalized_name]
            self._write_index(entries)
            return True

    def list_configurations(self) -> tuple[ConfigurationRecord, ...]:
        return self.list()

    def get_configuration(self, name: str) -> ConfigurationRecord:
        return self.get(name)

    def upsert_configuration(
        self,
        name: str,
        configuration: Mapping[str, Any] | str | bytes,
    ) -> ConfigurationRecord:
        return self.upsert(name, configuration)

    def delete_configuration(self, name: str) -> bool:
        return self.delete(name)


__all__ = [
    "ConfigurationDriftError",
    "ConfigurationNotFoundError",
    "ConfigurationRecord",
    "ConfigurationRegistry",
    "ConfigurationRegistryError",
    "apply_dotted_override",
    "apply_dotted_overrides",
    "apply_overrides",
    "canonical_toml_bytes",
    "deep_merge",
    "merge_configurations",
    "validate_configuration",
]
