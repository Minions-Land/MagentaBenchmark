from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from MagentaBench.runner.compiler import (
    CompilationError,
    CompiledRun,
    Compiler,
    IsolationViolation,
    canonical_manifest_json,
    enforce_allowed_diff,
)


ROOT = Path(__file__).parents[1]
EXPERIMENTS = ROOT / "MagentaBench" / "conformance" / "experiments"


def test_compiled_run_is_manifest_derived_and_rejects_legacy_kwargs() -> None:
    run = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")[0]
    assert run.canonical_json == canonical_manifest_json(run.manifest)
    assert run.manifest_digest == __import__("hashlib").sha256(
        run.canonical_json
    ).hexdigest()
    assert run.factor_values == run.manifest.metadata.factors
    for field in ("canonical_json", "wire_json", "manifest_digest", "factor_values"):
        with pytest.raises(TypeError):
            CompiledRun(run.manifest, **{field: None})


def test_same_toml_compiles_to_byte_identical_manifests_and_digests() -> None:
    compiler = Compiler(ROOT)
    first = compiler.compile(EXPERIMENTS / "fake-sweep.toml")
    second = compiler.compile(EXPERIMENTS / "fake-sweep.toml")

    assert len(first) == 8
    assert [run.canonical_json for run in first] == [run.canonical_json for run in second]
    assert [run.manifest_digest for run in first] == [
        run.manifest_digest for run in second
    ]
    assert len({run.manifest_digest for run in first}) == 8
    assert all(
        run.manifest_digest == run.manifest.canonical_digest() for run in first
    )
    assert [run.factor_values for run in first] == [
        {"repetition": repetition, "subject": subject}
        for repetition in (0, 1, 2, 3)
        for subject in ("fake.control", "fake.treatment")
    ]
    assert [run.manifest.metadata.run_id for run in first] == [
        f"fake-conformance-sweep__run{index:04d}" for index in range(8)
    ]


def test_manifest_identity_excludes_only_schema_declared_observation_fields() -> None:
    run = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")[0]
    observed = run.manifest.model_copy(
        update={
            "created_at": "2099-01-01T00:00:00Z",
            "wall_clock_start": "2099-01-01T00:00:01Z",
            "wall_clock_end": "2099-01-01T00:00:02Z",
            "record_root": "/different/root",
            "resume_count": 99,
            "runner_invocation_id": "different-invocation",
        }
    )
    assert canonical_manifest_json(run.manifest) == canonical_manifest_json(observed)


def test_allowed_diff_accepts_declared_subject_intervention() -> None:
    runs = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")
    pair = [run for run in runs if run.factor_values["repetition"] == 0]
    control = next(run for run in pair if run.manifest.subject.id == "fake.control")
    treatment = next(run for run in pair if run.manifest.subject.id == "fake.treatment")

    allowed = (
        "subject.artifact_digest",
        "subject.fixed_answer",
        "subject.id",
    )
    paths = enforce_allowed_diff(control.manifest, treatment.manifest, allowed)
    assert paths == allowed


