from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.gates import evaluate_run_report
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    ComparisonKind,
    ExperimentContrast,
    RunPurpose,
    schema_documents,
    verify_observation_report,
)


ROOT = Path(__file__).parents[1]
FACTOR_ID = "conformance.wall-clock-budget"


def _factor_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance/fixtures/fake_benchmark",
        project / "MagentaBench/conformance/fixtures/fake_benchmark",
    )
    factor_dir = project / "registries/factors"
    factor_dir.mkdir(parents=True, exist_ok=True)
    (factor_dir / "conformance-wall-clock-budget.toml").write_text(
        f'''[factor]
id = "{FACTOR_ID}"
kind = "factor"
adapter = "magentabench.factor"
bmp_version = "0.1"
category = "conformance_fixture"
selector_path = "execution.budget.max_wall_seconds"
applies_to = []
resolved_diff_paths = ["execution.budget.max_wall_seconds"]
activation_evidence = "none"
metadata_only = false

[[factor.levels]]
id = "one-second"
value = 1.0

[[factor.levels]]
id = "two-seconds"
value = 2.0
''',
        encoding="utf-8",
    )
    return project


def _experiment(project: Path) -> Path:
    path = project / "factor-contrast.toml"
    path.write_text(
        f'''
[experiment]
id = "factor-contrast"
benchmark = "fake.exact.v1"
dataset = "dataset.fake.exact.v1"
evaluator = "evaluator.fake.exact.v1"
metrics = ["reward.authoritative.v1"]
subject = "fake.treatment"
protocol = "fake.deterministic.v1"
factors = ["{FACTOR_ID}"]

[experiment.contrast]
mode = "one_factor"
factor_id = "{FACTOR_ID}"
control_level = "one-second"
treatment_level = "two-seconds"
counterbalanced = false

[experiment.design]
purpose = "exploratory"

[execution]
backend = "fake.local"
model = "none/deterministic"

[execution.budget]
max_tokens = 0
max_wall_seconds = 1.0
max_cost = 0.0
'''.lstrip(),
        encoding="utf-8",
    )
    return path


def test_factor_contrast_compiles_two_arms_without_subject_axis(tmp_path: Path) -> None:
    project = _factor_project(tmp_path)
    runs = Compiler(project).compile(_experiment(project))

    assert len(runs) == 2
    assert {
        run.factor_values["execution.budget.max_wall_seconds"] for run in runs
    } == {1.0, 2.0}
    assert {run.manifest.subject.id for run in runs} == {"fake.treatment"}
    assert {run.manifest.execution.budget.max_wall_seconds for run in runs} == {
        1.0,
        2.0,
    }


def test_factor_contrast_pairs_claim_effect_by_declared_factor(tmp_path: Path) -> None:
    project = _factor_project(tmp_path)
    result = Pipeline(project, tmp_path / "records").run(_experiment(project))
    registered_subject = Compiler(project)._subject_artifact("fake.nonfake")
    completed = []
    for item in result.runs:
        design = item.plan.manifest.claim_design.model_copy(
            update={
                "comparison_kind": ComparisonKind.coding_agent,
                "purpose": RunPurpose.claim,
                "intervention_factor_id": FACTOR_ID,
            }
        )
        manifest = item.plan.manifest.model_copy(
            update={"claim_design": design, "subject": registered_subject}
        )
        completed.append(replace(item, plan=replace(item.plan, manifest=manifest)))

    report = evaluate_run_report(
        completed=completed,
        expected_run_ids=tuple(
            item.plan.manifest.metadata.run_id for item in completed
        ),
        record_index_ref=None,
    )

    assert report.effect is not None
    assert report.effect.n_pairs == 1


def test_factor_contrast_round_trips_through_standalone_verifier(
    tmp_path: Path,
) -> None:
    project = _factor_project(tmp_path)
    result = Pipeline(project, tmp_path / "records").run(_experiment(project))

    verified = verify_observation_report(result.report_path)

    assert verified.report.experiment_id == "factor-contrast"


def test_factor_contrast_shape_is_closed() -> None:
    with pytest.raises(ValidationError, match="requires factor_id"):
        ExperimentContrast(
            mode="one_factor",
            counterbalanced=False,
        )
    with pytest.raises(ValidationError, match="must be distinct"):
        ExperimentContrast(
            mode="one_factor",
            factor_id="model.primary",
            control_level="same",
            treatment_level="same",
            counterbalanced=False,
        )
    with pytest.raises(ValidationError, match="ablation requires"):
        ExperimentContrast(
            mode="all_arms",
            design="ablation",
            counterbalanced=False,
        )


def test_factor_contrast_references_registered_levels() -> None:
    contrast = ExperimentContrast(
        mode="one_factor",
        factor_id="configuration.optional-flag",
        control_level="disabled",
        treatment_level="enabled",
        counterbalanced=False,
    )

    assert contrast.factor_id == "configuration.optional-flag"
    assert contrast.control_level == "disabled"
    assert contrast.treatment_level == "enabled"


def test_all_arms_round_trips_with_canonical_optional_fields() -> None:
    contrast = ExperimentContrast(
        mode="all_arms",
        counterbalanced=False,
    )
    assert (
        ExperimentContrast.model_validate(contrast.model_dump(mode="json"))
        == contrast
    )


def test_factor_contrast_json_schema_closes_arm_forms() -> None:
    schema = schema_documents()["experiment-contrast"]
    validator_type = jsonschema.validators.validator_for(schema)
    validator_type.check_schema(schema)
    validator = validator_type(schema)

    valid = (
        ExperimentContrast(
            mode="all_arms",
            counterbalanced=False,
        ),
        ExperimentContrast(
            mode="one_factor",
            factor_id="subject.primary",
            control_level="control",
            treatment_level="treatment",
            counterbalanced=False,
        ),
        ExperimentContrast(
            mode="one_factor",
            design="ablation",
            factor_id="agent.component.optional",
            control_level="disabled",
            treatment_level="enabled",
            counterbalanced=False,
        ),
    )
    for contrast in valid:
        validator.validate(contrast.model_dump(mode="json"))

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {
                "mode": "all_arms",
                "unexpected": "field",
                "counterbalanced": False,
            }
        )
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {
                "mode": "one_factor",
                "factor_id": "model.primary",
                "control_level": 1,
                "treatment_level": "model-b",
                "counterbalanced": False,
            }
        )
