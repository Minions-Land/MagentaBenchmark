from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from MagentaBench.runner.configuration import (
    ConfigurationDriftError,
    ConfigurationNotFoundError,
    ConfigurationRegistry,
    ConfigurationRegistryError,
    apply_dotted_override,
    apply_dotted_overrides,
    deep_merge,
)
from MagentaBench.runner.configuration_cli import main as configuration_main


def test_registry_crud_is_canonical_and_content_addressed(tmp_path: Path) -> None:
    root = tmp_path / "configuration-registry"
    registry = ConfigurationRegistry(root)
    assert registry.list() == ()

    document = {
        "tags": ["fair", "audited"],
        "model": {"temperature": 0.2, "enabled": True},
    }
    first = registry.upsert("profile.default", document)
    assert first.path == root / "objects" / f"{first.sha256}.toml"
    assert first.sha256 == hashlib.sha256(first.toml_bytes).hexdigest()
    assert first.size_bytes == len(first.toml_bytes)
    assert first.data == document

    equivalent = registry.upsert(
        "profile.copy",
        'tags = ["fair", "audited"]\n\n[model]\nenabled = true\ntemperature = 0.2\n',
    )
    assert equivalent.sha256 == first.sha256
    assert equivalent.path == first.path
    assert [record.name for record in registry.list()] == [
        "profile.copy",
        "profile.default",
    ]

    updated = registry.upsert(
        "profile.default",
        {"model": {"enabled": True, "temperature": 0.8}},
    )
    assert updated.sha256 != first.sha256
    assert first.path.is_file()
    assert registry.get("profile.default") == updated

    detached = updated.data
    detached["model"]["temperature"] = 99
    assert registry.get("profile.default").data["model"]["temperature"] == 0.8

    assert registry.delete("profile.copy") is True
    assert registry.delete("profile.copy") is False
    with pytest.raises(ConfigurationNotFoundError, match="does not exist"):
        registry.get("profile.copy")
    assert not tuple(root.rglob("*.tmp"))


def test_empty_toml_is_a_valid_content_addressed_configuration(
    tmp_path: Path,
) -> None:
    registry = ConfigurationRegistry(tmp_path / "registry")
    record = registry.upsert("empty", "")

    assert record.toml_bytes == b""
    assert record.data == {}
    assert record.sha256 == hashlib.sha256(b"").hexdigest()
    assert registry.get("empty") == record


def test_deep_merge_and_dotted_overrides_are_detached_and_closed() -> None:
    base = {
        "model": {"temperature": 0.2, "limits": {"tokens": 100}},
        "tags": ["base"],
    }
    override = {
        "model": {"limits": {"cost": 1.5}},
        "tags": ["override"],
    }
    merged = deep_merge(base, override)
    assert merged == {
        "model": {
            "temperature": 0.2,
            "limits": {"tokens": 100, "cost": 1.5},
        },
        "tags": ["override"],
    }
    assert base["model"]["limits"] == {"tokens": 100}
    assert override["model"]["limits"] == {"cost": 1.5}

    dotted = apply_dotted_overrides(
        base,
        {
            "model.limits.tokens": 256,
            "runtime.parallelism": 4,
        },
    )
    assert dotted["model"]["limits"]["tokens"] == 256
    assert dotted["runtime"] == {"parallelism": 4}
    assert apply_dotted_override(base, "model.temperature", 0.0)["model"][
        "temperature"
    ] == 0.0

    with pytest.raises(ConfigurationRegistryError, match="traverses non-table"):
        apply_dotted_override(base, "tags.value", "invalid")
    with pytest.raises(ConfigurationRegistryError, match="must not overlap"):
        apply_dotted_overrides(base, {"model": {}, "model.temperature": 0.5})
    with pytest.raises(ConfigurationRegistryError, match="normalized dotted"):
        apply_dotted_override(base, "model..temperature", 0.5)
    with pytest.raises(ConfigurationRegistryError, match="dotted-path mapping"):
        apply_dotted_overrides(base, [])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "document",
    [
        {"api_key": "value"},
        {"auth": {"accessToken": "value"}},
        {"providers": [{"credential_name": "value"}]},
    ],
)
def test_secret_like_keys_are_rejected_recursively(
    tmp_path: Path, document: dict[str, object]
) -> None:
    registry = ConfigurationRegistry(tmp_path / "registry")
    with pytest.raises(ConfigurationRegistryError, match="secret-like key"):
        registry.upsert("unsafe", document)
    with pytest.raises(ConfigurationRegistryError, match="secret-like key"):
        deep_merge({}, document)

    with pytest.raises(ConfigurationRegistryError, match="secret-like key"):
        apply_dotted_override({}, "provider.api_key", "value")
    assert registry.upsert("safe", {"description": "token is a value"}).data == {
        "description": "token is a value"
    }


