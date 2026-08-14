"""Strict, transport-independent identities for mirrored OCI acquisition."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IMAGE_SPEC_FORMAT: Literal["magentabench-oci-image-spec-v1"] = (
    "magentabench-oci-image-spec-v1"
)
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_MAX_SPEC_BYTES = 1024 * 1024
_REPOSITORY_COMPONENT = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{4,}"
    ),
)
_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
_CONFIG_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.container.image.v1+json",
        "application/vnd.oci.image.config.v1+json",
    }
)
_LAYER_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
    }
)


class ImageSpecError(ValueError):
    """Raised without embedding untrusted spec content in the message."""


class _DuplicateKeyError(ValueError):
    pass


class AcquisitionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=False,
    )


def _one_line_ascii(value: str, *, label: str) -> str:
    if not value or not value.isascii():
        raise ValueError(f"{label} must be non-empty ASCII")
    if any(character in value for character in ("\x00", "\r", "\n", "\t", " ")):
        raise ValueError(f"{label} must not contain control characters or whitespace")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError(f"{label} must not contain secret material")
    return value


def _validate_digest(value: str, *, label: str) -> str:
    _one_line_ascii(value, label=label)
    if re.fullmatch(DIGEST_PATTERN, value) is None:
        raise ValueError(f"{label} must be a lowercase sha256 digest")
    return value


def _validate_canonical_repository(value: str) -> str:
    _one_line_ascii(value, label="canonical_repository")
    if len(value) > 255 or not value.startswith("docker.io/"):
        raise ValueError("canonical_repository must be an explicit docker.io path")
    if value != value.lower() or any(token in value for token in ("@", ":", "\\")):
        raise ValueError("canonical_repository must be lowercase and unqualified")
    components = value.removeprefix("docker.io/").split("/")
    if len(components) < 2 or any(
        _REPOSITORY_COMPONENT.fullmatch(component) is None for component in components
    ):
        raise ValueError("canonical_repository has an invalid repository path")
    return value


class OciDescriptor(AcquisitionModel):
    media_type: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(ge=0, strict=True)
    digest: str

    @field_validator("media_type")
    @classmethod
    def media_type_is_safe(cls, value: str) -> str:
        value = _one_line_ascii(value, label="media_type")
        if (
            value
            not in _MANIFEST_MEDIA_TYPES | _CONFIG_MEDIA_TYPES | _LAYER_MEDIA_TYPES
        ):
            raise ValueError("media_type is not an approved OCI media type")
        return value

    @field_validator("digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        return _validate_digest(value, label="descriptor digest")


class OciPlatform(AcquisitionModel):
    os: Literal["linux"] = "linux"
    architecture: Literal["amd64"] = "amd64"
    variant: str | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("variant")
    @classmethod
    def variant_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _one_line_ascii(value, label="platform variant")

    @property
    def docker_value(self) -> str:
        suffix = f"/{self.variant}" if self.variant else ""
        return f"{self.os}/{self.architecture}{suffix}"


class OciImageSpec(AcquisitionModel):
    format: Literal["magentabench-oci-image-spec-v1"] = IMAGE_SPEC_FORMAT
    spec_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
    canonical_repository: str
    canonical_tag: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
    platform: OciPlatform
    manifest: OciDescriptor
    config: OciDescriptor
    layers: tuple[OciDescriptor, ...]
    rootfs_diff_ids: tuple[str, ...]

    @field_validator("spec_id")
    @classmethod
    def spec_id_has_no_secret_material(cls, value: str) -> str:
        return _one_line_ascii(value, label="spec_id")

    @field_validator("canonical_repository")
    @classmethod
    def repository_is_explicit(cls, value: str) -> str:
        return _validate_canonical_repository(value)

    @field_validator("canonical_tag")
    @classmethod
    def tag_is_one_line(cls, value: str) -> str:
        return _one_line_ascii(value, label="canonical_tag")

    @field_validator("rootfs_diff_ids")
    @classmethod
    def diff_ids_are_sha256(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _validate_digest(value, label="rootfs diff id") for value in values
        )

    @model_validator(mode="after")
    def descriptors_are_coherent(self) -> "OciImageSpec":
        if self.manifest.media_type not in _MANIFEST_MEDIA_TYPES:
            raise ValueError("manifest descriptor has the wrong media type")
        if self.config.media_type not in _CONFIG_MEDIA_TYPES:
            raise ValueError("config descriptor has the wrong media type")
        if self.manifest.size_bytes == 0 or self.config.size_bytes == 0:
            raise ValueError("manifest and config descriptors must be non-empty")
        if any(layer.media_type not in _LAYER_MEDIA_TYPES for layer in self.layers):
            raise ValueError("layer descriptor has the wrong media type")
        if len(self.layers) != len(self.rootfs_diff_ids):
            raise ValueError("layer and rootfs diff-id counts must match")
        digests = tuple(layer.digest for layer in self.layers)
        if self.manifest.digest in {self.config.digest, *digests}:
            raise ValueError("manifest digest must be distinct from child digests")
        return self

    @property
    def canonical_digest_ref(self) -> str:
        return f"{self.canonical_repository}@{self.manifest.digest}"

    @property
    def canonical_tag_ref(self) -> str:
        return f"{self.canonical_repository}:{self.canonical_tag}"

    @property
    def repository_path(self) -> str:
        return self.canonical_repository.removeprefix("docker.io/")

    def identity_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class LoadedImageSpec:
    spec: OciImageSpec
    file_sha256: str
    size_bytes: int
    source_name: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise _DuplicateKeyError
        output[key] = value
    return output


def load_image_spec(path: str | Path) -> LoadedImageSpec:
    """Load one bounded, non-symlink JSON spec without leaking invalid content."""

    candidate = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size <= 0
                or before.st_size > _MAX_SPEC_BYTES
            ):
                raise ImageSpecError(
                    "image spec size or file type is outside the accepted boundary"
                )
            data = handle.read(_MAX_SPEC_BYTES + 1)
            after = os.fstat(handle.fileno())
        if (
            len(data) != before.st_size
            or len(data) > _MAX_SPEC_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ImageSpecError("image spec size is outside the accepted boundary")
    except ImageSpecError:
        raise
    except OSError:
        raise ImageSpecError("image spec is unavailable") from None

    try:
        payload = json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(payload, dict):
            raise ValueError
        # JSON-mode validation retains strict scalar checks while accepting JSON
        # arrays for immutable tuple fields. Duplicate keys were rejected above.
        spec = OciImageSpec.model_validate_json(
            json.dumps(payload, allow_nan=False, ensure_ascii=True)
        )
    except Exception:
        raise ImageSpecError("image spec is invalid") from None

    return LoadedImageSpec(
        spec=spec,
        file_sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        source_name=candidate.name,
    )


__all__ = [
    "DIGEST_PATTERN",
    "IMAGE_SPEC_FORMAT",
    "ImageSpecError",
    "LoadedImageSpec",
    "OciDescriptor",
    "OciImageSpec",
    "OciPlatform",
    "load_image_spec",
]
