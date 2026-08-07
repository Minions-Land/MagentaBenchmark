"""Contract and compiler conformance tests for BMP 0.1."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.schemas import (
    IDENTITY_EXCLUDE,
    BackendSpec,
    BenchmarkSpecAdapter,
    Budget,
    ClaimReport,
    EnvironmentReceipt,
    EnvironmentSpec,
    EvidenceBundle,
    ExecutionSpec,
    GateName,
    ManifestCompiler,
    MountSpec,
    PackageRecord,
    ProtocolSpec,
    ProvenanceRecord,
    ResolvedExecutionSpec,
    SubjectSpecAdapter,
    VerifierEvidence,
    build_resolved_manifest,
    canonical_digest,
    check_allowed_diff,
    compile_benchmark_artifact,
    compile_subject_artifact,
    expand_factor_sweep,
    load_benchmark_spec,
    load_claim_report,
    load_evidence_bundle,
    load_execution_spec,
    load_subject_spec,
    schema_documents,
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


def test_json_schema_is_generated_for_public_contracts() -> None:
    documents = schema_documents()
    assert {
        "benchmark-spec",
        "subject-spec",
        "execution-spec",
        "environment-spec",
        "environment-receipt",
        "evidence-bundle",
        "claim-report",
        "resolved-bmp-manifest",
    }.issubset(documents)
    assert documents["subject-spec"]["discriminator"]["propertyName"] == "kind"
    assert "claim_eligible" in documents["claim-report"]["properties"]
    assert "effect_is_causal_claim" in documents["claim-report"]["properties"]


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
        MountSpec(host_path="relative/input", container_path="/workspace/input")

    environment = EnvironmentSpec(
        id="tb2-default",
        python_version="3.11",
        packages=("pytest>=8", "docker>=7"),
        env_var_names=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
        mounts=(
            MountSpec(
                host_path="/opt/benchmarks/tb2",
                container_path="/workspace/tasks",
            ),
        ),
    )
    backend = BackendSpec(
        id="subprocess-default",
        kind="subprocess",
        adapter="subprocess",
        executable="/usr/bin/env",
        version="1",
        digest="sha256:backend",
        environment=environment,
    )
    _round_trip(environment)
    _round_trip(backend)
    assert backend.environment is not None
    assert backend.environment.python_version == "3.11"
    assert backend.environment.mounts[0].read_only is True


def test_environment_receipt_is_content_addressed_and_absolute() -> None:
    package = PackageRecord(
        name="pydantic",
        version="2.13.4",
        sha256="a" * 64,
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
    with pytest.raises(ValidationError, match="sha256"):
        PackageRecord(name="broken", version="1", sha256="not-a-digest")


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
            kind="subprocess",
            adapter="subprocess",
            executable="/bin/true",
            version="1",
            digest="sha256:backend",
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
    declaration = {
        "id": "scored-benchmark",
        "kind": "task_suite",
        "adapter": "fake",
        "source": str(tmp_path),
        "commit": "content-sha",
        "task_manifest": "tasks.toml",
        "verifier": "native",
    }
    with pytest.raises(ValidationError, match="must be provided together"):
        BenchmarkSpecAdapter.validate_python(
            declaration | {"authoritative_reward_metric": "score"}
        )

    unscored = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(declaration)
    )
    scored = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(
            declaration
            | {
                "authoritative_reward_metric": "score",
                "reward_pass_value": 1.0,
            }
        )
    )
    changed_threshold = compile_benchmark_artifact(
        BenchmarkSpecAdapter.validate_python(
            declaration
            | {
                "authoritative_reward_metric": "score",
                "reward_pass_value": 0.5,
            }
        )
    )
    assert len(
        {
            unscored.artifact_digest,
            scored.artifact_digest,
            changed_threshold.artifact_digest,
        }
    ) == 3

    base_manifest = _manifest(tmp_path, created_at="now")
    scored_manifest = base_manifest.model_copy(update={"benchmark": scored})
    threshold_manifest = base_manifest.model_copy(update={"benchmark": changed_threshold})
    assert len(
        {
            base_manifest.canonical_digest(),
            scored_manifest.canonical_digest(),
            threshold_manifest.canonical_digest(),
        }
    ) == 3


def test_artifact_compile_normalizes_source_and_digest(tmp_path: Path) -> None:
    benchmark_spec = BenchmarkSpecAdapter.validate_python(
        {
            "id": "fake-benchmark",
            "kind": "task_suite",
            "adapter": "fake",
            "source": ".",
            "commit": "content-sha",
            "task_manifest": "tasks.toml",
            "verifier": "fake",
        }
    )
    artifact = compile_benchmark_artifact(benchmark_spec, base_dir=tmp_path)
    assert Path(artifact.source).is_absolute()
    assert artifact.source == str(tmp_path.resolve())
    assert len(artifact.artifact_digest) == 64
    assert artifact.artifact_digest == canonical_digest(
        artifact.model_dump(mode="json", exclude={"artifact_digest"})
    )


def _manifest(tmp_path: Path, *, created_at: str, seed: int = 7):
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
        kind="process",
        adapter="fake",
        executable="/bin/true",
        version="1",
        digest="sha256:backend",
    )
    execution = ResolvedExecutionSpec(
        backend=backend,
        model="fake/model",
        seed=seed,
        budget=Budget(max_tokens=10),
        protocol=ProtocolSpec(
            id="deterministic-v1",
            kind="conformance",
            adapter="fake",
            deterministic_conformance=True,
        ),
    )
    return build_resolved_manifest(
        experiment_id="conformance",
        run_id="conformance__run0000",
        benchmark=benchmark,
        subject=subject,
        execution=execution,
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


def test_fake_subject_is_scoped_to_deterministic_conformance(tmp_path: Path) -> None:
    registry = tmp_path / "registries"
    for collection in ("benchmarks", "subjects", "backends", "protocols"):
        (registry / collection).mkdir(parents=True)
    (registry / "benchmarks" / "fake.toml").write_text(
        "[benchmark]\nid='fake-benchmark'\nkind='task_suite'\nadapter='fake'\n"
        f"source='{tmp_path}'\n"
        "commit='content'\ntask_manifest='tasks.toml'\nverifier='fake'\n",
        encoding="utf-8",
    )
    (registry / "subjects" / "fake.toml").write_text(
        "[subject]\nid='fake-subject'\nkind='fake'\nadapter='fake'\n",
        encoding="utf-8",
    )
    (registry / "backends" / "fake.toml").write_text(
        "[backend]\nid='fake-backend'\nkind='process'\nadapter='fake'\n"
        "executable='/bin/true'\nversion='1'\ndigest='sha256:backend'\n",
        encoding="utf-8",
    )
    (registry / "protocols" / "normal.toml").write_text(
        "[protocol]\nid='normal'\nkind='fixed'\nadapter='fake'\n",
        encoding="utf-8",
    )
    compiler = ManifestCompiler(registry)
    execution = ExecutionSpec(
        backend="fake-backend",
        model="fake/model",
        seed=0,
        budget=Budget(max_tokens=1),
    )
    with pytest.raises(ValueError, match="fake subjects require"):
        compiler.compile(
            experiment_id="test",
            run_id="test__run0000",
            benchmark_id="fake-benchmark",
            subject_id="fake-subject",
            execution=execution,
            protocol_id="normal",
        )