def test_raw_toml_secret_like_keys_are_rejected(tmp_path: Path) -> None:
    registry = ConfigurationRegistry(tmp_path / "registry")

    with pytest.raises(ConfigurationRegistryError, match="secret-like key"):
        registry.upsert("unsafe", '[provider]\napi_key = "value"\n')
    with pytest.raises(ConfigurationRegistryError, match="secret-like key"):
        registry.upsert("unsafe-acronym", '[provider]\nAPIKey = "value"\n')
    with pytest.raises(ConfigurationRegistryError, match="secret-like key"):
        registry.upsert("unsafe-token", '[provider]\naccess_tokens = "value"\n')

    safe = registry.upsert("safe-budget", "tokens = 100\nmax_tokens = 50\n")
    assert safe.data == {"tokens": 100, "max_tokens": 50}


def test_upsert_rejects_unreadable_existing_object(tmp_path: Path) -> None:
    registry = ConfigurationRegistry(tmp_path / "registry")
    record = registry.upsert("first", {"value": 1})
    record.path.unlink()
    record.path.mkdir()

    with pytest.raises(ConfigurationDriftError, match="object is unreadable"):
        registry.upsert("second", {"value": 1})
    assert not tuple(registry.root.rglob("*.tmp"))


def test_registry_rejects_symlink_roots_indexes_and_objects(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ConfigurationDriftError, match="symlink"):
        ConfigurationRegistry(linked_root)

    index_registry = ConfigurationRegistry(tmp_path / "index-registry")
    index_registry.upsert("default", {"value": 1})
    index_path = index_registry.root / "index.json"
    saved_index = tmp_path / "saved-index.json"
    index_path.replace(saved_index)
    index_path.symlink_to(saved_index)
    with pytest.raises(ConfigurationDriftError, match="index is a symlink"):
        index_registry.list()

    object_registry = ConfigurationRegistry(tmp_path / "object-registry")
    record = object_registry.upsert("default", {"value": 1})
    outside = tmp_path / "outside.toml"
    outside.write_bytes(record.toml_bytes)
    record.path.unlink()
    record.path.symlink_to(outside)
    with pytest.raises(ConfigurationDriftError, match="object is a symlink"):
        object_registry.get("default")


def test_registry_rejects_content_and_directory_path_drift(tmp_path: Path) -> None:
    content_registry = ConfigurationRegistry(tmp_path / "content-registry")
    record = content_registry.upsert("default", {"value": 1})
    record.path.write_text('"value" = 2\n', encoding="utf-8")
    with pytest.raises(ConfigurationDriftError, match="content drift"):
        content_registry.get("default")
    with pytest.raises(ConfigurationDriftError, match="content drift"):
        content_registry.delete("default")

    directory_registry = ConfigurationRegistry(tmp_path / "directory-registry")
    directory_registry.upsert("default", {"value": 1})
    objects = directory_registry.root / "objects"
    displaced = directory_registry.root / "objects-old"
    objects.rename(displaced)
    objects.mkdir()
    with pytest.raises(ConfigurationDriftError, match="directory path drift"):
        directory_registry.list()


def test_configuration_cli_exposes_crud(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "project"
    source = tmp_path / "agent.toml"
    source.write_text('model = "gpt-5.4"\n', encoding="utf-8")

    assert configuration_main(
        ["--project-root", str(project), "put", "agent.demo", str(source)]
    ) == 0
    assert "objects" in capsys.readouterr().out
    assert configuration_main(["--project-root", str(project), "list"]) == 0
    assert "agent.demo" in capsys.readouterr().out
    assert configuration_main(["--project-root", str(project), "get", "agent.demo"]) == 0
    assert "gpt-5.4" in capsys.readouterr().out
    assert configuration_main(["--project-root", str(project), "delete", "agent.demo"]) == 0
    assert "True" in capsys.readouterr().out


def test_index_contract_and_names_cannot_redirect_object_paths(tmp_path: Path) -> None:
    registry = ConfigurationRegistry(tmp_path / "registry")
    registry.upsert("default", {"value": 1})
    index_path = registry.root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["entries"]["default"]["path"] = str(tmp_path / "outside.toml")
    index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ConfigurationDriftError, match="entry contract drift"):
        registry.get("default")

    fresh = ConfigurationRegistry(tmp_path / "fresh")
    with pytest.raises(ConfigurationRegistryError, match="normalized identifier"):
        fresh.upsert("../escape", {"value": 1})
    with pytest.raises(ConfigurationRegistryError, match="malformed"):
        fresh.upsert("broken", "not = [valid")
