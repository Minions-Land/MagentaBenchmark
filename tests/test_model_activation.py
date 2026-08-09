from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from MagentaBench.runner.backend.fake import CaseExecution
from MagentaBench.runner.evidence import artifact_ref, atomic_write_json, sha256_file
from MagentaBench.runner.model_activation import (
    ensure_model_activation_receipt,
    make_model_activation_receipt,
)
from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    AdapterCapability,
    AdapterCapabilityArtifact,
    CredentialRef,
    EvidenceBundle,
    ProviderBinding,
    ProvenanceRecord,
    RunPurpose,
    RunStatus,
    UsageRecord,
    verify_observation_report,
)
from MagentaBench.schemas.model_activation import replay_model_activation_receipt
from MagentaBench.schemas.verification import (
    _model_activation_isolation_reasons,
    _verify_bundle_artifacts,
    _verify_bundle_provenance,
)


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _run(binding: ProviderBinding | None):
    capability = SimpleNamespace(
        adapter_kind="execution",
        model_activation_source="native_result",
    )
    return SimpleNamespace(
        manifest=SimpleNamespace(
            execution=SimpleNamespace(
                model="provider/model-v1",
                provider_binding=binding,
            ),
            metadata=SimpleNamespace(
                adapter_capabilities=(SimpleNamespace(capability=capability),)
            ),
        )
    )


def _binding() -> ProviderBinding:
    return ProviderBinding(
        provider_id="provider",
        base_url="https://provider.example/v1",
        wire_api="responses",
        model_id="provider/model-v1",
        credential_ref=CredentialRef(
            name="provider-primary",
            value_sha256="a" * 64,
            secret=True,
            source_file="credentials/providers.toml",
        ),
    )


def test_native_model_activation_distinguishes_match_mismatch_and_unobserved(
    tmp_path: Path,
) -> None:
    native = tmp_path / "native-result.json"
    native.write_text(
        '{"format":"bmp-model-activation-evidence-v1",'
        '"activation_source":"native_result",'
        '"provider_id":"provider",'
        '"base_url":"https://provider.example/v1",'
        '"wire_api":"responses","model_id":"provider/model-v1",'
        '"credential_name":"provider-primary",'
        '"credential_value_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n',
        encoding="utf-8",
    )
    ref = artifact_ref(native)
    run = _run(_binding())

    matched = make_model_activation_receipt(
        run,
        activated_provider_id="provider",
        activated_model_id="provider/model-v1",
        evidence_refs=(ref,),
    )
    assert matched.status == "matched"
    assert matched.binding_digest == _binding().canonical_digest()

    mismatch_native = tmp_path / "mismatch-native-result.json"
    mismatch_native.write_text(
        '{"format":"bmp-model-activation-evidence-v1",'
        '"activation_source":"native_result",'
        '"provider_id":"provider",'
        '"base_url":"https://provider.example/v1",'
        '"wire_api":"responses","model_id":"provider/model-v2",'
        '"credential_name":"provider-primary",'
        '"credential_value_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n',
        encoding="utf-8",
    )
    mismatch = make_model_activation_receipt(
        run,
        activated_provider_id="provider",
        activated_model_id="provider/model-v2",
        evidence_refs=(artifact_ref(mismatch_native),),
    )
    assert mismatch.status == "mismatch"
    assert mismatch.reason

    endpoint_native = tmp_path / "endpoint-mismatch-native-result.json"
    endpoint_native.write_text(
        '{"format":"bmp-model-activation-evidence-v1",'
        '"activation_source":"native_result",'
        '"provider_id":"provider",'
        '"base_url":"https://attacker.example/v1",'
        '"wire_api":"responses","model_id":"provider/model-v1",'
        '"credential_name":"provider-primary",'
        '"credential_value_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n',
        encoding="utf-8",
    )
    endpoint_mismatch = make_model_activation_receipt(
        run,
        evidence_refs=(artifact_ref(endpoint_native),),
    )
    assert endpoint_mismatch.status == "mismatch"
    assert endpoint_mismatch.activated_model_id == "provider/model-v1"
    assert (
        endpoint_mismatch.activated_binding_digest
        != endpoint_mismatch.binding_digest
    )

    with pytest.raises(ValueError, match="disagrees with native evidence"):
        make_model_activation_receipt(
            run,
            activated_provider_id="attacker",
            activated_model_id="provider/model-v1",
            evidence_refs=(ref,),
        )

    unobserved = make_model_activation_receipt(run)
    assert unobserved.status == "unobserved"
    assert unobserved.activated_model_id is None


