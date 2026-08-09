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
from MagentaBench.schemas import ArtifactRef, ConfigurationArtifact


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


def test_allowed_diff_treats_configuration_value_as_causal_surface(
    tmp_path: Path,
) -> None:
    """Derived configuration digests must not become extra interventions."""

    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source += """

[experiment.configuration.values.agent]
model = "control-model"
"""
    experiment = tmp_path / "configuration-diff.toml"
    experiment.write_text(source, encoding="utf-8")
    try:
        run = Compiler(ROOT).compile(experiment)[0]
    finally:
        experiment.unlink(missing_ok=True)
    configuration = run.manifest.metadata.configuration
    assert configuration is not None
    values = {
        **configuration.values,
        "agent": {**configuration.values.get("agent", {}), "model": "treatment-model"},
    }
    treatment_configuration = configuration.model_copy(update={"values": values})
    treatment_metadata = run.manifest.metadata.model_copy(
        update={"configuration": treatment_configuration}
    )
    treatment = run.manifest.model_copy(update={"metadata": treatment_metadata})

    paths = enforce_allowed_diff(
        run.manifest,
        treatment,
        ("configuration.values.agent.model",),
    )
    assert paths == ("configuration.values.agent.model",)


def test_allowed_diff_rejects_configuration_schema_and_adapter_changes() -> None:
    run = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")[0]

    # Use a minimal synthetic configuration artifact so this test stays
    # independent of the repository's optional configuration registry entries.
    base = ConfigurationArtifact(
        id="inline",
        adapter="generic",
        values={"agent": {"model": "control"}},
        schema_digest="0" * 64,
        artifact_digest="0" * 64,
    )
    metadata = run.manifest.metadata.model_copy(update={"configuration": base})
    control = run.manifest.model_copy(update={"metadata": metadata})

    schema_changed = base.model_copy(
        update={"json_schema": {"type": "object"}}
    )
    treatment = control.model_copy(
        update={
            "metadata": metadata.model_copy(
                update={"configuration": schema_changed}
            )
        }
    )
    with pytest.raises(IsolationViolation) as caught:
        enforce_allowed_diff(control, treatment, ())
    assert "configuration.schema.type" in caught.value.forbidden_paths

    adapter_changed = base.model_copy(update={"adapter": "other-adapter"})
    treatment = control.model_copy(
        update={
            "metadata": metadata.model_copy(
                update={"configuration": adapter_changed}
            )
        }
    )
    with pytest.raises(IsolationViolation) as caught:
        enforce_allowed_diff(control, treatment, ())
    assert "configuration.adapter" in caught.value.forbidden_paths


def test_allowed_diff_ignores_source_location_but_rejects_source_content_change() -> None:
    run = Compiler(ROOT).compile(EXPERIMENTS / "fake-sweep.toml")[0]
    source = ArtifactRef(path="/tmp/config-a.toml", sha256="a" * 64, size_bytes=7)
    base = ConfigurationArtifact(
        id="profile",
        adapter="generic",
        source_refs=(source,),
        values={"agent": {"model": "control"}},
        schema_digest="0" * 64,
        artifact_digest="0" * 64,
    )
    metadata = run.manifest.metadata.model_copy(update={"configuration": base})
    control = run.manifest.model_copy(update={"metadata": metadata})

    relocated = source.model_copy(update={"path": "/other/location/config.toml"})
    treatment = control.model_copy(
        update={
            "metadata": metadata.model_copy(
                update={
                    "configuration": base.model_copy(
                        update={"source_refs": (relocated,)}
                    )
                }
            )
        }
    )
    assert enforce_allowed_diff(control, treatment, ()) == ()

    changed = source.model_copy(update={"sha256": "b" * 64})
    treatment = control.model_copy(
        update={
            "metadata": metadata.model_copy(
                update={
                    "configuration": base.model_copy(
                        update={"source_refs": (changed,)}
                    )
                }
            )
        }
    )
    with pytest.raises(IsolationViolation) as caught:
        enforce_allowed_diff(control, treatment, ())
    assert "configuration.source_refs.0.sha256" in caught.value.forbidden_paths


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
    assert overridden.manifest.metadata.test_override.forced_comparison_kind is None
    assert (
        overridden.manifest.claim_design.intervention_factor_id
        == "conformance.fake-subject"
    )
    assert overridden.manifest.metadata.allowed_diff == (
        "subject.artifact_digest",
        "subject.fixed_answer",
        "subject.id",
    )
    assert overridden.manifest_digest != normal.manifest_digest


