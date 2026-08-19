"""Digest-bound exploratory Apptainer runtime primitives.

This module deliberately stops before benchmark-specific execution and
standalone verification.  It owns the host-side runtime boundary only: an
absolute launcher, an immutable SIF or sandbox tree, generated argv, process
lifecycle, artifact export, and receipts.  A benchmark adapter must still add
an execution capability and verifier boundary before this runtime can produce
BMP result claims.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from MagentaBench.runner.backend.fake import CaseExecution
from MagentaBench.runner.compiler import CompiledRun
from MagentaBench.runner.evidence import (
    artifact_ref,
    atomic_write_bytes,
    atomic_write_json,
    sha256_file,
)
from MagentaBench.schemas import ArtifactRef


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTAINER_PATH = re.compile(r"^/(?:[A-Za-z0-9._+-]+/?)*$")
_INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
_RECEIPT_FORMAT = "magentabench-apptainer-runtime-receipt-v1"
_DEFAULT_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_ARTIFACT_ENTRIES = 4096
_DEFAULT_TERM_GRACE_SECONDS = 5.0
_MAX_LAUNCHER_OUTPUT_BYTES = 1024 * 1024
_PIPE_CHUNK_BYTES = 64 * 1024
_TRUNCATION_MARKER = b"\n[TRUNCATED]\n"
_XATTR_UNSUPPORTED = frozenset(
    {
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)
_EXPORT_RETRY_FORMAT = "magentabench-apptainer-export-retry-v1"
_TEARDOWN_INTENT_FORMAT = "magentabench-apptainer-teardown-intent-v1"
_TEARDOWN_RETRY_FORMAT = "magentabench-apptainer-teardown-retry-v1"
_CAPTURE_DRAIN_SECONDS = 1.0
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_RECEIPT_SEAL_FIELD = "receipt_payload_sha256"


class ApptainerConfigurationError(ValueError):
    """The declared Apptainer runtime boundary is incomplete or unsafe."""


class ApptainerIdentityDriftError(ApptainerConfigurationError):
    """A launcher, image, bind, overlay, or policy no longer matches its pin."""


class ApptainerLifecycleError(RuntimeError):
    """The runtime could not safely complete a lifecycle operation."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sealed_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    if _RECEIPT_SEAL_FIELD in payload:
        raise ApptainerConfigurationError("receipt payload contains a reserved field")
    payload[_RECEIPT_SEAL_FIELD] = _sha256_bytes(_canonical_json_bytes(payload))
    return payload


def _verify_mapping_seal(value: Mapping[str, Any], *, label: str) -> None:
    payload = dict(value)
    observed = payload.pop(_RECEIPT_SEAL_FIELD, None)
    if (
        not isinstance(observed, str)
        or _SHA256.fullmatch(observed) is None
        or observed != _sha256_bytes(_canonical_json_bytes(payload))
    ):
        raise ApptainerIdentityDriftError(f"{label} content seal drift")