def test_pipeline_normalizer_persists_explicit_unobserved_receipt(
    tmp_path: Path,
) -> None:
    run = _run(_binding())
    bundle = EvidenceBundle(
        run_id="attempt-0",
        status=RunStatus.unsupported,
        provenance=ProvenanceRecord(
            manifest_digest="0" * 64,
            runner_digest="1" * 64,
            benchmark_digest="2" * 64,
            subject_digest="3" * 64,
            backend_digest="4" * 64,
        ),
    )
    bundle_path = tmp_path / "evidence_bundle.json"
    atomic_write_json(bundle_path, bundle)
    execution = CaseExecution(
        case_id="case-0",
        bundle=bundle,
        bundle_path=bundle_path,
        bundle_digest=sha256_file(bundle_path),
    )

    normalized = ensure_model_activation_receipt(run, execution)

    receipt = normalized.bundle.provenance.model_activation
    assert receipt is not None
    assert receipt.status == "unobserved"
    assert "omitted" in receipt.reason[0]
    assert normalized.bundle_digest == sha256_file(bundle_path)
    assert EvidenceBundle.model_validate_json(bundle_path.read_bytes()) == normalized.bundle


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"provider_id": "provider", "model_id": "provider/model-v1"},
    ),
)
def test_untyped_native_json_cannot_substantiate_model_activation(
    tmp_path: Path,
    payload: dict[str, str],
) -> None:
    native = tmp_path / "untyped-native-result.json"
    atomic_write_json(native, payload)

    receipt = make_model_activation_receipt(
        _run(_binding()),
        evidence_refs=(artifact_ref(native),),
    )

    assert receipt.status == "unobserved"
    assert receipt.activated_provider_id is None
    assert receipt.activated_binding_digest is None
    assert replay_model_activation_receipt(
        receipt,
        requested_model=_binding().model_id,
        binding=_binding(),
        bundle_usage=None,
        require_usage=False,
    ) == ()


def test_invalid_native_evidence_errors_are_redacted(tmp_path: Path) -> None:
    native = tmp_path / "invalid-native-result.json"
    secret = "TOP-SECRET"
    atomic_write_json(native, {"api_key": secret})

    receipt = make_model_activation_receipt(
        _run(_binding()),
        evidence_refs=(artifact_ref(native),),
    )

    assert receipt.status == "unobserved"
    assert receipt.reason == (
        "model activation evidence violates the closed schema",
    )
    assert secret not in receipt.model_dump_json()
    assert replay_model_activation_receipt(
        receipt,
        requested_model=_binding().model_id,
        binding=_binding(),
        bundle_usage=None,
        require_usage=False,
    ) == ()


def test_invalid_harbor_binding_errors_are_redacted(tmp_path: Path) -> None:
    native = tmp_path / "invalid-harbor-result.json"
    secret = "TOP-SECRET"
    atomic_write_json(
        native,
        {
            "agent_result": {
                "provider_id": "provider",
                "model_id": "provider/model-v1",
                "base_url": f"https://user:{secret}@provider.example/v1",
                "wire_api": "responses",
                "credential_name": "provider-primary",
                "credential_value_sha256": "a" * 64,
            }
        },
    )

    receipt = make_model_activation_receipt(
        _run(_binding()),
        evidence_refs=(artifact_ref(native),),
    )

    assert receipt.status == "unobserved"
    assert receipt.reason == ("native Harbor binding evidence is incomplete",)
    assert secret not in receipt.model_dump_json()
    assert replay_model_activation_receipt(
        receipt,
        requested_model=_binding().model_id,
        binding=_binding(),
        bundle_usage=None,
        require_usage=False,
    ) == ()


