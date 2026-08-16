"""Offline discovery and validation for historical benchmark imports."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import TypeAdapter, ValidationError

from .import_models import (
    HistoricalAssetRecord,
    HistoricalDeclaration,
    HistoricalRecord,
    HistoricalRecordBase,
    HistoricalRun,
    HistoricalSource,
    canonical_repository_name,
    logical_key_digest,
    record_natural_identity,
    source_document_digest,
    source_snapshot_identity,
)
from .repository import CollaborationError

_RECORD_ADAPTER = TypeAdapter(HistoricalRecord)
_MAX_IMPORT_FILE_BYTES = 4 * 1024 * 1024
_MAX_SUPERSESSION_RECORDS = 10_000
_MAX_SUPERSESSION_EDGES = 100_000
_CHECKED_IN_PUBLICATION_DESTINATION = "minions-land/magentabenchmark"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_DIAGNOSTIC_PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_key|access_key|private_key|auth_token|access_token|"
    r"refresh_token|id_token|token|secret|password|credentials?)(?:_|$)",
    re.IGNORECASE,
)
_NON_SECRET_TOKEN_KEY_PATTERN = re.compile(
    r"^(?:(?:cache|completion|context|generation|input|max|max_context|"
    r"max_generation|output|prompt|request|response|retry|total)_)?tokens$|"
    r"^token_(?:budget|capacity|count|limit|quota|window)$|"
    r"^(?:(?:answer|diagnostic|evaluation|metric|prediction|retrieval)_)?"
    r"token_(?:accuracy|f1|precision|recall|score)$",
    re.IGNORECASE,
)
_NON_SECRET_KEY_NAMES = frozenset({"logical_key"})
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{4,}"
    ),
)
_CREDENTIAL_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "auth_token",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }
)
_FORBIDDEN_RAW_KEYS = frozenset(
    {
        "answer",
        "argv",
        "command",
        "commands",
        "environment",
        "metadata",
        "prompt",
        "raw",
        "raw_metadata",
        "script",
        "shell",
        "stderr",
        "stdout",
    }
)
_CREDENTIAL_QUERY_KEYS = _CREDENTIAL_KEYS | frozenset({"sig", "signature"})


class _DocumentError(ValueError):
    pass


@dataclass(frozen=True)
class HistoricalImportFinding:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True)
class LoadedHistoricalSource:
    source: HistoricalSource
    path: str
    snapshot_identity: str
    source_digest: str


@dataclass(frozen=True)
class LoadedHistoricalRecord:
    record: HistoricalRecordBase
    path: str
    logical_key_sha256: str


@dataclass(frozen=True)
class HistoricalImportSnapshot:
    sources: tuple[LoadedHistoricalSource, ...] = ()
    records: tuple[LoadedHistoricalRecord, ...] = ()

    def records_of_kind(self, kind: str) -> tuple[LoadedHistoricalRecord, ...]:
        return tuple(item for item in self.records if item.record.kind == kind)


@dataclass(frozen=True)
class HistoricalImportValidation:
    snapshot: HistoricalImportSnapshot
    errors: tuple[HistoricalImportFinding, ...] = ()
    warnings: tuple[HistoricalImportFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "error_count": len(self.errors),
            "errors": [finding.as_dict() for finding in self.errors],
            "format": "magentabench-historical-import-validation-v1",
            "ok": self.ok,
            "record_count": len(self.snapshot.records),
            "source_count": len(self.snapshot.sources),
            "warnings": [finding.as_dict() for finding in self.warnings],
        }


class HistoricalImportError(CollaborationError):
    """Historical imports failed closed before they could enter a ledger."""

    def __init__(self, errors: tuple[HistoricalImportFinding, ...]):
        self.errors = errors
        detail = "historical import validation failed"
        if errors:
            first = errors[0]
            location = "" if first.path is None else f" at {first.path}"
            detail = f"{detail} ({len(errors)} error(s)); {first.code}{location}: {first.message}"
        super().__init__(detail)


def _display_path(path: Path, project_root: Path, imports_root: Path) -> str:
    path = Path(os.path.abspath(os.fspath(path)))
    project_root = Path(os.path.abspath(os.fspath(project_root)))
    imports_root = Path(os.path.abspath(os.fspath(imports_root)))
    try:
        display = path.relative_to(project_root).as_posix()
    except ValueError:
        try:
            suffix = path.relative_to(imports_root).as_posix()
        except ValueError:
            return "<external-imports>"
        display = (
            "<external-imports>" if suffix == "." else f"<external-imports>/{suffix}"
        )
    return "/".join(_diagnostic_part(part) for part in display.split("/"))


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _DocumentError("file is missing, unreadable, or a symlink") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise _DocumentError("path must be a regular file")
        if details.st_size > _MAX_IMPORT_FILE_BYTES:
            raise _DocumentError(
                f"file exceeds the {_MAX_IMPORT_FILE_BYTES}-byte import limit"
            )
        chunks: list[bytes] = []
        remaining = _MAX_IMPORT_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _MAX_IMPORT_FILE_BYTES:
            raise _DocumentError(
                f"file exceeds the {_MAX_IMPORT_FILE_BYTES}-byte import limit"
            )
        return content
    finally:
        os.close(descriptor)


def _json_without_duplicate_keys(content: bytes) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DocumentError("duplicate JSON key is forbidden")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise _DocumentError(f"non-finite JSON number {value} is forbidden")

    try:
        return json.loads(
            content.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except _DocumentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _DocumentError(f"cannot parse UTF-8 JSON: {exc}") from exc


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _contains_secret_material(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _is_secret_like_key(value: str) -> bool:
    normalized = _normalized_key(value)
    if normalized in _NON_SECRET_KEY_NAMES:
        return False
    if normalized in _CREDENTIAL_KEYS:
        return True
    return bool(_SECRET_KEY_PATTERN.search(normalized)) and not (
        "token" in normalized
        and _NON_SECRET_TOKEN_KEY_PATTERN.fullmatch(normalized) is not None
    )


def _diagnostic_part(value: str) -> str:
    if value == "<external-imports>":
        return value
    if (
        _SAFE_DIAGNOSTIC_PART_RE.fullmatch(value) is None
        or _contains_secret_material(value)
        or _is_secret_like_key(value)
    ):
        return "<redacted-field>"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-field>"
    if (
        parsed.scheme
        or parsed.username is not None
        or parsed.password is not None
        or {
            key.casefold().replace("-", "_")
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        & _CREDENTIAL_QUERY_KEYS
    ):
        return "<redacted-field>"
    return value


def _diagnostic_location(location: str, key: str) -> str:
    return f"{location}.{_diagnostic_part(key)}"


def _inspect_untrusted_json(value: Any, *, location: str = "document") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = _diagnostic_location(location, key)
            if _contains_secret_material(key) or _is_secret_like_key(key):
                raise _DocumentError(
                    f"secret material is forbidden at {child_location}"
                )
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_RAW_KEYS:
                raise _DocumentError(
                    f"commands or raw metadata are forbidden at {child_location}"
                )
            _inspect_untrusted_json(child, location=child_location)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _inspect_untrusted_json(child, location=f"{location}[{index}]")
        return
    if not isinstance(value, str):
        return
    if _contains_secret_material(value):
        raise _DocumentError(f"secret material is forbidden at {location}")
    if value.startswith(("/", "\\", "~/")) or re.match(r"^[A-Za-z]:[\\/]", value):
        raise _DocumentError(f"absolute or host path is forbidden at {location}")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _DocumentError(
            f"malformed URL-like value is forbidden at {location}"
        ) from exc
    if parsed.scheme:
        if parsed.username is not None or parsed.password is not None:
            raise _DocumentError(f"authenticated URL is forbidden at {location}")
        query_keys = {
            key.casefold().replace("-", "_")
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        if query_keys & _CREDENTIAL_QUERY_KEYS:
            raise _DocumentError(f"URL credentials are forbidden at {location}")


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False, include_url=False)
    if not errors:
        return "document does not match the historical import schema"
    first = errors[0]
    location = (
        ".".join(
            str(part) if isinstance(part, int) else _diagnostic_part(str(part))
            for part in first.get("loc", ())
        )
        or "document"
    )
    message = str(first.get("msg", "invalid value"))
    if _contains_secret_material(message):
        message = "invalid value"
    return f"{location}: {message}"


def _parse_source(content: bytes) -> HistoricalSource:
    raw = _json_without_duplicate_keys(content)
    if not isinstance(raw, dict):
        raise _DocumentError("source document must be a JSON object")
    _inspect_untrusted_json(raw)
    try:
        return HistoricalSource.model_validate_json(content, strict=True)
    except ValidationError as exc:
        raise _DocumentError(_validation_message(exc)) from exc


def _parse_record(content: bytes) -> HistoricalRecordBase:
    raw = _json_without_duplicate_keys(content)
    if not isinstance(raw, dict):
        raise _DocumentError("record document must be a JSON object")
    _inspect_untrusted_json(raw)
    try:
        record = _RECORD_ADAPTER.validate_json(content, strict=True)
    except ValidationError as exc:
        raise _DocumentError(_validation_message(exc)) from exc
    if not isinstance(
        record, HistoricalRecordBase
    ):  # pragma: no cover - union contract
        raise _DocumentError("record document has an unsupported kind")
    return record


def _finding(
    errors: list[HistoricalImportFinding],
    code: str,
    message: str,
    *,
    path: Path | None,
    project_root: Path,
    imports_root: Path,
) -> None:
    errors.append(
        HistoricalImportFinding(
            code=code,
            message=message,
            path=(
                None
                if path is None
                else _display_path(path, project_root, imports_root)
            ),
        )
    )


def _validate_supersession_graph(
    records: list[LoadedHistoricalRecord],
    errors: list[HistoricalImportFinding],
) -> None:
    edge_count = sum(len(item.record.supersedes) for item in records)
    if len(records) > _MAX_SUPERSESSION_RECORDS or edge_count > _MAX_SUPERSESSION_EDGES:
        errors.append(
            HistoricalImportFinding(
                code="supersession-limit",
                message=(
                    "historical import exceeds the reviewed supersession graph limits"
                ),
                path=min((item.path for item in records), default=None),
            )
        )
        return

    by_id = {item.record.record_id: item for item in records}
    edges: dict[str, tuple[str, ...]] = {}

    def graph_finding(code: str, message: str, item: LoadedHistoricalRecord) -> None:
        errors.append(
            HistoricalImportFinding(code=code, message=message, path=item.path)
        )

    for item in records:
        record = item.record
        valid_targets: list[str] = []
        for target_id in record.supersedes:
            target = by_id.get(target_id)
            if target is None:
                graph_finding(
                    "supersedes-missing",
                    "supersedes references an unknown record id",
                    item,
                )
                continue
            if target.logical_key_sha256 != item.logical_key_sha256:
                graph_finding(
                    "supersedes-incompatible",
                    "supersedes must stay within one compatible kind and logical key",
                    item,
                )
                continue
            valid_targets.append(target_id)
        edges[record.record_id] = tuple(sorted(valid_targets))

    state: dict[str, int] = {}
    cycle_nodes: set[str] = set()

    for record_id in sorted(edges):
        if state.get(record_id, 0) != 0:
            continue
        state[record_id] = 1
        stack: list[tuple[str, int]] = [(record_id, 0)]
        positions = {record_id: 0}
        while stack:
            current, next_index = stack[-1]
            targets = edges.get(current, ())
            if next_index >= len(targets):
                state[current] = 2
                positions.pop(current, None)
                stack.pop()
                continue
            target_id = targets[next_index]
            stack[-1] = (current, next_index + 1)
            target_state = state.get(target_id, 0)
            if target_state == 0:
                state[target_id] = 1
                positions[target_id] = len(stack)
                stack.append((target_id, 0))
            elif target_state == 1:
                cycle_nodes.update(node for node, _ in stack[positions[target_id] :])
    if cycle_nodes:
        first = by_id[min(cycle_nodes)]
        graph_finding(
            "supersession-cycle",
            "supersedes graph contains a cycle",
            first,
        )

    def validate_histories(
        groups: dict[Any, list[LoadedHistoricalRecord]],
        *,
        code: str,
        message: str,
    ) -> None:
        for group in groups.values():
            if len(group) < 2:
                continue
            ids = {item.record.record_id for item in group}
            superseded = {
                target_id
                for item in group
                for target_id in edges.get(item.record.record_id, ())
                if target_id in ids
            }
            heads = sorted(ids - superseded)
            reachable: set[str] = set()
            if len(heads) == 1:
                pending = [heads[0]]
                while pending:
                    current = pending.pop()
                    if current in reachable:
                        continue
                    reachable.add(current)
                    pending.extend(
                        target for target in edges.get(current, ()) if target in ids
                    )
            if len(heads) != 1 or reachable != ids or cycle_nodes & ids:
                first = min(group, key=lambda item: item.path)
                graph_finding(code, message, first)

    by_logical_key: dict[str, list[LoadedHistoricalRecord]] = {}
    for item in records:
        by_logical_key.setdefault(item.logical_key_sha256, []).append(item)
    validate_histories(
        by_logical_key,
        code="logical-conflict",
        message=(
            "records sharing a logical key require one explicit, connected, "
            "acyclic supersession history"
        ),
    )

    by_natural_identity: dict[tuple[str, ...], list[LoadedHistoricalRecord]] = {}
    for item in records:
        by_natural_identity.setdefault(record_natural_identity(item.record), []).append(
            item
        )
    validate_histories(
        by_natural_identity,
        code="natural-identity-conflict",
        message=(
            "records sharing a source-scoped natural identity require one "
            "explicit, connected, acyclic supersession history"
        ),
    )


def _validate_record_references(
    records: list[LoadedHistoricalRecord],
    errors: list[HistoricalImportFinding],
) -> None:
    runs_by_identity: dict[tuple[str, str, str], list[LoadedHistoricalRecord]] = {}
    runs_by_source_and_id: dict[tuple[str, str], list[LoadedHistoricalRecord]] = {}
    experiment_ids: set[tuple[str, str]] = set()
    for item in records:
        record = item.record
        if isinstance(record, (HistoricalDeclaration, HistoricalRun)):
            experiment_ids.add((record.source_id, record.experiment.experiment_id))
        if isinstance(record, HistoricalRun):
            runs_by_identity.setdefault(
                (
                    record.source_id,
                    record.experiment.experiment_id,
                    record.run_id,
                ),
                [],
            ).append(item)
            runs_by_source_and_id.setdefault(
                (record.source_id, record.run_id), []
            ).append(item)

    def reference_finding(
        code: str, message: str, item: LoadedHistoricalRecord
    ) -> None:
        errors.append(
            HistoricalImportFinding(code=code, message=message, path=item.path)
        )

    for item in records:
        record = item.record
        if isinstance(record, HistoricalRun) and record.parent_run_id is not None:
            if record.parent_run_id == record.run_id:
                reference_finding(
                    "parent-run-self-reference",
                    "parent_run_id must not reference the same run",
                    item,
                )
                continue
            parents = runs_by_identity.get(
                (
                    record.source_id,
                    record.experiment.experiment_id,
                    record.parent_run_id,
                ),
                [],
            )
            if not parents:
                reference_finding(
                    "parent-run-missing",
                    "parent_run_id references no run in the same source and experiment",
                    item,
                )
        if not isinstance(record, HistoricalAssetRecord):
            continue
        if (
            record.experiment_id is not None
            and (
                record.source_id,
                record.experiment_id,
            )
            not in experiment_ids
        ):
            reference_finding(
                "asset-experiment-missing",
                "asset experiment_id references no experiment in the same source snapshot",
                item,
            )
        if record.run_id is None:
            continue
        source_runs = runs_by_source_and_id.get((record.source_id, record.run_id), [])
        linked_runs = (
            source_runs
            if record.experiment_id is None
            else runs_by_identity.get(
                (record.source_id, record.experiment_id, record.run_id), []
            )
        )
        if not linked_runs:
            if record.experiment_id is not None and source_runs:
                reference_finding(
                    "asset-reference-mismatch",
                    "asset experiment_id and run_id resolve to different experiments",
                    item,
                )
            else:
                reference_finding(
                    "asset-run-missing",
                    "asset run_id references no run in the same source snapshot",
                    item,
                )
        elif (
            record.experiment_id is None
            and len(
                {
                    linked.record.experiment.experiment_id
                    for linked in linked_runs
                    if isinstance(linked.record, HistoricalRun)
                }
            )
            > 1
        ):
            reference_finding(
                "asset-run-ambiguous",
                "asset run_id resolves to more than one experiment",
                item,
            )


def validate_historical_imports(
    project_root: str | Path,
    *,
    imports_dir: str | Path | None = None,
) -> HistoricalImportValidation:
    """Scan every source directory and return deterministic validation facts."""

    root = Path(project_root).resolve()
    explicit_imports_root = imports_dir is not None
    configured = Path("imports") if imports_dir is None else Path(imports_dir)
    configured = configured.expanduser()
    candidate = configured if configured.is_absolute() else root / configured
    # Observe the configured object before canonicalizing intermediate aliases;
    # otherwise a direct symlink root would disappear from the validation view.
    configured_imports_root = Path(os.path.abspath(os.fspath(candidate)))
    errors: list[HistoricalImportFinding] = []
    sources: list[LoadedHistoricalSource] = []
    records: list[LoadedHistoricalRecord] = []

    if configured_imports_root.is_symlink():
        _finding(
            errors,
            "imports-root",
            "imports root must be a real directory, not a symlink",
            path=configured_imports_root,
            project_root=root,
            imports_root=configured_imports_root,
        )
        return HistoricalImportValidation(
            snapshot=HistoricalImportSnapshot(),
            errors=tuple(errors),
        )
    # Canonical spelling makes publication classification and projected paths
    # independent of aliases in parent directories.
    try:
        imports_root = configured_imports_root.resolve(strict=False)
    except (OSError, RuntimeError):
        _finding(
            errors,
            "imports-root",
            "imports root cannot be resolved safely",
            path=configured_imports_root,
            project_root=root,
            imports_root=configured_imports_root,
        )
        return HistoricalImportValidation(
            snapshot=HistoricalImportSnapshot(),
            errors=tuple(errors),
        )
    if not imports_root.exists():
        if explicit_imports_root:
            _finding(
                errors,
                "imports-root",
                "explicit imports root does not exist",
                path=imports_root,
                project_root=root,
                imports_root=imports_root,
            )
            return HistoricalImportValidation(
                snapshot=HistoricalImportSnapshot(),
                errors=tuple(errors),
            )
        return HistoricalImportValidation(snapshot=HistoricalImportSnapshot())
    if not imports_root.is_dir():
        _finding(
            errors,
            "imports-root",
            "imports root must be a real directory, not a symlink",
            path=imports_root,
            project_root=root,
            imports_root=imports_root,
        )
        return HistoricalImportValidation(
            snapshot=HistoricalImportSnapshot(),
            errors=tuple(errors),
        )

    try:
        source_entries = sorted(imports_root.iterdir(), key=lambda path: path.name)
    except OSError:
        source_entries = []
        _finding(
            errors,
            "imports-root",
            "imports root cannot be enumerated",
            path=imports_root,
            project_root=root,
            imports_root=imports_root,
        )
    source_ids: dict[str, LoadedHistoricalSource] = {}
    snapshot_ids: dict[str, LoadedHistoricalSource] = {}
    record_ids: dict[str, LoadedHistoricalRecord] = {}

    for source_dir in source_entries:
        if source_dir.name in {".gitkeep", "README.md"}:
            if source_dir.is_symlink() or not source_dir.is_file():
                _finding(
                    errors,
                    "source-layout",
                    "imports root documentation entries must be regular files",
                    path=source_dir,
                    project_root=root,
                    imports_root=imports_root,
                )
            continue
        if source_dir.is_symlink() or not source_dir.is_dir():
            _finding(
                errors,
                "source-layout",
                "every imports entry must be a real source directory",
                path=source_dir,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        try:
            children = sorted(source_dir.iterdir(), key=lambda path: path.name)
        except OSError:
            _finding(
                errors,
                "source-layout",
                "source directory cannot be enumerated",
                path=source_dir,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        for child in children:
            if child.name not in {"source.json", "records"}:
                _finding(
                    errors,
                    "source-layout",
                    "source directory may contain only source.json and records/",
                    path=child,
                    project_root=root,
                    imports_root=imports_root,
                )

        source_path = source_dir / "source.json"
        try:
            source_content = _read_regular_file(source_path)
            source = _parse_source(source_content)
        except _DocumentError as exc:
            _finding(
                errors,
                "source-json",
                str(exc),
                path=source_path,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        checked_in = imports_root == (root / "imports").resolve(strict=False)
        approved_private_projection = (
            source.visibility == "private"
            and source.publication_approval is not None
            and canonical_repository_name(
                source.publication_approval.destination_repository
            )
            == _CHECKED_IN_PUBLICATION_DESTINATION
        )
        normally_publishable = (
            source.visibility == "public"
            and source.license_status == "declared"
            and source.publication_approval is None
        )
        if checked_in and not (
            normally_publishable or approved_private_projection
        ):
            _finding(
                errors,
                "publication-approval",
                (
                    "checked-in imports require a public license-declared source "
                    "or an approved private typed-results projection for this "
                    "repository"
                ),
                path=source_path,
                project_root=root,
                imports_root=imports_root,
            )
        if source.source_id != source_dir.name:
            _finding(
                errors,
                "source-id-mismatch",
                "source_id must equal its containing directory name",
                path=source_path,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        snapshot_identity = source_snapshot_identity(source)
        loaded_source = LoadedHistoricalSource(
            source=source,
            path=_display_path(source_path, root, imports_root),
            snapshot_identity=snapshot_identity,
            source_digest=source_document_digest(source),
        )
        if source.source_id in source_ids:
            _finding(
                errors,
                "duplicate-source-id",
                "source_id appears more than once",
                path=source_path,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        source_ids[source.source_id] = loaded_source
        if snapshot_identity in snapshot_ids:
            _finding(
                errors,
                "duplicate-snapshot-identity",
                "another source_id binds the same repository snapshot and normalizer",
                path=source_path,
                project_root=root,
                imports_root=imports_root,
            )
        else:
            snapshot_ids[snapshot_identity] = loaded_source
        sources.append(loaded_source)

        records_dir = source_dir / "records"
        if records_dir.is_symlink():
            _finding(
                errors,
                "records-layout",
                "records must be a real directory, not a symlink",
                path=records_dir,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        if not records_dir.exists():
            continue
        if not records_dir.is_dir():
            _finding(
                errors,
                "records-layout",
                "records must be a real directory, not a symlink",
                path=records_dir,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        try:
            record_entries = sorted(records_dir.iterdir(), key=lambda path: path.name)
        except OSError:
            _finding(
                errors,
                "records-layout",
                "records directory cannot be enumerated",
                path=records_dir,
                project_root=root,
                imports_root=imports_root,
            )
            continue
        for record_path in record_entries:
            if (
                record_path.is_symlink()
                or not record_path.is_file()
                or record_path.suffix != ".json"
                or _SHA256_RE.fullmatch(record_path.stem) is None
            ):
                _finding(
                    errors,
                    "record-layout",
                    "record entries must be regular <record-id>.json files",
                    path=record_path,
                    project_root=root,
                    imports_root=imports_root,
                )
                continue
            try:
                record_content = _read_regular_file(record_path)
                record = _parse_record(record_content)
            except _DocumentError as exc:
                _finding(
                    errors,
                    "record-json",
                    str(exc),
                    path=record_path,
                    project_root=root,
                    imports_root=imports_root,
                )
                continue
            if record.record_id != record_path.stem:
                _finding(
                    errors,
                    "record-filename-mismatch",
                    "record filename must equal the canonical record_id",
                    path=record_path,
                    project_root=root,
                    imports_root=imports_root,
                )
                continue
            if record.source_id != source.source_id:
                _finding(
                    errors,
                    "record-source-mismatch",
                    "record source_id must equal its containing source snapshot",
                    path=record_path,
                    project_root=root,
                    imports_root=imports_root,
                )
                continue
            if record.source_snapshot_sha256 != snapshot_identity:
                _finding(
                    errors,
                    "record-snapshot-mismatch",
                    "record source_snapshot_sha256 differs from its containing source snapshot",
                    path=record_path,
                    project_root=root,
                    imports_root=imports_root,
                )
                continue
            if (
                checked_in
                and approved_private_projection
                and isinstance(record, HistoricalAssetRecord)
            ):
                _finding(
                    errors,
                    "publication-scope",
                    (
                        "approved private typed-results projections cannot "
                        "publish asset records"
                    ),
                    path=record_path,
                    project_root=root,
                    imports_root=imports_root,
                )
            loaded_record = LoadedHistoricalRecord(
                record=record,
                path=_display_path(record_path, root, imports_root),
                logical_key_sha256=logical_key_digest(record.kind, record.logical_key),
            )
            if record.record_id in record_ids:
                _finding(
                    errors,
                    "duplicate-record-id",
                    "record_id appears in more than one source snapshot",
                    path=record_path,
                    project_root=root,
                    imports_root=imports_root,
                )
                continue
            record_ids[record.record_id] = loaded_record
            records.append(loaded_record)

    _validate_supersession_graph(records, errors)
    _validate_record_references(records, errors)
    snapshot = HistoricalImportSnapshot(
        sources=tuple(
            sorted(
                sources,
                key=lambda item: (
                    item.source.source_id,
                    item.snapshot_identity,
                    item.path,
                ),
            )
        ),
        records=tuple(
            sorted(
                records,
                key=lambda item: (
                    item.record.source_id,
                    item.record.kind,
                    item.record.logical_key,
                    item.record.record_id,
                    item.path,
                ),
            )
        ),
    )
    ordered_errors = tuple(
        sorted(errors, key=lambda item: (item.path or "", item.code, item.message))
    )
    return HistoricalImportValidation(snapshot=snapshot, errors=ordered_errors)


def load_historical_imports(
    project_root: str | Path,
    *,
    imports_dir: str | Path | None = None,
) -> HistoricalImportSnapshot:
    """Load a valid immutable snapshot or raise a collaboration-style error."""

    validation = validate_historical_imports(project_root, imports_dir=imports_dir)
    if not validation.ok:
        raise HistoricalImportError(validation.errors)
    return validation.snapshot


__all__ = [
    "HistoricalImportError",
    "HistoricalImportFinding",
    "HistoricalImportSnapshot",
    "HistoricalImportValidation",
    "LoadedHistoricalRecord",
    "LoadedHistoricalSource",
    "load_historical_imports",
    "validate_historical_imports",
]