def _load_sealed_json(path: Path, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ApptainerLifecycleError(f"{label} has duplicate fields")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ApptainerLifecycleError(f"{label} contains a non-finite number")

    try:
        value = json.loads(
            _bounded_regular_file(
                path,
                limit=_MAX_RECEIPT_BYTES,
                label=label,
            ).decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except ApptainerLifecycleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApptainerLifecycleError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise ApptainerLifecycleError(f"{label} must be an object")
    _verify_mapping_seal(value, label=label)
    return value


def _path_token(path: Path) -> str:
    """Bind a path without serializing a machine-specific locator into receipts."""

    return _sha256_bytes(os.fsencode(str(path)))


def _validate_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ApptainerConfigurationError(f"{label} must be a lowercase SHA-256")
    return value


def _validate_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ApptainerConfigurationError(f"{label} is invalid")
    return value


def _validate_container_path(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or _CONTAINER_PATH.fullmatch(value) is None
        or value == "/"
        or "//" in value
        or value.endswith("/")
        or any(part in {".", ".."} for part in PurePosixPath(value).parts)
    ):
        raise ApptainerConfigurationError(
            f"{label} must be an absolute normalized path"
        )
    return value


def _validate_bind_grammar_path(path: Path, *, label: str) -> None:
    raw = os.fsdecode(path)
    if any(marker in raw for marker in (":", ",", "\\", "\r", "\n", "\x00")):
        raise ApptainerConfigurationError(
            f"{label} contains an Apptainer bind separator"
        )
    if any(part in {".", ".."} for part in raw.split("/")):
        raise ApptainerConfigurationError(f"{label} contains path traversal")


def _root_path(path_value: Any, *, label: str) -> Path:
    if not isinstance(path_value, (str, os.PathLike)):
        raise ApptainerConfigurationError(f"{label} must be a path")
    path = Path(path_value)
    _assert_no_symlink_components(path, label=label)
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise ApptainerConfigurationError(f"{label} is unavailable") from exc
    if resolved.exists() and not resolved.is_dir():
        raise ApptainerConfigurationError(f"{label} must be a directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return _stable_path(resolved, label=label, directory=True)


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    """Reject all symlink traversal before a path is used for execution."""

    if not path.is_absolute():
        raise ApptainerConfigurationError(f"{label} must be an absolute path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ApptainerIdentityDriftError(f"{label} contains a symlink")


def _stable_path(path_value: Any, *, label: str, directory: bool) -> Path:
    if not isinstance(path_value, (str, os.PathLike)):
        raise ApptainerConfigurationError(f"{label} must be a path")
    path = Path(path_value)
    _assert_no_symlink_components(path, label=label)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ApptainerConfigurationError(f"{label} is unavailable") from exc
    valid_kind = resolved.is_dir() if directory else resolved.is_file()
    if resolved.is_symlink() or not valid_kind:
        kind = "directory" if directory else "regular file"
        raise ApptainerConfigurationError(f"{label} must be a {kind}")
    return resolved


def _tree_digest(root: Path) -> tuple[str, int]:
    """Hash sandbox bytes and execution-relevant filesystem metadata.

    Sandbox images are directories rather than a single SIF byte stream.  A
    directory mtime is not an identity, but entry type, mode, ownership,
    hardlink structure, empty directories, xattrs, and regular-file bytes all
    affect runtime behavior and therefore belong to the immutable identity.
    """

    root = _stable_path(root, label="sandbox image", directory=True)
    digest = hashlib.sha256()
    total_size = 0
    entries = (
        root,
        *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()),
    )
    hardlinks: dict[tuple[int, int], str] = {}
    for path in entries:
        try:
            details = path.lstat()
        except OSError as exc:
            raise ApptainerIdentityDriftError(
                "sandbox image entry is unavailable"
            ) from exc
        if stat.S_ISLNK(details.st_mode):
            raise ApptainerIdentityDriftError("sandbox image contains a symlink")
        if stat.S_ISDIR(details.st_mode):
            entry_type = "directory"
        elif stat.S_ISREG(details.st_mode):
            entry_type = "file"
        else:
            raise ApptainerIdentityDriftError(
                "sandbox image contains a non-regular entry"
            )
        relative_text = "." if path == root else path.relative_to(root).as_posix()
        metadata: dict[str, Any] = {
            "gid": details.st_gid,
            "mode": stat.S_IMODE(details.st_mode),
            "path": relative_text,
            "type": entry_type,
            "uid": details.st_uid,
        }
        if entry_type == "file":
            inode = (details.st_dev, details.st_ino)
            first_path = hardlinks.setdefault(inode, relative_text)
            metadata["hardlink_to"] = (
                None if first_path == relative_text else first_path
            )
            metadata["size_bytes"] = details.st_size
        try:
            xattr_names = sorted(os.listxattr(path, follow_symlinks=False))
            xattr_entries = [
                {
                    "name": name,
                    "sha256": _sha256_bytes(
                        os.getxattr(path, name, follow_symlinks=False)
                    ),
                }
                for name in xattr_names
            ]
        except OSError as exc:
            if exc.errno in _XATTR_UNSUPPORTED:
                metadata["xattrs"] = {"entries": [], "state": "unsupported"}
            else:
                raise ApptainerIdentityDriftError(
                    "sandbox image xattrs are unavailable"
                ) from exc
        else:
            metadata["xattrs"] = {"entries": xattr_entries, "state": "supported"}
        digest.update(_canonical_json_bytes(metadata))
        digest.update(b"\0")
        if entry_type == "file":
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            digest.update(b"\0")
            total_size += details.st_size
    return digest.hexdigest(), total_size


def _path_identity(path: Path, *, directory: bool, label: str) -> tuple[str, int]:
    stable = _stable_path(path, label=label, directory=directory)
    if directory:
        return _tree_digest(stable)
    return sha256_file(stable), stable.stat().st_size


def _normal_relative_path(value: str | Path, *, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ApptainerConfigurationError(f"{label} must be a path")
    raw = os.fsdecode(value)
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or "\\" in raw
        or "\x00" in raw
        or "\r" in raw
        or "\n" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise ApptainerConfigurationError(f"{label} must be a normalized relative path")
    return path


def _validated_command(command: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
        raise ApptainerConfigurationError("Apptainer command must be an argv sequence")
    if not command or any(not isinstance(item, str) for item in command):
        raise ApptainerConfigurationError("Apptainer command argv is invalid")
    values = tuple(command)
    if any(
        not item or "\x00" in item or "\r" in item or "\n" in item for item in values
    ):
        raise ApptainerConfigurationError("Apptainer command argv is invalid")
    return values


def _validated_exports(exports: Sequence[str | Path]) -> tuple[Path, ...]:
    if not isinstance(exports, Sequence) or isinstance(exports, (str, bytes)):
        raise ApptainerConfigurationError(
            "Apptainer artifact exports must be a sequence"
        )
    values = tuple(
        _normal_relative_path(item, label="Apptainer artifact export")
        for item in exports
    )
    if len(set(values)) != len(values):
        raise ApptainerConfigurationError("Apptainer artifact exports must be unique")
    for index, path in enumerate(values):
        for other in values[index + 1 :]:
            if path in other.parents or other in path.parents:
                raise ApptainerConfigurationError(
                    "Apptainer artifact exports must not overlap"
                )
    return values


def _bounded_regular_file(path: Path, *, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ApptainerLifecycleError(f"{label} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ApptainerLifecycleError(f"{label} is not a regular file")
        if before.st_size > limit:
            raise ApptainerLifecycleError(f"{label} exceeds its byte limit")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(_PIPE_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(content) > limit:
            raise ApptainerLifecycleError(f"{label} exceeds its byte limit")
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field) for field in identity_fields
        ):
            raise ApptainerLifecycleError(f"{label} changed while being read")
        if len(content) != before.st_size:
            raise ApptainerLifecycleError(f"{label} changed while being read")
        return content
    finally:
        os.close(descriptor)


@dataclass
class _BoundedCapture:
    limit: int
    secret_lookahead: int
    content: bytearray = field(default_factory=bytearray)
    observed_bytes: int = 0
    truncated: bool = False
    error_type: str | None = None

    def append(self, chunk: bytes) -> None:
        self.observed_bytes += len(chunk)
        raw_limit = self.limit + self.secret_lookahead
        remaining = max(0, raw_limit - len(self.content))
        if remaining:
            self.content.extend(chunk[:remaining])

    def rendered(self, secrets: Mapping[str, str]) -> bytes:
        content = _redact_bytes(bytes(self.content), secrets)
        self.truncated = self.observed_bytes > self.limit or len(content) > self.limit
        content = content[: self.limit]
        if self.truncated:
            content += _TRUNCATION_MARKER
        for index in range(len(self.content)):
            self.content[index] = 0
        return content


def _safe_child(root: Path, relative: Path, *, label: str) -> Path:
    candidate = root / relative
    _assert_no_symlink_components(candidate, label=label)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ApptainerLifecycleError(
            f"{label} is unavailable or escapes its root"
        ) from exc
    return resolved


def _artifact_tree(
    root: Path, *, max_entries: int | None = None
) -> tuple[tuple[Path, ...], int]:
    """List export files while refusing symlink or special-file substitution."""

    if root.is_symlink():
        raise ApptainerLifecycleError("artifact export contains a symlink")
    if root.is_file():
        if max_entries is not None and max_entries < 1:
            raise ApptainerLifecycleError("artifact export exceeds its entry limit")
        return (root,), 1
    if not root.is_dir():
        raise ApptainerLifecycleError(
            "artifact export is not a regular file or directory"
        )
    files: list[Path] = []
    entry_count = 1
    if max_entries is not None and entry_count > max_entries:
        raise ApptainerLifecycleError("artifact export exceeds its entry limit")
    for candidate in root.rglob("*"):
        entry_count += 1
        if max_entries is not None and entry_count > max_entries:
            raise ApptainerLifecycleError("artifact export exceeds its entry limit")
        if candidate.is_symlink():
            raise ApptainerLifecycleError("artifact export contains a symlink")
        if candidate.is_file():
            files.append(candidate)
        elif not candidate.is_dir():
            raise ApptainerLifecycleError(
                "artifact export contains a non-regular entry"
            )
    return (
        tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix())),
        entry_count,
    )


def _redact_bytes(content: bytes, secrets: Mapping[str, str]) -> bytes:
    redacted = content
    for name, value in sorted(
        secrets.items(), key=lambda item: len(item[1]), reverse=True
    ):
        if value:
            redacted = redacted.replace(
                os.fsencode(value), f"[REDACTED:{name}]".encode("ascii")
            )
    return redacted


def _close_selector_fileobj(fileobj: object) -> None:
    close = getattr(fileobj, "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            pass


@dataclass(frozen=True)
class ApptainerBind:
    """One host bind with content identity but no persisted host locator."""

    source: Path
    destination: str
    read_only: bool
    source_digest: str
    source_size_bytes: int
    source_is_directory: bool

    @classmethod
    def from_mapping(cls, value: Any) -> "ApptainerBind":
        if not isinstance(value, Mapping) or set(value) != {
            "source",
            "destination",
            "read_only",
        }:
            raise ApptainerConfigurationError(
                "each Apptainer bind requires source, destination, and read_only"
            )
        source_raw = value["source"]
        path = Path(source_raw) if isinstance(source_raw, (str, os.PathLike)) else None
        if path is None:
            raise ApptainerConfigurationError("Apptainer bind source must be a path")
        _validate_bind_grammar_path(path, label="Apptainer bind source")
        _assert_no_symlink_components(path, label="Apptainer bind source")
        try:
            source = path.resolve(strict=True)
        except OSError as exc:
            raise ApptainerConfigurationError(
                "Apptainer bind source is unavailable"
            ) from exc
        if source.is_symlink() or not (source.is_file() or source.is_dir()):
            raise ApptainerConfigurationError(
                "Apptainer bind source must be a regular file or directory"
            )
        _validate_bind_grammar_path(source, label="Apptainer bind source")
        directory = source.is_dir()
        digest, size = _path_identity(
            source,
            directory=directory,
            label="Apptainer bind source",
        )
        read_only = value["read_only"]
        if type(read_only) is not bool:
            raise ApptainerConfigurationError(
                "Apptainer bind read_only must be boolean"
            )
        return cls(
            source=source,
            destination=_validate_container_path(
                value["destination"], label="Apptainer bind destination"
            ),
            read_only=read_only,
            source_digest=digest,
            source_size_bytes=size,
            source_is_directory=directory,
        )

    def verify(self) -> None:
        digest, size = _path_identity(
            self.source,
            directory=self.source_is_directory,
            label="Apptainer bind source",
        )
        if digest != self.source_digest or size != self.source_size_bytes:
            raise ApptainerIdentityDriftError("Apptainer bind source content drift")

    def receipt(self, *, post_run: bool = False) -> dict[str, Any]:
        receipt = {
            "destination": self.destination,
            "read_only": self.read_only,
            "source_digest": self.source_digest,
            "source_is_directory": self.source_is_directory,
            "source_path_sha256": _path_token(self.source),
            "source_size_bytes": self.source_size_bytes,
        }
        if post_run and not self.read_only:
            digest, size = _path_identity(
                self.source,
                directory=self.source_is_directory,
                label="Apptainer bind source",
            )
            receipt["post_run_digest"] = digest
            receipt["post_run_size_bytes"] = size
        return receipt


@dataclass(frozen=True)
class ApptainerOverlay:
    """One declared overlay image; writable overlays retain their post-run pin."""

    path: Path
    mode: str
    digest: str
    size_bytes: int

    @classmethod
    def from_mapping(cls, value: Any) -> "ApptainerOverlay | None":
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != {"path", "mode", "sha256"}:
            raise ApptainerConfigurationError(
                "Apptainer overlay requires path, mode, and sha256"
            )
        mode = value["mode"]
        if mode not in {"ro", "rw"}:
            raise ApptainerConfigurationError("Apptainer overlay mode must be ro or rw")
        raw_path = Path(value["path"])
        _validate_bind_grammar_path(raw_path, label="Apptainer overlay")
        path = _stable_path(raw_path, label="Apptainer overlay", directory=False)
        _validate_bind_grammar_path(path, label="Apptainer overlay")
        digest, size = _path_identity(path, directory=False, label="Apptainer overlay")
        expected = _validate_sha256(value["sha256"], label="Apptainer overlay sha256")
        if digest != expected:
            raise ApptainerIdentityDriftError(
                "Apptainer overlay digest does not match pin"
            )
        return cls(path=path, mode=mode, digest=digest, size_bytes=size)

    def verify_before_launch(self) -> None:
        digest, size = _path_identity(
            self.path, directory=False, label="Apptainer overlay"
        )
        if digest != self.digest or size != self.size_bytes:
            raise ApptainerIdentityDriftError("Apptainer overlay content drift")

    def receipt(self, *, post_run: bool = False) -> dict[str, Any]:
        digest = self.digest
        size = self.size_bytes
        if not post_run or self.mode == "rw":
            digest, size = _path_identity(
                self.path,
                directory=False,
                label="Apptainer overlay",
            )
        if not post_run and (digest != self.digest or size != self.size_bytes):
            raise ApptainerIdentityDriftError("Apptainer overlay content drift")
        receipt = {
            "mode": self.mode,
            "path_sha256": _path_token(self.path),
            "sha256": self.digest,
            "size_bytes": self.size_bytes,
        }
        if post_run and self.mode == "rw":
            receipt["post_run_sha256"] = digest
            receipt["post_run_size_bytes"] = size
        return receipt


@dataclass(frozen=True)
class ApptainerGpu:
    """Optional NVIDIA passthrough identity required by ``--nv``."""

    library_path: Path
    library_digest: str

    @classmethod
    def from_defaults(
        cls, defaults: Mapping[str, Any], *, enabled: bool
    ) -> "ApptainerGpu | None":
        if not enabled:
            if any(
                key in defaults for key in ("gpu_library_path", "gpu_library_sha256")
            ):
                raise ApptainerConfigurationError(
                    "GPU library identity is only valid when gpu is enabled"
                )
            return None
        path = _stable_path(
            defaults.get("gpu_library_path"),
            label="Apptainer GPU library",
            directory=False,
        )
        expected = _validate_sha256(
            defaults.get("gpu_library_sha256"), label="Apptainer GPU library sha256"
        )
        observed, _ = _path_identity(
            path, directory=False, label="Apptainer GPU library"
        )
        if observed != expected:
            raise ApptainerIdentityDriftError("Apptainer GPU library digest drift")
        return cls(library_path=path, library_digest=observed)

    def verify(self) -> None:
        observed, _ = _path_identity(
            self.library_path,
            directory=False,
            label="Apptainer GPU library",
        )
        if observed != self.library_digest:
            raise ApptainerIdentityDriftError("Apptainer GPU library digest drift")

    def receipt(self) -> dict[str, str]:
        return {
            "library_path_sha256": _path_token(self.library_path),
            "library_sha256": self.library_digest,
        }


@dataclass(frozen=True)
class ApptainerRuntimeConfig:
    """Validated exact host/runtime binding constructed from one backend spec."""

    launcher: Path
    launcher_digest: str
    launcher_version: str
    launcher_build_config_digest: str
    image: Path
    image_kind: str
    image_digest: str
    image_size_bytes: int
    binds: tuple[ApptainerBind, ...]
    overlay: ApptainerOverlay | None
    network_argv: tuple[str, ...]
    gpu: ApptainerGpu | None
    fakeroot: bool
    forwarded_env_names: tuple[str, ...]
    keep_workspace_on_failure: bool
    max_capture_bytes: int
    max_artifact_bytes: int
    max_artifact_entries: int
    termination_grace_seconds: float

    @classmethod
    def from_backend(cls, backend: Any) -> "ApptainerRuntimeConfig":
        if getattr(backend, "adapter", None) != "apptainer":
            raise ApptainerConfigurationError(
                "Apptainer factory received another backend"
            )
        if getattr(backend, "kind", None) != "container":
            raise ApptainerConfigurationError(
                "Apptainer backend kind must be container"
            )
        executable = getattr(backend, "executable", None)
        launcher = _stable_path(executable, label="Apptainer launcher", directory=False)
        if not os.access(launcher, os.X_OK):
            raise ApptainerConfigurationError("Apptainer launcher is not executable")
        launcher_digest, _ = _path_identity(
            launcher, directory=False, label="Apptainer launcher"
        )
        expected_launcher_digest = _validate_sha256(
            getattr(backend, "digest", None), label="Apptainer launcher digest"
        )
        if launcher_digest != expected_launcher_digest:
            raise ApptainerIdentityDriftError("Apptainer launcher digest drift")
        version = getattr(backend, "version", None)
        if (
            not isinstance(version, str)
            or not version.strip()
            or any(marker in version for marker in ("\r", "\n", "\x00", "="))
        ):
            raise ApptainerConfigurationError("Apptainer launcher version is invalid")
        defaults = getattr(backend, "defaults", None)
        if not isinstance(defaults, Mapping):
            raise ApptainerConfigurationError(
                "Apptainer backend defaults must be a mapping"
            )
        actual_version = _launcher_output(launcher, "--version")
        if actual_version != version:
            raise ApptainerIdentityDriftError("Apptainer launcher version drift")
        build_digest = _validate_sha256(
            defaults.get("launcher_build_config_sha256"),
            label="Apptainer launcher build configuration sha256",
        )
        if _sha256_bytes(_launcher_output_bytes(launcher, "buildcfg")) != build_digest:
            raise ApptainerIdentityDriftError(
                "Apptainer launcher build configuration drift"
            )

        image_kind = defaults.get("image_kind")
        if image_kind not in {"sif", "sandbox"}:
            raise ApptainerConfigurationError(
                "Apptainer image_kind must be sif or sandbox"
            )
        image = _stable_path(
            getattr(backend, "image", None),
            label="Apptainer image",
            directory=image_kind == "sandbox",
        )
        image_digest, image_size = _path_identity(
            image,
            directory=image_kind == "sandbox",
            label="Apptainer image",
        )
        expected_image_digest = _validate_sha256(
            defaults.get("image_sha256"), label="Apptainer image sha256"
        )
        if image_digest != expected_image_digest:
            raise ApptainerIdentityDriftError("Apptainer image digest drift")

        raw_binds = defaults.get("binds", ())
        if not isinstance(raw_binds, (list, tuple)):
            raise ApptainerConfigurationError("Apptainer binds must be a list")
        binds = tuple(ApptainerBind.from_mapping(item) for item in raw_binds)
        destinations = [bind.destination for bind in binds]
        if len(set(destinations)) != len(destinations) or "/workspace" in destinations:
            raise ApptainerConfigurationError("Apptainer bind destinations conflict")

        overlay = ApptainerOverlay.from_mapping(defaults.get("overlay"))
        network_argv = _network_argv(defaults.get("network_mode"))
        gpu_enabled = defaults.get("gpu", False)
        if type(gpu_enabled) is not bool:
            raise ApptainerConfigurationError("Apptainer gpu must be boolean")
        gpu = ApptainerGpu.from_defaults(defaults, enabled=gpu_enabled)
        fakeroot = defaults.get("fakeroot", False)
        if type(fakeroot) is not bool:
            raise ApptainerConfigurationError("Apptainer fakeroot must be boolean")

        forwarded_env_names = _credential_names(defaults.get("forwarded_env_names", ()))
        keep_workspace = defaults.get("keep_workspace_on_failure", True)
        if type(keep_workspace) is not bool:
            raise ApptainerConfigurationError(
                "Apptainer keep_workspace_on_failure must be boolean"
            )
        max_capture = _positive_int(
            defaults.get("max_capture_bytes", _DEFAULT_MAX_CAPTURE_BYTES),
            label="Apptainer max_capture_bytes",
        )
        max_artifact = _positive_int(
            defaults.get("max_artifact_bytes", _DEFAULT_MAX_ARTIFACT_BYTES),
            label="Apptainer max_artifact_bytes",
        )
        max_artifact_entries = _positive_int(
            defaults.get("max_artifact_entries", _DEFAULT_MAX_ARTIFACT_ENTRIES),
            label="Apptainer max_artifact_entries",
        )
        grace = defaults.get("termination_grace_seconds", _DEFAULT_TERM_GRACE_SECONDS)
        if type(grace) not in {int, float} or not 0.0 <= float(grace) <= 120.0:
            raise ApptainerConfigurationError(
                "Apptainer termination_grace_seconds must be between 0 and 120"
            )
        return cls(
            launcher=launcher,
            launcher_digest=launcher_digest,
            launcher_version=version,
            launcher_build_config_digest=build_digest,
            image=image,
            image_kind=image_kind,
            image_digest=image_digest,
            image_size_bytes=image_size,
            binds=binds,
            overlay=overlay,
            network_argv=network_argv,
            gpu=gpu,
            fakeroot=fakeroot,
            forwarded_env_names=forwarded_env_names,
            keep_workspace_on_failure=keep_workspace,
            max_capture_bytes=max_capture,
            max_artifact_bytes=max_artifact,
            max_artifact_entries=max_artifact_entries,
            termination_grace_seconds=float(grace),
        )

    def verify(self, *, allow_mutable_drift: bool = False) -> None:
        launcher_digest, _ = _path_identity(
            self.launcher, directory=False, label="Apptainer launcher"
        )
        if launcher_digest != self.launcher_digest:
            raise ApptainerIdentityDriftError("Apptainer launcher digest drift")
        if _launcher_output(self.launcher, "--version") != self.launcher_version:
            raise ApptainerIdentityDriftError("Apptainer launcher version drift")
        if (
            _sha256_bytes(_launcher_output_bytes(self.launcher, "buildcfg"))
            != self.launcher_build_config_digest
        ):
            raise ApptainerIdentityDriftError(
                "Apptainer launcher build configuration drift"
            )
        image_digest, image_size = _path_identity(
            self.image,
            directory=self.image_kind == "sandbox",
            label="Apptainer image",
        )
        if image_digest != self.image_digest or image_size != self.image_size_bytes:
            raise ApptainerIdentityDriftError("Apptainer image content drift")
        for bind in self.binds:
            _validate_bind_grammar_path(bind.source, label="Apptainer bind source")
            if not (allow_mutable_drift and not bind.read_only):
                bind.verify()
        if self.overlay is not None and not (
            allow_mutable_drift and self.overlay.mode == "rw"
        ):
            _validate_bind_grammar_path(self.overlay.path, label="Apptainer overlay")
            self.overlay.verify_before_launch()
        if self.gpu is not None:
            self.gpu.verify()

    def receipt_identity(self) -> dict[str, Any]:
        return {
            "binds": [bind.receipt() for bind in self.binds],
            "forwarded_env_names": list(self.forwarded_env_names),
            "fakeroot": self.fakeroot,
            "gpu": None if self.gpu is None else self.gpu.receipt(),
            "image_digest": self.image_digest,
            "image_kind": self.image_kind,
            "image_path_sha256": _path_token(self.image),
            "image_size_bytes": self.image_size_bytes,
            "keep_workspace_on_failure": self.keep_workspace_on_failure,
            "launcher_build_config_sha256": self.launcher_build_config_digest,
            "launcher_path_sha256": _path_token(self.launcher),
            "launcher_sha256": self.launcher_digest,
            "launcher_version": self.launcher_version,
            "network_argv": list(self.network_argv),
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_artifact_entries": self.max_artifact_entries,
            "max_capture_bytes": self.max_capture_bytes,
            "overlay": (
                None
                if self.overlay is None
                else {
                    "mode": self.overlay.mode,
                    "path_sha256": _path_token(self.overlay.path),
                    "sha256": self.overlay.digest,
                    "size_bytes": self.overlay.size_bytes,
                }
            ),
            "termination_grace_seconds": self.termination_grace_seconds,
        }

    def policy_digest(self) -> str:
        return _sha256_bytes(_canonical_json_bytes(self.receipt_identity()))


def _launcher_output_bytes(launcher: Path, argument: str) -> bytes:
    captures = {
        "stderr": _BoundedCapture(_MAX_LAUNCHER_OUTPUT_BYTES, 0),
        "stdout": _BoundedCapture(_MAX_LAUNCHER_OUTPUT_BYTES, 0),
    }
    selector: selectors.BaseSelector | None = None
    process: subprocess.Popen[bytes] | None = None

    def drain(*, timeout: float) -> None:
        assert selector is not None
        for key, _events in selector.select(timeout):
            capture = captures[str(key.data)]
            try:
                chunk = os.read(key.fd, _PIPE_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError as exc:
                capture.error_type = type(exc).__name__
                chunk = b""
            if chunk:
                capture.append(chunk)
                continue
            try:
                selector.unregister(key.fileobj)
            except (KeyError, ValueError):
                pass
            _close_selector_fileobj(key.fileobj)

    try:
        process = subprocess.Popen(
            (str(launcher), argument),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                name: os.environ[name] for name in _INHERITED_ENV if name in os.environ
            },
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                raise OSError("launcher capture pipe is unavailable")
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + 30.0
        while process.poll() is None:
            drain(timeout=0.02)
            if time.monotonic() >= deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise subprocess.TimeoutExpired((str(launcher), argument), 30.0)
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        drain_deadline = time.monotonic() + _CAPTURE_DRAIN_SECONDS
        while selector.get_map() and time.monotonic() < drain_deadline:
            drain(timeout=0.02)
        if selector.get_map():
            raise OSError("launcher capture did not close")
    except (OSError, subprocess.TimeoutExpired) as exc:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise ApptainerConfigurationError(
            "Apptainer launcher identity command failed"
        ) from exc
    finally:
        if selector is not None:
            for key in tuple(selector.get_map().values()):
                try:
                    selector.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass
                _close_selector_fileobj(key.fileobj)
            selector.close()
    assert process is not None
    stdout = captures["stdout"].rendered(MappingProxyType({}))
    captures["stderr"].rendered(MappingProxyType({}))
    if (
        process.returncode != 0
        or any(capture.error_type is not None for capture in captures.values())
        or any(capture.truncated for capture in captures.values())
    ):
        raise ApptainerConfigurationError("Apptainer launcher identity command failed")
    return stdout


def _launcher_output(launcher: Path, argument: str) -> str:
    output = _launcher_output_bytes(launcher, argument)
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApptainerConfigurationError(
            "Apptainer launcher identity is not UTF-8"
        ) from exc
    value = text.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ApptainerConfigurationError("Apptainer launcher identity is malformed")
    return value


def _network_argv(value: Any) -> tuple[str, ...]:
    if value == "none":
        return ("--net", "--network", "none")
    if value == "isolated":
        return ("--net",)
    raise ApptainerConfigurationError(
        "Apptainer network_mode must be none or isolated; host networking is not generic-core safe"
    )


def _credential_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ApptainerConfigurationError(
            "Apptainer forwarded_env_names must be a list"
        )
    names = tuple(str(item) for item in value)
    if len(set(names)) != len(names) or any(
        _ENV_NAME.fullmatch(name) is None for name in names
    ):
        raise ApptainerConfigurationError("Apptainer forwarded_env_names are invalid")
    return names


def _positive_int(value: Any, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ApptainerConfigurationError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class ApptainerExecution:
    """Durable result of one exploratory host-runtime lifecycle."""

    attempt_id: str
    argv: tuple[str, ...]
    artifact_refs: tuple[ArtifactRef, ...]
    receipt_path: Path
    receipt_sha256: str
    status: str
    workspace: Path
    workspace_retained: bool


class ApptainerBackend:
    """A shell-free Apptainer lifecycle runtime with replayable receipts.

    This is intentionally not an ``ExecutionAdapter``.  Issue #30 must bind a
    concrete benchmark, subject interface, and verifier before normal BMP
    pipelines can select it.
    """

    adapter = "apptainer"

    def __init__(
        self,
        record_root: str | Path,
        *,
        workspace_root: str | Path,
        config: ApptainerRuntimeConfig,
    ) -> None:
        self.record_root = _root_path(record_root, label="Apptainer record root")
        self.workspace_root = _root_path(
            workspace_root, label="Apptainer workspace root"
        )
        self.config = config
        self.runner_digest = self._runtime_digest()
        self._manifest_write_lock = threading.Lock()

    @staticmethod
    def _runtime_digest() -> str:
        package_root = Path(__file__).parents[2]
        paths = tuple(
            sorted(
                (
                    Path(__file__),
                    package_root / "runner" / "evidence.py",
                    package_root / "runner" / "backend" / "fake.py",
                )
            )
        )
        digest = hashlib.sha256()
        for path in paths:
            digest.update(str(path.relative_to(package_root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def run_directory(self, run: CompiledRun) -> Path:
        return (
            self.record_root / run.manifest.metadata.experiment_id / run.manifest_digest
        )

    def load_completed(
        self,
        run: CompiledRun,
        bundle_path: Path,
        *,
        expected_runner_digest: str,
    ) -> CaseExecution | None:
        """Fail closed until a benchmark-specific verifier owns BMP bundles."""

        del run, bundle_path, expected_runner_digest
        raise ApptainerLifecycleError(
            "Apptainer runtime core has no standalone verifier or execution adapter"
        )

    @staticmethod
    def reset_state(case_id: str, policy: str) -> dict[str, str]:
        return {
            "case_id": _validate_identifier(case_id, label="Apptainer case id"),
            "policy": policy,
            "mechanism": "fresh_workspace",
        }

    def _workspace(self, run: CompiledRun, attempt_id: str) -> Path:
        attempt = _validate_identifier(attempt_id, label="Apptainer attempt id")
        experiment = _validate_identifier(
            run.manifest.metadata.experiment_id,
            label="Apptainer experiment id",
        )
        manifest_digest = run.manifest_digest
        if _SHA256.fullmatch(manifest_digest) is None:
            raise ApptainerConfigurationError("Apptainer manifest digest is invalid")
        return self.workspace_root / experiment / manifest_digest / attempt

    def _case_root(self, run: CompiledRun, attempt_id: str) -> Path:
        attempt = _validate_identifier(attempt_id, label="Apptainer attempt id")
        return self.run_directory(run) / "apptainer" / attempt

    def build_argv(self, workspace: Path, command: Sequence[str]) -> tuple[str, ...]:
        command_values = _validated_command(command)
        workspace = _stable_path(workspace, label="Apptainer workspace", directory=True)
        _validate_bind_grammar_path(workspace, label="Apptainer workspace")
        argv: list[str] = [
            str(self.config.launcher),
            "exec",
            "--cleanenv",
            "--containall",
            "--no-home",
            "--pwd",
            "/workspace",
            "--bind",
            f"{workspace}:/workspace:rw",
        ]
        if self.config.fakeroot:
            argv.append("--fakeroot")
        for bind in self.config.binds:
            _validate_bind_grammar_path(bind.source, label="Apptainer bind source")
            mode = "ro" if bind.read_only else "rw"
            argv.extend(("--bind", f"{bind.source}:{bind.destination}:{mode}"))
        if self.config.overlay is not None:
            _validate_bind_grammar_path(
                self.config.overlay.path, label="Apptainer overlay"
            )
            argv.extend(
                ("--overlay", f"{self.config.overlay.path}:{self.config.overlay.mode}")
            )
        argv.extend(self.config.network_argv)
        if self.config.gpu is not None:
            argv.append("--nv")
        argv.append(str(self.config.image))
        argv.extend(command_values)
        return tuple(argv)

    def _child_environment(self) -> tuple[dict[str, str], Mapping[str, str]]:
        environment = {
            name: os.environ[name] for name in _INHERITED_ENV if name in os.environ
        }
        environment.setdefault("LANG", "C.UTF-8")
        forwarded = {
            name: os.environ[name]
            for name in self.config.forwarded_env_names
            if name in os.environ
        }
        environment.update(
            {f"APPTAINERENV_{name}": value for name, value in forwarded.items()}
        )
        return environment, MappingProxyType(forwarded)

    def _write_log(
        self, content: bytes, destination: Path, secrets: Mapping[str, str]
    ) -> ArtifactRef:
        if len(content) > self.config.max_capture_bytes + len(b"\n[TRUNCATED]\n"):
            raise ApptainerLifecycleError("Apptainer capture exceeded its byte limit")
        atomic_write_bytes(destination, _redact_bytes(content, secrets))
        return artifact_ref(destination)

    @staticmethod
    def _drain_ready_captures(
        selector: selectors.BaseSelector,
        captures: Mapping[str, _BoundedCapture],
        *,
        timeout: float,
    ) -> None:
        for key, _events in selector.select(timeout):
            capture = captures[str(key.data)]
            for _ in range(16):
                try:
                    chunk = os.read(key.fd, _PIPE_CHUNK_BYTES)
                except BlockingIOError:
                    break
                except OSError as exc:
                    capture.error_type = type(exc).__name__
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    _close_selector_fileobj(key.fileobj)
                    break
                capture.append(chunk)

    @staticmethod
    def _finish_captures(
        selector: selectors.BaseSelector,
        captures: Mapping[str, _BoundedCapture],
    ) -> None:
        deadline = time.monotonic() + _CAPTURE_DRAIN_SECONDS
        while selector.get_map() and time.monotonic() < deadline:
            ApptainerBackend._drain_ready_captures(selector, captures, timeout=0.02)
        for key in tuple(selector.get_map().values()):
            capture = captures[str(key.data)]
            capture.truncated = True
            capture.error_type = capture.error_type or "CaptureDrainTimeout"
            try:
                selector.unregister(key.fileobj)
            except (KeyError, ValueError):
                pass
            _close_selector_fileobj(key.fileobj)
        selector.close()

    def _export_artifacts(
        self,
        workspace: Path,
        case_root: Path,
        exports: Sequence[Path],
        secrets: Mapping[str, str],
    ) -> tuple[ArtifactRef, ...]:
        target_root = case_root / "artifacts"
        refs: list[ArtifactRef] = []
        remaining_bytes = self.config.max_artifact_bytes
        remaining_entries = self.config.max_artifact_entries
        for relative in exports:
            source = _safe_child(workspace, relative, label="Apptainer artifact export")
            files, entry_count = _artifact_tree(
                source,
                max_entries=remaining_entries,
            )
            remaining_entries -= entry_count
            if not files:
                raise ApptainerLifecycleError("Apptainer artifact export is empty")
            source_root = source if source.is_dir() else source.parent
            for file_path in files:
                subpath = file_path.relative_to(source_root)
                if source.is_file():
                    subpath = Path(source.name)
                destination = (
                    target_root / relative / subpath
                    if source.is_dir()
                    else target_root / relative
                )
                _assert_no_symlink_components(
                    destination, label="Apptainer artifact export target"
                )
                data = _bounded_regular_file(
                    file_path,
                    limit=remaining_bytes,
                    label="Apptainer artifact",
                )
                remaining_bytes -= len(data)
                if any(
                    value and os.fsencode(value) in data for value in secrets.values()
                ):
                    raise ApptainerLifecycleError(
                        "Apptainer artifact contains a forwarded credential value"
                    )
                if destination.exists() or destination.is_symlink():
                    existing = _bounded_regular_file(
                        destination,
                        limit=self.config.max_artifact_bytes,
                        label="Apptainer artifact export target",
                    )
                    if existing != data:
                        raise ApptainerLifecycleError(
                            "Apptainer artifact export target drift"
                        )
                else:
                    atomic_write_bytes(destination, data)
                refs.append(artifact_ref(destination))
        return tuple(refs)

    @staticmethod
    def _terminate(
        process: subprocess.Popen[bytes], *, grace_seconds: float
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "kill_sent": False,
            "residual_kill_sent": False,
            "residual_term_sent": False,
            "term_sent": False,
        }
        if process.poll() is not None:
            receipt["returncode"] = process.returncode
            return receipt
        try:
            os.killpg(process.pid, signal.SIGTERM)
            receipt["term_sent"] = True
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                receipt["kill_sent"] = True
            except ProcessLookupError:
                pass
            process.wait()
        receipt["returncode"] = process.returncode
        return receipt

    @staticmethod
    def _terminate_residual_group(
        process: subprocess.Popen[bytes], *, grace_seconds: float
    ) -> dict[str, bool]:
        receipt = {
            "residual_kill_sent": False,
            "residual_term_sent": False,
        }

        def group_exists() -> bool:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError as exc:
                raise ApptainerLifecycleError(
                    "Apptainer residual process group is not controllable"
                ) from exc
            return True

        if not group_exists():
            return receipt
        try:
            os.killpg(process.pid, signal.SIGTERM)
            receipt["residual_term_sent"] = True
        except ProcessLookupError:
            return receipt
        deadline = time.monotonic() + grace_seconds
        while group_exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if group_exists():
            try:
                os.killpg(process.pid, signal.SIGKILL)
                receipt["residual_kill_sent"] = True
            except ProcessLookupError:
                pass
        return receipt

    def _remove_workspace(self, workspace: Path) -> str:
        if not workspace.exists():
            return "already_absent"
        root = _stable_path(
            self.workspace_root,
            label="Apptainer workspace root",
            directory=True,
        )
        _assert_no_symlink_components(workspace, label="Apptainer workspace")
        resolved = workspace.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ApptainerLifecycleError(
                "Apptainer workspace escapes configured root"
            ) from exc
        _artifact_tree(resolved)
        shutil.rmtree(resolved)
        return "removed"

    def execute(
        self,
        run: CompiledRun,
        *,
        command: Sequence[str],
        attempt_id: str,
        artifact_exports: Sequence[str | Path] = (),
        remaining_wall_seconds: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> ApptainerExecution:
        """Run one generic exploratory command and retain its lifecycle receipt.

        ``artifact_exports`` are workspace-relative files/directories copied
        before teardown.  This method is purposely not used by BMP pipelines
        until a later execution adapter provides benchmark-native case and
        verifier semantics.
        """

        self.config.verify()
        command_values = _validated_command(command)
        normalized_exports = _validated_exports(artifact_exports)
        if remaining_wall_seconds is None:
            timeout = None
        elif (
            type(remaining_wall_seconds) in {int, float}
            and float(remaining_wall_seconds) > 0
        ):
            timeout = float(remaining_wall_seconds)
        else:
            raise ApptainerConfigurationError(
                "Apptainer remaining_wall_seconds must be positive"
            )
        case_root = self._case_root(run, attempt_id)
        if case_root.exists() or case_root.is_symlink():
            raise ApptainerLifecycleError("Apptainer attempt record already exists")
        workspace = self._workspace(run, attempt_id)
        if workspace.exists() or workspace.is_symlink():
            raise ApptainerLifecycleError("Apptainer attempt workspace already exists")
        _validate_bind_grammar_path(workspace, label="Apptainer workspace")
        _assert_no_symlink_components(case_root, label="Apptainer attempt record")
        _assert_no_symlink_components(workspace, label="Apptainer workspace")
        if not isinstance(run.wire_json, bytes):
            raise ApptainerConfigurationError("Apptainer resolved manifest is invalid")
        manifest_content = run.wire_json + b"\n"
        if len(manifest_content) > _MAX_MANIFEST_BYTES:
            raise ApptainerConfigurationError(
                "Apptainer resolved manifest exceeds its byte limit"
            )
        manifest_path = self.run_directory(run) / "resolved_manifest.json"
        _assert_no_symlink_components(
            manifest_path, label="Apptainer resolved manifest"
        )
        with self._manifest_write_lock:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            if manifest_path.exists() or manifest_path.is_symlink():
                existing_manifest = _bounded_regular_file(
                    manifest_path,
                    limit=_MAX_MANIFEST_BYTES,
                    label="Apptainer resolved manifest",
                )
                if existing_manifest != manifest_content:
                    raise ApptainerIdentityDriftError(
                        "Apptainer resolved manifest content drift"
                    )
            else:
                atomic_write_bytes(manifest_path, manifest_content)
            persisted_manifest = _bounded_regular_file(
                manifest_path,
                limit=_MAX_MANIFEST_BYTES,
                label="Apptainer resolved manifest",
            )
            if persisted_manifest != manifest_content:
                raise ApptainerIdentityDriftError(
                    "Apptainer resolved manifest content drift"
                )
        manifest_file_sha256 = _sha256_bytes(persisted_manifest)
        manifest_file_size = len(persisted_manifest)
        case_root.mkdir(parents=True, exist_ok=False)
        try:
            workspace.mkdir(parents=True, exist_ok=False)
        except OSError:
            case_root.rmdir()
            raise
        argv = self.build_argv(workspace, command_values)
        argv_digest = _sha256_bytes(_canonical_json_bytes(list(argv)))
        environment, secrets = self._child_environment()
        forwarded_value_digests = {
            name: _sha256_bytes(os.fsencode(value)) for name, value in secrets.items()
        }
        secret_lookahead = max(
            (len(os.fsencode(value)) - 1 for value in secrets.values() if value),
            default=0,
        )
        captures = {
            "stderr": _BoundedCapture(self.config.max_capture_bytes, secret_lookahead),
            "stdout": _BoundedCapture(self.config.max_capture_bytes, secret_lookahead),
        }
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        capture_selector: selectors.BaseSelector | None = None
        termination: dict[str, Any] = {
            "kill_sent": False,
            "residual_kill_sent": False,
            "residual_term_sent": False,
            "returncode": None,
            "term_sent": False,
        }
        returncode: int | None = None
        process_status = "launch_error"
        launch_error: str | None = None
        try:
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            capture_selector = selectors.DefaultSelector()
            for name, stream in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            ):
                if stream is None:
                    raise ApptainerLifecycleError(
                        "Apptainer capture pipe is unavailable"
                    )
                os.set_blocking(stream.fileno(), False)
                capture_selector.register(stream, selectors.EVENT_READ, name)
            while process.poll() is None:
                self._drain_ready_captures(capture_selector, captures, timeout=0.02)
                elapsed = time.monotonic() - started
                if cancellation_event is not None and cancellation_event.is_set():
                    termination = self._terminate(
                        process, grace_seconds=self.config.termination_grace_seconds
                    )
                    process_status = "cancelled"
                    break
                if timeout is not None and elapsed >= timeout:
                    termination = self._terminate(
                        process, grace_seconds=self.config.termination_grace_seconds
                    )
                    process_status = "timeout"
                    break
            if process.poll() is not None and process_status == "launch_error":
                returncode = process.returncode
                process_status = "completed" if returncode == 0 else "process_error"
                termination["returncode"] = returncode
            elif process.poll() is not None:
                returncode = process.returncode
        except (OSError, ApptainerLifecycleError) as exc:
            launch_error = type(exc).__name__
            process_status = "launch_error"
            if process is not None and process.poll() is None:
                termination = self._terminate(
                    process, grace_seconds=self.config.termination_grace_seconds
                )
                returncode = process.returncode
        finally:
            if process is not None and process.poll() is not None:
                termination.update(
                    self._terminate_residual_group(
                        process,
                        grace_seconds=self.config.termination_grace_seconds,
                    )
                )
            if capture_selector is not None:
                self._finish_captures(capture_selector, captures)
        wall_seconds = time.monotonic() - started
        capture_error = next(
            (
                capture.error_type
                for capture in captures.values()
                if capture.error_type is not None
            ),
            None,
        )
        if capture_error is not None:
            process_status = "capture_error"
        status = process_status
        stdout_ref = self._write_log(
            captures["stdout"].rendered(secrets), case_root / "stdout.log", secrets
        )
        stderr_ref = self._write_log(
            captures["stderr"].rendered(secrets), case_root / "stderr.log", secrets
        )
        export_error: str | None = None
        exported: tuple[ArtifactRef, ...] = ()
        try:
            exported = self._export_artifacts(
                workspace,
                case_root,
                normalized_exports,
                secrets,
            )
        except (OSError, ApptainerConfigurationError, ApptainerLifecycleError) as exc:
            export_error = type(exc).__name__
            status = "export_error"
        post_identity_error: str | None = None
        try:
            self.config.verify(allow_mutable_drift=True)
        except ApptainerConfigurationError as exc:
            post_identity_error = type(exc).__name__
            status = "identity_drift"
        bind_receipts: list[dict[str, Any]] = []
        for bind in self.config.binds:
            try:
                bind_receipts.append(bind.receipt(post_run=True))
            except ApptainerConfigurationError as exc:
                bind_receipt = bind.receipt(post_run=False)
                bind_receipt["post_run_error_type"] = type(exc).__name__
                bind_receipts.append(bind_receipt)
                post_identity_error = type(exc).__name__
                status = "identity_drift"
        overlay_receipt = None
        if self.config.overlay is not None:
            try:
                overlay_receipt = self.config.overlay.receipt(post_run=True)
            except ApptainerConfigurationError as exc:
                overlay_receipt = {
                    **self.config.receipt_identity()["overlay"],
                    "post_run_error_type": type(exc).__name__,
                }
                post_identity_error = type(exc).__name__
                status = "identity_drift"
        workspace_retained = (
            (status != "completed" and self.config.keep_workspace_on_failure)
            or export_error is not None
            or post_identity_error is not None
        )
        teardown_result = "retained"
        teardown_error: str | None = None
        if not workspace_retained:
            try:
                teardown_result = self._remove_workspace(workspace)
            except (OSError, ApptainerLifecycleError) as exc:
                workspace_retained = True
                teardown_result = "error"
                teardown_error = type(exc).__name__
                status = "teardown_error"
        identity = self.config.receipt_identity()
        identity["binds"] = bind_receipts
        identity["overlay"] = overlay_receipt
        receipt = {
            "artifact_export": {
                "complete": export_error is None,
                "error_type": export_error,
                "refs": [ref.model_dump(mode="json") for ref in exported],
                "requested": [item.as_posix() for item in normalized_exports],
            },
            "attempt_id": attempt_id,
            "forwarded_env_names": list(self.config.forwarded_env_names),
            "forwarded_env_value_sha256": forwarded_value_digests,
            "format": _RECEIPT_FORMAT,
            "identity": identity,
            "lifecycle": {
                "capture_error_type": capture_error,
                "launch_error_type": launch_error,
                "post_identity_error_type": post_identity_error,
                "process_returncode": returncode,
                "process_status": process_status,
                "status": status,
                "termination": termination,
                "wall_clock_seconds": wall_seconds,
            },
            "log_refs": [
                stdout_ref.model_dump(mode="json"),
                stderr_ref.model_dump(mode="json"),
            ],
            "manifest_digest": run.manifest_digest,
            "policy_sha256": self.config.policy_digest(),
            "resolved_manifest_sha256": manifest_file_sha256,
            "resolved_manifest_size_bytes": manifest_file_size,
            "runner_sha256": self.runner_digest,
            "shell": False,
            "teardown": {
                "artifact_export_before_destroy": True,
                "error_type": teardown_error,
                "result": teardown_result,
                "workspace_path_sha256": _path_token(workspace),
                "workspace_retained": workspace_retained,
            },
            "argv_sha256": argv_digest,
        }
        receipt = _sealed_mapping(receipt)
        receipt_path = case_root / "runtime_receipt.json"
        atomic_write_json(receipt_path, receipt)
        return ApptainerExecution(
            attempt_id=attempt_id,
            argv=argv,
            artifact_refs=exported,
            receipt_path=receipt_path,
            receipt_sha256=sha256_file(receipt_path),
            status=status,
            workspace=workspace,
            workspace_retained=workspace_retained,
        )

    @staticmethod
    def _verify_receipt_ref(value: Any, *, case_root: Path, label: str) -> ArtifactRef:
        try:
            ref = ArtifactRef.model_validate(value)
            path = _stable_path(ref.path, label=label, directory=False)
            path.relative_to(case_root)
        except (OSError, ValueError, ApptainerConfigurationError) as exc:
            raise ApptainerLifecycleError(f"{label} is invalid") from exc
        if path.stat().st_size != ref.size_bytes or sha256_file(path) != ref.sha256:
            raise ApptainerIdentityDriftError(f"{label} content drift")
        return ref

    def _verify_artifact_refs(
        self,
        values: Sequence[Any],
        *,
        requested: Sequence[str],
        case_root: Path,
        label: str,
    ) -> tuple[ArtifactRef, ...]:
        try:
            normalized = _validated_exports(requested)
        except ApptainerConfigurationError as exc:
            raise ApptainerLifecycleError(f"{label} request is malformed") from exc
        artifact_root = case_root / "artifacts"
        covered = {item: False for item in normalized}
        refs: list[ArtifactRef] = []
        for index, raw_ref in enumerate(values):
            ref = self._verify_receipt_ref(
                raw_ref,
                case_root=case_root,
                label=f"{label} ref {index}",
            )
            path = _stable_path(ref.path, label=f"{label} ref {index}", directory=False)
            try:
                relative = path.relative_to(artifact_root)
            except ValueError as exc:
                raise ApptainerLifecycleError(
                    f"{label} ref {index} escapes the artifact root"
                ) from exc
            matches = tuple(
                item
                for item in normalized
                if relative == item or item in relative.parents
            )
            if len(matches) != 1:
                raise ApptainerLifecycleError(
                    f"{label} ref {index} does not prove one requested export"
                )
            covered[matches[0]] = True
            refs.append(ref)
        if any(not present for present in covered.values()):
            raise ApptainerLifecycleError(
                f"{label} does not cover every requested export"
            )
        if not normalized and refs:
            raise ApptainerLifecycleError(f"{label} has refs without requested exports")
        return tuple(refs)

    @staticmethod
    def _verify_mutable_identity(
        observed: Any,
        expected: Mapping[str, Any],
        *,
        path: Path,
        directory: bool,
        digest_key: str,
        size_key: str,
        label: str,
    ) -> None:
        if not isinstance(observed, Mapping) or observed.get("post_run_error_type"):
            raise ApptainerIdentityDriftError(
                f"{label} post-run identity is unavailable"
            )
        base = dict(observed)
        observed_digest = base.pop(digest_key, None)
        observed_size = base.pop(size_key, None)
        if _canonical_json_bytes(base) != _canonical_json_bytes(expected):
            raise ApptainerIdentityDriftError(f"{label} policy drift")
        try:
            current_digest, current_size = _path_identity(
                path,
                directory=directory,
                label=label,
            )
        except ApptainerConfigurationError as exc:
            raise ApptainerIdentityDriftError(f"{label} path drift") from exc
        if observed_digest != current_digest or observed_size != current_size:
            raise ApptainerIdentityDriftError(f"{label} post-run content drift")

    @staticmethod
    def _validate_error_type(value: Any, *, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            raise ApptainerLifecycleError(f"{label} is malformed")
        return value

    def _validate_receipt_state(self, value: Mapping[str, Any], path: Path) -> None:
        expected_fields = {
            "argv_sha256",
            "artifact_export",
            "attempt_id",
            "format",
            "forwarded_env_names",
            "forwarded_env_value_sha256",
            "identity",
            "lifecycle",
            "log_refs",
            "manifest_digest",
            "policy_sha256",
            _RECEIPT_SEAL_FIELD,
            "resolved_manifest_sha256",
            "resolved_manifest_size_bytes",
            "runner_sha256",
            "shell",
            "teardown",
        }
        if set(value) != expected_fields:
            raise ApptainerLifecycleError(
                "Apptainer runtime receipt fields are malformed"
            )
        attempt_id = _validate_identifier(
            value.get("attempt_id"), label="Apptainer receipt attempt id"
        )
        case_root = path.parent
        run_directory = case_root.parent.parent
        if (
            case_root.name != attempt_id
            or case_root.parent.name != "apptainer"
            or _SHA256.fullmatch(run_directory.name) is None
        ):
            raise ApptainerLifecycleError("Apptainer runtime receipt layout is invalid")
        _validate_identifier(
            run_directory.parent.name, label="Apptainer receipt experiment id"
        )
        manifest_digest = _validate_sha256(
            value.get("manifest_digest"), label="Apptainer manifest digest"
        )
        if manifest_digest != run_directory.name:
            raise ApptainerIdentityDriftError("Apptainer manifest digest drift")
        _validate_sha256(
            value.get("resolved_manifest_sha256"),
            label="Apptainer resolved manifest file digest",
        )
        manifest_size = value.get("resolved_manifest_size_bytes")
        if (
            type(manifest_size) is not int
            or manifest_size < 0
            or manifest_size > _MAX_MANIFEST_BYTES
        ):
            raise ApptainerLifecycleError(
                "Apptainer resolved manifest size is malformed"
            )
        _validate_sha256(value.get("runner_sha256"), label="Apptainer runner digest")
        if value.get("shell") is not False:
            raise ApptainerIdentityDriftError("Apptainer runtime shell policy drift")
        _validate_sha256(value.get("argv_sha256"), label="Apptainer argv digest")
        if value.get("forwarded_env_names") != list(self.config.forwarded_env_names):
            raise ApptainerIdentityDriftError(
                "Apptainer forwarded environment policy drift"
            )
        forwarded_hashes = value.get("forwarded_env_value_sha256")
        if not isinstance(forwarded_hashes, Mapping) or any(
            name not in self.config.forwarded_env_names
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            for name, digest in forwarded_hashes.items()
        ):
            raise ApptainerLifecycleError(
                "Apptainer forwarded environment evidence is malformed"
            )

        artifact_export = value.get("artifact_export")
        if not isinstance(artifact_export, Mapping) or set(artifact_export) != {
            "complete",
            "error_type",
            "refs",
            "requested",
        }:
            raise ApptainerLifecycleError(
                "Apptainer artifact export receipt is malformed"
            )
        complete = artifact_export.get("complete")
        refs = artifact_export.get("refs")
        requested = artifact_export.get("requested")
        error_type = self._validate_error_type(
            artifact_export.get("error_type"),
            label="Apptainer artifact export error type",
        )
        if (
            type(complete) is not bool
            or not isinstance(refs, list)
            or not isinstance(requested, list)
        ):
            raise ApptainerLifecycleError(
                "Apptainer artifact export receipt is malformed"
            )
        try:
            normalized_requested = _validated_exports(requested)
        except ApptainerConfigurationError as exc:
            raise ApptainerLifecycleError(
                "Apptainer artifact export request is malformed"
            ) from exc
        if [item.as_posix() for item in normalized_requested] != requested:
            raise ApptainerLifecycleError(
                "Apptainer artifact export request is not canonical"
            )
        if (
            complete != (error_type is None)
            or (not complete and refs)
            or (complete and bool(requested) != bool(refs))
        ):
            raise ApptainerLifecycleError(
                "Apptainer artifact export state is inconsistent"
            )

        lifecycle = value.get("lifecycle")
        if not isinstance(lifecycle, Mapping) or set(lifecycle) != {
            "capture_error_type",
            "launch_error_type",
            "post_identity_error_type",
            "process_returncode",
            "process_status",
            "status",
            "termination",
            "wall_clock_seconds",
        }:
            raise ApptainerLifecycleError("Apptainer lifecycle receipt is malformed")
        capture_error = self._validate_error_type(
            lifecycle.get("capture_error_type"),
            label="Apptainer capture error type",
        )
        launch_error = self._validate_error_type(
            lifecycle.get("launch_error_type"),
            label="Apptainer launch error type",
        )
        post_identity_error = self._validate_error_type(
            lifecycle.get("post_identity_error_type"),
            label="Apptainer post-run identity error type",
        )
        process_status = lifecycle.get("process_status")
        final_status = lifecycle.get("status")
        allowed_process_statuses = {
            "cancelled",
            "capture_error",
            "completed",
            "launch_error",
            "process_error",
            "timeout",
        }
        if process_status not in allowed_process_statuses or not isinstance(
            final_status, str
        ):
            raise ApptainerLifecycleError("Apptainer lifecycle status is malformed")
        returncode = lifecycle.get("process_returncode")
        if returncode is not None and (
            type(returncode) is not int or not -(2**31) <= returncode < 2**31
        ):
            raise ApptainerLifecycleError(
                "Apptainer lifecycle return code is malformed"
            )
        wall_seconds = lifecycle.get("wall_clock_seconds")
        if (
            not isinstance(wall_seconds, (int, float))
            or isinstance(wall_seconds, bool)
            or wall_seconds < 0.0
            or not wall_seconds < float("inf")
        ):
            raise ApptainerLifecycleError("Apptainer lifecycle duration is malformed")
        termination = lifecycle.get("termination")
        if not isinstance(termination, Mapping) or set(termination) != {
            "kill_sent",
            "residual_kill_sent",
            "residual_term_sent",
            "returncode",
            "term_sent",
        }:
            raise ApptainerLifecycleError("Apptainer termination receipt is malformed")
        if any(
            type(termination.get(name)) is not bool
            for name in (
                "kill_sent",
                "residual_kill_sent",
                "residual_term_sent",
                "term_sent",
            )
        ):
            raise ApptainerLifecycleError("Apptainer termination signals are malformed")
        if termination.get("returncode") != returncode:
            raise ApptainerLifecycleError(
                "Apptainer termination return code is inconsistent"
            )
        if process_status == "completed" and (
            returncode != 0 or launch_error is not None or capture_error is not None
        ):
            raise ApptainerLifecycleError(
                "Apptainer completed process state is inconsistent"
            )
        if process_status == "process_error" and (
            type(returncode) is not int or returncode == 0 or launch_error is not None
        ):
            raise ApptainerLifecycleError(
                "Apptainer failed process state is inconsistent"
            )
        if process_status in {"timeout", "cancelled"} and type(returncode) is not int:
            raise ApptainerLifecycleError(
                "Apptainer interrupted process state is inconsistent"
            )
        if process_status == "launch_error" and launch_error is None:
            raise ApptainerLifecycleError(
                "Apptainer launch failure state is inconsistent"
            )
        if process_status == "capture_error" and capture_error is None:
            raise ApptainerLifecycleError(
                "Apptainer capture failure state is inconsistent"
            )

        teardown = value.get("teardown")
        if not isinstance(teardown, Mapping) or set(teardown) != {
            "artifact_export_before_destroy",
            "error_type",
            "result",
            "workspace_path_sha256",
            "workspace_retained",
        }:
            raise ApptainerLifecycleError("Apptainer teardown receipt is malformed")
        teardown_error = self._validate_error_type(
            teardown.get("error_type"), label="Apptainer teardown error type"
        )
        retained = teardown.get("workspace_retained")
        result = teardown.get("result")
        if (
            teardown.get("artifact_export_before_destroy") is not True
            or type(retained) is not bool
            or not isinstance(teardown.get("workspace_path_sha256"), str)
            or _SHA256.fullmatch(teardown["workspace_path_sha256"]) is None
        ):
            raise ApptainerLifecycleError("Apptainer teardown state is malformed")
        if teardown_error is not None:
            if not retained or result != "error":
                raise ApptainerLifecycleError(
                    "Apptainer teardown error state is inconsistent"
                )
        elif retained:
            if result != "retained":
                raise ApptainerLifecycleError(
                    "Apptainer retained workspace state is inconsistent"
                )
        elif result not in {"removed", "already_absent"}:
            raise ApptainerLifecycleError(
                "Apptainer removed workspace state is inconsistent"
            )
        if not complete and not retained:
            raise ApptainerLifecycleError(
                "Apptainer incomplete export did not retain its workspace"
            )

        expected_final = process_status
        if not complete:
            expected_final = "export_error"
        if post_identity_error is not None:
            expected_final = "identity_drift"
        if teardown_error is not None:
            expected_final = "teardown_error"
        if final_status != expected_final:
            raise ApptainerIdentityDriftError("Apptainer lifecycle status drift")
        if expected_final == "completed" and retained:
            raise ApptainerLifecycleError(
                "Apptainer completed lifecycle retained its workspace"
            )
        log_refs = value.get("log_refs")
        if not isinstance(log_refs, list) or len(log_refs) != 2:
            raise ApptainerLifecycleError("Apptainer runtime log refs are malformed")

    def verify_receipt(
        self,
        receipt_path: str | Path,
        *,
        expected_receipt_sha256: str,
    ) -> Mapping[str, Any]:
        """Verify an old lifecycle receipt against the current host identity.

        This is the recovery-side gate.  It intentionally does not restart a
        command; callers must make a separate, explicitly leased decision.
        """

        path = _stable_path(
            receipt_path,
            label="Apptainer runtime receipt",
            directory=False,
        )
        try:
            path.relative_to(self.record_root)
        except ValueError as exc:
            raise ApptainerLifecycleError(
                "Apptainer runtime receipt escapes the record root"
            ) from exc
        expected_receipt_sha256 = _validate_sha256(
            expected_receipt_sha256, label="Apptainer expected receipt digest"
        )
        if sha256_file(path) != expected_receipt_sha256:
            raise ApptainerIdentityDriftError(
                "Apptainer runtime receipt content-address drift"
            )
        value = _load_sealed_json(path, label="Apptainer runtime receipt")
        if value.get("format") != _RECEIPT_FORMAT:
            raise ApptainerLifecycleError("Apptainer runtime receipt format drift")
        self._validate_receipt_state(value, path)
        identity = value.get("identity")
        if not isinstance(identity, Mapping):
            raise ApptainerLifecycleError("Apptainer runtime receipt lacks identity")
        if value.get("policy_sha256") != self.config.policy_digest():
            raise ApptainerIdentityDriftError("Apptainer runtime policy drift")
        if value.get("runner_sha256") != self.runner_digest:
            raise ApptainerIdentityDriftError("Apptainer runtime runner drift")
        manifest_path = path.parent.parent.parent / "resolved_manifest.json"
        manifest_content = _bounded_regular_file(
            manifest_path,
            limit=_MAX_MANIFEST_BYTES,
            label="Apptainer resolved manifest",
        )
        if len(manifest_content) != value.get(
            "resolved_manifest_size_bytes"
        ) or _sha256_bytes(manifest_content) != value.get("resolved_manifest_sha256"):
            raise ApptainerIdentityDriftError(
                "Apptainer resolved manifest content drift"
            )
        expected_identity = self.config.receipt_identity()
        observed_identity = dict(identity)
        expected_binds = expected_identity.pop("binds")
        observed_binds = observed_identity.pop("binds", None)
        expected_overlay = expected_identity.pop("overlay")
        observed_overlay = observed_identity.pop("overlay", None)
        if _canonical_json_bytes(observed_identity) != _canonical_json_bytes(
            expected_identity
        ):
            raise ApptainerIdentityDriftError(
                "Apptainer runtime receipt identity drift"
            )
        if not isinstance(observed_binds, list) or len(observed_binds) != len(
            self.config.binds
        ):
            raise ApptainerIdentityDriftError("Apptainer runtime receipt bind drift")
        for bind, observed_bind, expected_bind in zip(
            self.config.binds,
            observed_binds,
            expected_binds,
            strict=True,
        ):
            if bind.read_only:
                if _canonical_json_bytes(observed_bind) != _canonical_json_bytes(
                    expected_bind
                ):
                    raise ApptainerIdentityDriftError(
                        "Apptainer runtime receipt bind drift"
                    )
            else:
                self._verify_mutable_identity(
                    observed_bind,
                    expected_bind,
                    path=bind.source,
                    directory=bind.source_is_directory,
                    digest_key="post_run_digest",
                    size_key="post_run_size_bytes",
                    label="Apptainer writable bind",
                )
        if self.config.overlay is None:
            if observed_overlay is not None or expected_overlay is not None:
                raise ApptainerIdentityDriftError(
                    "Apptainer runtime receipt overlay drift"
                )
        elif self.config.overlay.mode == "ro":
            if _canonical_json_bytes(observed_overlay) != _canonical_json_bytes(
                expected_overlay
            ):
                raise ApptainerIdentityDriftError(
                    "Apptainer runtime receipt overlay drift"
                )
        else:
            assert isinstance(expected_overlay, Mapping)
            self._verify_mutable_identity(
                observed_overlay,
                expected_overlay,
                path=self.config.overlay.path,
                directory=False,
                digest_key="post_run_sha256",
                size_key="post_run_size_bytes",
                label="Apptainer writable overlay",
            )
        try:
            self.config.verify(allow_mutable_drift=True)
        except ApptainerConfigurationError as exc:
            raise ApptainerIdentityDriftError(
                "Apptainer runtime identity drift"
            ) from exc
        case_root = path.parent
        log_refs = value.get("log_refs")
        artifact_export = value.get("artifact_export")
        if not isinstance(log_refs, list) or not isinstance(artifact_export, Mapping):
            raise ApptainerLifecycleError(
                "Apptainer runtime receipt refs are malformed"
            )
        artifact_refs = artifact_export.get("refs")
        if not isinstance(artifact_refs, list):
            raise ApptainerLifecycleError(
                "Apptainer runtime receipt refs are malformed"
            )
        for index, raw_ref in enumerate(log_refs):
            self._verify_receipt_ref(
                raw_ref,
                case_root=case_root,
                label=f"Apptainer runtime log ref {index}",
            )
        if artifact_export.get("complete"):
            requested = artifact_export.get("requested")
            assert isinstance(requested, list)
            self._verify_artifact_refs(
                artifact_refs,
                requested=requested,
                case_root=case_root,
                label="Apptainer runtime artifact",
            )
        return MappingProxyType(dict(value))

    def _retained_workspace_candidate(
        self,
        receipt_path: Path,
        receipt: Mapping[str, Any],
    ) -> Path:
        teardown = receipt.get("teardown")
        attempt_id = receipt.get("attempt_id")
        if not isinstance(teardown, Mapping) or not isinstance(attempt_id, str):
            raise ApptainerLifecycleError("Apptainer runtime receipt is incomplete")
        expected_path_token = teardown.get("workspace_path_sha256")
        if not isinstance(expected_path_token, str):
            raise ApptainerLifecycleError(
                "Apptainer runtime receipt lacks workspace identity"
            )
        case_root = receipt_path.parent
        run_directory = case_root.parent.parent
        if (
            case_root.name != attempt_id
            or case_root.parent.name != "apptainer"
            or _SHA256.fullmatch(run_directory.name) is None
        ):
            raise ApptainerLifecycleError("Apptainer runtime receipt layout is invalid")
        experiment_id = _validate_identifier(
            run_directory.parent.name,
            label="Apptainer experiment id",
        )
        candidate = (
            self.workspace_root / experiment_id / run_directory.name / attempt_id
        )
        if _path_token(candidate) != expected_path_token:
            raise ApptainerIdentityDriftError("Apptainer retained workspace path drift")
        root = _stable_path(
            self.workspace_root,
            label="Apptainer workspace root",
            directory=True,
        )
        _assert_no_symlink_components(candidate, label="Apptainer retained workspace")
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ApptainerLifecycleError(
                "Apptainer retained workspace escapes its configured root"
            ) from exc
        return candidate

    def _retained_workspace(
        self,
        receipt_path: Path,
        receipt: Mapping[str, Any],
    ) -> Path:
        candidate = self._retained_workspace_candidate(receipt_path, receipt)
        return _stable_path(
            candidate,
            label="Apptainer retained workspace",
            directory=True,
        )

    def _verify_export_retry(
        self,
        retry_path: Path,
        *,
        parent_receipt: Path,
        requested: Sequence[str],
        expected_retry_sha256: str,
    ) -> Mapping[str, Any]:
        expected_retry_sha256 = _validate_sha256(
            expected_retry_sha256,
            label="Apptainer expected artifact export retry digest",
        )
        if sha256_file(retry_path) != expected_retry_sha256:
            raise ApptainerIdentityDriftError(
                "Apptainer artifact export retry content-address drift"
            )
        retry = _load_sealed_json(
            retry_path, label="Apptainer artifact export retry receipt"
        )
        if (
            set(retry)
            != {
                "complete",
                "format",
                "parent_receipt_sha256",
                "refs",
                "requested",
                _RECEIPT_SEAL_FIELD,
            }
            or retry.get("format") != _EXPORT_RETRY_FORMAT
            or retry.get("parent_receipt_sha256") != sha256_file(parent_receipt)
            or retry.get("complete") is not True
            or retry.get("requested") != list(requested)
            or not isinstance(retry.get("refs"), list)
        ):
            raise ApptainerIdentityDriftError(
                "Apptainer artifact export retry receipt drift"
            )
        self._verify_artifact_refs(
            retry["refs"],
            requested=requested,
            case_root=parent_receipt.parent,
            label="Apptainer artifact export retry",
        )
        return MappingProxyType(retry)

    def retry_export(
        self,
        receipt_path: str | Path,
        *,
        expected_receipt_sha256: str,
    ) -> tuple[ArtifactRef, ...]:
        """Retry a failed artifact export without re-running the command.

        Existing target bytes must match exactly, so repeated recovery calls
        are idempotent.  The original receipt remains immutable; this method
        writes a separately content-addressed export retry receipt.
        """

        receipt = self.verify_receipt(
            receipt_path,
            expected_receipt_sha256=expected_receipt_sha256,
        )
        teardown = receipt.get("teardown")
        artifact_export = receipt.get("artifact_export")
        if not isinstance(teardown, Mapping) or not isinstance(
            artifact_export, Mapping
        ):
            raise ApptainerLifecycleError("Apptainer runtime receipt is incomplete")
        if not teardown.get("workspace_retained"):
            raise ApptainerLifecycleError(
                "Apptainer workspace is not retained for export retry"
            )
        if artifact_export.get("complete") is not False:
            raise ApptainerLifecycleError(
                "Apptainer artifact export is already complete"
            )
        requested = artifact_export.get("requested")
        if not isinstance(requested, list) or any(
            not isinstance(item, str) for item in requested
        ):
            raise ApptainerLifecycleError("Apptainer export retry request is malformed")
        source_receipt = _stable_path(
            receipt_path,
            label="Apptainer runtime receipt",
            directory=False,
        )
        case_root = source_receipt.parent
        attempt_id = receipt.get("attempt_id")
        if not isinstance(attempt_id, str):
            raise ApptainerLifecycleError("Apptainer runtime receipt lacks attempt id")
        workspace = self._retained_workspace(source_receipt, receipt)
        recorded_secret_hashes = receipt.get("forwarded_env_value_sha256")
        if not isinstance(recorded_secret_hashes, Mapping):
            raise ApptainerLifecycleError(
                "Apptainer receipt lacks forwarded environment evidence"
            )
        _, current_secrets = self._child_environment()
        if set(current_secrets) != set(recorded_secret_hashes):
            raise ApptainerIdentityDriftError(
                "Apptainer forwarded environment presence drift"
            )
        retry_secrets: dict[str, str] = {}
        for name, expected_digest in recorded_secret_hashes.items():
            current_value = current_secrets.get(name)
            if (
                current_value is None
                or _sha256_bytes(os.fsencode(current_value)) != expected_digest
            ):
                raise ApptainerIdentityDriftError(
                    "Apptainer forwarded environment value drift"
                )
            retry_secrets[name] = current_value
        normalized_requested = _validated_exports(requested)
        refs = self._export_artifacts(
            workspace,
            case_root,
            normalized_requested,
            MappingProxyType(retry_secrets),
        )
        retry_path = case_root / "artifact_export_retry.json"
        retry = _sealed_mapping(
            {
                "complete": True,
                "format": _EXPORT_RETRY_FORMAT,
                "parent_receipt_sha256": expected_receipt_sha256,
                "refs": [ref.model_dump(mode="json") for ref in refs],
                "requested": requested,
            }
        )
        if retry_path.exists():
            existing = _bounded_regular_file(
                retry_path,
                limit=_MAX_RECEIPT_BYTES,
                label="Apptainer artifact export retry receipt",
            )
            expected = _canonical_json_bytes(retry) + b"\n"
            if existing != expected:
                raise ApptainerLifecycleError("Apptainer artifact export retry drift")
        else:
            atomic_write_json(retry_path, retry)
        return refs

    def retry_teardown(
        self,
        receipt_path: str | Path,
        *,
        expected_receipt_sha256: str,
        expected_export_retry_sha256: str | None = None,
    ) -> Mapping[str, Any]:
        """Remove a retained workspace once export evidence is durable.

        The operation writes an immutable child receipt. Repeating it verifies
        and returns that receipt without requiring the workspace to reappear.
        """

        receipt = self.verify_receipt(
            receipt_path,
            expected_receipt_sha256=expected_receipt_sha256,
        )
        source_receipt = _stable_path(
            receipt_path,
            label="Apptainer runtime receipt",
            directory=False,
        )
        case_root = source_receipt.parent
        intent_path = case_root / "teardown_intent.json"
        retry_path = case_root / "teardown_retry.json"
        teardown = receipt.get("teardown")
        artifact_export = receipt.get("artifact_export")
        if not isinstance(teardown, Mapping) or not teardown.get("workspace_retained"):
            raise ApptainerLifecycleError(
                "Apptainer workspace is not retained for teardown retry"
            )
        if not isinstance(artifact_export, Mapping):
            raise ApptainerLifecycleError(
                "Apptainer runtime receipt lacks artifact export state"
            )
        requested = artifact_export.get("requested")
        if not isinstance(requested, list) or any(
            not isinstance(item, str) for item in requested
        ):
            raise ApptainerLifecycleError(
                "Apptainer runtime receipt has malformed artifact exports"
            )
        export_proof_sha256 = expected_receipt_sha256
        if not artifact_export.get("complete"):
            if expected_export_retry_sha256 is None:
                raise ApptainerLifecycleError(
                    "Apptainer teardown requires the artifact export retry digest"
                )
            export_retry_path = case_root / "artifact_export_retry.json"
            if not export_retry_path.is_file():
                raise ApptainerLifecycleError(
                    "Apptainer teardown requires a completed artifact export retry"
                )
            self._verify_export_retry(
                export_retry_path,
                parent_receipt=source_receipt,
                requested=requested,
                expected_retry_sha256=expected_export_retry_sha256,
            )
            export_proof_sha256 = expected_export_retry_sha256
        workspace = self._retained_workspace_candidate(source_receipt, receipt)
        workspace_token = _path_token(workspace)
        parent_digest = expected_receipt_sha256

        intent = _sealed_mapping(
            {
                "artifact_export_receipt_sha256": export_proof_sha256,
                "format": _TEARDOWN_INTENT_FORMAT,
                "parent_receipt_sha256": parent_digest,
                "workspace_path_sha256": workspace_token,
            }
        )
        expected_intent = _canonical_json_bytes(intent) + b"\n"
        if intent_path.exists() or intent_path.is_symlink():
            existing_intent = _bounded_regular_file(
                intent_path,
                limit=_MAX_RECEIPT_BYTES,
                label="Apptainer teardown intent receipt",
            )
            if existing_intent != expected_intent:
                raise ApptainerIdentityDriftError(
                    "Apptainer teardown intent receipt drift"
                )
        else:
            if retry_path.exists() or retry_path.is_symlink():
                raise ApptainerIdentityDriftError(
                    "Apptainer teardown completion lacks its intent"
                )
            if not workspace.exists() and not workspace.is_symlink():
                raise ApptainerLifecycleError(
                    "Apptainer retained workspace disappeared before teardown intent"
                )
            self._retained_workspace(source_receipt, receipt)
            atomic_write_json(intent_path, intent)
        intent_sha256 = sha256_file(intent_path)

        def teardown_retry(result: str) -> dict[str, Any]:
            return _sealed_mapping(
                {
                    "format": _TEARDOWN_RETRY_FORMAT,
                    "intent_sha256": intent_sha256,
                    "parent_receipt_sha256": parent_digest,
                    "result": result,
                    "workspace_path_sha256": workspace_token,
                }
            )

        if retry_path.exists():
            existing = _load_sealed_json(
                retry_path, label="Apptainer teardown retry receipt"
            )
            if (
                set(existing)
                != {
                    "format",
                    "intent_sha256",
                    "parent_receipt_sha256",
                    "result",
                    _RECEIPT_SEAL_FIELD,
                    "workspace_path_sha256",
                }
                or existing.get("format") != _TEARDOWN_RETRY_FORMAT
                or existing.get("intent_sha256") != intent_sha256
                or existing.get("parent_receipt_sha256") != parent_digest
                or existing.get("workspace_path_sha256") != workspace_token
                or existing.get("result") not in {"already_absent", "removed"}
            ):
                raise ApptainerIdentityDriftError(
                    "Apptainer teardown retry receipt drift"
                )
            if workspace.exists() or workspace.is_symlink():
                raise ApptainerIdentityDriftError(
                    "Apptainer removed workspace reappeared"
                )
            return MappingProxyType(existing)

        if workspace.exists() or workspace.is_symlink():
            try:
                retained_workspace = self._retained_workspace(source_receipt, receipt)
            except ApptainerConfigurationError:
                if workspace.exists() or workspace.is_symlink():
                    raise
                result = "already_absent"
            else:
                result = self._remove_workspace(retained_workspace)
            if result not in {"removed", "already_absent"}:
                raise ApptainerLifecycleError(
                    "Apptainer retained workspace was not removed"
                )
        else:
            result = "already_absent"
        completed = teardown_retry(result)
        atomic_write_json(retry_path, completed)
        return MappingProxyType(completed)


class ApptainerBackendFactory:
    """Factory implementation shared by the project plugin and focused tests."""

    adapter = "apptainer"

    def build(
        self,
        run: CompiledRun,
        *,
        record_root: Path,
        workspace_root: Path,
    ) -> ApptainerBackend:
        # ``adapter_registry`` imports backend implementations, so defer this
        # import until the factory is actually called.
        from MagentaBench.runner.adapter_registry import AdapterRegistryError

        try:
            config = ApptainerRuntimeConfig.from_backend(run.manifest.execution.backend)
        except ApptainerConfigurationError as exc:
            raise AdapterRegistryError(str(exc)) from exc
        return ApptainerBackend(
            record_root,
            workspace_root=workspace_root,
            config=config,
        )


__all__ = [
    "ApptainerBackend",
    "ApptainerBackendFactory",
    "ApptainerBind",
    "ApptainerConfigurationError",
    "ApptainerExecution",
    "ApptainerGpu",
    "ApptainerIdentityDriftError",
    "ApptainerLifecycleError",
    "ApptainerOverlay",
    "ApptainerRuntimeConfig",
]