@pytest.mark.parametrize(
    ("usage_fields", "expected_reason"),
    (
        (
            {"usage": {"input_tokens": 1, "api_key": "TOP-SECRET"}},
            "native usage is invalid",
        ),
        (
            {
                "n_input_tokens": 1,
                "n_output_tokens": 1,
                "cost_usd": "TOP-SECRET",
            },
            "native Harbor usage is invalid",
        ),
    ),
)
def test_invalid_harbor_usage_errors_are_redacted(
    tmp_path: Path,
    usage_fields: dict[str, object],
    expected_reason: str,
) -> None:
    native = tmp_path / "invalid-harbor-usage.json"
    secret = "TOP-SECRET"
    atomic_write_json(
        native,
        {
            "agent_result": {
                "provider_id": "provider",
                "model_id": "provider/model-v1",
                "base_url": "https://provider.example/v1",
                "wire_api": "responses",
                "credential_name": "provider-primary",
                "credential_value_sha256": "a" * 64,
                **usage_fields,
            }
        },
    )

    receipt = make_model_activation_receipt(
        _run(_binding()),
        evidence_refs=(artifact_ref(native),),
    )

    assert receipt.status == "unobserved"
    assert receipt.reason == (expected_reason,)
    assert secret not in receipt.model_dump_json()
    exploratory_errors = replay_model_activation_receipt(
        receipt,
        requested_model=_binding().model_id,
        binding=_binding(),
        bundle_usage=None,
        require_usage=False,
    )
    claim_errors = replay_model_activation_receipt(
        receipt,
        requested_model=_binding().model_id,
        binding=_binding(),
        bundle_usage=None,
        require_usage=True,
    )
    assert exploratory_errors == ()
    assert "real-model claim native usage evidence is missing" in claim_errors
    assert secret not in "\n".join(claim_errors)


def test_unsubstantiated_evidence_digest_drift_still_fails_closed(
    tmp_path: Path,
) -> None:
    native = tmp_path / "invalid-native-result.json"
    atomic_write_json(native, {})
    receipt = make_model_activation_receipt(
        _run(_binding()),
        evidence_refs=(artifact_ref(native),),
    )
    native.write_text('{"changed":true}\n', encoding="utf-8")

    errors = replay_model_activation_receipt(
        receipt,
        requested_model=_binding().model_id,
        binding=_binding(),
        bundle_usage=None,
        require_usage=False,
    )

    assert len(errors) == 1
    assert "model activation evidence digest drift" in errors[0]


