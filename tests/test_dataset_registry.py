from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import (
    CompilationError,
    Compiler,
    RegistryLookupError,
    enforce_allowed_diff,
)
from MagentaBench.schemas import DatasetSpec, load_dataset_spec


ROOT = Path(__file__).parents[1]


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "registries", project / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance/fixtures/fake_benchmark",
        project / "MagentaBench/conformance/fixtures/fake_benchmark",
    )

    source = project / "dataset-source"
    source.mkdir()
    (source / "rows.jsonl").write_text('{"id":"case-1"}\n', encoding="utf-8")

    registry = project / "registries/datasets/demo.toml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        '''[dataset]
id = "demo.dataset.v1"
kind = "dataset"
adapter = "fake"
bmp_version = "0.1"
source = "../../dataset-source"
content_globs = ["rows.jsonl"]
format = "jsonl"
split = "test"

[dataset.config]
id_field = "id"
''',
        encoding="utf-8",
    )

    experiment = project / "dataset-experiment.toml"
    experiment.write_text(
        '''[experiment]
id = "dataset-registry"
benchmark = "fake.exact.v1"
dataset = "demo.dataset.v1"
evaluator = "evaluator.fake.exact.v1"
metrics = ["reward.authoritative.v1"]
subject = "fake.control"
protocol = "fake.deterministic.v1"
factors = []

[experiment.contrast]
mode = "all_arms"
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
''',
        encoding="utf-8",
    )
    return project, registry, experiment


def test_dataset_toml_contract_is_strict_and_immutable(tmp_path: Path) -> None:
    path = tmp_path / "dataset.toml"
    path.write_text(
        '''[dataset]
id = "demo.dataset.v1"
kind = "dataset"
adapter = "demo"
bmp_version = "0.1"
source = "./data"
content_globs = ["rows/*.jsonl"]
format = "jsonl"

[dataset.config]
id_field = "id"
''',
        encoding="utf-8",
    )
    spec = load_dataset_spec(path)
    assert isinstance(spec, DatasetSpec)
    assert spec.content_globs == ("rows/*.jsonl",)
    assert spec.config == {"id_field": "id"}
    with pytest.raises(TypeError, match="immutable"):
        spec.config["id_field"] = "other"  # type: ignore[index]

    payload = spec.model_dump(mode="python")
    payload["undeclared"] = True
    with pytest.raises(ValidationError, match="undeclared"):
        DatasetSpec.model_validate(payload)


def test_compiler_binds_dataset_content_and_registry_declaration(
    tmp_path: Path,
) -> None:
    project, registry, experiment = _project(tmp_path)
    first = Compiler(project).compile(experiment)[0]
    dataset = first.manifest.dataset
    assert dataset is not None
    assert dataset.id == "demo.dataset.v1"
    assert dataset.source == str((project / "dataset-source").resolve())
    assert dataset.commit is None
    assert dataset.config == {"id_field": "id"}
    assert len(dataset.source_content_digest) == 64
    assert len(dataset.artifact_digest) == 64

    relocated = project / "relocated-dataset"
    shutil.copytree(project / "dataset-source", relocated)
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            'source = "../../dataset-source"',
            'source = "../../relocated-dataset"',
        ),
        encoding="utf-8",
    )
    same_content = Compiler(project).compile(experiment)[0]
    assert same_content.manifest.dataset is not None
    assert same_content.manifest.dataset.source != dataset.source
    # The source path is provenance-only, but changing the TOML declaration
    # itself changes its declaration ref and therefore the strict artifact
    # identity.  Content closure remains the same.
    assert (
        same_content.manifest.dataset.source_content_digest
        == dataset.source_content_digest
    )
    assert same_content.manifest.dataset.artifact_digest != dataset.artifact_digest
    assert same_content.manifest_digest != first.manifest_digest
    assert enforce_allowed_diff(
        first.manifest,
        same_content.manifest,
        (
            "dataset.artifact_digest",
            "dataset.declaration_ref.sha256",
            "dataset.declaration_ref.size_bytes",
        ),
    ) == (
        "dataset.artifact_digest",
        "dataset.declaration_ref.sha256",
        "dataset.declaration_ref.size_bytes",
    )

    (relocated / "rows.jsonl").write_text('{"id":"case-2"}\n', encoding="utf-8")
    changed = Compiler(project).compile(experiment)[0]
    assert changed.manifest.dataset is not None
    assert changed.manifest.dataset.source_content_digest != dataset.source_content_digest
    assert changed.manifest.dataset.artifact_digest != dataset.artifact_digest
    assert changed.manifest_digest != first.manifest_digest
    dataset_diff = (
        "dataset.artifact_digest",
        "dataset.declaration_ref.sha256",
        "dataset.declaration_ref.size_bytes",
        "dataset.source_content_digest",
    )
    assert enforce_allowed_diff(
        first.manifest,
        changed.manifest,
        dataset_diff,
    ) == dataset_diff


def test_opaque_dataset_release_label_is_identity_bearing(tmp_path: Path) -> None:
    project, registry, experiment = _project(tmp_path)
    declaration = registry.read_text(encoding="utf-8")
    registry.write_text(
        declaration.replace(
            'split = "test"',
            'split = "test"\ncommit = "fixture-release-v1"',
        ),
        encoding="utf-8",
    )
    first = Compiler(project).compile(experiment)[0].manifest.dataset
    assert first is not None
    assert first.commit == "fixture-release-v1"

    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            'commit = "fixture-release-v1"',
            'commit = "fixture-release-v2"',
        ),
        encoding="utf-8",
    )
    second = Compiler(project).compile(experiment)[0].manifest.dataset
    assert second is not None
    assert second.commit == "fixture-release-v2"
    assert second.artifact_digest != first.artifact_digest


def test_dataset_registry_lookup_rejects_duplicate_ids(tmp_path: Path) -> None:
    project, registry, experiment = _project(tmp_path)
    duplicate = registry.with_name("duplicate.toml")
    duplicate.write_bytes(registry.read_bytes())

    with pytest.raises(RegistryLookupError, match="duplicate dataset registry id"):
        Compiler(project).compile(experiment)


def test_compiler_rejects_dataset_loader_adapter_mismatch(tmp_path: Path) -> None:
    project, registry, experiment = _project(tmp_path)
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            'adapter = "fake"',
            'adapter = "different_loader"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(CompilationError, match="does not match benchmark adapter"):
        Compiler(project).compile(experiment)