def test_forbidden_diff_is_rejected_and_audited_before_execution(tmp_path: Path) -> None:
    compiler = Compiler(ROOT)
    with pytest.raises(IsolationViolation) as caught:
        compiler.compile(
            EXPERIMENTS / "fake-isolation-violation.toml",
            record_root=tmp_path,
        )

    assert "subject.id" in caught.value.forbidden_paths
    audits = list(
        (tmp_path / "fake-isolation-violation").glob(
            "REJECTED_*/isolation_violation.json"
        )
    )
    assert len(audits) == 1
    report = json.loads(audits[0].read_text(encoding="utf-8"))
    assert report["purpose"] == "exploratory"
    assert "claim_eligible" not in report
    assert "gates" not in report
    receipt = json.loads(
        (audits[0].parent / "isolation_violation_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["rejection_type"] == "isolation_violation"
    assert "subject.id" in receipt["reason"]
    assert not list(tmp_path.rglob("evidence_bundle.json"))


def test_deterministic_conformance_protocol_rejects_non_fake_subject() -> None:
    with pytest.raises(CompilationError, match="all-fake exploratory mechanism-validation"):
        Compiler(ROOT).compile(EXPERIMENTS / "fake-protocol-real-subject.toml")


def test_unobserved_model_activation_is_rejected(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "aose-zero-cost-run-a.toml").read_text(
        encoding="utf-8"
    )
    source = source.replace('model = "none"', 'model = "provider/model"')
    experiment = tmp_path / "unobserved-model.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="ModelActivationReceipt"):
        Compiler(ROOT).compile(experiment)


def test_unknown_claim_mode_is_rejected_without_fallback(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-taxonomy.toml").read_text(encoding="utf-8")
    source = source.replace(
        'protocol = "fake.deterministic.v1"',
        'protocol = "fake.deterministic.v1"\nclaim_mode = "invented"',
    )
    experiment = tmp_path / "unknown-claim-mode.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match=r"claim_mode is forbidden.*ExperimentContrast"):
        Compiler(ROOT).compile(experiment)


def test_allow_test_override_is_identity_marked() -> None:
    normal = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")[0]
    overridden = Compiler(ROOT, allow_test_override=True).compile(
        EXPERIMENTS / "fake-sweep.toml"
    )[0]
    assert normal.manifest.metadata.test_override is None
    assert overridden.manifest.metadata.test_override is not None
    assert overridden.manifest.metadata.test_override.reason
    assert overridden.manifest.metadata.test_override.forced_purpose == "exploratory"
    assert overridden.manifest.metadata.test_override.forced_scope == "conformance"
    assert overridden.manifest_digest != normal.manifest_digest


@pytest.mark.parametrize(
    ("field", "value"),
    [("purpose", "claim"), ("scope", "whole_harness")],
)
def test_allow_test_override_forces_exploratory_conformance(
    tmp_path: Path, field: str, value: str
) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source = source.replace(
        f'{field} = "{("exploratory" if field == "purpose" else "conformance")}"',
        f'{field} = "{value}"',
    )
    experiment = tmp_path / f"override-{field}.toml"
    experiment.write_text(source, encoding="utf-8")
    runs = Compiler(ROOT, allow_test_override=True).compile(experiment)
    assert runs
    assert all(
        run.manifest.claim_design.purpose.value == "exploratory"
        and run.manifest.claim_design.scope.value == "conformance"
        and run.manifest.claim_design.vary == ()
        for run in runs
    )


def test_experiment_design_is_required_without_fallback(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-taxonomy.toml").read_text(encoding="utf-8")
    source = source.replace(
        '\n[experiment.design]\nscope = "conformance"\npurpose = "exploratory"\nvary = []\n',
        "",
    )
    experiment = tmp_path / "missing-design.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match=r"\[experiment\.design\] is required"):
        Compiler(ROOT).compile(experiment)


@pytest.mark.parametrize(
    ("scope", "proof_type"),
    [
        ("component", "AssemblySidecarRef"),
        ("model", "ModelActivationReceipt"),
        ("checkpoint", "CheckpointLoadReceipt"),
        ("evolver", "EvolutionRunEvidence"),
        ("meta_evolver", "NestedIsolationReceipt"),
        ("ablation", "AssemblySidecarRef"),
        ("hyperparameter", "HyperparameterActivationReceipt"),
    ],
)
def test_inactive_scopes_name_the_missing_evidence_class(
    tmp_path: Path, scope: str, proof_type: str
) -> None:
    source = (EXPERIMENTS / "aose-zero-cost-run-a.toml").read_text(
        encoding="utf-8"
    )
    source = source.replace('scope = "whole_harness"', f'scope = "{scope}"')
    experiment = tmp_path / f"blocked-{scope}.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match=proof_type):
        Compiler(ROOT).compile(experiment)


def test_schedule_scope_rejects_without_native_case_set_path(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "aose-zero-cost-run-a.toml").read_text(
        encoding="utf-8"
    )
    source = source.replace('scope = "whole_harness"', 'scope = "schedule"')
    experiment = tmp_path / "blocked-schedule.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="CaseSetActivationReceipt"):
        Compiler(ROOT).compile(experiment)




def _whole_harness_experiment(*, vary: str) -> str:
    return f'''[experiment]
id = "aose-whole-harness-compile"
benchmark = "aosebench.biomnibench-da.v1"
subject = "aose.dryrun.true"
protocol = "aose.zero-cost-dryrun.v1"
allowed_diff = [
  "subject.artifact_digest",
  "subject.emits_trace",
  "subject.entrypoint",
  "subject.id",
]

[experiment.contrast]
mode = "one_factor"
control_id = "aose.dryrun.true"
treatment_id = "aose.dryrun.echo"
counterbalanced = false

[experiment.design]
scope = "whole_harness"
purpose = "claim"
vary = [{vary}]

[execution]
backend = "aose.docker.immutable"
model = "none"

[execution.budget]
max_tokens = 0
max_wall_seconds = 120.0
max_cost = 0.0

[factors]
subject = ["aose.dryrun.true", "aose.dryrun.echo"]
'''


def test_whole_harness_scope_accepts_exact_subject_contrast(tmp_path: Path) -> None:
    vary = ", ".join(
        repr(path)
        for path in (
            "subject.artifact_digest",
            "subject.emits_trace",
            "subject.entrypoint",
            "subject.id",
        )
    )
    experiment = tmp_path / "whole-harness.toml"
    experiment.write_text(_whole_harness_experiment(vary=vary), encoding="utf-8")
    with pytest.raises(CompilationError, match="runtime support is not active"):
        Compiler(ROOT).compile(experiment)


def test_whole_harness_scope_rejects_non_subject_vary_path(tmp_path: Path) -> None:
    experiment = tmp_path / "whole-harness-model-drift.toml"
    experiment.write_text(
        _whole_harness_experiment(vary='"execution.model"'), encoding="utf-8"
    )
    with pytest.raises(CompilationError, match=r"only subject\.\* vary paths"):
        Compiler(ROOT).compile(experiment)


def test_fake_exact_verifier_rejects_misdeclared_authoritative_metric(
    tmp_path: Path,
) -> None:
    project = tmp_path / "metric-project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance",
        project / "MagentaBench/conformance",
    )
    benchmark_path = project / "registries/benchmarks/fake-exact.toml"
    benchmark_text = benchmark_path.read_text(encoding="utf-8")
    assert 'authoritative_reward_metric = "exact_match"' in benchmark_text
    benchmark_path.write_text(
        benchmark_text.replace(
            'authoritative_reward_metric = "exact_match"',
            'authoritative_reward_metric = "overall"',
        ),
        encoding="utf-8",
    )
    experiment = project / "MagentaBench/conformance/experiments/fake-sweep.toml"
    with pytest.raises(
        CompilationError,
        match="fake.exact.v1 requires authoritative_reward_metric='exact_match'",
    ):
        Compiler(project).compile(experiment)


def _project_with_protocol_edit(
    tmp_path: Path, old: str, new: str
) -> tuple[Path, Path]:
    project = tmp_path / "protocol-project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance",
        project / "MagentaBench/conformance",
    )
    protocol_path = project / "registries/protocols/fake-deterministic.toml"
    text = protocol_path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"protocol edit target not present: {old!r}")
    replaced = text.replace(old, new)
    assert replaced != text
    protocol_path.write_text(replaced, encoding="utf-8")
    return project, project / "MagentaBench/conformance/experiments/fake-sweep.toml"


def test_adaptive_budget_requires_activation_receipt(tmp_path: Path) -> None:
    project, experiment = _project_with_protocol_edit(
        tmp_path, "adaptive_budget = false", "adaptive_budget = true"
    )
    with pytest.raises(CompilationError, match="AdaptiveBudgetReceipt"):
        Compiler(project).compile(experiment)


def test_unknown_candidate_selection_requires_activation_receipt(
    tmp_path: Path,
) -> None:
    project, experiment = _project_with_protocol_edit(
        tmp_path, 'candidate_selection = "single"', 'candidate_selection = "invented"'
    )
    with pytest.raises(CompilationError, match="candidate_selection"):
        Compiler(project).compile(experiment)


def test_claim_design_cannot_vary_across_expanded_runs(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "aose-zero-cost-run-a.toml").read_text(
        encoding="utf-8"
    )
    source += '\n[factors]\n"experiment.design.purpose" = ["claim", "exploratory"]\n'
    experiment = tmp_path / "mixed-purpose.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="claim design must be invariant"):
        Compiler(ROOT).compile(experiment)