def test_standalone_verifier_replays_model_binding_source_and_evidence(
    tmp_path: Path,
) -> None:
    run = Compiler(ROOT).compile(EXPERIMENT)[0]
    implementation = tmp_path / "execution.py"
    declaration = tmp_path / "execution.toml"
    implementation.write_text("# execution adapter\n", encoding="utf-8")
    declaration.write_text("[adapter]\n", encoding="utf-8")
    implementation_ref = artifact_ref(implementation)
    capability = AdapterCapability(
        id="fake-real-model",
        kind="adapter",
        adapter="fake",
        adapter_kind="execution",
        entrypoint="adapter:Execution",
        digest=implementation_ref.sha256,
        supported_benchmark_kinds=("task_suite",),
        supported_subject_kinds=("fake",),
        supported_subject_adapters=("fake",),
        supported_backend_kinds=("local",),
        supported_backend_adapters=("fake",),
        model_activation_source="native_result",
        supported_state_reset_policies=("never",),
    )
    provisional = AdapterCapabilityArtifact(
        capability=capability,
        declaration_ref=artifact_ref(declaration),
        implementation_ref=implementation_ref,
        artifact_digest="0" * 64,
    )
    capability_artifact = provisional.model_copy(
        update={"artifact_digest": provisional.canonical_digest()}
    )
    binding = _binding()
    execution = run.manifest.execution.model_copy(
        update={"model": binding.model_id, "provider_binding": binding}
    )
    metadata = run.manifest.metadata.model_copy(
        update={"adapter_capabilities": (capability_artifact,)}
    )
    manifest = run.manifest.model_copy(
        update={"execution": execution, "metadata": metadata}
    )
    run = run.__class__(manifest=manifest)
    native = tmp_path / "native-result.json"
    native.write_text(
        '{"format":"bmp-model-activation-evidence-v1",'
        '"activation_source":"native_result",'
        '"provider_id":"provider",'
        '"base_url":"https://provider.example/v1",'
        '"wire_api":"responses","model_id":"provider/model-v1",'
        '"credential_name":"provider-primary",'
        '"credential_value_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n',
        encoding="utf-8",
    )
    receipt = make_model_activation_receipt(
        run,
        activated_provider_id=binding.provider_id,
        activated_model_id=binding.model_id,
        evidence_refs=(artifact_ref(native),),
    )
    provenance = ProvenanceRecord(
        manifest_digest=manifest.canonical_digest(),
        runner_digest="1" * 64,
        benchmark_digest=manifest.benchmark.artifact_digest,
        subject_digest=manifest.subject.artifact_digest,
        backend_digest="1" * 64,
        backend_kind="fake",
        model_activation=receipt,
    )
    bundle = EvidenceBundle(
        run_id="attempt-0",
        status=RunStatus.unsupported,
        usage=None,
        provenance=provenance,
    )
    mismatches: list[str] = []
    _verify_bundle_artifacts(
        bundle,
        label="bundle",
        path_map={},
        mismatches=mismatches,
    )
    _verify_bundle_provenance(
        bundle,
        manifest,
        label="bundle",
        path_map={},
        mismatches=mismatches,
    )
    assert mismatches == []

    missing = bundle.model_copy(
        update={
            "provenance": provenance.model_copy(update={"model_activation": None})
        }
    )
    rejected: list[str] = []
    _verify_bundle_provenance(
        missing,
        manifest,
        label="bundle",
        path_map={},
        mismatches=rejected,
    )
    assert rejected == []
    isolation_reasons = _model_activation_isolation_reasons(
        SimpleNamespace(case_id="case-1"),
        missing,
        manifest,
    )
    assert any(
        "ModelActivationReceipt is missing" in item
        for item in isolation_reasons
    )

    claim_manifest = manifest.model_copy(
        update={
            "claim_design": manifest.claim_design.model_copy(
                update={"purpose": RunPurpose.claim}
            )
        }
    )
    claim_bundle = bundle.model_copy(
        update={
            "provenance": provenance.model_copy(
                update={"manifest_digest": claim_manifest.canonical_digest()}
            )
        }
    )
    claim_rejected: list[str] = []
    _verify_bundle_provenance(
        claim_bundle,
        claim_manifest,
        label="bundle",
        path_map={},
        mismatches=claim_rejected,
    )
    assert any("token usage is unobservable" in item for item in claim_rejected)
    assert any("cost usage is unobservable" in item for item in claim_rejected)


