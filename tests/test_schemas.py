"""Contract and compiler conformance tests for BMP 0.1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import (
    AdapterCapability,
    AdapterCapabilityArtifact,
    ArtifactRef,
    IDENTITY_EXCLUDE,
    BackendSpec,
    BenchmarkSpecAdapter,
    Budget,
    ClaimDesign,
    ClaimReport,
    ComparisonKind,
    ConfigurationArtifact,
    ConfigurationSelection,
    ConfigurationSpec,
    EnvironmentReceipt,
    EnvironmentSpec,
    EvidenceBundle,
    EvaluatorArtifact,
    EvaluatorMetricBinding,
    EvaluatorSpec,
    ExecutionSpec,
    ExperimentContrast,
    GateName,
    LineageRef,
    MountSpec,
    MetricArtifact,
    MetricSpec,
    ObservationReport,
    PackageRecord,
    ProtocolSpec,
    ProvenanceRecord,
    ResolvedBmpManifest,
    ResolvedExecutionSpec,
    ResolvedManifestMetadata,
    RunPurpose,
    RunReportAdapter,
    SUBJECT_KIND_COMPARISON_MATRIX,
    SubjectSpecAdapter,
    VerifierEvidence,
    canonical_digest,
    check_allowed_diff,
    expand_factor_sweep,
    load_benchmark_spec,
    load_claim_report,
    load_dataset_spec,
    load_evidence_bundle,
    load_evaluator_spec,
    load_execution_spec,
    load_metric_spec,
    load_subject_spec,
    schema_documents,
)
from MagentaBench.schemas.models import SubjectKind
from MagentaBench.schemas.compiler import (
    _compile_benchmark_artifact as compile_benchmark_artifact,
    _compile_dataset_artifact as compile_dataset_artifact,
    _compile_subject_artifact as compile_subject_artifact,
    _resolve_execution_spec as resolve_execution_spec,
    _source_content_digest,
)

EXAMPLES = Path(__file__).parents[1] / "MagentaBench" / "schemas" / "examples"


def _round_trip(
    model: object,
    validator: object | None = None,
    *,
    derived_fields: set[str] | None = None,
) -> None:
    payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    input_payload = {
        key: value for key, value in payload.items() if key not in (derived_fields or set())
    }
    if validator is None:
        reconstructed = type(model).model_validate(input_payload)  # type: ignore[attr-defined]
    else:
        reconstructed = validator.validate_python(input_payload)  # type: ignore[attr-defined]
    assert reconstructed.model_dump(mode="json") == payload


def test_toml_round_trip_for_each_core_contract() -> None:
    benchmark = load_benchmark_spec(EXAMPLES / "benchmark.toml")
    dataset = load_dataset_spec(EXAMPLES / "dataset.toml")
    evaluator = load_evaluator_spec(EXAMPLES / "evaluator.toml")
    metric = load_metric_spec(EXAMPLES / "metric.toml")
    subject = load_subject_spec(EXAMPLES / "subject.toml")
    execution = load_execution_spec(EXAMPLES / "execution.toml")
    evidence = load_evidence_bundle(EXAMPLES / "evidence.toml")
    claim = load_claim_report(EXAMPLES / "claim.toml")

    _round_trip(benchmark, BenchmarkSpecAdapter)
    _round_trip(dataset)
    _round_trip(evaluator)
    _round_trip(metric)
    _round_trip(subject, SubjectSpecAdapter)
    _round_trip(execution)
    _round_trip(evidence)
    _round_trip(
        claim,
        derived_fields={"claim_eligible", "effect_is_causal_claim"},
    )

    assert evidence.status.value == "pass"
    assert claim.claim_eligible is True
    assert claim.effect_is_causal_claim is True


def test_typed_toml_loaders_reject_unknown_top_level_sections(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.toml"
    path.write_text(
        (EXAMPLES / "benchmark.toml").read_text(encoding="utf-8")
        + "\n[unexpected]\nvalue = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown top-level keys.*unexpected"):
        load_benchmark_spec(path)


def test_custom_benchmark_contract_is_adapter_owned_but_orthogonal() -> None:
    benchmark = BenchmarkSpecAdapter.validate_python(
        {
            "id": "custom.demo",
            "kind": "custom",
            "adapter": "external.benchmark",
            "bmp_version": "0.1",
        }
    )
    assert benchmark.kind == "custom"
    assert benchmark.adapter == "external.benchmark"

    with pytest.raises(ValidationError, match="source"):
        BenchmarkSpecAdapter.validate_python(
            benchmark.model_dump(mode="python") | {"source": "/tmp/data"}
        )


def test_json_schema_is_generated_for_public_contracts() -> None:
    documents = schema_documents()
    assert {
        "benchmark-spec",
        "dataset-spec",
        "evaluator-spec",
        "metric-spec",
        "subject-spec",
        "execution-spec",
        "environment-spec",
        "environment-receipt",
        "resource-spec",
        "credential-ref",
        "provider-binding",
        "model-activation-receipt",
        "evidence-bundle",
        "adapter-capability-artifact",
        "network-observation",
        "resolved-network-policy",
        "journal-record",
        "system-prompt-record",
        "workspace-record",
        "claim-report",
        "observation-report",
        "run-report",
        "resolved-bmp-manifest",
        "adapter-capability-artifact",
    }.issubset(documents)
    provenance_schema = documents["evidence-bundle"]["$defs"]["ProvenanceRecord"]
    assert "runtime_manifest_receipt" in provenance_schema["properties"]
    assert "model_activation" in provenance_schema["properties"]
    assert documents["subject-spec"]["discriminator"]["propertyName"] == "kind"
    assert "claim_eligible" in documents["claim-report"]["properties"]
    assert "effect_is_causal_claim" in documents["claim-report"]["properties"]
    gate_order = [
        "execution_valid",
        "protocol_valid",
        "isolation_valid",
        "scoring_valid",
        "statistics_valid",
    ]
    claim_schema = documents["claim-report"]
    assert claim_schema["properties"]["gates"]["propertyNames"]["enum"] == gate_order
    assert (
        claim_schema["allOf"][0]["if"]["properties"]["gates"]["required"]
        == gate_order
    )


def test_checked_in_json_schemas_exactly_match_public_models() -> None:
    documents = schema_documents()
    schema_root = Path(__file__).parents[1] / "MagentaBench/schemas/json"
    expected_names = {
        f"{name}.schema.json" for name in documents
    }
    observed_names = {
        path.name for path in schema_root.glob("*.schema.json")
    }

    assert observed_names == expected_names
    for name, document in documents.items():
        path = schema_root / f"{name}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8")) == document, name


def test_claim_design_is_required_and_closed(tmp_path: Path) -> None:
    manifest_payload = _manifest(tmp_path, created_at="now").model_dump(mode="python")
    manifest_payload.pop("claim_design")
    with pytest.raises(ValidationError, match="claim_design"):
        ResolvedBmpManifest.model_validate(manifest_payload)
    with pytest.raises(ValidationError, match="comparison_kind"):
        ClaimDesign.model_validate(
            {"comparison_kind": "invented", "purpose": "exploratory"}
        )
    with pytest.raises(ValidationError, match="purpose"):
        ClaimDesign.model_validate(
            {"comparison_kind": "coding_agent", "purpose": "invented"}
        )
    with pytest.raises(ValidationError, match="registered intervention"):
        ClaimDesign.model_validate(
            {"comparison_kind": "coding_agent", "purpose": "claim"}
        )


def test_experiment_contrast_is_required_closed_and_identity_bearing(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, created_at="now")
    payload = manifest.model_dump(mode="python")
    del payload["contrast"]
    with pytest.raises(ValidationError, match="contrast"):
        ResolvedBmpManifest.model_validate(payload)
    with pytest.raises(ValidationError, match="requires factor_id"):
        ExperimentContrast(mode="one_factor", counterbalanced=True)
    with pytest.raises(ValidationError, match="forbids control/treatment"):
        ExperimentContrast(
            mode="all_arms",
            control_level="control",
            counterbalanced=False,
        )

    filtered = manifest.model_copy(
        update={
            "contrast": ExperimentContrast(
                mode="one_factor",
                factor_id="agent.subject",
                control_level="control",
                treatment_level="treatment",
                counterbalanced=True,
            )
        }
    )
    assert manifest.canonical_digest() != filtered.canonical_digest()


def test_comparison_kind_and_purpose_are_identity_bearing(tmp_path: Path) -> None:
    base = _manifest(tmp_path, created_at="now")
    different_comparison = base.model_copy(
        update={
            "claim_design": ClaimDesign(
                comparison_kind=ComparisonKind.coding_agent,
                purpose=RunPurpose.exploratory,
            )
        }
    )
    different_purpose = base.model_copy(
        update={
            "claim_design": ClaimDesign(
                comparison_kind=ComparisonKind.coding_agent,
                purpose=RunPurpose.claim,
                intervention_factor_id="agent.subject",
            )
        }
    )
    assert len(
        {
            base.canonical_digest(),
            different_comparison.canonical_digest(),
            different_purpose.canonical_digest(),
        }
    ) == 3
    assert SUBJECT_KIND_COMPARISON_MATRIX["fake"] == frozenset()
    assert SUBJECT_KIND_COMPARISON_MATRIX["opaque_agent"] == frozenset(
        {ComparisonKind.agent, ComparisonKind.coding_agent}
    )


def test_observation_report_is_structurally_not_a_claim_report() -> None:
    report = ObservationReport(
        purpose=RunPurpose.exploratory,
        comparison_kind=None,
        subject_kinds=(SubjectKind.fake,),
        experiment_id="exploration",
        manifest_digest="a" * 64,
        isolation_valid=False,
        isolation_reasons=("network evidence unavailable",),
    )
    serialized = report.model_dump(mode="json")
    assert "claim_eligible" not in serialized
    assert "effect" not in serialized
    assert "gates" not in serialized
    assert serialized["isolation_valid"] is False
    assert isinstance(RunReportAdapter.validate_python(serialized), ObservationReport)
    with pytest.raises(ValidationError):
        RunReportAdapter.validate_python(serialized | {"claim_eligible": True})
    with pytest.raises(ValidationError, match="requires failure reasons"):
        ObservationReport.model_validate(
            serialized | {"isolation_valid": False, "isolation_reasons": []}
        )
    with pytest.raises(ValidationError, match="cannot have failure reasons"):
        ObservationReport.model_validate(serialized | {"isolation_valid": True})


def test_report_lineage_requires_locatable_evidence_refs() -> None:
    payload = {
        "run_id": "run-1",
        "attempt_id": "run-1__case-1__attempt-0",
        "case_id": "case-1",
        "evidence_bundle_ref": {
            "path": "/records/run-1/evidence_bundle.json",
            "sha256": "a" * 64,
            "size_bytes": 10,
        },
        "schedule_receipt_ref": {
            "path": "/records/run-1/schedule_activation_receipt.json",
            "sha256": "b" * 64,
            "size_bytes": 20,
        },
        "case_set_receipt_ref": {
            "path": "/records/run-1/case_set_activation_receipt.json",
            "sha256": "c" * 64,
            "size_bytes": 30,
        },
    }
    lineage = LineageRef.model_validate(payload)
    assert lineage.schedule_receipt_ref.sha256 == "b" * 64
    assert lineage.case_set_receipt_ref.sha256 == "c" * 64
    with pytest.raises(ValidationError, match="schedule_receipt_ref"):
        LineageRef.model_validate(
            {key: value for key, value in payload.items() if key != "schedule_receipt_ref"}
        )
    with pytest.raises(ValidationError, match="case_set_receipt_ref"):
        LineageRef.model_validate(
            {key: value for key, value in payload.items() if key != "case_set_receipt_ref"}
        )
    with pytest.raises(ValidationError, match="absolute|pattern"):
        LineageRef.model_validate(
            payload
            | {
                "schedule_receipt_ref": {
                    "path": "relative/receipt.json",
                    "sha256": "b" * 64,
                    "size_bytes": 20,
                }
            }
        )


def test_protocol_budget_is_fallback_and_normalized_out_of_resolved_identity() -> None:
    backend = BackendSpec(
        id="local",
        kind="local",
        adapter="subprocess",
        executable="/bin/true",
        digest="a" * 64,
    )
    protocol = ProtocolSpec(
        id="scaling",
        kind="test_time_scaling",
        adapter="magentabench.scheduler",
        candidate_selection="single",
        budget=Budget(max_tokens=20),
    )
    fallback = resolve_execution_spec(
        ExecutionSpec(backend="local", model="model"),
        backend=backend,
        protocol=protocol,
    )
    assert fallback.budget.max_tokens == 20
    assert fallback.protocol is not None
    assert fallback.protocol.budget is None

    explicit = resolve_execution_spec(
        ExecutionSpec(
            backend="local",
            model="model",
            budget=Budget(max_tokens=10),
        ),
        backend=backend,
        protocol=protocol,
    )
    assert explicit.budget.max_tokens == 10
    assert explicit.protocol is not None
    assert explicit.protocol.budget is None

    with pytest.raises(ValueError, match="must declare budget"):
        resolve_execution_spec(
            ExecutionSpec(backend="local", model="model", seed=1),
            backend=backend,
        )
    with pytest.raises(ValidationError, match="test_time_scaling"):
        ProtocolSpec(
            id="invented",
            kind="invented",
            adapter="magentabench.scheduler",
            candidate_selection="single",
        )


def test_environment_spec_requires_explicit_interpreter_and_names_only() -> None:
    with pytest.raises(ValidationError, match="python_version"):
        EnvironmentSpec.model_validate({"id": "tb2-default"})
    with pytest.raises(ValidationError, match="invalid environment variable names"):
        EnvironmentSpec(
            id="tb2-default",
            python_version="3.11",
            env_var_names=("OPENAI_API_KEY=secret",),
        )
    with pytest.raises(ValidationError, match="mount paths must be absolute"):
        MountSpec(
            host_path="relative/input",
            name="relative-input",
            content_sha256="a" * 64,
            container_path="/workspace/input",
        )

    environment = EnvironmentSpec(
        id="tb2-default",
        python_version="3.11",
        packages=("pytest>=8", "docker>=7"),
        env_var_names=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
        mounts=(
            MountSpec(
                host_path="/opt/benchmarks/tb2",
                name="terminal-bench-tasks",
                content_sha256="a" * 64,
                container_path="/workspace/tasks",
            ),
        ),
    )
    backend = BackendSpec(
        id="subprocess-default",
        kind="local",
        adapter="subprocess",
        executable="/usr/bin/env",
        digest="a" * 64,
        environment=environment,
    )
    _round_trip(environment)
    _round_trip(backend)
    assert backend.environment is not None
    assert backend.environment.python_version == "3.11"
    assert backend.environment.mounts[0].read_only is True


def test_backend_adapter_fields_are_closed_to_native_read_sets() -> None:
    with pytest.raises(ValidationError, match="fake forbids"):
        BackendSpec(
            id="fake-invalid",
            kind="local",
            adapter="fake",
            digest="a" * 64,
        )
    with pytest.raises(ValidationError, match="subprocess forbids"):
        BackendSpec(
            id="subprocess-invalid",
            kind="local",
            adapter="subprocess",
            executable="/bin/true",
            version="1",
            digest="a" * 64,
        )
    with pytest.raises(ValidationError, match="same image"):
        BackendSpec(
            id="aose-invalid",
            kind="container",
            adapter="aose-docker",
            image="sha256:" + "a" * 64,
            digest="b" * 64,
        )
    with pytest.raises(ValidationError, match="forbids backend image"):
        BackendSpec(
            id="harbor-invalid",
            kind="local",
            adapter="harbor",
            executable="/usr/bin/harbor",
            version="0.20.0",
            digest="a" * 64,
            image="sha256:" + "a" * 64,
        )


def test_environment_receipt_is_content_addressed_and_absolute() -> None:
    package = PackageRecord(
        name="pydantic",
        version="2.13.4",
    )
    receipt = EnvironmentReceipt(
        spec_id="tb2-default",
        spec_digest="b" * 64,
        python_executable="/opt/bmp/envs/tb2/bin/python",
        python_version="3.11.13",
        installed_packages=(package,),
        build_duration_seconds=312.5,
        built_at="2026-08-06T15:47:00Z",
    )
    _round_trip(receipt)
    with pytest.raises(ValidationError, match="absolute path"):
        EnvironmentReceipt.model_validate(
            receipt.model_dump(mode="python") | {"python_executable": "bin/python"}
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PackageRecord(name="broken", version="1", sha256="unverified")


def test_provenance_rejects_plaintext_environment_maps() -> None:
    with pytest.raises(ValidationError, match="environment"):
        ProvenanceRecord.model_validate(
            {
                "manifest_digest": "a" * 64,
                "runner_digest": "b" * 64,
                "benchmark_digest": "c" * 64,
                "subject_digest": "d" * 64,
                "backend_digest": "sha256:backend",
                "environment": {"OPENAI_API_KEY": "secret"},
            }
        )


def test_provenance_records_optional_executable_digest() -> None:
    provenance = ProvenanceRecord(
        manifest_digest="a" * 64,
        runner_digest="b" * 64,
        benchmark_digest="c" * 64,
        subject_digest="d" * 64,
        backend_digest="sha256:backend",
        executable="/usr/bin/echo",
        executable_digest="e" * 64,
    )
    _round_trip(provenance)
    with pytest.raises(ValidationError, match="executable_digest"):
        ProvenanceRecord.model_validate(
            provenance.model_dump(mode="python")
            | {"executable_digest": "not-a-sha256"}
        )


def test_provenance_rejects_key_value_strings() -> None:
    with pytest.raises(ValidationError, match="possible key=value secret"):
        ProvenanceRecord(
            manifest_digest="a" * 64,
            runner_digest="b" * 64,
            benchmark_digest="c" * 64,
            subject_digest="d" * 64,
            backend_digest="sha256:backend",
            backend_kind="KEY=secret",
        )


def test_generic_metadata_maps_reject_secret_like_keys() -> None:
    with pytest.raises(ValidationError, match="secret-like key"):
        BackendSpec(
            id="unsafe-backend",
            kind="local",
            adapter="subprocess",
            executable="/bin/true",
            digest="a" * 64,
            defaults={"OPENAI_API_KEY": "secret"},
        )
    with pytest.raises(ValidationError, match="secret-like key"):
        ExecutionSpec(
            backend="unsafe-backend",
            model="provider/model",
            seed=0,
            budget=Budget(max_tokens=1),
            backend_overrides={"nested": {"access_token": "secret"}},
        )
    with pytest.raises(ValidationError, match="secret-like key"):
        VerifierEvidence(
            verifier="unsafe-verifier",
            passed=True,
            details={"password": "secret"},
        )


def test_unknown_discriminated_kinds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        BenchmarkSpecAdapter.validate_python(
            {
                "id": "unknown-benchmark",
                "kind": "mystery",
                "adapter": "fake",
                "source": "/tmp",
                "commit": "abc",
            }
        )
    with pytest.raises(ValidationError):
        SubjectSpecAdapter.validate_python(
            {"id": "unknown-subject", "kind": "mystery", "adapter": "fake"}
        )


def _artifact_ref(path: Path) -> ArtifactRef:
    content = path.read_bytes()
    return ArtifactRef(
        path=str(path.resolve()),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _compile_evaluator_artifact(
    spec: EvaluatorSpec,
    declaration_path: Path,
) -> EvaluatorArtifact:
    provisional = EvaluatorArtifact(
        evaluator=spec,
        declaration_ref=_artifact_ref(declaration_path),
        artifact_digest="0" * 64,
    )
    return provisional.model_copy(
        update={"artifact_digest": provisional.canonical_digest()}
    )


def _compile_metric_artifact(
    spec: MetricSpec,
    declaration_path: Path,
) -> MetricArtifact:
    provisional = MetricArtifact(
        metric=spec,
        declaration_ref=_artifact_ref(declaration_path),
        artifact_digest="0" * 64,
    )
    return provisional.model_copy(
        update={"artifact_digest": provisional.canonical_digest()}
    )


def _reward_binding(
    *,
    source_key: str = "score",
    success_threshold: float | None = 1.0,
) -> EvaluatorMetricBinding:
    return EvaluatorMetricBinding(
        metric_id="reward.authoritative.v1",
        source_key=source_key,
        authoritative=True,
        success_operator="eq" if success_threshold is not None else None,
        success_threshold=success_threshold,
    )


def test_evaluator_scoring_semantics_are_complete_and_identity_bearing(
    tmp_path: Path,
) -> None:
    declaration = {
        "id": "evaluator.scored.v1",
        "kind": "evaluator",
        "adapter": "fake",
        "implementation": "fake.scored.v1",
        "metrics": (_reward_binding().model_dump(mode="python"),),
    }
    with pytest.raises(ValidationError, match="scoring_kind"):
        EvaluatorSpec.model_validate(declaration)
    with pytest.raises(ValidationError, match="requires success operator and threshold"):
        EvaluatorSpec.model_validate(
            declaration
            | {
                "scoring_kind": "binary",
                "metrics": (
                    _reward_binding(success_threshold=None).model_dump(mode="python"),
                ),
            }
        )
    with pytest.raises(ValidationError, match="continuous.*forbids success rules"):
        EvaluatorSpec.model_validate(declaration | {"scoring_kind": "continuous"})

    declaration_path = tmp_path / "evaluator-binary.toml"
    declaration_path.write_text(
        '''[evaluator]
id = "evaluator.scored.v1"
kind = "evaluator"
adapter = "fake"
bmp_version = "0.1"
implementation = "fake.scored.v1"
scoring_kind = "binary"

[[evaluator.metrics]]
metric_id = "reward.authoritative.v1"
source_key = "score"
authoritative = true
success_operator = "eq"
success_threshold = 1.0
''',
        encoding="utf-8",
    )
    binary = _compile_evaluator_artifact(
        load_evaluator_spec(declaration_path),
        declaration_path,
    )
    threshold_path = tmp_path / "evaluator-threshold.toml"
    threshold_path.write_text(
        declaration_path.read_text(encoding="utf-8").replace(
            "success_threshold = 1.0",
            "success_threshold = 0.5",
        ),
        encoding="utf-8",
    )
    changed_threshold = _compile_evaluator_artifact(
        load_evaluator_spec(threshold_path),
        threshold_path,
    )
    continuous_path = tmp_path / "evaluator-continuous.toml"
    continuous_path.write_text(
        '''[evaluator]
id = "evaluator.scored.v1"
kind = "evaluator"
adapter = "fake"
bmp_version = "0.1"
implementation = "fake.scored.v1"
scoring_kind = "continuous"

[[evaluator.metrics]]
metric_id = "reward.authoritative.v1"
source_key = "overall"
authoritative = true
''',
        encoding="utf-8",
    )
    continuous = _compile_evaluator_artifact(
        load_evaluator_spec(continuous_path),
        continuous_path,
    )
    assert len(
        {
            binary.artifact_digest,
            changed_threshold.artifact_digest,
            continuous.artifact_digest,
        }
    ) == 3

    base_manifest = _manifest(tmp_path, created_at="now")
    threshold_manifest = base_manifest.model_copy(
        update={"evaluator": changed_threshold}
    )
    continuous_manifest = base_manifest.model_copy(update={"evaluator": continuous})
    assert len(
        {
            base_manifest.canonical_digest(),
            threshold_manifest.canonical_digest(),
            continuous_manifest.canonical_digest(),
        }
    ) == 3


def test_dataset_artifact_compile_normalizes_source_and_digest(
    tmp_path: Path,
) -> None:
    (tmp_path / "tasks.toml").write_text("fake = true\n", encoding="utf-8")
    declaration_path = tmp_path / "dataset.toml"
    declaration_path.write_text(
        '''[dataset]
id = "dataset.fake.v1"
kind = "dataset"
adapter = "fake"
bmp_version = "0.1"
source = "."
commit = "content-sha"
content_globs = ["tasks.toml"]
format = "toml-task-suite"
''',
        encoding="utf-8",
    )
    artifact = compile_dataset_artifact(
        load_dataset_spec(declaration_path),
        declaration_path=declaration_path,
    )
    assert Path(artifact.source).is_absolute()
    assert artifact.source == str(tmp_path.resolve())
    assert len(artifact.artifact_digest) == 64
    assert artifact.artifact_digest == artifact.canonical_digest()


def test_source_paths_are_provenance_only_but_declared_content_is_identity(
    tmp_path: Path,
) -> None:
    first_package = tmp_path / "first"
    second_package = tmp_path / "second"
    first_root = first_package / "data"
    second_root = second_package / "data"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    for root in (first_root, second_root):
        (root / "tasks.toml").write_text("task = 'same'\n", encoding="utf-8")
    dataset_toml = '''[dataset]
id = "dataset.cross-root.v1"
kind = "dataset"
adapter = "fake"
bmp_version = "0.1"
source = "data"
content_globs = ["tasks.toml"]
format = "toml-task-suite"
'''
    first_declaration = first_package / "dataset.toml"
    second_declaration = second_package / "dataset.toml"
    first_declaration.write_text(dataset_toml, encoding="utf-8")
    second_declaration.write_text(dataset_toml, encoding="utf-8")
    first = compile_dataset_artifact(
        load_dataset_spec(first_declaration),
        declaration_path=first_declaration,
    )
    second = compile_dataset_artifact(
        load_dataset_spec(second_declaration),
        declaration_path=second_declaration,
    )
    assert first.source != second.source
    assert first.source_content_digest == second.source_content_digest
    assert first.artifact_digest == second.artifact_digest

    first_manifest = _manifest(first_root, created_at="now")
    second_manifest = _manifest(second_root, created_at="now")
    assert first_manifest.canonical_digest() == second_manifest.canonical_digest()

    (second_root / "tasks.toml").write_text("task = 'changed'\n", encoding="utf-8")
    changed = compile_dataset_artifact(
        load_dataset_spec(second_declaration),
        declaration_path=second_declaration,
    )
    assert changed.source_content_digest != first.source_content_digest
    assert changed.artifact_digest != first.artifact_digest

    subject_declaration = {
        "id": "opaque-cross-root",
        "kind": "opaque_agent",
        "comparison_kind": "coding_agent",
        "adapter": "cli-agent",
        "entrypoint": "/usr/bin/python3",
        "launch_argv": ("/usr/bin/python3", "-c", "print('same')"),
        "interface": "task_to_output",
        "commit": None,
    }
    subject_first = compile_subject_artifact(
        SubjectSpecAdapter.validate_python(
            subject_declaration | {"source": str(first_root)}
        )
    )
    subject_second = compile_subject_artifact(
        SubjectSpecAdapter.validate_python(
            subject_declaration | {"source": str(second_root)}
        )
    )
    assert subject_first.source != subject_second.source
    assert subject_first.artifact_digest == subject_second.artifact_digest
    changed_subject = compile_subject_artifact(
        SubjectSpecAdapter.validate_python(
            subject_declaration
            | {
                "source": str(second_root),
                "launch_argv": (
                    "/usr/bin/python3",
                    "-c",
                    "print('changed')",
                ),
            }
        )
    )
    assert changed_subject.artifact_digest != subject_first.artifact_digest


def _git_source(root: Path) -> str:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "bmp@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "BMP Tests"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "fixture"], check=True
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_opaque_subject_explicit_content_closure_binds_driver_bytes(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-subject"
    second_root = tmp_path / "second-subject"
    first_root.mkdir()
    second_root.mkdir()
    for root in (first_root, second_root):
        (root / "driver.py").write_text("print('same')\n", encoding="utf-8")
    declaration = {
        "id": "opaque-content-bound",
        "kind": "opaque_agent",
        "comparison_kind": "agent",
        "adapter": "native-driver",
        "bmp_version": "0.1",
        "entrypoint": "/usr/bin/python3",
        "launch_argv": ("/usr/bin/python3", "{subject_source}/driver.py"),
        "interface": "native-benchmark-v1",
        "content_globs": ("driver.py",),
    }
    first = compile_subject_artifact(
        SubjectSpecAdapter.validate_python(
            declaration | {"source": str(first_root)}
        )
    )
    second = compile_subject_artifact(
        SubjectSpecAdapter.validate_python(
            declaration | {"source": str(second_root)}
        )
    )
    assert first.source_content_digest == second.source_content_digest
    assert first.artifact_digest == second.artifact_digest

    (second_root / "driver.py").write_text("print('changed')\n", encoding="utf-8")
    changed = compile_subject_artifact(
        SubjectSpecAdapter.validate_python(
            declaration | {"source": str(second_root)}
        )
    )
    assert changed.source_content_digest != first.source_content_digest
    assert changed.artifact_digest != first.artifact_digest


def test_dataset_source_rejects_mismatch_dirty_untracked_and_symlink(
    tmp_path: Path,
) -> None:
    git_root = tmp_path / "git"
    git_root.mkdir()
    (git_root / "tasks.toml").write_text("task = true\n", encoding="utf-8")
    head = _git_source(git_root)
    declaration_path = tmp_path / "dataset.toml"
    declaration = f'''[dataset]
id = "dataset.git-source.v1"
kind = "dataset"
adapter = "fake"
bmp_version = "0.1"
source = "{git_root.as_posix()}"
commit = "{head}"
content_globs = ["tasks.toml"]
format = "toml-task-suite"
'''
    declaration_path.write_text(declaration, encoding="utf-8")
    artifact = compile_dataset_artifact(
        load_dataset_spec(declaration_path),
        declaration_path=declaration_path,
    )
    assert artifact.commit == head
    declaration_path.write_text(
        declaration.replace(head, "0" * 40),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match checkout HEAD"):
        compile_dataset_artifact(
            load_dataset_spec(declaration_path),
            declaration_path=declaration_path,
        )
    declaration_path.write_text(declaration, encoding="utf-8")

    (git_root / "tasks.toml").write_text("task = false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty or untracked"):
        compile_dataset_artifact(
            load_dataset_spec(declaration_path),
            declaration_path=declaration_path,
        )

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    (symlink_root / "real.toml").write_text("task = true\n", encoding="utf-8")
    (symlink_root / "tasks.toml").symlink_to("real.toml")
    declaration_path.write_text(
        declaration.replace(git_root.as_posix(), symlink_root.as_posix()).replace(
            f'commit = "{head}"\n',
            "",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="symlink"):
        compile_dataset_artifact(
            load_dataset_spec(declaration_path),
            declaration_path=declaration_path,
        )


def test_required_content_pattern_failure_is_not_masked(tmp_path: Path) -> None:
    root = tmp_path / "masked"
    for index in range(5):
        task_dir = root / "tasks" / f"case-{index}"
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text("task = true\n", encoding="utf-8")
    missing_pattern = "tasks/*/nonexistent_*.xyz"
    with pytest.raises(ValueError, match=r"tasks/\*/nonexistent_\*\.xyz"):
        _source_content_digest(
            root,
            patterns=("tasks/*/task.toml", missing_pattern),
            declared_commit=None,
            adapter="test-adapter",
        )


def test_nested_dataset_dependency_mutation_changes_content_digest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tool-suite"
    nested = root / "benchmark" / "tasks" / "case-1"
    nested.mkdir(parents=True)
    task = nested / "task.toml"
    tests_root = nested / "tests"
    tests_root.mkdir()
    judge = tests_root / "llm_judge.py"
    test_sh = tests_root / "test.sh"
    instruction = nested / "instruction.md"
    rubric = tests_root / "rubric.txt"
    task.write_text("name = 'one'\n", encoding="utf-8")
    instruction.write_text("Do the task.\n", encoding="utf-8")
    rubric.write_text("Score the task.\n", encoding="utf-8")
    judge.write_text("SCORE = 1\n", encoding="utf-8")
    test_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    declaration_path = tmp_path / "dataset.toml"
    declaration_path.write_text(
        f'''[dataset]
id = "dataset.tool-source.v1"
kind = "dataset"
adapter = "aosebench"
bmp_version = "0.1"
source = "{root.as_posix()}"
content_globs = [
  "benchmark/tasks/*/task.toml",
  "benchmark/tasks/*/instruction.md",
  "benchmark/tasks/*/tests/rubric.txt",
  "benchmark/tasks/*/tests/llm_judge.py",
  "benchmark/tasks/*/tests/test.sh",
]
format = "aosebench-task-suite"

[dataset.config]
task_root = "benchmark/tasks"
''',
        encoding="utf-8",
    )
    first = compile_dataset_artifact(
        load_dataset_spec(declaration_path),
        declaration_path=declaration_path,
    )
    task.write_text("name = 'two'\n", encoding="utf-8")
    second = compile_dataset_artifact(
        load_dataset_spec(declaration_path),
        declaration_path=declaration_path,
    )
    assert first.source_content_digest != second.source_content_digest
    assert first.artifact_digest != second.artifact_digest
    task.write_text("name = 'one'\n", encoding="utf-8")
    judge.write_text("SCORE = 2\n", encoding="utf-8")
    judge_changed = compile_dataset_artifact(
        load_dataset_spec(declaration_path),
        declaration_path=declaration_path,
    )
    assert judge_changed.source_content_digest != first.source_content_digest
    assert judge_changed.artifact_digest != first.artifact_digest
    judge.write_text("SCORE = 1\n", encoding="utf-8")
    test_sh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    test_changed = compile_dataset_artifact(
        load_dataset_spec(declaration_path),
        declaration_path=declaration_path,
    )
    assert test_changed.source_content_digest != first.source_content_digest
    assert test_changed.artifact_digest != first.artifact_digest
    test_sh.unlink()
    with pytest.raises(ValueError, match=r"tests/test\.sh"):
        compile_dataset_artifact(
            load_dataset_spec(declaration_path),
            declaration_path=declaration_path,
        )


def _manifest(tmp_path: Path, *, created_at: str, seed: int = 7):
    (tmp_path / "tasks.toml").write_text("fake = true\n", encoding="utf-8")
    benchmark_path = tmp_path / "benchmark.toml"
    benchmark_path.write_text(
        '''[benchmark]
id = "fake-benchmark"
kind = "task_suite"
adapter = "fake"
bmp_version = "0.1"
''',
        encoding="utf-8",
    )
    benchmark = compile_benchmark_artifact(
        load_benchmark_spec(benchmark_path),
        declaration_path=benchmark_path,
    )
    dataset_path = tmp_path / "dataset.toml"
    dataset_path.write_text(
        '''[dataset]
id = "dataset.fake.v1"
kind = "dataset"
adapter = "fake"
bmp_version = "0.1"
source = "."
commit = "content-sha"
content_globs = ["tasks.toml"]
format = "toml-task-suite"
split = "test"

[dataset.config]
task_manifest = "tasks.toml"
''',
        encoding="utf-8",
    )
    dataset = compile_dataset_artifact(
        load_dataset_spec(dataset_path),
        declaration_path=dataset_path,
    )
    evaluator_path = tmp_path / "evaluator.toml"
    evaluator_path.write_text(
        '''[evaluator]
id = "evaluator.fake.v1"
kind = "evaluator"
adapter = "fake"
bmp_version = "0.1"
implementation = "fake.exact.v1"
scoring_kind = "binary"

[[evaluator.metrics]]
metric_id = "reward.authoritative.v1"
source_key = "score"
authoritative = true
success_operator = "eq"
success_threshold = 1.0
absolute_tolerance = 0.0
''',
        encoding="utf-8",
    )
    evaluator = _compile_evaluator_artifact(
        load_evaluator_spec(evaluator_path),
        evaluator_path,
    )
    metric_path = tmp_path / "metric.toml"
    metric_path.write_text(
        '''[metric]
id = "reward.authoritative.v1"
kind = "metric"
adapter = "magentabench.measurement"
bmp_version = "0.1"
value_kind = "continuous"
level = "rollout"
direction = "maximize"
unit = "reward"
source = "evaluator"
source_field = "evaluator_binding"
formula = "direct_v1"
population = "evaluator_observations"
missing_observation = "invalidate"
''',
        encoding="utf-8",
    )
    metric = _compile_metric_artifact(
        load_metric_spec(metric_path),
        metric_path,
    )
    subject = compile_subject_artifact(
        SubjectSpecAdapter.validate_python(
            {"id": "fake-subject", "kind": "fake", "fixed_answer": "BMP_OK"}
        )
    )
    backend = BackendSpec(
        id="local-fake",
        kind="local",
        adapter="fake",
    )
    execution = ResolvedExecutionSpec(
        backend=backend,
        model="fake/model",
        seed=seed,
        budget=Budget(max_tokens=10),
        protocol=ProtocolSpec(
            id="deterministic-v1",
            kind="mechanism_validation",
            adapter="magentabench.scheduler",
            case_order="seeded_random",
            candidate_selection="exact",
            deterministic_conformance=True,
        ),
    )
    return ResolvedBmpManifest(
        benchmark=benchmark,
        dataset=dataset,
        evaluator=evaluator,
        metrics=(metric,),
        subject=subject,
        execution=execution,
        claim_design=ClaimDesign(
            comparison_kind=None,
            purpose=RunPurpose.exploratory,
        ),
        contrast=ExperimentContrast(
            mode="all_arms",
            counterbalanced=False,
        ),
        metadata=ResolvedManifestMetadata(
            experiment_id="conformance",
            run_id="conformance__run0000",
        ),
        created_at=created_at,
    )


def test_manifest_digest_is_deterministic_and_excludes_volatile_fields(
    tmp_path: Path,
) -> None:
    first = _manifest(tmp_path, created_at="2026-01-01T00:00:00Z")
    second = first.model_copy(
        update={
            "created_at": "2027-01-01T00:00:00Z",
            "wall_clock_start": "2027-01-01T00:00:01Z",
            "wall_clock_end": "2027-01-01T00:00:02Z",
            "record_root": "/different/root",
            "resume_count": 9,
            "runner_invocation_id": "different-invocation",
        }
    )
    assert first.canonical_digest() == second.canonical_digest()
    assert canonical_digest(first) == first.canonical_digest()
    assert first.IDENTITY_EXCLUDE == IDENTITY_EXCLUDE
    assert IDENTITY_EXCLUDE == frozenset(
        {
            "created_at",
            "wall_clock_start",
            "wall_clock_end",
            "record_root",
            "resume_count",
            "runner_invocation_id",
        }
    )

    changed_identity = first.model_copy(
        update={"execution": first.execution.model_copy(update={"seed": 8})}
    )
    assert first.canonical_digest() != changed_identity.canonical_digest()


def test_configuration_trees_are_deeply_immutable_and_digest_stable() -> None:
    spec = ConfigurationSpec(
        id="agent.config",
        kind="configuration",
        adapter="agent",
        values={"agent": {"models": ["gpt-5.4"]}},
        schema={"type": "object", "properties": {"agent": {}}},
    )
    selection = ConfigurationSelection(
        values={"runtime": {"parallelism": 2}},
    )
    artifact = ConfigurationArtifact(
        id="agent.config",
        adapter="agent",
        values=spec.values,
        json_schema=spec.json_schema,
        ownership={"agent.models": "agent"},
        schema_digest="b" * 64,
        artifact_digest="0" * 64,
    )
    digest = artifact.canonical_digest()

    for mutate in (
        lambda: spec.values["agent"].__setitem__("new", True),
        lambda: spec.values["agent"]["models"].append("claude-opus-4.6"),
        lambda: selection.values.__setitem__("new", True),
        lambda: artifact.values["agent"]["models"].append("claude-opus-4.6"),
        lambda: artifact.json_schema.__setitem__("required", ["agent"]),
        lambda: artifact.ownership.__setitem__("runtime", "runtime"),
    ):
        with pytest.raises(TypeError, match="immutable"):
            mutate()
    copied = artifact.model_copy(update={"values": {"copy": {"safe": True}}})
    with pytest.raises(TypeError, match="immutable"):
        copied.values["copy"]["safe"] = False
    assert artifact.canonical_digest() == digest


@pytest.mark.parametrize("invalid", (False, 0, [], ""))
def test_configuration_object_fields_reject_explicit_falsy_non_mappings(
    invalid: object,
) -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        ConfigurationSpec(
            id="invalid.config",
            kind="configuration",
            adapter="generic",
            values=invalid,
        )
    with pytest.raises(ValidationError, match="JSON object"):
        ConfigurationSelection(values=invalid)


def test_manifest_artifact_ref_locations_are_not_identity(tmp_path: Path) -> None:
    base = _manifest(tmp_path, created_at="now")

    def metadata(root: str) -> ResolvedManifestMetadata:
        configuration = ConfigurationArtifact(
            id="agent.config",
            adapter="generic",
            source_refs=(
                ArtifactRef(
                    path=f"/{root}/configuration.toml",
                    sha256="a" * 64,
                    size_bytes=11,
                ),
            ),
            schema_digest="b" * 64,
            values={
                "agent": {"model": "same-model"},
                # Adapter-owned mappings can look like an ArtifactRef but are
                # semantic configuration and must not be shape-stripped.
                "output": {
                    "path": "/semantic/output",
                    "sha256": "not-an-artifact-digest",
                    "size_bytes": 5,
                },
            },
            artifact_digest="0" * 64,
        )
        configuration = configuration.model_copy(
            update={"artifact_digest": configuration.canonical_digest()}
        )
        capability = AdapterCapability(
            id="external.demo",
            kind="adapter",
            adapter="external.demo",
            adapter_kind="benchmark_loader",
            source="plugins/demo",
            entrypoint="loader.py:Loader",
            digest="c" * 64,
        )
        adapter_artifact = AdapterCapabilityArtifact(
            capability=capability,
            declaration_ref=ArtifactRef(
                path=f"/{root}/adapter.toml",
                sha256="d" * 64,
                size_bytes=13,
            ),
            implementation_ref=ArtifactRef(
                path=f"/{root}/loader.py",
                sha256=capability.digest,
                size_bytes=17,
            ),
            artifact_digest="0" * 64,
        )
        adapter_artifact = adapter_artifact.model_copy(
            update={"artifact_digest": adapter_artifact.canonical_digest()}
        )
        return base.metadata.model_copy(
            update={
                "configuration": configuration,
                "adapter_capabilities": (adapter_artifact,),
            }
        )

    first = base.model_copy(update={"metadata": metadata("first/root")})
    second = base.model_copy(update={"metadata": metadata("second/root")})

    assert first.canonical_digest() == second.canonical_digest()
    identity = first.identity_data()
    assert "path" not in identity["metadata"]["configuration"]["source_refs"][0]
    assert (
        "path"
        not in identity["metadata"]["adapter_capabilities"][0]["declaration_ref"]
    )
    assert (
        identity["metadata"]["configuration"]["values"]["output"]["path"]
        == "/semantic/output"
    )


def test_allowed_diff_checker_uses_exact_dotted_leaf_paths(tmp_path: Path) -> None:
    control = _manifest(tmp_path, created_at="now", seed=7)
    treatment = control.model_copy(
        update={"execution": control.execution.model_copy(update={"seed": 8})}
    )
    accepted = check_allowed_diff(control, treatment, {"execution.seed"})
    rejected = check_allowed_diff(control, treatment, {"execution"})
    assert accepted.valid is True
    assert accepted.differing_paths == ("execution.seed",)
    assert rejected.valid is False
    assert rejected.disallowed_paths == ("execution.seed",)


def test_factor_sweep_order_and_ids_are_deterministic() -> None:
    runs = expand_factor_sweep(
        "factor-test",
        {"model": ["z", "a"], "budget": [20, 10]},
    )
    assert [run.run_id for run in runs] == [
        "factor-test__run0000",
        "factor-test__run0001",
        "factor-test__run0002",
        "factor-test__run0003",
    ]
    assert [run.factors for run in runs] == [
        {"budget": 10, "model": "a"},
        {"budget": 10, "model": "z"},
        {"budget": 20, "model": "a"},
        {"budget": 20, "model": "z"},
    ]


def test_ineligible_claim_may_keep_descriptive_effect() -> None:
    payload = load_claim_report(EXAMPLES / "claim.toml").model_dump(
        mode="python",
        exclude={"claim_eligible", "effect_is_causal_claim"},
    )
    payload["gates"][GateName.statistics_valid] = {
        "valid": False,
        "reason": "insufficient pairs",
    }
    report = ClaimReport.model_validate(payload)
    assert report.effect is not None
    assert report.claim_eligible is False
    assert report.effect_is_causal_claim is False
