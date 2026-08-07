"""JSON Schema generation for the public BMP 0.1 contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    BenchmarkArtifactAdapter,
    BenchmarkSpecAdapter,
    CheckpointLoadReceipt,
    CheckpointSaveReceipt,
    ClaimReport,
    CredentialRef,
    EnvironmentReceipt,
    EnvironmentSpec,
    EvidenceBundle,
    ExecutionSpec,
    ExperimentContrast,
    JournalRecord,
    NetworkObservation,
    ObservationReport,
    ProviderBinding,
    RecordIndex,
    ResourceSpec,
    ResolvedBmpManifest,
    RunReportAdapter,
    ResolvedExecutionSpec,
    ScheduleActivationReceipt,
    SubjectArtifactAdapter,
    SubjectSpecAdapter,
    SystemPromptRecord,
    WorkspaceRecord,
)


def schema_documents() -> dict[str, dict[str, Any]]:
    """Return JSON Schema documents keyed by their canonical file stem."""

    return {
        "benchmark-spec": BenchmarkSpecAdapter.json_schema(),
        "benchmark-artifact": BenchmarkArtifactAdapter.json_schema(),
        "subject-spec": SubjectSpecAdapter.json_schema(),
        "subject-artifact": SubjectArtifactAdapter.json_schema(),
        "execution-spec": ExecutionSpec.model_json_schema(),
        "experiment-contrast": ExperimentContrast.model_json_schema(),
        "environment-spec": EnvironmentSpec.model_json_schema(),
        "environment-receipt": EnvironmentReceipt.model_json_schema(),
        "resource-spec": ResourceSpec.model_json_schema(),
        "credential-ref": CredentialRef.model_json_schema(),
        "checkpoint-load-receipt": CheckpointLoadReceipt.model_json_schema(),
        "checkpoint-save-receipt": CheckpointSaveReceipt.model_json_schema(),
        "provider-binding": ProviderBinding.model_json_schema(),
        "record-index": RecordIndex.model_json_schema(),
        "resolved-execution-spec": ResolvedExecutionSpec.model_json_schema(),
        "evidence-bundle": EvidenceBundle.model_json_schema(),
        "network-observation": NetworkObservation.model_json_schema(),
        "journal-record": JournalRecord.model_json_schema(),
        "system-prompt-record": SystemPromptRecord.model_json_schema(),
        "workspace-record": WorkspaceRecord.model_json_schema(),
        "claim-report": ClaimReport.model_json_schema(mode="serialization"),
        "observation-report": ObservationReport.model_json_schema(),
        "run-report": RunReportAdapter.json_schema(mode="serialization"),
        "schedule-activation-receipt": ScheduleActivationReceipt.model_json_schema(),
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
