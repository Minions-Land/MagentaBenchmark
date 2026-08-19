"""Provider-neutral, fail-closed materialization of external evidence.

This module deliberately stops at byte materialization.  It does not fetch a
provider on its own, invoke a benchmark verifier, or create ledger rows.  A
caller supplies a small fetcher for a validated :class:`ExternalLocator` and
the materializer verifies every declared file before writing a redacted
receipt.  This keeps public evidence portable while leaving provider
authentication and benchmark-specific semantics at their existing boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
from typing import Callable, Mapping
from urllib.parse import urlsplit


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_CREDENTIAL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MANIFEST_FORMAT = "magentabench-external-evidence-manifest-v1"
_RECEIPT_FORMAT = "magentabench-external-materialization-receipt-v1"
_RECEIPT_NAME = "MATERIALIZATION_RECEIPT.json"
_SECRET_VALUE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN .*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
    r"\s*[:=]\s*[^\s]{4,})",
    re.IGNORECASE,
)


class ExternalEvidenceError(ValueError):
    """A malformed, unsafe, unavailable, or drifted external artifact."""


def _fail(message: str) -> ExternalEvidenceError:
    # Never include a locator, credential name, or fetched content in errors.
    return ExternalEvidenceError(message)


def _string(value: object, field: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise _fail(f"{field} must be a non-empty string")
    if any(ord(character) < 0x20 for character in value):
        raise _fail(f"{field} contains control characters")
    return value


def _safe_public_value(value: object, field: str, *, max_length: int = 4096) -> str:
    result = _string(value, field, max_length=max_length)
    if _SECRET_VALUE_RE.search(result):
        raise _fail(f"{field} contains secret-like material")
    return result


def _known_fields(
    value: Mapping[str, object], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _fail(f"{label} has unsupported fields")


def _validate_format(value: object | None) -> None:
    if value is not None and value != _MANIFEST_FORMAT:
        raise _fail("materialization spec format is not supported")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def load_external_evidence_spec(path: Path) -> ExternalEvidenceSpec:
    """Load one strict JSON manifest without accepting duplicate keys."""

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise _fail("materialization manifest contains duplicate keys")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise _fail("materialization manifest contains a non-finite number")

    try:
        raw = Path(path).read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise _fail("materialization manifest exceeds the size limit")
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except ExternalEvidenceError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("materialization manifest is unreadable or malformed") from exc
    if not isinstance(decoded, Mapping):
        raise _fail("materialization manifest must be an object")
    return ExternalEvidenceSpec.from_mapping(decoded)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _fail("materialized artifact is unavailable") from exc
    return digest.hexdigest()


def _validate_absolute_prefix(value: object, field: str) -> str:
    result = _string(value, field, max_length=4096)
    path = PurePosixPath(result)
    if not path.is_absolute() or result.startswith("//") or ".." in path.parts:
        raise _fail(f"{field} must be a normalized absolute POSIX prefix")
    if "\\" in result or "/./" in result or result.endswith("/."):
        raise _fail(f"{field} must be a normalized absolute POSIX prefix")
    normalized = os.path.normpath(result).replace(os.sep, "/")
    if normalized != result:
        raise _fail(f"{field} must be a normalized absolute POSIX prefix")
    return result


def _safe_destination(value: object) -> str:
    result = _string(value, "destination", max_length=1024)
    if "\\" in result:
        raise _fail("destination must use POSIX separators")
    path = PurePosixPath(result)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise _fail("destination must be relative and cannot traverse")
    if any(part in ("", ".") for part in path.parts):
        raise _fail("destination must be normalized")
    normalized = path.as_posix()
    if normalized != result:
        raise _fail("destination must be normalized")
    if normalized == _RECEIPT_NAME:
        raise _fail("destination is reserved for the materialization receipt")
    return normalized


def _validate_locator_uri(locator: str) -> None:
    parts = urlsplit(locator)
    if not parts.scheme or parts.scheme.casefold() == "file":
        raise _fail("locator must use a non-file URI scheme")
    if not parts.netloc:
        raise _fail("locator must be an absolute provider URI")
    if parts.username or parts.password:
        raise _fail("locator cannot contain embedded credentials")
    # Query-bearing locators are too easy to turn into signed URLs.  Public
    # immutable identity belongs in ``revision`` instead.
    if parts.query or parts.fragment:
        raise _fail("locator cannot contain query or fragment data")
    if _SECRET_VALUE_RE.search(locator):
        raise _fail("locator contains secret-like material")


@dataclass(frozen=True)
class ExternalLocator:
    """Public locator metadata; credential values are intentionally absent."""

    provider: str
    locator: str
    revision: str | None = None
    credential_names: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ExternalLocator":
        if not isinstance(value, Mapping):
            raise _fail("locator must be an object")
        _known_fields(
            value,
            frozenset({"provider", "locator", "revision", "credential_names"}),
            "locator",
        )
        provider = _string(value.get("provider"), "locator.provider", max_length=64)
        if not _NAME_RE.fullmatch(provider):
            raise _fail("locator.provider has an invalid name")
        locator = _safe_public_value(value.get("locator"), "locator.locator")
        _validate_locator_uri(locator)
        revision_value = value.get("revision")
        revision = None
        if revision_value is not None:
            revision = _safe_public_value(
                revision_value, "locator.revision", max_length=256
            )
        names_value = value.get("credential_names", ())
        if not isinstance(names_value, (list, tuple)):
            raise _fail("locator.credential_names must be a list")
        names: list[str] = []
        for name in names_value:
            if not isinstance(name, str) or not _CREDENTIAL_NAME_RE.fullmatch(name):
                raise _fail("locator credential entries must be names only")
            if name in names:
                raise _fail("locator credential names must be unique")
            names.append(name)
        return cls(provider, locator, revision, tuple(names))

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": self.provider,
            "locator": self.locator,
            "credential_names": list(self.credential_names),
        }
        if self.revision is not None:
            result["revision"] = self.revision
        return result


# A descriptive alias for callers that prefer the full name.
ExternalEvidenceLocator = ExternalLocator


@dataclass(frozen=True)
class EvidenceFile:
    destination: str
    locator: ExternalLocator
    size_bytes: int
    sha256: str
    role: str = "evidence"

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "EvidenceFile":
        if not isinstance(value, Mapping):
            raise _fail("file entry must be an object")
        _known_fields(
            value,
            frozenset({"destination", "locator", "role", "sha256", "size_bytes"}),
            "file entry",
        )
        destination = _safe_destination(value.get("destination"))
        locator = ExternalLocator.from_mapping(value.get("locator"))  # type: ignore[arg-type]
        size = value.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _fail("file.size_bytes must be a non-negative integer")
        sha256 = _string(value.get("sha256"), "file.sha256", max_length=64).lower()
        if not SHA256_RE.fullmatch(sha256):
            raise _fail("file.sha256 must be a lowercase SHA-256 digest")
        role = value.get("role", "evidence")
        role = _safe_public_value(role, "file.role", max_length=80)
        if not _NAME_RE.fullmatch(role):
            raise _fail("file.role has an invalid name")
        return cls(destination, locator, size, sha256, role)

    def as_dict(self) -> dict[str, object]:
        return {
            "destination": self.destination,
            "locator": self.locator.as_dict(),
            "role": self.role,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


ExternalEvidenceFile = EvidenceFile


@dataclass(frozen=True)
class RelocationMap:
    """A caller-supplied absolute-prefix replacement, never applied implicitly."""

    old_prefix: str
    new_prefix: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "RelocationMap":
        if not isinstance(value, Mapping):
            raise _fail("relocation map must be an object")
        _known_fields(value, frozenset({"old_prefix", "new_prefix"}), "relocation map")
        return cls(
            _validate_absolute_prefix(value.get("old_prefix"), "relocation.old_prefix"),
            _validate_absolute_prefix(value.get("new_prefix"), "relocation.new_prefix"),
        )

    def redacted(self) -> dict[str, object]:
        return {
            "old_prefix": "<redacted-absolute-prefix>",
            "new_prefix": "<redacted-absolute-prefix>",
            "old_prefix_sha256": _sha256_bytes(self.old_prefix.encode("utf-8")),
            "new_prefix_sha256": _sha256_bytes(self.new_prefix.encode("utf-8")),
        }


@dataclass(frozen=True)
class ExternalEvidenceSpec:
    source_id: str
    candidate: str
    license_id: str
    files: tuple[EvidenceFile, ...]
    relocation_maps: tuple[RelocationMap, ...] = ()
    non_claim: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ExternalEvidenceSpec":
        if not isinstance(value, Mapping):
            raise _fail("materialization spec must be an object")
        _known_fields(
            value,
            frozenset(
                {
                    "candidate",
                    "files",
                    "format",
                    "license_id",
                    "non_claim",
                    "relocation_maps",
                    "source_id",
                }
            ),
            "materialization spec",
        )
        _validate_format(value.get("format"))
        source_id = _safe_public_value(
            value.get("source_id"), "source_id", max_length=160
        )
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise _fail("source_id has an invalid name")
        candidate = _safe_public_value(
            value.get("candidate"), "candidate", max_length=240
        )
        license_id = _safe_public_value(
            value.get("license_id"), "license_id", max_length=120
        )
        files_value = value.get("files")
        if not isinstance(files_value, (list, tuple)) or not files_value:
            raise _fail("materialization spec requires at least one file")
        files = tuple(EvidenceFile.from_mapping(item) for item in files_value)
        destinations = [item.destination for item in files]
        if len({item.casefold() for item in destinations}) != len(destinations):
            raise _fail("file destinations must be unique")
        maps_value = value.get("relocation_maps", ())
        if not isinstance(maps_value, (list, tuple)):
            raise _fail("relocation_maps must be a list")
        relocation_maps = tuple(RelocationMap.from_mapping(item) for item in maps_value)
        prefixes = [item.old_prefix for item in relocation_maps]
        if len(set(prefixes)) != len(prefixes):
            raise _fail("relocation old prefixes must be unique")
        non_claim = value.get("non_claim", True)
        if not isinstance(non_claim, bool) or not non_claim:
            raise _fail("external materialization must remain non_claim")
        return cls(source_id, candidate, license_id, files, relocation_maps, True)

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "files": [item.as_dict() for item in self.files],
            "format": _MANIFEST_FORMAT,
            "license_id": self.license_id,
            "non_claim": True,
            "relocation_maps": [item.redacted() for item in self.relocation_maps],
            "source_id": self.source_id,
        }


ExternalEvidenceManifest = ExternalEvidenceSpec


def _validated_spec(
    spec: ExternalEvidenceSpec | Mapping[str, object],
) -> ExternalEvidenceSpec:
    if not isinstance(spec, ExternalEvidenceSpec):
        return ExternalEvidenceSpec.from_mapping(spec)
    # Public dataclasses are convenient for adapters, but direct construction
    # must not bypass the strict mapping validators at the trust boundary.
    return ExternalEvidenceSpec.from_mapping(
        {
            "candidate": spec.candidate,
            "files": [item.as_dict() for item in spec.files],
            "format": _MANIFEST_FORMAT,
            "license_id": spec.license_id,
            "non_claim": spec.non_claim,
            "relocation_maps": [
                {
                    "old_prefix": item.old_prefix,
                    "new_prefix": item.new_prefix,
                }
                for item in spec.relocation_maps
            ],
            "source_id": spec.source_id,
        }
    )


def _validated_maps(
    relocation_maps: tuple[RelocationMap, ...] | list[RelocationMap],
) -> tuple[RelocationMap, ...]:
    if not isinstance(relocation_maps, (list, tuple)):
        raise _fail("relocation maps must be a list or tuple")
    return tuple(
        RelocationMap.from_mapping(
            {"old_prefix": item.old_prefix, "new_prefix": item.new_prefix}
        )
        if isinstance(item, RelocationMap)
        else RelocationMap.from_mapping(item)
        for item in relocation_maps
    )


def apply_relocation(
    recorded_path: str,
    relocation_maps: tuple[RelocationMap, ...] | list[RelocationMap],
) -> Path:
    """Map one recorded absolute path through the longest explicit prefix.

    No map means no relocation.  The function is intentionally separate from
    fetch/materialization so callers pass the same explicit map to their
    standalone verifier rather than silently rewriting report content.
    """

    recorded = _validate_absolute_prefix(recorded_path, "recorded_path")
    relocation_maps = _validated_maps(relocation_maps)
    matches = [
        item
        for item in relocation_maps
        if recorded == item.old_prefix
        or item.old_prefix == "/"
        or recorded.startswith(item.old_prefix + "/")
    ]
    if not matches:
        return Path(recorded)
    selected = max(matches, key=lambda item: len(item.old_prefix))
    suffix = recorded[len(selected.old_prefix) :].lstrip("/")
    if any(part in {"", ".", ".."} for part in suffix.split("/") if part):
        raise _fail("recorded path suffix is not normalized")
    return Path(selected.new_prefix) / suffix


def relocation_path_map(
    relocation_maps: tuple[RelocationMap, ...] | list[RelocationMap],
) -> dict[str, str]:
    """Return the verifier-ready map after validating duplicate source roots."""

    result: dict[str, str] = {}
    for item in _validated_maps(relocation_maps):
        if item.old_prefix in result:
            raise _fail("relocation old prefixes must be unique")
        result[item.old_prefix] = item.new_prefix
    return result


@dataclass(frozen=True)
class MaterializationReceipt:
    root: Path
    source_id: str
    candidate: str
    license_id: str
    files: tuple[EvidenceFile, ...]
    relocation_maps: tuple[RelocationMap, ...]
    receipt_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "files": [item.as_dict() for item in self.files],
            "format": _RECEIPT_FORMAT,
            "license_id": self.license_id,
            "non_claim": True,
            "relocation_maps": [item.redacted() for item in self.relocation_maps],
            "source_id": self.source_id,
            "standalone_verification": "not-run",
            "status": "materialized-bytes-verified",
        }

    @property
    def receipt_sha256(self) -> str:
        return _sha256_file(self.receipt_path)


Fetcher = Callable[[ExternalLocator, Path], bytes | bytearray | None]


def _fresh_root(parent: Path, source_id: str, root_name: str | None) -> Path:
    configured_parent = parent.expanduser()
    try:
        if configured_parent.is_symlink():
            raise _fail("materialization parent must be a real directory")
        parent = configured_parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail("materialization parent is unavailable") from exc
    if root_name is not None:
        root_name = _safe_destination(root_name)
        if "/" in root_name:
            raise _fail("root_name must be one path component")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root_name = f"{source_id}-{stamp}-{secrets.token_hex(6)}"
    root = parent / root_name
    try:
        root.mkdir(mode=0o755, exist_ok=False)
    except FileExistsError as exc:
        raise _fail("materialization root already exists") from exc
    except OSError as exc:
        raise _fail("materialization root cannot be created") from exc
    return root


def _safe_output_path(root: Path, destination: str, *, create_parents: bool) -> Path:
    target = root.joinpath(*PurePosixPath(destination).parts)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise _fail("materialized destination escapes root") from exc
    current = root
    for part in PurePosixPath(destination).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail("materialized destination uses a symlink")
        if create_parents:
            try:
                current.mkdir(exist_ok=True)
            except OSError as exc:
                raise _fail("materialized destination cannot be created") from exc
    return target


def _assert_target_under_root(root: Path, target: Path) -> None:
    """Reject a fetcher-created symlink escape before hashing its output."""

    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise _fail("materialized destination escapes root") from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            if current.is_symlink() or not current.is_dir():
                raise _fail("materialized destination uses a symlink")
        except OSError as exc:
            raise _fail("materialized destination is unavailable") from exc


def _verify_target(root: Path, path: Path, expected: EvidenceFile) -> None:
    _assert_target_under_root(root, path)
    try:
        details = path.lstat()
    except OSError as exc:
        raise _fail("materialized artifact is missing") from exc
    if not path.is_file() or path.is_symlink():
        raise _fail("materialized artifact is not a regular file")
    if details.st_size != expected.size_bytes:
        raise _fail("materialized artifact size does not match declaration")
    if _sha256_file(path) != expected.sha256:
        raise _fail("materialized artifact digest does not match declaration")


def _verify_inventory(root: Path, spec: ExternalEvidenceSpec) -> None:
    """Reject unindexed provider output and symlinked inventory entries."""

    allowed = {PurePosixPath(item.destination) for item in spec.files}
    allowed.add(PurePosixPath(_RECEIPT_NAME))
    try:
        entries = tuple(root.rglob("*"))
    except OSError as exc:
        raise _fail("materialization inventory is unavailable") from exc
    for entry in entries:
        if entry.is_symlink():
            raise _fail("materialization inventory contains a symlink")
        if entry.is_dir():
            continue
        try:
            relative = PurePosixPath(entry.relative_to(root).as_posix())
        except ValueError as exc:
            raise _fail("materialization inventory escapes root") from exc
        if relative not in allowed:
            raise _fail("materialization root contains an unindexed artifact")


def _receipt_payload(spec: ExternalEvidenceSpec) -> dict[str, object]:
    return {
        "candidate": spec.candidate,
        "files": [item.as_dict() for item in spec.files],
        "format": _RECEIPT_FORMAT,
        "license_id": spec.license_id,
        "non_claim": True,
        "relocation_maps": [item.redacted() for item in spec.relocation_maps],
        "source_id": spec.source_id,
        "standalone_verification": "not-run",
        "status": "materialized-bytes-verified",
    }


def verify_materialized_evidence(
    root: Path,
    spec: ExternalEvidenceSpec | Mapping[str, object],
) -> MaterializationReceipt:
    """Recheck every materialized byte and the exact redacted receipt.

    This is the boundary a caller uses immediately before invoking a
    standalone report verifier or linking an external record elsewhere.  It
    never creates a ledger row and it rejects receipt drift as well as file
    drift.
    """

    spec = _validated_spec(spec)
    declared_root = Path(root).expanduser()
    try:
        if declared_root.is_symlink():
            raise _fail("materialization root must be a real directory")
        verified_root = declared_root.resolve(strict=True)
    except OSError as exc:
        raise _fail("materialization root is unavailable") from exc
    if not verified_root.is_dir() or verified_root.is_symlink():
        raise _fail("materialization root must be a real directory")
    _verify_inventory(verified_root, spec)
    for item in spec.files:
        _verify_target(
            verified_root,
            _safe_output_path(verified_root, item.destination, create_parents=False),
            item,
        )
    receipt_path = verified_root / _RECEIPT_NAME
    try:
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise _fail("materialization receipt is not a regular file")
        received = receipt_path.read_bytes()
    except OSError as exc:
        raise _fail("materialization receipt is unavailable") from exc
    expected = _canonical_json(_receipt_payload(spec)) + b"\n"
    if received != expected:
        raise _fail("materialization receipt does not match verified declaration")
    return MaterializationReceipt(
        root=verified_root,
        source_id=spec.source_id,
        candidate=spec.candidate,
        license_id=spec.license_id,
        files=spec.files,
        relocation_maps=spec.relocation_maps,
        receipt_path=receipt_path,
    )


def materialize_external_evidence(
    spec: ExternalEvidenceSpec | Mapping[str, object],
    destination_parent: Path,
    fetcher: Fetcher,
    *,
    root_name: str | None = None,
) -> MaterializationReceipt:
    """Materialize and verify declared bytes into one fresh root.

    ``fetcher`` may use a provider SDK, a local fixture, or a pinned Git
    checkout.  It receives only the validated locator and target path.  It may
    return bytes for the materializer to write, or write the target itself and
    return ``None``.  On any failure the newly created root is removed, so an
    incomplete tree cannot be mistaken for evidence.
    """

    if not callable(fetcher):
        raise _fail("fetcher must be callable")
    spec = _validated_spec(spec)
    root = _fresh_root(Path(destination_parent), spec.source_id, root_name)
    try:
        for item in spec.files:
            target = _safe_output_path(root, item.destination, create_parents=True)
            try:
                returned = fetcher(item.locator, target)
            except ExternalEvidenceError:
                raise
            except Exception as exc:  # provider errors must not leak details
                raise _fail("provider could not materialize artifact") from exc
            if returned is not None:
                if not isinstance(returned, (bytes, bytearray)):
                    raise _fail("fetcher must return bytes or write the target")
                try:
                    if target.exists() or target.is_symlink():
                        raise _fail("fetcher attempted to overwrite a destination")
                    target.write_bytes(bytes(returned))
                except OSError as exc:
                    raise _fail("materialized artifact cannot be written") from exc
            _verify_target(root, target, item)

        receipt_path = root / _RECEIPT_NAME
        if receipt_path.exists() or receipt_path.is_symlink():
            raise _fail("fetcher attempted to create the materialization receipt")
        payload = _receipt_payload(spec)
        try:
            receipt_path.write_bytes(_canonical_json(payload) + b"\n")
        except OSError as exc:
            raise _fail("materialization receipt cannot be written") from exc
        return verify_materialized_evidence(root, spec)
    except Exception:
        # The root is ours and was created with exist_ok=False.  Removing it
        # prevents partial provider output from entering any downstream index.
        shutil.rmtree(root, ignore_errors=True)
        raise


def materialize(
    spec: ExternalEvidenceSpec | Mapping[str, object],
    destination_parent: Path,
    fetcher: Fetcher,
    *,
    root_name: str | None = None,
) -> MaterializationReceipt:
    """Short alias retained for provider adapters and fixture scripts."""

    return materialize_external_evidence(
        spec, destination_parent, fetcher, root_name=root_name
    )


__all__ = [
    "EvidenceFile",
    "ExternalEvidenceError",
    "ExternalEvidenceFile",
    "ExternalEvidenceLocator",
    "ExternalEvidenceManifest",
    "ExternalEvidenceSpec",
    "ExternalLocator",
    "Fetcher",
    "MaterializationReceipt",
    "RelocationMap",
    "apply_relocation",
    "materialize",
    "materialize_external_evidence",
    "load_external_evidence_spec",
    "relocation_path_map",
    "verify_materialized_evidence",
]