def test_claim_usage_must_equal_the_same_native_activation_evidence(
    tmp_path: Path,
) -> None:
    binding = _binding()
    native = tmp_path / "native-result.json"
    native.write_text(
        '{"format":"bmp-model-activation-evidence-v1",'
        '"activation_source":"native_result",'
        '"provider_id":"provider",'
        '"base_url":"https://provider.example/v1",'
        '"wire_api":"responses","model_id":"provider/model-v1",'
        '"credential_name":"provider-primary",'
        '"credential_value_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"usage":{"input_tokens":11,"output_tokens":7,'
        '"total_tokens":18,"cost":0.03}}\n',
        encoding="utf-8",
    )
    receipt = make_model_activation_receipt(
        _run(binding),
        evidence_refs=(artifact_ref(native),),
    )
    assert replay_model_activation_receipt(
        receipt,
        requested_model=binding.model_id,
        binding=binding,
        bundle_usage=UsageRecord(
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            cost=0.03,
        ),
        require_usage=True,
    ) == ()
    errors = replay_model_activation_receipt(
        receipt,
        requested_model=binding.model_id,
        binding=binding,
        bundle_usage=UsageRecord(
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            cost=123.0,
        ),
        require_usage=True,
    )
    assert "real-model claim usage differs from native activation evidence" in errors


