"""Contract and compiler conformance tests for BMP 0.1."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import (
    IDENTITY_EXCLUDE,
    BackendSpec,
    BenchmarkSpecAdapter,
    Budget,
    ClaimDesign,
    ClaimReport,
    ClaimScope,
    EnvironmentReceipt,
    EnvironmentSpec,
    EvidenceBundle,
    ExecutionSpec,
    ExperimentContrast,
    GateName,
    LineageRef,
    MountSpec,
    ObservationReport,
    PackageRecord,
    ProtocolSpec,
    ProvenanceRecord,
    ResolvedBmpManifest,
    ResolvedExecutionSpec,
    ResolvedManifestMetadata,
    RunPurpose,
    RunReportAdapter,
    SUBJECT_KIND_SCOPE_MATRIX,
    SubjectSpecAdapter,
    VerifierEvidence,
    canonical_digest,
    check_allowed_diff,
    expand_factor_sweep,
    load_benchmark_spec,
    load_claim_report,
    load_evidence_bundle,
    load_execution_spec,
    load_subject_spec,
    schema_documents,
)
from MagentaBench.schemas.models import SubjectKind
from MagentaBench.schemas.compiler import (
    _compile_benchmark_artifact as compile_benchmark_artifact,
    _compile_subject_artifact as compile_subject_artifact,
    _resolve_execution_spec as resolve_execution_spec,
)
from MagentaBench.schemas.compiler import (
    _compile_benchmark_artifact as compile_benchmark_artifact,
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
    subject = load_subject_spec(EXAMPLES / "subject.toml")
    execution = load_execution_spec(EXAMPLES / "execution.toml")
    evidence = load_evidence_bundle(EXAMPLES / "evidence.toml")
    claim = load_claim_report(EXAMPLES / "claim.toml")

    _round_trip(benchmark, BenchmarkSpecAdapter)
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


def test_custom_benchmark_contract_is_adapter_owned() -> None:
    benchmark = BenchmarkSpecAdapter.validate_python(
        {
            "id": "custom.demo",
            "kind": "custom",
            "adapter": "external.benchmark",
            "bmp_version": "0.1",
            "source": "/tmp/custom-benchmark",
            "content_globs": ("tasks/*.json", "verifier.py"),
            "verifier": "external.verifier:v1",
            "scoring_kind": "continuous",
            "authoritative_reward_metric": "quality",
            "config": {"dataset": "demo"},
        }
    )
    assert benchmark.kind == "custom"
    assert benchmark.adapter == "external.benchmark"
    assert benchmark.config["dataset"] == "demo"


def test_json_schema_is_generated_for_public_contracts() -> None:
    documents = schema_documents()
    assert {
        "benchmark-spec",
        "subject-spec",
        "execution-spec",
        "environment-spec",
        "environment-receipt",
        "resource-spec",
        "credential-ref",
        "provider-binding",
        "evidence-bundle",
        "network-observation",
        "resolved-network-policy",
        "journal-record",
        "system-prompt-record",
        "workspace-record",
        "claim-report",
        "observation-report",
        "run-report",
        "resolved-bmp-manifest",
    }.issubset(documents)
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


def test_claim_design_is_required_and_closed(tmp_path: Path) -> None:
    manifest_payload = _manifest(tmp_path, created_at="now").model_dump(mode="python")
    manifest_payload.pop("claim_design")
    with pytest.raises(ValidationError, match="claim_design"):
        ResolvedBmpManifest.model_validate(manifest_payload)
    with pytest.raises(ValidationError, match="scope"):
        ClaimDesign.model_validate(
            {"scope": "invented", "purpose": "exploratory", "vary": []}
        )
    with pytest.raises(ValidationError, match="purpose"):
        ClaimDesign.model_validate(
            {"scope": "conformance", "purpose": "invented", "vary": []}
        )
    with pytest.raises(ValidationError, match="vary"):
        ClaimDesign.model_validate(
            {"scope": "conformance", "purpose": "exploratory"}
        )


def test_experiment_contrast_is_required_closed_and_identity_bearing(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, created_at="now")
    payload = manifest.model_dump(mode="python")
    del payload["contrast"]
    with pytest.raises(ValidationError, match="contrast"):
        ResolvedBmpManifest.model_validate(payload)
    with pytest.raises(ValidationError, match="requires control_id and treatment_id"):
        ExperimentContrast(mode="one_factor", counterbalanced=True)
    with pytest.raises(ValidationError, match="forbids arm filtering"):
        ExperimentContrast(
            mode="all_arms",
            control_id="fake.control",
            counterbalanced=False,
        )

    filtered = manifest.model_copy(
        update={
            "contrast": ExperimentContrast(
                mode="one_factor",
                control_id="fake.control",
                treatment_id="fake.treatment",
                counterbalanced=True,
            )
        }
    )
    assert manifest.canonical_digest() != filtered.canonical_digest()


def test_claim_scope_and_purpose_are_identity_bearing(tmp_path: Path) -> None:
    base = _manifest(tmp_path, created_at="now")
    different_scope = base.model_copy(
        update={
            "claim_design": ClaimDesign(
                scope=ClaimScope.whole_harness,
                purpose=RunPurpose.exploratory,
                vary=("subject.artifact_digest",),
            )
        }
    )
    different_purpose = base.model_copy(
        update={
            "claim_design": ClaimDesign(
                scope=ClaimScope.conformance,
                purpose=RunPurpose.claim,
                vary=(),
            )
        }
    )
    assert len(
        {
            base.canonical_digest(),
            different_scope.canonical_digest(),
            different_purpose.canonical_digest(),
        }
    ) == 3
    assert SUBJECT_KIND_SCOPE_MATRIX["fake"] == frozenset({ClaimScope.conformance})
    assert ClaimScope.component not in SUBJECT_KIND_SCOPE_MATRIX["opaque_agent"]


def test_observation_report_is_structurally_not_a_claim_report() -> None:
    report = ObservationReport(
        purpose=RunPurpose.exploratory,
        subject_kind=SubjectKind.fake,
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


def test_benchmark_scoring_semantics_are_complete_and_identity_bearing(
    tmp_path: Path,
) -> None:
    (tmp_path / "tasks.toml").write_text("fake = true\n", encoding="utf-8")
    declaration = {
        "id": "scored-benchmark",
        "kind": "task_suite",
        "adapter": "fake",
        "source": str(tmp_path),
        "commit": "content-sha",
        "task_manifest": "tasks.toml",
        "verifier": "native",
    }
    with pytest.raises(ValidationError, match="scoring_kind"):
        BenchmarkSpecAdapter.validate_python(declaration)
    with pytest.raises(ValidationError, match="binary scoring requires"):
        BenchmarkSpecAdapter.validate_python(
            declaration
            | {
                "scoring_kind": "binary",
                "authoritative_reward_metric": "score",
            }
        )
    with pytest.raises(ValidationError, match="continuous scoring forbids"):
        BenchmarkSpecAdapter.validate_python(
            declaration
            | {
                "scoring_kind": "continuous",
                "authoritative_reward_metric": "overall",
                "reward_pass_value": 0.5,
            }
        )

    binary = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(
            declaration
            | {
                "scoring_kind": "binary",
                "authoritative_reward_metric": "score",
                "reward_pass_value": 1.0,
            }
        )
    )
    changed_threshold = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(
            declaration
            | {
                "scoring_kind": "binary",
                "authoritative_reward_metric": "score",
                "reward_pass_value": 0.5,
            }
        )
    )
    continuous = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(
            declaration
            | {
                "scoring_kind": "continuous",
                "authoritative_reward_metric": "overall",
            }
        )
    )
    assert len(
        {
            binary.artifact_digest,
            changed_threshold.artifact_digest,
            continuous.artifact_digest,
        }
    ) == 3

    base_manifest = _manifest(tmp_path, created_at="now")
    binary_manifest = base_manifest.model_copy(update={"benchmark": binary})
    continuous_manifest = base_manifest.model_copy(update={"benchmark": continuous})
    assert len(
        {
            base_manifest.canonical_digest(),
            binary_manifest.canonical_digest(),
            continuous_manifest.canonical_digest(),
        }
    ) == 3


def test_artifact_compile_normalizes_source_and_digest(tmp_path: Path) -> None:
    (tmp_path / "tasks.toml").write_text("fake = true\n", encoding="utf-8")
    benchmark_spec = BenchmarkSpecAdapter.validate_python(
        {
            "id": "fake-benchmark",
            "kind": "task_suite",
            "adapter": "fake",
            "source": ".",
            "commit": "content-sha",
            "task_manifest": "tasks.toml",
            "verifier": "fake",
            "scoring_kind": "binary",
            "authoritative_reward_metric": "score",
            "reward_pass_value": 1.0,
        }
    )
    artifact = compile_benchmark_artifact(benchmark_spec, base_dir=tmp_path)
    assert Path(artifact.source).is_absolute()
    assert artifact.source == str(tmp_path.resolve())
    assert len(artifact.artifact_digest) == 64
    assert artifact.artifact_digest == canonical_digest(
        artifact.model_dump(mode="json", exclude={"artifact_digest", "source"})
    )


def test_source_paths_are_provenance_only_but_declared_content_is_identity(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    for root in (first_root, second_root):
        (root / "tasks.toml").write_text("task = 'same'\n", encoding="utf-8")
    declaration = {
        "id": "cross-root",
        "kind": "task_suite",
        "adapter": "fake",
        "commit": None,
        "task_manifest": "tasks.toml",
        "verifier": "fake",
        "scoring_kind": "binary",
        "authoritative_reward_metric": "score",
        "reward_pass_value": 1.0,
    }
    first = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration | {"source": str(first_root)})
    )
    second = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration | {"source": str(second_root)})
    )
    assert first.source != second.source
    assert first.source_content_digest == second.source_content_digest
    assert first.artifact_digest == second.artifact_digest

    first_manifest = _manifest(first_root, created_at="now")
    second_manifest = _manifest(second_root, created_at="now")
    assert first_manifest.canonical_digest() == second_manifest.canonical_digest()

    (second_root / "tasks.toml").write_text("task = 'changed'\n", encoding="utf-8")
    changed = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration | {"source": str(second_root)})
    )
    assert changed.source_content_digest != first.source_content_digest
    assert changed.artifact_digest != first.artifact_digest

    subject_declaration = {
        "id": "opaque-cross-root",
        "kind": "opaque_agent",
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


def test_source_profile_rejects_mismatch_dirty_untracked_and_symlink(tmp_path: Path) -> None:
    git_root = tmp_path / "git"
    git_root.mkdir()
    (git_root / "tasks.toml").write_text("task = true\n", encoding="utf-8")
    head = _git_source(git_root)
    declaration = {
        "id": "git-source",
        "kind": "task_suite",
        "adapter": "fake",
        "source": str(git_root),
        "commit": head,
        "task_manifest": "tasks.toml",
        "verifier": "fake",
        "scoring_kind": "binary",
        "authoritative_reward_metric": "score",
        "reward_pass_value": 1.0,
    }
    artifact = compile_benchmark_artifact(BenchmarkSpecAdapter.validate_python(declaration))
    assert artifact.commit == head
    with pytest.raises(ValueError, match="does not match checkout HEAD"):
        compile_benchmark_artifact(
            BenchmarkSpecAdapter.validate_python(declaration | {"commit": "0" * 40})
        )

    (git_root / "tasks.toml").write_text("task = false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty or untracked"):
        compile_benchmark_artifact(BenchmarkSpecAdapter.validate_python(declaration))

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    (symlink_root / "real.toml").write_text("task = true\n", encoding="utf-8")
    (symlink_root / "tasks.toml").symlink_to("real.toml")
    with pytest.raises(ValueError, match="symlink"):
        compile_benchmark_artifact(
            BenchmarkSpecAdapter.validate_python(
                declaration
                | {
                    "source": str(symlink_root),
                    "commit": None,
                }
            )
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


def test_nested_declared_dependency_mutation_changes_content_digest(tmp_path: Path) -> None:
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
    declaration = {
        "id": "tool-source",
        "kind": "tool_agent_suite",
        "adapter": "aosebench",
        "source": str(root),
        "commit": None,
        "task_root": "benchmark/tasks",
        "input_contract": "input",
        "output_contract": ("output",),
        "evaluator": "evaluator",
        "scoring_kind": "continuous",
        "authoritative_reward_metric": "overall",
    }
    first = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration)
    )
    task.write_text("name = 'two'\n", encoding="utf-8")
    second = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration)
    )
    assert first.source_content_digest != second.source_content_digest
    assert first.artifact_digest != second.artifact_digest
    task.write_text("name = 'one'\n", encoding="utf-8")
    judge.write_text("SCORE = 2\n", encoding="utf-8")
    judge_changed = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration)
    )
    assert judge_changed.source_content_digest != first.source_content_digest
    assert judge_changed.artifact_digest != first.artifact_digest
    judge.write_text("SCORE = 1\n", encoding="utf-8")
    test_sh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    test_changed = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration)
    )
    assert test_changed.source_content_digest != first.source_content_digest
    assert test_changed.artifact_digest != first.artifact_digest
    test_sh.unlink()
    with pytest.raises(ValueError, match=r"tests/test\.sh"):
        compile_benchmark_artifact(BenchmarkSpecAdapter.validate_python(declaration))


def _manifest(tmp_path: Path, *, created_at: str, seed: int = 7):
    (tmp_path / "tasks.toml").write_text("fake = true\n", encoding="utf-8")
    benchmark = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(
            {
                "id": "fake-benchmark",
                "kind": "task_suite",
                "adapter": "fake",
                "source": str(tmp_path),
                "commit": "content-sha",
                "task_manifest": "tasks.toml",
                "verifier": "fake",
                "scoring_kind": "binary",
                "authoritative_reward_metric": "score",
                "reward_pass_value": 1.0,
            }
        )
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
        subject=subject,
        execution=execution,
        claim_design=ClaimDesign(
            scope=ClaimScope.conformance,
            purpose=RunPurpose.exploratory,
            vary=(),
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
