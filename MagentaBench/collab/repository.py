"""Validation and change-scope policy for experiment collaboration bundles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaSchemaError
from pydantic import ValidationError

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from MagentaBench.lab import LabIssueState, LabStatus, LabStore
from MagentaBench.lab.store import LabError, utc_now
from MagentaBench.schemas.compiler import load_backend_spec
from MagentaBench.schemas.models import AdapterCapability

from .models import (
    BUNDLE_FORMAT,
    BundleDesign,
    BundleEvidence,
    BundleExecution,
    BundlePurpose,
    ExecutionMode,
    ExperimentBundle,
    portable_path,
)


class CollaborationError(ValueError):
    """A bundle or repository collaboration contract is invalid."""


def _authorize_preflight(
    bundle: ExperimentBundle,
    summary: BundleSummary,
    state: LabIssueState,
    *,
    actor: str,
    environment: Mapping[str, str],
    at: datetime,
) -> None:
    """Require an explicit live lease before any benchmark command runs."""

    if not actor or "\x00" in actor or "\n" in actor or "\r" in actor:
        raise CollaborationError("preflight actor must be a non-empty single-line identifier")
    if summary.lab_status != LabStatus.running.value or state.status != LabStatus.running:
        raise CollaborationError(
            f"preflight requires the primary lab issue to be running; observed {state.status.value}"
        )
    if summary.blocker_count or state.blockers:
        raise CollaborationError(
            f"preflight is blocked by {summary.blocker_count or len(state.blockers)} lab blocker(s)"
        )
    if not summary.dependencies_complete:
        raise CollaborationError("preflight requires every declared dependency to be done")
    lease = state.active_lease(at)
    if lease is None:
        raise CollaborationError("preflight requires a live lease on the primary lab issue")
    if lease.owner != actor:
        raise CollaborationError(
            f"preflight actor does not hold the primary lease (holder={lease.owner!r})"
        )
    missing = sorted(
        name for name in bundle.execution.required_env if not environment.get(name)
    )
    if missing:
        raise CollaborationError(
            "preflight required environment variables are missing: "
            + ", ".join(missing)
        )


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True)
class BundleSummary:
    id: str
    purpose: str
    bmp_spec: str
    protocol_id: str
    lab_issue: str
    lab_status: str
    lease_holder: str | None
    blocker_count: int
    dependencies_complete: bool
    available: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "blocker_count": self.blocker_count,
            "bmp_spec": self.bmp_spec,
            "dependencies_complete": self.dependencies_complete,
            "id": self.id,
            "lab_issue": self.lab_issue,
            "lab_status": self.lab_status,
            "lease_holder": self.lease_holder,
            "protocol_id": self.protocol_id,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class ValidationReport:
    bundles: tuple[BundleSummary, ...] = ()
    errors: tuple[Finding, ...] = ()
    warnings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle_count": len(self.bundles),
            "bundles": [bundle.as_dict() for bundle in self.bundles],
            "errors": [finding.as_dict() for finding in self.errors],
            "format": "magentabench-collaboration-validation-v1",
            "ok": self.ok,
            "warnings": [finding.as_dict() for finding in self.warnings],
        }


@dataclass(frozen=True)
class ChangeScopeReport:
    paths: tuple[str, ...]
    classes: Mapping[str, tuple[str, ...]]
    errors: tuple[Finding, ...] = ()
    warnings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "classes": {key: list(value) for key, value in sorted(self.classes.items())},
            "errors": [finding.as_dict() for finding in self.errors],
            "format": "magentabench-change-scope-v1",
            "ok": self.ok,
            "paths": list(self.paths),
            "warnings": [finding.as_dict() for finding in self.warnings],
        }


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9/+_.${}-]{8,}"
    ),
    re.compile(r"https?://[^/\s:@]+:[^/@\s]+@"),
)


def _reject_secret_material(value: Any, *, path: str = "bundle") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_secret_material(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_material(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise CollaborationError(f"bundle appears to contain secret material at {path}")


def _json_without_duplicate_keys(content: bytes, *, path: Path) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CollaborationError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollaborationError(f"cannot parse bundle JSON {path}: {exc}") from exc


def _toml(path: Path) -> Mapping[str, Any]:
    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CollaborationError(f"cannot parse TOML {path}: {exc}") from exc
    if not isinstance(document, dict):  # pragma: no cover - tomllib returns a dict
        raise CollaborationError(f"TOML document is not a table: {path}")
    return document


def _canonical_bundle_bytes(bundle: ExperimentBundle) -> bytes:
    return (
        json.dumps(
            bundle.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _bundle_model_input(raw: Any) -> Any:
    """Adapt JSON arrays/enums to the intentionally strict Python model."""

    if isinstance(raw, list):
        return tuple(_bundle_model_input(item) for item in raw)
    if isinstance(raw, dict):
        converted = {key: _bundle_model_input(value) for key, value in raw.items()}
        if "purpose" in converted and isinstance(converted["purpose"], str):
            try:
                converted["purpose"] = BundlePurpose(converted["purpose"])
            except ValueError as exc:
                raise CollaborationError(f"invalid bundle purpose: {converted['purpose']!r}") from exc
        execution = converted.get("execution")
        if isinstance(execution, dict) and isinstance(execution.get("mode"), str):
            try:
                execution["mode"] = ExecutionMode(execution["mode"])
            except ValueError as exc:
                raise CollaborationError(f"invalid execution mode: {execution['mode']!r}") from exc
        return converted
    return raw


def _normalized_changed_path(value: str) -> str:
    try:
        return portable_path(value, label="changed path")
    except ValueError as exc:
        raise CollaborationError(str(exc)) from exc


def _path_class(path: str) -> str:
    if path.startswith("imports/"):
        return "experiment-import"
    if path.startswith("experiments/"):
        return "experiment-bundle"
    if (
        path.startswith("execution-profiles/")
        or path == "docs/governance/EXECUTION_MODES.md"
    ):
        return "execution-target"
    if path.startswith("lab/issues/") or path in {"lab/README.md", "MagentaBench/lab/__init__.py"}:
        return "lab-ledger"
    if path.startswith("MagentaBench/lab/") or path == "tests/test_lab_operations.py":
        return "lab-control-plane"
    if path.startswith("MagentaBench/collab/") or path in {
        "tests/test_collaboration.py",
        "tests/test_experiment_ledger.py",
        "scripts/validate_collaboration.sh",
        "docs/EXPERIMENT_COLLABORATION.md",
        "docs/EXPERIMENT_LEDGER.md",
    }:
        return "collaboration-control-plane"
    if path.startswith("MagentaBench/conformance/experiments/"):
        return "experiment-definition"
    if path.startswith(".github/"):
        return "github-governance"
    if path.startswith("MagentaBench/schemas/") or path.startswith("MagentaBench/runner/"):
        return "bmp-protocol"
    if path.startswith("plugins/") or path.startswith("registries/"):
        return "bmp-protocol"
    if path in {"pyproject.toml", "uv.lock", "scripts/audit_hcp_boundary.sh"}:
        return "bmp-protocol"
    if path.startswith("docs/governance/bmp-") or path == "EVIDENCE.md":
        return "bmp-protocol"
    if path.startswith("MagentaBench/"):
        # Default closed: a new package cannot evade protocol review by being
        # omitted from a hand-maintained path list.
        return "bmp-protocol"
    if path.startswith("tests/") or path.startswith("scripts/"):
        return "verification"
    if path.startswith("docs/") or path in {"README.md", "AGENTS.md"}:
        return "documentation"
    return "repository-metadata"


def classify_changed_paths(
    paths: Iterable[str], *, allow_protocol_change: bool = False
) -> ChangeScopeReport:
    """Classify a patch and enforce the experiment/BMP review boundary."""

    normalized = tuple(sorted({_normalized_changed_path(path) for path in paths}))
    buckets: dict[str, list[str]] = {}
    for path in normalized:
        buckets.setdefault(_path_class(path), []).append(path)
    classes = {key: tuple(value) for key, value in sorted(buckets.items())}
    errors: list[Finding] = []
    warnings: list[Finding] = []
    protocol = classes.get("bmp-protocol", ())
    bundles = classes.get("experiment-bundle", ())
    if protocol and not allow_protocol_change:
        errors.append(
            Finding(
                code="protocol-review-required",
                message=(
                    "BMP protocol/shared registry paths changed without explicit "
                    "protocol-change approval"
                ),
            )
        )
    if protocol and bundles:
        warnings.append(
            Finding(
                code="split-protocol-and-experiment",
                message=(
                    "This patch mixes BMP protocol paths with experiment bundles; "
                    "split them into separate PRs unless one atomic migration is required"
                ),
            )
        )
    registry_declarations = tuple(
        path
        for path in protocol
        if path.startswith("registries/")
        and path.endswith(".toml")
        and path != "registries/registry.lock.toml"
    )
    if registry_declarations and "registries/registry.lock.toml" not in normalized:
        errors.append(
            Finding(
                code="registry-lock-not-updated",
                message="Registry declarations changed without registries/registry.lock.toml",
            )
        )
    bundle_ids = {
        PurePosixPath(path).parts[1]
        for path in bundles
        if len(PurePosixPath(path).parts) >= 3
        and not PurePosixPath(path).parts[1].startswith("_")
    }
    if len(bundle_ids) > 1:
        warnings.append(
            Finding(
                code="multi-bundle-patch",
                message=(
                    "The patch changes multiple experiment bundles; one bundle per PR "
                    "usually produces safer parallel merges"
                ),
            )
        )
    return ChangeScopeReport(
        paths=normalized,
        classes=classes,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


class ExperimentRepository:
    """Discover and validate collaboration bundles against BMP and lab facts."""

    def __init__(self, project_root: str | Path) -> None:
        lexical = Path(os.path.abspath(os.fspath(Path(project_root).expanduser())))
        if lexical.is_symlink() or not lexical.is_dir():
            raise CollaborationError(f"project root must be a real directory: {lexical}")
        self.root = lexical.resolve(strict=True)
        self.experiments_root = self.root / "experiments"

    def bundle_paths(self) -> tuple[Path, ...]:
        if not self.experiments_root.is_dir() or self.experiments_root.is_symlink():
            raise CollaborationError(
                f"experiments root must be a regular directory: {self.experiments_root}"
            )
        paths: list[Path] = []
        for entry in sorted(self.experiments_root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("_") or entry.name in {"README.md", ".gitkeep"}:
                continue
            if entry.is_symlink() or not entry.is_dir():
                raise CollaborationError(f"experiment entry must be a regular directory: {entry}")
            bundle = entry / "bundle.json"
            if not bundle.is_file() or bundle.is_symlink():
                raise CollaborationError(f"experiment bundle is missing or non-regular: {bundle}")
            paths.append(bundle)
        return tuple(paths)

    def load_bundle(self, path: str | Path) -> ExperimentBundle:
        selected = Path(path)
        if not selected.is_absolute():
            selected = self.root / selected
        selected = Path(os.path.abspath(os.fspath(selected)))
        if selected.is_symlink() or not selected.is_file():
            raise CollaborationError(f"bundle path must be a regular file: {selected}")
        try:
            selected.resolve(strict=True).relative_to(self.root)
        except ValueError as exc:
            raise CollaborationError(f"bundle path escapes project root: {selected}") from exc
        raw = _json_without_duplicate_keys(selected.read_bytes(), path=selected)
        _reject_secret_material(raw)
        try:
            bundle = ExperimentBundle.model_validate(_bundle_model_input(raw))
        except ValidationError as exc:
            raise CollaborationError(f"invalid experiment bundle {selected}: {exc}") from exc
        if selected.parent.name != bundle.id:
            raise CollaborationError(
                f"bundle id {bundle.id!r} must match directory {selected.parent.name!r}"
            )
        return bundle

    def _project_file(self, relative: str, *, label: str) -> Path:
        normalized = portable_path(relative, label=label)
        lexical = self.root.joinpath(*PurePosixPath(normalized).parts)
        if lexical.is_symlink() or not lexical.is_file():
            raise CollaborationError(f"{label} is missing or non-regular: {normalized}")
        resolved = lexical.resolve(strict=True)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise CollaborationError(f"{label} escapes project root: {normalized}") from exc
        current = lexical
        while current != self.root:
            if current.is_symlink():
                raise CollaborationError(f"{label} contains a symlink: {normalized}")
            current = current.parent
        return resolved

    def _protocols(self) -> dict[str, tuple[Path, Mapping[str, Any]]]:
        root = self.root / "registries/protocols"
        if not root.is_dir() or root.is_symlink():
            raise CollaborationError("registries/protocols must be a regular directory")
        protocols: dict[str, tuple[Path, Mapping[str, Any]]] = {}
        for path in sorted(root.rglob("*.toml")):
            if path.is_symlink() or not path.is_file():
                raise CollaborationError(f"protocol declaration is non-regular: {path}")
            document = _toml(path)
            protocol = document.get("protocol")
            protocol_id = protocol.get("id") if isinstance(protocol, dict) else None
            if not isinstance(protocol_id, str):
                raise CollaborationError(f"protocol declaration has no string id: {path}")
            if protocol_id in protocols:
                raise CollaborationError(f"duplicate protocol id: {protocol_id}")
            protocols[protocol_id] = (path, protocol)
        return protocols

    def _backends(self) -> dict[str, tuple[Path, Mapping[str, Any]]]:
        root = self.root / "registries/backends"
        if not root.is_dir() or root.is_symlink():
            raise CollaborationError("registries/backends must be a regular directory")
        backends: dict[str, tuple[Path, Mapping[str, Any]]] = {}
        for path in sorted(root.rglob("*.toml")):
            if path.is_symlink() or not path.is_file():
                raise CollaborationError(f"backend declaration is non-regular: {path}")
            try:
                backend_spec = load_backend_spec(path)
            except (OSError, ValueError, ValidationError) as exc:
                raise CollaborationError(
                    f"invalid backend declaration {path}: {exc}"
                ) from exc
            backend = backend_spec.model_dump(mode="python")
            backend_id = backend_spec.id
            if backend_id in backends:
                raise CollaborationError(f"duplicate backend id: {backend_id}")
            backends[backend_id] = (path, backend)
        return backends

    def _backend_factory_adapters(self) -> frozenset[str]:
        """Return backend adapters with a declared, usable factory policy."""

        builtins = {"fake", "subprocess"}
        root = self.root / "registries/adapters"
        if root.is_symlink() or not root.is_dir():
            raise CollaborationError("registries/adapters must be a regular directory")
        factories = set(builtins)
        seen: set[str] = set()
        for path in sorted(root.rglob("*.toml")):
            if path.is_symlink() or not path.is_file():
                raise CollaborationError(f"adapter declaration is non-regular: {path}")
            document = _toml(path)
            raw = document.get("adapter")
            if not isinstance(raw, dict):
                raise CollaborationError(f"adapter declaration has no [adapter] table: {path}")
            try:
                capability = AdapterCapability.model_validate(raw)
            except ValidationError as exc:
                raise CollaborationError(
                    f"invalid adapter declaration {path}: {exc}"
                ) from exc
            if capability.adapter_kind != "backend_factory":
                continue
            if capability.adapter in seen:
                raise CollaborationError(
                    f"duplicate backend_factory capability for adapter {capability.adapter!r}"
                )
            seen.add(capability.adapter)
            if capability.backend_default_read_set is None:
                raise CollaborationError(
                    f"backend_factory capability {capability.id!r} lacks a default read-set"
                )
            if capability.adapter not in capability.supported_backend_adapters:
                raise CollaborationError(
                    f"backend_factory capability {capability.id!r} does not support "
                    f"its adapter {capability.adapter!r}"
                )
            factories.add(capability.adapter)
        return frozenset(factories)

    def execution_modes(self) -> tuple[dict[str, Any], ...]:
        """Return validated profiles joined to backends and lab work items."""

        grouped: dict[ExecutionMode, list[dict[str, Any]]] = {
            mode: [] for mode in ExecutionMode
        }
        factory_adapters = self._backend_factory_adapters()
        for backend_id, (path, backend) in self._backends().items():
            mode, boundary = self._backend_mode(backend)
            grouped[mode].append(
                {
                    "adapter": str(backend.get("adapter")),
                    "backend_id": backend_id,
                    "boundary": boundary,
                    "declaration": path.relative_to(self.root).as_posix(),
                    "kind": str(backend.get("kind")),
                    "configured": str(backend.get("adapter")) in factory_adapters,
                    "standalone_verifier_boundary_closed": (
                        self._backend_verifier_boundary_closed(backend)
                    ),
                }
            )
        profiles = self._execution_profiles()
        try:
            lab_states = {
                state.issue.issue_id: state for state in LabStore(self.root).list()
            }
        except LabError as exc:
            raise CollaborationError(f"cannot load execution-profile lab links: {exc}") from exc

        results: list[dict[str, Any]] = []
        linked_issues: set[str] = set()
        for mode in ExecutionMode:
            profile_path, profile = profiles[mode]
            backends = sorted(grouped[mode], key=lambda item: item["backend_id"])
            declared_ids = list(profile["registered_backend_ids"])
            expected_ids = [item["backend_id"] for item in backends]
            if declared_ids != sorted(declared_ids):
                raise CollaborationError(
                    f"execution profile {mode.value} backend ids must be sorted"
                )
            if declared_ids != expected_ids:
                raise CollaborationError(
                    f"execution profile {mode.value} backend ids drift: "
                    f"expected {expected_ids}, observed {declared_ids}"
                )
            expected_boundary = {
                ExecutionMode.local_process: "process",
                ExecutionMode.docker: "task-container",
                ExecutionMode.apptainer: "task-container",
                ExecutionMode.appcontainer: "task-container",
                ExecutionMode.e2b: "microvm",
                ExecutionMode.remote_sandbox: "microvm",
            }[mode]
            if profile["isolation_boundary"] != expected_boundary:
                raise CollaborationError(
                    f"execution profile {mode.value} isolation boundary drift: "
                    f"expected {expected_boundary}, observed {profile['isolation_boundary']}"
                )
            if any(item["boundary"] != expected_boundary for item in backends):
                raise CollaborationError(
                    f"registered backend boundary differs from the {mode.value} profile"
                )
            configured_backends = [item for item in backends if item["configured"]]
            boundary_closed = bool(configured_backends) and all(
                item["standalone_verifier_boundary_closed"]
                for item in configured_backends
            )
            expected_ceiling = "bmp-gated" if boundary_closed else "exploratory"
            if profile["evidence_ceiling"] != expected_ceiling:
                raise CollaborationError(
                    f"execution profile {mode.value} evidence ceiling drift: "
                    f"expected {expected_ceiling}, observed {profile['evidence_ceiling']}"
                )
            lab_issue = profile["lab_issue"]
            if not boundary_closed and lab_issue is None:
                raise CollaborationError(
                    f"open-boundary execution profile {mode.value} requires a lab_issue"
                )
            if lab_issue is not None and lab_issue not in lab_states:
                raise CollaborationError(
                    f"execution profile {mode.value} references missing lab issue {lab_issue!r}"
                )
            if lab_issue is not None and lab_issue in linked_issues:
                raise CollaborationError(
                    f"execution profile lab issue {lab_issue!r} is linked by multiple modes"
                )
            if lab_issue is not None:
                linked_issues.add(lab_issue)
                if "execution" not in lab_states[lab_issue].issue.labels:
                    raise CollaborationError(
                        f"execution profile {mode.value} lab issue {lab_issue!r} "
                        "lacks the execution label"
                    )
            if (
                not boundary_closed
                and lab_issue is not None
                and lab_states[lab_issue].status in {LabStatus.done, LabStatus.cancelled}
            ):
                raise CollaborationError(
                    f"open-boundary execution profile {mode.value} links terminal lab issue "
                    f"{lab_issue!r}"
                )
            results.append(
                {
                    "backends": backends,
                    "configured": bool(configured_backends),
                    "declared_evidence_ceiling": profile["evidence_ceiling"],
                    "isolation_boundary": profile["isolation_boundary"],
                    "lab_issue": lab_issue,
                    "lab_status": (
                        None if lab_issue is None else lab_states[lab_issue].status.value
                    ),
                    "maximum_evidence_label": (
                        "claim-candidate" if boundary_closed else "exploratory"
                    ),
                    "mode": mode.value,
                    "network_policy": profile["network_policy"],
                    "profile": profile_path.relative_to(self.root).as_posix(),
                    "standalone_verifier_boundary_closed": boundary_closed,
                    "workspace_lifecycle": profile["workspace_lifecycle"],
                }
            )
        return tuple(results)

    def _execution_profiles(
        self,
    ) -> dict[ExecutionMode, tuple[Path, Mapping[str, Any]]]:
        root = self.root / "execution-profiles"
        if root.is_symlink() or not root.is_dir():
            raise CollaborationError("execution-profiles must be a regular directory")
        schema_path = self._project_file(
            "execution-profiles/schema.json", label="execution profile schema"
        )
        schema = _json_without_duplicate_keys(schema_path.read_bytes(), path=schema_path)
        if not isinstance(schema, dict):
            raise CollaborationError("execution profile schema must be a JSON object")
        try:
            Draft202012Validator.check_schema(schema)
        except JsonSchemaSchemaError as exc:
            raise CollaborationError(f"invalid execution profile schema: {exc.message}") from exc
        validator = Draft202012Validator(schema)

        expected_names = {mode.value for mode in ExecutionMode}
        for entry in root.iterdir():
            if entry.name in {"README.md", "schema.json"}:
                if entry.is_symlink() or not entry.is_file():
                    raise CollaborationError(
                        f"execution profile metadata is non-regular: {entry}"
                    )
                continue
            if (
                entry.is_symlink()
                or not entry.is_dir()
                or entry.name not in expected_names
            ):
                raise CollaborationError(f"unknown execution profile entry: {entry}")

        profiles: dict[ExecutionMode, tuple[Path, Mapping[str, Any]]] = {}
        for mode in ExecutionMode:
            relative = f"execution-profiles/{mode.value}/profile.json"
            path = self._project_file(relative, label=f"{mode.value} execution profile")
            profile = _json_without_duplicate_keys(path.read_bytes(), path=path)
            if not isinstance(profile, dict):
                raise CollaborationError(
                    f"execution profile must be a JSON object: {relative}"
                )
            _reject_secret_material(profile, path=relative)
            failures = sorted(
                validator.iter_errors(profile),
                key=lambda item: tuple(str(part) for part in item.absolute_path),
            )
            if failures:
                failure = failures[0]
                location = (
                    ".".join(str(part) for part in failure.absolute_path) or "<root>"
                )
                raise CollaborationError(
                    f"invalid execution profile {relative} at {location}: {failure.message}"
                )
            if profile["mode"] != mode.value:
                raise CollaborationError(
                    f"execution profile mode {profile['mode']!r} must match "
                    f"directory {mode.value!r}"
                )
            profiles[mode] = (path, profile)
        return profiles

    @staticmethod
    def _verifier_boundary_closed(mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.local_process, ExecutionMode.docker}

    @staticmethod
    def _backend_verifier_boundary_closed(backend: Mapping[str, Any]) -> bool:
        return backend.get("adapter") in {
            "fake",
            "subprocess",
            "harbor-shim",
            "aose-docker",
            "harbor",
        }

    @staticmethod
    def _backend_mode(backend: Mapping[str, Any]) -> tuple[ExecutionMode, str]:
        adapter = backend.get("adapter")
        defaults = backend.get("defaults")
        defaults = defaults if isinstance(defaults, dict) else {}
        if adapter in {"fake", "subprocess", "harbor-shim"}:
            return ExecutionMode.local_process, "process"
        if adapter in {"aose-docker", "harbor"}:
            return ExecutionMode.docker, "task-container"
        if adapter == "apptainer":
            return ExecutionMode.apptainer, "task-container"
        if adapter == "appcontainer":
            return ExecutionMode.appcontainer, "task-container"
        if adapter == "e2b":
            return ExecutionMode.e2b, "microvm"
        if defaults.get("environment_type") == "apptainer":
            return ExecutionMode.apptainer, "task-container"
        if defaults.get("environment_type") == "docker":
            return ExecutionMode.docker, "task-container"
        return ExecutionMode.remote_sandbox, "microvm"

    @staticmethod
    def _command_contract(bundle: ExperimentBundle) -> None:
        spec = bundle.bmp_spec
        preflight = bundle.execution.preflight_argv
        run = bundle.execution.run_argv
        verify = bundle.evidence.verifier_argv
        if preflight != ("bash", "scripts/preflight_experiment.sh", spec):
            raise CollaborationError(
                "preflight_argv must call scripts/preflight_experiment.sh with the pinned bmp_spec"
            )
        if "bmp-run" not in run or run.count(spec) != 1 or run.count("{record_root}") != 1:
            raise CollaborationError(
                "run_argv must name bmp-run, the pinned bmp_spec, and {record_root} exactly once"
            )
        if "--record-root" not in run:
            raise CollaborationError("run_argv must pass an explicit --record-root")
        if "bmp-verify-report" not in verify or verify.count("{report}") != 1:
            raise CollaborationError(
                "verifier_argv must name bmp-verify-report and {report} exactly once"
            )

    def _validate_bundle(
        self,
        path: Path,
        bundle: ExperimentBundle,
        *,
        protocols: Mapping[str, tuple[Path, Mapping[str, Any]]],
        backends: Mapping[str, tuple[Path, Mapping[str, Any]]],
        lab: LabStore,
        states: Mapping[str, Any],
        at: datetime,
    ) -> BundleSummary:
        if not bundle.bmp_spec.startswith("MagentaBench/conformance/experiments/"):
            raise CollaborationError(
                "bmp_spec must stay in MagentaBench/conformance/experiments; "
                "the collaboration bundle must not replace the BMP declaration"
            )
        spec_path = self._project_file(bundle.bmp_spec, label="bmp_spec")
        content = spec_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != bundle.bmp_spec_sha256:
            raise CollaborationError(
                f"bmp_spec digest drift: expected {bundle.bmp_spec_sha256}, observed {digest}"
            )
        document = _toml(spec_path)
        experiment = document.get("experiment")
        if not isinstance(experiment, dict):
            raise CollaborationError("bmp_spec has no [experiment] table")
        if experiment.get("id") != bundle.id:
            raise CollaborationError(
                f"bundle id differs from BMP experiment id: {bundle.id!r} != {experiment.get('id')!r}"
            )
        if experiment.get("protocol") != bundle.protocol_id:
            raise CollaborationError("bundle protocol_id differs from the BMP declaration")
        design = experiment.get("design")
        purpose = design.get("purpose") if isinstance(design, dict) else None
        if purpose != bundle.purpose.value:
            raise CollaborationError("bundle purpose differs from [experiment.design].purpose")
        metrics = experiment.get("metrics")
        if not isinstance(metrics, list) or not set(bundle.design.primary_metrics).issubset(metrics):
            raise CollaborationError("bundle primary_metrics are not selected by the BMP declaration")
        if bundle.protocol_id not in protocols:
            raise CollaborationError(f"bundle references unknown protocol: {bundle.protocol_id}")
        _, protocol = protocols[bundle.protocol_id]
        execution = document.get("execution")
        backend_id = execution.get("backend") if isinstance(execution, dict) else None
        if backend_id != bundle.execution.backend_id:
            raise CollaborationError("bundle backend_id differs from [execution].backend")
        if bundle.execution.backend_id not in backends:
            raise CollaborationError(f"bundle references unknown backend: {bundle.execution.backend_id}")
        _, backend = backends[bundle.execution.backend_id]
        expected_mode, expected_boundary = self._backend_mode(backend)
        _, mode_profile = self._execution_profiles()[expected_mode]
        if backend.get("adapter") not in self._backend_factory_adapters():
            raise CollaborationError(
                f"backend adapter {backend.get('adapter')!r} lacks a validated backend_factory capability"
            )
        if bundle.execution.mode != expected_mode:
            raise CollaborationError(
                f"bundle execution mode {bundle.execution.mode.value!r} differs from "
                f"registered backend mode {expected_mode.value!r}"
            )
        if bundle.execution.isolation_boundary != expected_boundary:
            raise CollaborationError(
                "bundle isolation_boundary differs from the registered backend class"
            )
        if bundle.execution.workspace_lifecycle != mode_profile["workspace_lifecycle"]:
            raise CollaborationError(
                "bundle workspace_lifecycle differs from the selected execution profile"
            )
        if bundle.execution.network_policy != mode_profile["network_policy"]:
            raise CollaborationError(
                "bundle network_policy differs from the selected execution profile"
            )
        if (
            bundle.purpose == BundlePurpose.claim
            and not self._backend_verifier_boundary_closed(backend)
        ):
            raise CollaborationError(
                "claim bundles require a closed standalone-verifier boundary; "
                f"{expected_mode.value} remains exploratory"
            )
        repetitions = protocol.get("rollouts_per_case")
        if repetitions != bundle.design.repetitions_per_case:
            raise CollaborationError(
                "bundle repetitions_per_case differs from the registered protocol"
            )
        case_order = protocol.get("case_order")
        explicit_cases = tuple(protocol.get("explicit_case_ids", ()))
        if case_order == "explicit":
            if explicit_cases != bundle.design.planned_case_ids:
                raise CollaborationError(
                    "bundle planned_case_ids differ from the explicit registered protocol order"
                )
        elif bundle.design.planned_case_ids:
            raise CollaborationError(
                "planned_case_ids must be empty when the protocol does not use explicit order"
            )
        self._command_contract(bundle)
        if "record_index.json" not in bundle.evidence.required_files:
            raise CollaborationError("required_files must retain record_index.json")
        report_name = (
            "observation_report.json"
            if bundle.purpose == BundlePurpose.exploratory
            else "claim_report.json"
        )
        if report_name not in bundle.evidence.required_files:
            raise CollaborationError(f"required_files must retain {report_name}")
        plan = path.parent / "PLAN.md"
        if plan.is_symlink() or not plan.is_file() or not plan.read_text(encoding="utf-8").strip():
            raise CollaborationError(f"bundle requires a non-empty regular PLAN.md: {plan}")
        _reject_secret_material(plan.read_text(encoding="utf-8"), path=f"{bundle.id}.PLAN.md")
        try:
            state = lab.load(bundle.lab_issue)
        except LabError as exc:
            raise CollaborationError(f"cannot load primary lab issue {bundle.lab_issue}: {exc}") from exc
        if state.issue.experiment != bundle.bmp_spec:
            raise CollaborationError(
                f"primary lab issue {bundle.lab_issue} is not bound to {bundle.bmp_spec}"
            )
        related = set(bundle.related_issues)
        if related != set(state.issue.dependencies):
            raise CollaborationError(
                "related_issues must exactly match the primary lab issue dependencies"
            )
        missing_related = sorted(related - set(states))
        if missing_related:
            raise CollaborationError(
                "bundle references missing related lab issues: " + ", ".join(missing_related)
            )
        dependencies_complete = all(states[item].status == LabStatus.done for item in related)
        active = state.active_lease(at)
        available = (
            state.status in {LabStatus.open, LabStatus.planned, LabStatus.ready}
            and not state.blockers
            and dependencies_complete
            and active is None
        )
        return BundleSummary(
            id=bundle.id,
            purpose=bundle.purpose.value,
            bmp_spec=bundle.bmp_spec,
            protocol_id=bundle.protocol_id,
            lab_issue=bundle.lab_issue,
            lab_status=state.status.value,
            lease_holder=None if active is None else active.owner,
            blocker_count=len(state.blockers),
            dependencies_complete=dependencies_complete,
            available=available,
        )

    def validate(self, *, at: datetime | None = None) -> ValidationReport:
        errors: list[Finding] = []
        warnings: list[Finding] = []
        summaries: list[BundleSummary] = []
        evaluated_at = at or utc_now()
        try:
            paths = self.bundle_paths()
        except CollaborationError as exc:
            return ValidationReport(errors=(Finding("layout", str(exc)),))
        if not paths:
            errors.append(Finding("no-bundles", "repository contains no experiment bundles"))
        try:
            protocols = self._protocols()
        except CollaborationError as exc:
            return ValidationReport(errors=(Finding("protocol-registry", str(exc)),))
        try:
            backends = self._backends()
        except CollaborationError as exc:
            return ValidationReport(errors=(Finding("backend-registry", str(exc)),))
        try:
            self.execution_modes()
        except CollaborationError as exc:
            errors.append(Finding("execution-profiles", str(exc), "execution-profiles"))
        try:
            lab = LabStore(self.root)
            doctor = lab.doctor(at=evaluated_at)
            if not doctor["ok"]:
                for message in doctor["errors"]:
                    errors.append(Finding("lab-doctor", message, "lab"))
            for message in doctor["warnings"]:
                warnings.append(Finding("lab-doctor", message, "lab"))
            states = {state.issue.issue_id: state for state in lab.list()}
        except LabError as exc:
            return ValidationReport(errors=(Finding("lab-ledger", str(exc), "lab"),))
        seen_specs: dict[str, str] = {}
        seen_issues: dict[str, str] = {}
        for path in paths:
            relative = path.relative_to(self.root).as_posix()
            try:
                bundle = self.load_bundle(path)
                previous = seen_specs.get(bundle.bmp_spec)
                if previous is not None:
                    raise CollaborationError(
                        f"bmp_spec is already owned by bundle {previous!r}"
                    )
                seen_specs[bundle.bmp_spec] = bundle.id
                previous_issue = seen_issues.get(bundle.lab_issue)
                if previous_issue is not None:
                    raise CollaborationError(
                        f"primary lab issue is already owned by bundle {previous_issue!r}"
                    )
                seen_issues[bundle.lab_issue] = bundle.id
                summaries.append(
                    self._validate_bundle(
                        path,
                        bundle,
                        protocols=protocols,
                        backends=backends,
                        lab=lab,
                        states=states,
                        at=evaluated_at,
                    )
                )
            except (CollaborationError, UnicodeDecodeError) as exc:
                errors.append(Finding("bundle-invalid", str(exc), relative))
        return ValidationReport(
            bundles=tuple(sorted(summaries, key=lambda item: item.id)),
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    def authorize_preflight(
        self,
        experiment_id: str,
        *,
        actor: str,
        environment: Mapping[str, str] | None = None,
        at: datetime | None = None,
    ) -> ExperimentBundle:
        """Validate one bundle and authorize its lease-bound preflight."""

        evaluated_at = at or utc_now()
        report = self.validate(at=evaluated_at)
        if not report.ok:
            finding = report.errors[0]
            raise CollaborationError(f"repository validation failed: {finding.message}")
        matches = [item for item in report.bundles if item.id == experiment_id]
        if len(matches) != 1:
            raise CollaborationError(f"unknown experiment bundle: {experiment_id}")
        bundle_path = self.experiments_root / experiment_id / "bundle.json"
        bundle = self.load_bundle(bundle_path)
        try:
            state = LabStore(self.root).load(bundle.lab_issue)
        except LabError as exc:
            raise CollaborationError(
                f"cannot load primary lab issue {bundle.lab_issue}: {exc}"
            ) from exc
        _authorize_preflight(
            bundle,
            matches[0],
            state,
            actor=actor,
            environment=os.environ if environment is None else environment,
            at=evaluated_at,
        )
        return bundle

    def scaffold(
        self,
        *,
        experiment_id: str,
        bmp_spec: str,
        lab_issue: str,
        question: str,
        hypothesis: str,
        stop_conditions: tuple[str, ...],
        related_issues: tuple[str, ...] = (),
        required_env: tuple[str, ...] = (),
        primary_metrics: tuple[str, ...] = (),
    ) -> tuple[Path, bool]:
        """Create one isolated bundle without changing any BMP declaration."""

        normalized_spec = portable_path(bmp_spec, label="bmp_spec")
        spec_path = self._project_file(normalized_spec, label="bmp_spec")
        document = _toml(spec_path)
        experiment = document.get("experiment")
        if not isinstance(experiment, dict) or experiment.get("id") != experiment_id:
            raise CollaborationError("experiment_id must match the BMP declaration")
        protocol_id = experiment.get("protocol")
        design = experiment.get("design")
        purpose = design.get("purpose") if isinstance(design, dict) else None
        metrics = experiment.get("metrics")
        if not isinstance(protocol_id, str) or purpose not in {"exploratory", "claim"}:
            raise CollaborationError("BMP declaration lacks protocol or supported purpose")
        if not isinstance(metrics, list) or not metrics:
            raise CollaborationError("BMP declaration has no metrics")
        try:
            lab_state = LabStore(self.root).load(lab_issue)
        except LabError as exc:
            raise CollaborationError(f"cannot load lab issue {lab_issue}: {exc}") from exc
        if lab_state.issue.experiment != normalized_spec:
            raise CollaborationError(
                f"lab issue {lab_issue!r} is not bound to {normalized_spec!r}"
            )
        if set(related_issues) != set(lab_state.issue.dependencies):
            raise CollaborationError(
                "related issues must exactly match the primary lab issue dependencies"
            )
        if not set(primary_metrics or (str(metrics[0]),)).issubset(metrics):
            raise CollaborationError("primary metrics must be selected by the BMP declaration")
        protocols = self._protocols()
        if protocol_id not in protocols:
            raise CollaborationError(f"unknown protocol: {protocol_id}")
        _, protocol = protocols[protocol_id]
        execution_document = document.get("execution")
        backend_id = (
            execution_document.get("backend")
            if isinstance(execution_document, dict)
            else None
        )
        if not isinstance(backend_id, str):
            raise CollaborationError("BMP declaration has no execution backend")
        backends = self._backends()
        if backend_id not in backends:
            raise CollaborationError(f"unknown backend: {backend_id}")
        _, backend = backends[backend_id]
        execution_mode, isolation_boundary = self._backend_mode(backend)
        workspace_lifecycle = "persist-on-failure"
        network_policy = "benchmark-defined"
        if (self.root / "execution-profiles").is_dir():
            mode_inventory = {
                item["mode"]: item for item in self.execution_modes()
            }
            selected_mode = mode_inventory[execution_mode.value]
            selected_backend = next(
                (
                    item
                    for item in selected_mode["backends"]
                    if item["backend_id"] == backend_id
                ),
                None,
            )
            if selected_backend is None or not selected_backend["configured"]:
                raise CollaborationError(
                    f"backend {backend_id!r} is registered but has no usable backend_factory"
                )
            workspace_lifecycle = selected_mode["workspace_lifecycle"]
            network_policy = selected_mode["network_policy"]
        selected_metrics = primary_metrics or (str(metrics[0]),)
        bundle = ExperimentBundle(
            format=BUNDLE_FORMAT,
            id=experiment_id,
            bmp_spec=normalized_spec,
            bmp_spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            protocol_id=protocol_id,
            purpose=BundlePurpose(purpose),
            lab_issue=lab_issue,
            related_issues=related_issues,
            summary=question,
            design=BundleDesign(
                question=question,
                hypothesis=hypothesis,
                primary_metrics=selected_metrics,
                planned_case_ids=tuple(protocol.get("explicit_case_ids", ())),
                repetitions_per_case=int(protocol.get("rollouts_per_case", 1)),
                seeds=(),
                stop_conditions=stop_conditions,
            ),
            execution=BundleExecution(
                mode=execution_mode,
                backend_id=backend_id,
                isolation_boundary=isolation_boundary,
                workspace_lifecycle=workspace_lifecycle,
                network_policy=network_policy,
                artifact_export_required=True,
                preflight_argv=("bash", "scripts/preflight_experiment.sh", normalized_spec),
                run_argv=(
                    "uv",
                    "run",
                    "bmp-run",
                    normalized_spec,
                    "--project-root",
                    ".",
                    "--record-root",
                    "{record_root}",
                ),
                required_env=required_env,
                record_root_template=f"{{artifact_root}}/{experiment_id}/{{run_id}}",
            ),
            evidence=BundleEvidence(
                classification=("exploratory" if purpose == "exploratory" else "claim-candidate"),
                required_files=(
                    "record_index.json",
                    "observation_report.json" if purpose == "exploratory" else "claim_report.json",
                ),
                verifier_argv=("uv", "run", "bmp-verify-report", "{report}"),
                retention_policy=(
                    "Retain every terminal attempt state and all indexed bytes in a durable, "
                    "access-controlled artifact root; never use .runs as the sole copy."
                ),
            ),
        )
        destination = self.experiments_root / experiment_id
        bundle_path = destination / "bundle.json"
        plan_path = destination / "PLAN.md"
        bundle_bytes = _canonical_bundle_bytes(bundle)
        plan_text = (
            f"# {experiment_id}\n\n"
            f"## Question\n\n{question}\n\n"
            f"## Hypothesis\n\n{hypothesis}\n\n"
            "## Stop Conditions\n\n"
            + "".join(f"- {condition}\n" for condition in stop_conditions)
            + "\n## Review\n\n"
            "Record live ownership and progress in the linked lab issue. Retain outputs as "
            f"{bundle.evidence.classification}; this plan does not change BMP.\n"
        )
        if destination.exists():
            if (
                bundle_path.is_file()
                and plan_path.is_file()
                and bundle_path.read_bytes() == bundle_bytes
                and plan_path.read_text(encoding="utf-8") == plan_text
            ):
                return bundle_path, False
            raise CollaborationError(f"experiment bundle already exists with other content: {destination}")
        try:
            self.experiments_root.mkdir(parents=True, exist_ok=True)
            destination.mkdir(parents=False)
            with bundle_path.open("xb") as handle:
                handle.write(bundle_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            with plan_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(plan_text)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            for path in (plan_path, bundle_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
        return bundle_path, True