def test_real_model_reaches_exploratory_pipeline_with_native_receipt(
    tmp_path: Path,
) -> None:
    project = tmp_path / "real-model-project"
    for directory in (
        "registries/adapters",
        "registries/backends",
        "registries/benchmarks",
        "registries/protocols",
        "registries/subjects",
        "plugins",
        "MagentaBench/conformance/fixtures",
    ):
        (project / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        ROOT / "registries/benchmarks/fake-exact.toml",
        project / "registries/benchmarks/fake-exact.toml",
    )
    shutil.copy2(
        ROOT / "registries/subjects/fake-nonfake.toml",
        project / "registries/subjects/fake-nonfake.toml",
    )
    shutil.copy2(
        ROOT / "registries/protocols/benchmark-evaluation.v1.toml",
        project / "registries/protocols/benchmark-evaluation.v1.toml",
    )
    shutil.copytree(
        ROOT / "MagentaBench/conformance/fixtures/fake_benchmark",
        project / "MagentaBench/conformance/fixtures/fake_benchmark",
    )
    plugin = project / "plugins/runtime.py"
    plugin.write_text(
        '''from hashlib import sha256
from pathlib import Path

from MagentaBench.runner.backend.fake import FakeBackend
from MagentaBench.runner.evidence import artifact_ref
from MagentaBench.runner.model_activation import bind_model_activation, make_model_activation_receipt


_DIGEST = sha256(Path(__file__).read_bytes()).hexdigest()


class Backend(FakeBackend):
    adapter = "provider-runtime"

    @staticmethod
    def reset_state(case_id, policy):
        return {"case_id": case_id, "policy": policy, "mechanism": "fresh"}


class BackendFactory:
    adapter = "provider-runtime"
    digest = _DIGEST

    def build(self, run, *, record_root, workspace_root):
        return Backend(record_root)


class Execution:
    benchmark_adapter = "fake"
    backend_adapter = "provider-runtime"
    subject_interface = "task_to_output"
    digest = _DIGEST

    def execute(self, backend, run, case, attempt):
        execution = backend.execute(
            run,
            case.task,
            activated_case_set_digest=case.case_set_digest,
            case_id=attempt.attempt_id,
            execution_run_id=attempt.attempt_id,
            attempt_budget=attempt.allocation,
            remaining_wall_seconds=attempt.remaining_wall_seconds,
        )
        if run.manifest.execution.model == "provider/model-unobserved":
            return execution
        native = execution.bundle_path.parent / "provider-result.json"
        native.write_text(
            '{"format":"bmp-model-activation-evidence-v1",'
            '"activation_source":"native_result",'
            '"provider_id":"provider",'
            '"base_url":"https://provider.example/v1",'
            '"wire_api":"responses","model_id":"provider/model-v1",'
            '"credential_name":"provider-primary",'
            '"credential_value_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\\n',
            encoding="utf-8",
        )
        receipt = make_model_activation_receipt(
            run,
            activated_provider_id="provider",
            activated_model_id="provider/model-v1",
            evidence_refs=(artifact_ref(native),),
        )
        return bind_model_activation(execution, receipt)

    def reset_state(self, backend, case_id, policy):
        return backend.reset_state(case_id, policy)
''',
        encoding="utf-8",
    )
    digest = sha256_file(plugin)
    (project / "registries/backends/provider.toml").write_text(
        '''[backend]
id = "provider.runtime.v1"
kind = "local"
adapter = "provider-runtime"
bmp_version = "0.1"
''',
        encoding="utf-8",
    )
    (project / "registries/adapters/provider-backend.toml").write_text(
        f'''[adapter]
id = "provider.runtime.backend"
kind = "adapter"
adapter = "provider-runtime"
bmp_version = "0.1"
adapter_kind = "backend_factory"
source = "."
entrypoint = "plugins/runtime.py:BackendFactory"
digest = "{digest}"
supported_backend_kinds = ["local"]
supported_backend_adapters = ["provider-runtime"]
backend_default_read_set = []
''',
        encoding="utf-8",
    )
    (project / "registries/adapters/provider-execution.toml").write_text(
        f'''[adapter]
id = "provider.runtime.execution"
kind = "adapter"
adapter = "fake"
bmp_version = "0.1"
adapter_kind = "execution"
source = "."
entrypoint = "plugins/runtime.py:Execution"
digest = "{digest}"
supported_benchmark_kinds = ["task_suite"]
supported_subject_kinds = ["opaque_agent"]
supported_subject_adapters = ["fake"]
supported_backend_kinds = ["local"]
supported_backend_adapters = ["provider-runtime"]
supported_subject_interfaces = ["task_to_output"]
model_activation_source = "native_result"
supported_state_reset_policies = ["per_case"]
''',
        encoding="utf-8",
    )
    experiment = project / "experiment.toml"
    experiment.write_text(
        '''[experiment]
id = "real-model-exploratory"
benchmark = "fake.exact.v1"
subject = "fake.nonfake"
protocol = "benchmark.evaluation.v1"

[experiment.design]
scope = "model"
purpose = "exploratory"
vary = []

[execution]
backend = "provider.runtime.v1"
model = "provider/model-v1"

[execution.provider_binding]
provider_id = "provider"
base_url = "https://provider.example/v1"
wire_api = "responses"
model_id = "provider/model-v1"

[execution.provider_binding.credential_ref]
name = "provider-primary"
value_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
secret = true
source_file = "credentials/providers.toml"

[execution.budget]
max_tokens = 10
max_wall_seconds = 5.0
max_cost = 1.0
''',
        encoding="utf-8",
    )

    result = Pipeline(project, tmp_path / "records").run(experiment)

    assert len(result.runs) == 1
    receipt = result.runs[0].case.bundle.provenance.model_activation
    assert receipt is not None and receipt.status == "matched"
    assert receipt.activated_model_id == "provider/model-v1"
    assert result.runs[0].case.bundle.usage is not None
    assert result.runs[0].case.bundle.usage.total_tokens == 0
    verified = verify_observation_report(result.report_path)
    assert verified.report == result.report

    unobserved_experiment = project / "unobserved-experiment.toml"
    unobserved_experiment.write_text(
        experiment.read_text(encoding="utf-8")
        .replace(
            'id = "real-model-exploratory"',
            'id = "real-model-unobserved"',
        )
        .replace(
            'model = "provider/model-v1"',
            'model = "provider/model-unobserved"',
        )
        .replace(
            'model_id = "provider/model-v1"',
            'model_id = "provider/model-unobserved"',
        ),
        encoding="utf-8",
    )
    unobserved = Pipeline(
        project,
        tmp_path / "records-unobserved",
    ).run(unobserved_experiment)
    unobserved_receipt = (
        unobserved.runs[0].case.bundle.provenance.model_activation
    )
    assert unobserved_receipt is not None
    assert unobserved_receipt.status == "unobserved"
    assert unobserved.report.isolation_valid is False
    assert any(
        "model activation is unobserved" in reason
        for reason in unobserved.report.isolation_reasons
    )
    assert verify_observation_report(unobserved.report_path).report == (
        unobserved.report
    )
