from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from MagentaBench.runner.adapter_source import import_closure
from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas import (
    ArtifactRef,
    MetricArtifact,
    MetricSpec,
    ResolvedBmpManifest,
)
from MagentaBench.schemas.models import AdapterCapability, AdapterCapabilityArtifact
from MagentaBench.schemas.verification import (
    _verify_manifest_adapter_capabilities,
    _verify_manifest_measurement_registry,
)


ROOT = Path(__file__).parents[1]
FAKE_SWEEP = ROOT / "MagentaBench/conformance/experiments/fake-sweep.toml"


def _artifact_ref(path: Path) -> ArtifactRef:
    content = path.read_bytes()
    return ArtifactRef(
        path=str(path.resolve()),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _external_metric(config: Mapping[str, Any]) -> MetricArtifact:
    metric = MetricSpec(
        id="cell-ledger.test.v1",
        kind="metric",
        adapter="magentabench.cell-metrics",
        bmp_version="0.1",
        value_kind="continuous",
        level="experiment",
        direction="maximize",
        unit="reward",
        source="regime",
        source_field="cell_ledger",
        formula="external_adapter_v1",
        population="domains",
        config=config,
        missing_observation="invalidate",
    )
    declaration_ref = _artifact_ref(
        ROOT / "registries/metrics/reward-authoritative-v1.toml"
    )
    provisional = MetricArtifact(
        metric=metric,
        declaration_ref=declaration_ref,
        artifact_digest="0" * 64,
    )
    return provisional.model_copy(
        update={"artifact_digest": provisional.canonical_digest()}
    )


def _manifest_with_external_metric(
    config: Mapping[str, Any],
    *,
    capability_artifact: AdapterCapabilityArtifact,
) -> ResolvedBmpManifest:
    compiler = Compiler(ROOT)
    manifest = compiler.compile(FAKE_SWEEP)[0].manifest
    metadata = manifest.metadata.model_copy(
        update={"adapter_capabilities": (capability_artifact,)}
    )
    return manifest.model_copy(
        update={
            "metrics": (*manifest.metrics, _external_metric(config)),
            "metadata": metadata,
        }
    )


def _isolated_metric_capability(tmp_path: Path) -> AdapterCapabilityArtifact:
    """Compile a self-contained capability with its current implementation."""

    project = tmp_path / "capability-project"
    declaration = project / "registries/adapters/cell-metrics-source.toml"
    declaration.parent.mkdir(parents=True)
    implementation = ROOT / "plugins/cell_metrics/adapter.py"
    implementation_digest = hashlib.sha256(implementation.read_bytes()).hexdigest()
    declaration_lines = []
    for line in (
        ROOT / "registries/adapters/cell-metrics-source.toml"
    ).read_text(encoding="utf-8").splitlines():
        if line.startswith("digest = "):
            line = f'digest = "{implementation_digest}"'
        declaration_lines.append(line)
    declaration.write_text("\n".join(declaration_lines) + "\n", encoding="utf-8")

    for source in import_closure(ROOT, implementation):
        target = project / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    artifact = Compiler(project)._adapter_capability_artifact(
        "magentabench.cell-metrics", "metric_source"
    )
    assert artifact is not None
    return artifact


def _valid_external_metric_config() -> dict[str, Any]:
    return {
        "group_axis": "domain",
        "member_axis": "task",
        "stage_id": "sample",
        "checkpoint_id": "final",
        "members_by_group": {"domain-a": ["task-a"]},
        "within_group": "mean",
        "across_groups": "macro_mean",
    }


@pytest.mark.parametrize(
    ("mutate", "expected"),
    (
        (
            lambda text: text + "\n[undeclared]\nenabled = true\n",
            "declaration must contain only [adapter]",
        ),
        (
            lambda text: text.replace(
                'supported_metric_sources = ["regime"]',
                'supported_metric_sources = ["regime", "evolution"]',
            ),
            "declaration/spec drift",
        ),
    ),
)
def test_adapter_capability_declaration_is_strictly_replayed(
    tmp_path: Path,
    mutate: Callable[[str], str],
    expected: str,
) -> None:
    original = _isolated_metric_capability(tmp_path)

    # The isolated import closure keeps every non-declaration check valid. The
    # attack changes only declaration bytes, then updates their ArtifactRef.
    declaration = Path(original.declaration_ref.path)
    original_text = Path(original.declaration_ref.path).read_text(encoding="utf-8")
    declaration.write_text(mutate(original_text), encoding="utf-8")

    attacked = original.model_copy(
        update={
            "declaration_ref": _artifact_ref(declaration),
            "artifact_digest": "0" * 64,
        }
    )
    attacked = attacked.model_copy(
        update={"artifact_digest": attacked.canonical_digest()}
    )
    manifest = _manifest_with_external_metric(
        _valid_external_metric_config(),
        capability_artifact=attacked,
    )

    mismatches: list[str] = []
    _verify_manifest_adapter_capabilities(
        manifest,
        label="manifest.adapter_capabilities",
        path_map={},
        mismatches=mismatches,
    )
    assert any(expected in mismatch for mismatch in mismatches), mismatches


def test_external_metric_config_is_revalidated_against_capability_schema(
    tmp_path: Path,
) -> None:
    capability = _isolated_metric_capability(tmp_path)
    manifest = _manifest_with_external_metric(
        {"undeclared": True}, capability_artifact=capability
    )
    mismatches: list[str] = []

    _verify_manifest_adapter_capabilities(
        manifest,
        label="manifest.adapter_capabilities",
        path_map={},
        mismatches=mismatches,
    )

    assert any(
        "metric config fails capability JSON Schema" in mismatch
        for mismatch in mismatches
    ), mismatches


def test_external_metric_capability_json_schema_is_rechecked(
    tmp_path: Path,
) -> None:
    original = _isolated_metric_capability(tmp_path)
    invalid_schema = dict(original.capability.metric_config_schema)
    invalid_schema["type"] = 42
    schema_digest = hashlib.sha256(
        json.dumps(
            invalid_schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    capability_payload = original.capability.model_dump(mode="json")
    capability_payload.update(
        {
            "metric_config_schema": invalid_schema,
            "metric_config_schema_digest": schema_digest,
        }
    )
    invalid_capability = AdapterCapability.model_validate(capability_payload)

    declaration = Path(original.declaration_ref.path)
    declaration_text = declaration.read_text(encoding="utf-8")
    declaration_text = declaration_text.replace(
        f'metric_config_schema_digest = "{original.capability.metric_config_schema_digest}"',
        f'metric_config_schema_digest = "{schema_digest}"',
    ).replace('type = "object"', "type = 42", 1)
    declaration.write_text(declaration_text, encoding="utf-8")
    attacked = original.model_copy(
        update={
            "capability": invalid_capability,
            "declaration_ref": _artifact_ref(declaration),
            "artifact_digest": "0" * 64,
        }
    )
    attacked = attacked.model_copy(
        update={"artifact_digest": attacked.canonical_digest()}
    )
    manifest = _manifest_with_external_metric(
        _valid_external_metric_config(), capability_artifact=attacked
    )
    mismatches: list[str] = []

    _verify_manifest_adapter_capabilities(
        manifest,
        label="manifest.adapter_capabilities",
        path_map={},
        mismatches=mismatches,
    )

    assert any(
        "capability metric config JSON Schema is invalid" in mismatch
        for mismatch in mismatches
    ), mismatches


def _relocatable_regime_project(
    tmp_path: Path,
) -> tuple[Path, Path, ResolvedBmpManifest]:
    original = tmp_path / "original"
    shutil.copytree(ROOT / "registries", original / "registries")
    shutil.copytree(
        ROOT / "MagentaBench/conformance",
        original / "MagentaBench/conformance",
    )
    dataset_declaration = original / "registries/datasets/fake-exact.toml"
    dataset_source = (
        original / "MagentaBench/conformance/fixtures/fake_benchmark"
    ).resolve()
    dataset_declaration.write_text(
        dataset_declaration.read_text(encoding="utf-8").replace(
            'source = "../../MagentaBench/conformance/fixtures/fake_benchmark"',
            f'source = "{dataset_source}"',
        ),
        encoding="utf-8",
    )
    compiler = Compiler(original)
    base_experiment = (
        original / "MagentaBench/conformance/experiments/fake-sweep.toml"
    )
    manifest = compiler.compile(base_experiment)[0].manifest
    regime = compiler._regime_artifact("repeated-sampling.fake.v1")
    metadata = manifest.metadata.model_copy(update={"regime_stage_id": "sample"})
    # The test exercises the standalone registry verifier directly. Avoid
    # coupling this fixture to unrelated active-stage compiler policies.
    manifest = manifest.model_copy(
        update={"regime": regime, "metadata": metadata}
    )
    relocated = tmp_path / "relocated"
    shutil.copytree(original, relocated)
    return original, relocated, manifest


def test_regime_dataset_dependency_uses_relocated_source_root(
    tmp_path: Path,
) -> None:
    original, relocated, manifest = _relocatable_regime_project(tmp_path)
    old_source = (
        original / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    )
    relocated_source = (
        relocated / "MagentaBench/conformance/fixtures/fake_benchmark/tasks.toml"
    )
    original_bytes = old_source.read_bytes()
    relocation = {str(original): str(relocated)}

    # Poisoning the still-present old checkout must not affect verification.
    old_source.write_bytes(b"old checkout must never be read\n")
    mismatches: list[str] = []
    _verify_manifest_measurement_registry(
        manifest,
        label="manifest.measurement_registry",
        path_map=relocation,
        mismatches=mismatches,
    )
    assert not any(
        "regime.dependencies" in mismatch for mismatch in mismatches
    ), mismatches

    # Conversely, drift in the relocated source must be observed even though
    # the original checkout still contains the recorded bytes.
    old_source.write_bytes(original_bytes)
    relocated_source.write_bytes(b"relocated checkout drift\n")
    mismatches = []
    _verify_manifest_measurement_registry(
        manifest,
        label="manifest.measurement_registry",
        path_map=relocation,
        mismatches=mismatches,
    )
    assert any(
        "regime.dependencies" in mismatch and "artifact_digest drift" in mismatch
        for mismatch in mismatches
    ), mismatches
