from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import CompilationError, Compiler
from MagentaBench.schemas import (
    EvolutionSurfacePolicy,
    build_registry_lock,
    load_evolution_method_spec,
    load_meta_evolution_method_spec,
)


ROOT = Path(__file__).parents[1]
EXPERIMENTS = ROOT / "MagentaBench/conformance/experiments"


def test_evolution_method_registries_are_strict_and_typed(tmp_path: Path) -> None:
    evolver = load_evolution_method_spec(
        ROOT / "registries/evolvers/deterministic-v1.toml"
    )
    meta = load_meta_evolution_method_spec(
        ROOT / "registries/meta_evolvers/deterministic-v1.toml"
    )

    assert evolver.comparison_kind == "evolution_method"
    assert evolver.selection.metric_id == "reward.authoritative.v1"
    assert meta.comparison_kind == "meta_evolution_method"
    assert meta.parent_evolver_id == evolver.id
    sections = {entry.section for entry in build_registry_lock(ROOT / "registries").entries}
    assert {"evolver", "meta_evolver"}.issubset(sections)

    malformed = tmp_path / "malformed.toml"
    malformed.write_text(
        (ROOT / "registries/evolvers/deterministic-v1.toml")
        .read_text(encoding="utf-8")
        .replace('target = "harness"', 'target = "harness"\nunknown = true'),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_evolution_method_spec(malformed)


def test_evolution_surface_policy_fails_closed() -> None:
    policy = EvolutionSurfacePolicy(
        editable_paths=("parent_evolver",),
        protected_paths=("dataset", "evaluator", "metrics", "sealed_holdout"),
    )
    policy.assert_changes_allowed(("parent_evolver.selection_policy",))

    with pytest.raises(ValueError, match="exceed the registered editable surface"):
        policy.assert_changes_allowed(("evaluator.threshold",))
    with pytest.raises(ValidationError, match="overlap"):
        EvolutionSurfacePolicy(
            editable_paths=("evaluator.threshold",),
            protected_paths=("dataset", "evaluator", "metrics", "sealed_holdout"),
        )
    with pytest.raises(ValidationError, match="measurement authority"):
        EvolutionSurfacePolicy(
            editable_paths=("candidate",),
            protected_paths=("dataset", "evaluator", "metrics"),
        )


def test_compiler_binds_evolver_and_meta_evolver_lineage() -> None:
    compiler = Compiler(ROOT)
    evolution = compiler.compile(
        EXPERIMENTS / "deterministic-evolution-smoke.toml"
    )[0].manifest
    meta = compiler.compile(
        EXPERIMENTS / "deterministic-meta-evolution-smoke.toml"
    )[0].manifest

    assert evolution.metadata.evolver is not None
    assert evolution.metadata.meta_evolver is None
    assert evolution.metadata.configuration is not None
    assert (
        evolution.metadata.evolver.configuration_digest
        == evolution.metadata.configuration.artifact_digest
    )
    assert meta.metadata.evolver is not None
    assert meta.metadata.meta_evolver is not None
    assert (
        meta.metadata.meta_evolver.parent_evolver_digest
        == meta.metadata.evolver.artifact_digest
    )
    assert meta.metadata.meta_evolver.comparison_kind == "meta_evolution_method"


def test_method_digest_changes_with_bound_configuration() -> None:
    compiler = Compiler(ROOT)
    experiment = EXPERIMENTS / "deterministic-evolution-smoke.toml"
    control = compiler.compile(experiment)[0].manifest
    changed = compiler.compile(
        experiment,
        config_overrides={"evolution.generation_step": 3},
    )[0].manifest

    assert control.metadata.configuration is not None
    assert changed.metadata.configuration is not None
    assert control.metadata.evolver is not None
    assert changed.metadata.evolver is not None
    assert (
        control.metadata.configuration.artifact_digest
        != changed.metadata.configuration.artifact_digest
    )
    assert (
        control.metadata.evolver.artifact_digest
        != changed.metadata.evolver.artifact_digest
    )


def test_compiler_rejects_missing_method_and_selector_drift(tmp_path: Path) -> None:
    source = (EXPERIMENTS / "deterministic-evolution-smoke.toml").read_text(
        encoding="utf-8"
    )
    missing = tmp_path / "missing-method.toml"
    missing.write_text(
        source.replace('evolver = "evolver.deterministic.v1"\n', ""),
        encoding="utf-8",
    )
    with pytest.raises(CompilationError, match=r"requires \[experiment\].evolver"):
        Compiler(ROOT).compile(missing)

    with pytest.raises(CompilationError, match="selection configuration drift"):
        Compiler(ROOT).compile(
            EXPERIMENTS / "deterministic-evolution-smoke.toml",
            config_overrides={"evolution.selection.direction": "minimize"},
        )
