from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import Compiler
from MagentaBench.runner.gates import _evaluate_claim
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    ExperimentContrast,
    RunPurpose,
    schema_documents,
    verify_observation_report,
)


ROOT = Path(__file__).parents[1]


def _experiment(tmp_path: Path) -> Path:
    path = tmp_path / "factor-contrast.toml"
    path.write_text(
        '''
[experiment]
id = "factor-contrast"
benchmark = "fake.exact.v1"
subject = "fake.treatment"
protocol = "fake.deterministic.v1"
allowed_diff = ["execution.budget.max_wall_seconds"]

[experiment.contrast]
mode = "one_factor"
factor_path = "execution.budget.max_wall_seconds"
control_value = 1.0
treatment_value = 2.0
counterbalanced = false

[experiment.design]
scope = "conformance"
purpose = "exploratory"
vary = []

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
    runs = Compiler(ROOT).compile(_experiment(tmp_path))

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
    result = Pipeline(ROOT, tmp_path / "records").run(_experiment(tmp_path))
    completed = []
    for item in result.runs:
        design = item.plan.manifest.claim_design.model_copy(
            update={"purpose": RunPurpose.claim}
        )
        manifest = item.plan.manifest.model_copy(update={"claim_design": design})
        completed.append(replace(item, plan=replace(item.plan, manifest=manifest)))

    report = _evaluate_claim(
        completed=completed,
        expected_run_count=2,
        control_id="__factor_control__",
        treatment_id="__factor_treatment__",
        deterministic_conformance=False,
        counterbalanced=False,
        record_index_ref=None,
        contrast_factor_path="execution.budget.max_wall_seconds",
        contrast_control_value=1.0,
        contrast_treatment_value=2.0,
    )

    assert report.effect is not None
    assert report.effect.n_pairs == 1


def test_factor_contrast_round_trips_through_standalone_verifier(
    tmp_path: Path,
) -> None:
    result = Pipeline(ROOT, tmp_path / "records").run(_experiment(tmp_path))

    verified = verify_observation_report(result.report_path)

    assert verified.report.experiment_id == "factor-contrast"


def test_factor_contrast_shape_is_closed() -> None:
    with pytest.raises(ValidationError, match="cannot provide control_id"):
        ExperimentContrast(
            mode="one_factor",
            control_id="legacy-control",
            factor_path="execution.model",
            control_value="model-a",
            treatment_value="model-b",
            counterbalanced=False,
        )
    with pytest.raises(ValidationError, match="must be distinct"):
        ExperimentContrast(
            mode="one_factor",
            factor_path="execution.model",
            control_value={"model": "same"},
            treatment_value={"model": "same"},
            counterbalanced=False,
        )


def test_factor_contrast_allows_json_null_as_an_arm_value() -> None:
    contrast = ExperimentContrast(
        mode="one_factor",
        factor_path="execution.backend_overrides.optional_flag",
        control_value=None,
        treatment_value=True,
        counterbalanced=False,
    )

    assert contrast.control_value is None
    assert contrast.treatment_value is True
    assert "control_value" in contrast.model_fields_set


def test_all_arms_ignores_canonical_optional_nulls() -> None:
    # Optional arm fields serialize as null in canonical manifests.  Reloading
    # such a manifest must remain valid and equivalent to the omitted form.
    contrast = ExperimentContrast(
        mode="all_arms",
        control_value=None,
        treatment_value=None,
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
            control_id="control",
            treatment_id="treatment",
            counterbalanced=False,
        ),
        ExperimentContrast(
            mode="one_factor",
            factor_path="configuration.values.optional",
            control_value=None,
            treatment_value=True,
            counterbalanced=False,
        ),
    )
    for contrast in valid:
        validator.validate(contrast.model_dump(mode="json"))

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {
                "mode": "all_arms",
                "factor_path": "execution.model",
                "counterbalanced": False,
            }
        )
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {
                "mode": "one_factor",
                "factor_path": "execution.model",
                "control_id": "legacy-control",
                "control_value": "model-a",
                "treatment_value": "model-b",
                "counterbalanced": False,
            }
        )