@pytest.mark.parametrize(
    ("field", "value"),
    [("purpose", "claim"), ("comparison_kind", "coding_agent")],
)
def test_allow_test_override_forces_exploratory_conformance(
    tmp_path: Path, field: str, value: str
) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    if field == "purpose":
        source = source.replace('purpose = "exploratory"', f'{field} = "{value}"')
    else:
        source = source.replace(
            "[experiment.design]\n",
            f'[experiment.design]\n{field} = "{value}"\n',
        )
    experiment = tmp_path / f"override-{field}.toml"
    experiment.write_text(source, encoding="utf-8")
    runs = Compiler(ROOT, allow_test_override=True).compile(experiment)
    assert runs
    assert all(
        run.manifest.claim_design.purpose.value == "exploratory"
        and run.manifest.claim_design.comparison_kind is None
        and run.manifest.claim_design.intervention_factor_id
        == "conformance.fake-subject"
        for run in runs
    )


def test_experiment_design_is_required_without_fallback(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-taxonomy.toml").read_text(encoding="utf-8")
    source = source.replace(
        '\n[experiment.design]\npurpose = "exploratory"\n',
        "",
    )
    experiment = tmp_path / "missing-design.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match=r"\[experiment\.design\] is required"):
        Compiler(ROOT).compile(experiment)


@pytest.mark.parametrize(
    "retired_field",
    (
        'scope = "whole_harness"',
        'vary = ["subject.id"]',
        'intervention_factor_id = "agent.subject"',
    ),
)
def test_retired_design_fields_are_rejected(
    tmp_path: Path, retired_field: str
) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source = source.replace(
        "[experiment.design]\n",
        f"[experiment.design]\n{retired_field}\n",
    )
    experiment = tmp_path / "retired-design-field.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(
        CompilationError,
        match="scope/vary/intervention_factor_id are derived or retired",
    ):
        Compiler(ROOT).compile(experiment)


def test_inline_allowed_diff_is_rejected(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source = source.replace(
        'protocol = "fake.deterministic.v1"',
        'protocol = "fake.deterministic.v1"\nallowed_diff = ["subject.id"]',
    )
    experiment = tmp_path / "inline-allowed-diff.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="unknown .*allowed_diff"):
        Compiler(ROOT).compile(experiment)


def test_inline_factor_table_is_rejected(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source += '\n[factors]\nsubject = ["fake.control", "fake.treatment"]\n'
    experiment = tmp_path / "inline-factors.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="unknown top-level TOML sections"):
        Compiler(ROOT).compile(experiment)


def test_unregistered_factor_id_is_rejected(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source = source.replace('"repetition.four"', '"repetition.missing"')
    experiment = tmp_path / "unregistered-factor.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="factor registry id .* not found"):
        Compiler(ROOT).compile(experiment)


def test_contrast_factor_must_be_selected(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source = source.replace(
        'factor_id = "conformance.fake-subject"',
        'factor_id = "conformance.unselected"',
    )
    experiment = tmp_path / "unselected-contrast-factor.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="contrast factor .* is not selected"):
        Compiler(ROOT).compile(experiment)


def test_conformance_factor_cannot_enter_research_comparison(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "fake-sweep.toml").read_text(encoding="utf-8")
    source = source.replace(
        "[experiment.design]\n",
        '[experiment.design]\ncomparison_kind = "coding_agent"\n',
    ).replace('purpose = "exploratory"', 'purpose = "claim"')
    experiment = tmp_path / "conformance-factor-research.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(
        CompilationError,
        match="conformance_fixture factors cannot enter research comparisons",
    ):
        Compiler(ROOT).compile(experiment)


def test_fake_exact_evaluator_rejects_misdeclared_authoritative_source_key(
    tmp_path: Path,
) -> None:
    project = tmp_path / "metric-project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance",
        project / "MagentaBench/conformance",
    )
    evaluator_path = project / "registries/evaluators/fake-exact-v1.toml"
    evaluator_text = evaluator_path.read_text(encoding="utf-8")
    assert 'source_key = "exact_match"' in evaluator_text
    evaluator_path.write_text(
        evaluator_text.replace(
            'source_key = "exact_match"',
            'source_key = "overall"',
        ),
        encoding="utf-8",
    )
    experiment = project / "MagentaBench/conformance/experiments/fake-sweep.toml"
    with pytest.raises(
        CompilationError,
        match="fake.exact.v1 requires authoritative source_key='exact_match'",
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


def test_claim_requires_registered_intervention_factor(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "aose-zero-cost-run-a.toml").read_text(
        encoding="utf-8"
    )
    source = source.replace('purpose = "exploratory"', 'purpose = "claim"')
    experiment = tmp_path / "claim-without-factor.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(CompilationError, match="registered intervention factor"):
        Compiler(ROOT).compile(experiment)
