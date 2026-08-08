"""Static source-closure discovery for project-provided BMP adapters.

Adapter declarations identify executable entrypoints, but an entrypoint can
delegate its behavior to local helper modules.  This module discovers the
local Python import closure without importing plugin code.  The compiler and
runtime registry use the same closure algorithm so a helper-byte mutation is
observable before execution.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath


class AdapterSourceError(ValueError):
    """A plugin source tree or static import closure is not admissible."""


def _assert_inside(path: Path, root: Path, *, label: str) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AdapterSourceError(f"{label} escapes adapter source root") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise AdapterSourceError(f"{label} is not a normalized relative path")
    return path


def _assert_no_symlink_components(path: Path, root: Path, *, label: str) -> None:
    """Reject symlinks in a source path, including the leaf itself."""

    relative = _assert_inside(path, root, label=label).relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AdapterSourceError(f"{label} contains symlink: {current}")


def resolve_source_root(project_root: str | Path, source: str) -> Path:
    """Resolve one project-relative adapter source directory safely."""

    root = Path(project_root).expanduser().resolve(strict=True)
    unresolved = root / source
    _assert_no_symlink_components(unresolved, root, label="adapter source")
    candidate = unresolved.resolve(strict=True)
    _assert_inside(candidate, root, label="adapter source")
    if not candidate.is_dir():
        raise AdapterSourceError(f"adapter source is not a directory: {candidate}")
    return candidate


def resolve_entrypoint(source_root: Path, entrypoint: str) -> Path:
    """Resolve ``module:object`` to a stable Python source file."""

    module_name, separator, _ = entrypoint.partition(":")
    if not separator:
        raise AdapterSourceError("adapter entrypoint must contain ':'")
    if module_name.endswith(".py"):
        relative = PurePosixPath(module_name)
    else:
        relative = PurePosixPath(module_name.replace(".", "/") + ".py")
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise AdapterSourceError("adapter entrypoint must be normalized below source root")
    unresolved = source_root / Path(*relative.parts)
    _assert_no_symlink_components(
        unresolved, source_root, label="adapter entrypoint"
    )
    candidate = unresolved.resolve(strict=True)
    _assert_inside(candidate, source_root, label="adapter entrypoint")
    if not candidate.is_file() or candidate.suffix != ".py":
        raise AdapterSourceError(f"adapter entrypoint is not a Python file: {candidate}")
    return candidate


def _module_file(source_root: Path, module_parts: tuple[str, ...]) -> Path | None:
    if not module_parts or any(
        not part or part in {".", ".."} for part in module_parts
    ):
        return None
    relative = Path(*module_parts)
    candidates = (source_root / relative.with_suffix(".py"), source_root / relative / "__init__.py")
    for candidate in candidates:
        _assert_no_symlink_components(candidate, source_root, label="adapter import")
        if not candidate.exists():
            continue
        resolved = candidate.resolve(strict=True)
        _assert_inside(resolved, source_root, label="adapter import")
        if resolved.is_file():
            return resolved
    return None


def _module_files(source_root: Path, module_parts: tuple[str, ...]) -> tuple[Path, ...]:
    """Return a module and every local package initializer it executes."""

    target = _module_file(source_root, module_parts)
    if target is None:
        return ()
    files: list[Path] = []
    # Python executes each package initializer before the requested module;
    # bind those bytes as well so ``import pkg.helper`` cannot hide drift in
    # ``pkg/__init__.py``.
    for depth in range(1, len(module_parts)):
        initializer = source_root.joinpath(
            *module_parts[:depth], "__init__.py"
        )
        _assert_no_symlink_components(
            initializer, source_root, label="adapter import"
        )
        if not initializer.exists():
            continue
        resolved = initializer.resolve(strict=True)
        _assert_inside(resolved, source_root, label="adapter import")
        if resolved.is_file():
            files.append(resolved)
    files.append(target)
    return tuple(files)


def _relative_module_parts(path: Path, source_root: Path) -> tuple[str, ...]:
    relative = path.relative_to(source_root)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    elif parts:
        parts.pop()
    return tuple(parts)


def _import_targets(path: Path, source_root: Path) -> tuple[Path, ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise AdapterSourceError(f"adapter source is not parseable: {path}") from exc

    current_package = _relative_module_parts(path, source_root)
    targets: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = tuple(alias.name for alias in node.names)
            for name in names:
                targets.extend(_module_files(source_root, tuple(name.split("."))))
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            # ``level=1`` means this module's package; larger levels walk
            # toward the source root.  Imports that would leave the root are
            # rejected instead of becoming unbound executable dependencies.
            if node.level - 1 > len(current_package):
                raise AdapterSourceError(
                    f"relative adapter import escapes source root: {path}"
                )
            base = current_package[: len(current_package) - (node.level - 1)]
        else:
            base = ()
        module_parts = tuple(node.module.split(".")) if node.module else ()
        target_parts = base + module_parts
        targets.extend(_module_files(source_root, target_parts))
        # ``from package import helper`` may resolve helper as a child module
        # even when package itself has only an __init__.py.
        for alias in node.names:
            targets.extend(
                _module_files(
                    source_root,
                    target_parts + tuple(alias.name.split(".")),
                )
            )
    return tuple(targets)


def import_closure(source_root: Path, entrypoint: Path) -> tuple[Path, ...]:
    """Return the deterministic local Python import closure for an entrypoint."""

    root = source_root.resolve(strict=True)
    _assert_no_symlink_components(entrypoint, root, label="adapter entrypoint")
    first = entrypoint.resolve(strict=True)
    _assert_inside(first, root, label="adapter entrypoint")
    pending = [first]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for imported in _import_targets(current, root):
            if imported not in seen:
                pending.append(imported)
    return tuple(sorted(seen, key=lambda item: item.relative_to(root).as_posix()))


def closure_entries(source_root: Path, paths: tuple[Path, ...]) -> tuple[dict[str, str | int], ...]:
    entries: list[dict[str, str | int]] = []
    root = source_root.resolve(strict=True)
    for path in paths:
        unresolved = path if path.is_absolute() else root / path
        _assert_no_symlink_components(unresolved, root, label="adapter closure")
        resolved = unresolved.resolve(strict=True)
        relative = _assert_inside(
            resolved, root, label="adapter closure"
        ).relative_to(root)
        content = resolved.read_bytes()
        entries.append(
            {
                "path": relative.as_posix(),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return tuple(entries)


def closure_digest(source_root: Path, paths: tuple[Path, ...]) -> str:
    entries = sorted(
        closure_entries(source_root, paths), key=lambda item: str(item["path"])
    )
    payload = json.dumps(
        entries,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "AdapterSourceError",
    "closure_digest",
    "closure_entries",
    "import_closure",
    "resolve_entrypoint",
    "resolve_source_root",
]
