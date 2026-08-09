"""JSON Schema generation for the public BMP 0.1 contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    AdapterCapability,
    AdapterCapabilityArtifact,
    BenchmarkArtifactAdapter,
    BenchmarkSpecAdapter,
    CaseOrderArtifact,
    CaseSetActivationReceipt,
    CaseSetArtifact,
    CustomCaseOrderSpec,
    ConfigurationActivationReceipt,
    CheckpointLoadReceipt,
    CheckpointSaveReceipt,
    ClaimReport,
    ConfigurationArtifact,
    ConfigurationSelection,
    ConfigurationSpec,
    CredentialRef,
    EnvironmentReceipt,
    EnvironmentSpec,
    EvidenceBundle,
    EvolutionCandidateRecord,
    EvolutionRunEvidence,
    EvolutionTransitionRecord,
    ExecutionSpec,
    ExperimentContrast,
    JournalRecord,
    ExternalProtocolAuthorityReceipt,
    IntegrationProbeRecord,
    NetworkObservation,
    ObservationReport,
    ModelActivationReceipt,
    ModelActivationEvidence,
    ModelActivationUsage,
    ProviderBinding,
    RecordIndex,
    ResourceSpec,
    ResolvedBmpManifest,
    ResolvedNetworkPolicy,
    RunReportAdapter,
    ResolvedExecutionSpec,
    ScheduleActivationReceipt,
    StatisticalAnalysisPlan,
    StatisticalAnalysisReceipt,
    SubjectArtifactAdapter,
    SubjectSpecAdapter,
    SystemPromptRecord,
    WorkspaceRecord,
)
from .evolution import (
    EvolutionBudgetEvent,
    EvolutionBudgetLedger,
    EvolutionEvaluationRecord,
    EvolutionRuntimeReceipt,
    EvolutionSealedHoldoutReceipt,
)


def schema_documents() -> dict[str, dict[str, Any]]:
    """Return JSON Schema documents keyed by their canonical file stem."""

    return {
        "benchmark-spec": BenchmarkSpecAdapter.json_schema(),
        "benchmark-artifact": BenchmarkArtifactAdapter.json_schema(),
        "configuration-spec": ConfigurationSpec.model_json_schema(),
        "configuration-selection": ConfigurationSelection.model_json_schema(),
        "configuration-artifact": ConfigurationArtifact.model_json_schema(),
        "configuration-activation-receipt": ConfigurationActivationReceipt.model_json_schema(),
        "adapter-capability": AdapterCapability.model_json_schema(),
        "adapter-capability-artifact": AdapterCapabilityArtifact.model_json_schema(),
        "subject-spec": SubjectSpecAdapter.json_schema(),
        "subject-artifact": SubjectArtifactAdapter.json_schema(),
        "execution-spec": ExecutionSpec.model_json_schema(),
        "experiment-contrast": ExperimentContrast.model_json_schema(),
        "environment-spec": EnvironmentSpec.model_json_schema(),
        "environment-receipt": EnvironmentReceipt.model_json_schema(),
        "resource-spec": ResourceSpec.model_json_schema(),
        "credential-ref": CredentialRef.model_json_schema(),
        "case-order-artifact": CaseOrderArtifact.model_json_schema(),
        "case-set-artifact": CaseSetArtifact.model_json_schema(),
        "case-set-activation-receipt": CaseSetActivationReceipt.model_json_schema(),
        "custom-case-order-spec": CustomCaseOrderSpec.model_json_schema(),
        "checkpoint-load-receipt": CheckpointLoadReceipt.model_json_schema(),
        "checkpoint-save-receipt": CheckpointSaveReceipt.model_json_schema(),
        "provider-binding": ProviderBinding.model_json_schema(),
        "model-activation-receipt": ModelActivationReceipt.model_json_schema(),
        "model-activation-evidence": ModelActivationEvidence.model_json_schema(),
        "model-activation-usage": ModelActivationUsage.model_json_schema(),
        "record-index": RecordIndex.model_json_schema(),
        "resolved-execution-spec": ResolvedExecutionSpec.model_json_schema(),
        "evidence-bundle": EvidenceBundle.model_json_schema(),
        "evolution-candidate-record": EvolutionCandidateRecord.model_json_schema(),
        "evolution-transition-record": EvolutionTransitionRecord.model_json_schema(),
        "evolution-run-evidence": EvolutionRunEvidence.model_json_schema(),
        "evolution-budget-event": EvolutionBudgetEvent.model_json_schema(),
        "evolution-budget-ledger": EvolutionBudgetLedger.model_json_schema(),
        "evolution-evaluation-record": EvolutionEvaluationRecord.model_json_schema(),
        "evolution-runtime-receipt": EvolutionRuntimeReceipt.model_json_schema(),
        "evolution-sealed-holdout-receipt": EvolutionSealedHoldoutReceipt.model_json_schema(),
        "network-observation": NetworkObservation.model_json_schema(),
        "resolved-network-policy": ResolvedNetworkPolicy.model_json_schema(),
        "journal-record": JournalRecord.model_json_schema(),
        "integration-probe-record": IntegrationProbeRecord.model_json_schema(),
        "external-protocol-authority-receipt": ExternalProtocolAuthorityReceipt.model_json_schema(),
        "system-prompt-record": SystemPromptRecord.model_json_schema(),
        "workspace-record": WorkspaceRecord.model_json_schema(),
        "claim-report": ClaimReport.model_json_schema(mode="serialization"),
        "observation-report": ObservationReport.model_json_schema(),
        "run-report": RunReportAdapter.json_schema(mode="serialization"),
        "schedule-activation-receipt": ScheduleActivationReceipt.model_json_schema(),
        "statistical-analysis-plan": StatisticalAnalysisPlan.model_json_schema(),
        "statistical-analysis-receipt": StatisticalAnalysisReceipt.model_json_schema(),
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
