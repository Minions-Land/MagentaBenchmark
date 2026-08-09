from __future__ import annotations

from pathlib import Path

import pytest

from MagentaBench.schemas import (
    RegistryLockError,
    build_registry_lock,
    verify_registry_lock,
    write_registry_lock,
)


def _registry(root: Path) -> Path:
    directory = root / "registries"
    (directory / "metrics").mkdir(parents=True)
    (directory / "metrics/reward.toml").write_text(
        '[metric]\nid = "metric.reward.v1"\nkind = "metric"\nadapter = "test"\nbmp_version = "0.1"\nvalue_kind = "continuous"\nlevel = "rollout"\ndirection = "maximize"\nunit = "score"\nsource = "evaluator"\nsource_field = "score"\nformula = "direct_v1"\npopulation = "evaluator_observations"\nmissing_observation = "invalidate"\n',
        encoding="utf-8",
    )
    return directory


def test_registry_lock_round_trips_and_detects_byte_drift(tmp_path: Path) -> None:
    root = _registry(tmp_path)
    lock = write_registry_lock(root)
    catalog = verify_registry_lock(root, lock)
    assert catalog.entries[0].path == "metrics/reward.toml"

    declaration = root / "metrics/reward.toml"
    declaration.write_text(declaration.read_text() + "\n", encoding="utf-8")
    with pytest.raises(RegistryLockError, match="changed=.*reward.toml"):
        verify_registry_lock(root, lock)


def test_registry_lock_rejects_extra_and_malformed_declarations(tmp_path: Path) -> None:
    root = _registry(tmp_path)
    lock = write_registry_lock(root)
    (root / "metrics/extra.toml").write_text(
        '[metric]\nid = "metric.extra.v1"\nkind = "metric"\nadapter = "test"\nbmp_version = "0.1"\nvalue_kind = "continuous"\nlevel = "rollout"\ndirection = "maximize"\nunit = "score"\nsource = "evaluator"\nsource_field = "score"\nformula = "direct_v1"\npopulation = "evaluator_observations"\nmissing_observation = "invalidate"\n',
        encoding="utf-8",
    )
    with pytest.raises(RegistryLockError, match="extra=.*extra.toml"):
        verify_registry_lock(root, lock)

    (root / "metrics/bad.toml").write_text("[unknown]\nid = 'bad'\n", encoding="utf-8")
    with pytest.raises(RegistryLockError, match="exactly one known section"):
        build_registry_lock(root)
