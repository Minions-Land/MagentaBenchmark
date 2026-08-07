"""Subject registry and resolved artifact contracts."""

from typing import Any

from .models import (
    AssemblySubjectArtifact,
    AssemblySubjectSpec,
    FakeSubjectArtifact,
    FakeSubjectSpec,
    AssemblySidecarRef,
    SubjectArtifact,
    SubjectArtifactAdapter,
    SubjectSpec,
    SubjectSpecAdapter,
)


def validate_subject_spec(value: Any) -> SubjectSpec:
    """Validate a Python/TOML mapping as the closed SubjectSpec union."""

    return SubjectSpecAdapter.validate_python(value)


__all__ = [
    "AssemblySubjectArtifact",
    "AssemblySubjectSpec",
    "FakeSubjectArtifact",
    "FakeSubjectSpec",
    "AssemblySidecarRef",
    "SubjectArtifact",
    "SubjectArtifactAdapter",
    "SubjectSpec",
    "SubjectSpecAdapter",
    "validate_subject_spec",
]
