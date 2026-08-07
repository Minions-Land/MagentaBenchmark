"""JSON Schema generation for the public BMP 0.1 contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    BenchmarkArtifactAdapter,
    BenchmarkSpecAdapter,
    ClaimReport,
    EnvironmentReceipt,
    EnvironmentSpec,
    EvidenceBundle,
    ExecutionSpec,
    ResolvedBmpManifest,
    ResolvedExecutionSpec,
    SubjectArtifactAdapter,
    SubjectSpecAdapter,
)


def schema_documents() -> dict[str, dict[str, Any]]:
    """Return JSON Schema documents keyed by their canonical file stem."""

    return {
        "benchmark-spec": BenchmarkSpecAdapter.json_schema(),
        "benchmark-artifact": BenchmarkArtifactAdapter.json_schema(),
        "subject-spec": SubjectSpecAdapter.json_schema(),
        "subject-artifact": SubjectArtifactAdapter.json_schema(),
        "execution-spec": ExecutionSpec.model_json_schema(),
        "environment-spec": EnvironmentSpec.model_json_schema(),
        "environment-receipt": EnvironmentReceipt.model_json_schema(),
        "resolved-execution-spec": ResolvedExecutionSpec.model_json_schema(),
        "evidence-bundle": EvidenceBundle.model_json_schema(),
        "claim-report": ClaimReport.model_json_schema(mode="serialization"),
        "resolved-bmp-manifest": ResolvedBmpManifest.model_json_schema(),
    }


def write_json_schemas(output_dir: str | Path) -> tuple[Path, ...]:
    """Generate deterministic, human-readable ``*.schema.json`` files."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in sorted(schema_documents().items()):
        path = destination / f"{name}.schema.json"
        path.write_text(
            json.dumps(schema, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return tuple(written)


__all__ = ["schema_documents", "write_json_schemas"]
