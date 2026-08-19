#!/usr/bin/env python3
"""Check an adopted owner/worker project spine without mutating it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath


def _relative_path(value: str, label: str) -> Path:
    if "\\" in value or any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{label} must use safe POSIX separators")
    candidate = PurePosixPath(value)
    if (
        not candidate.parts
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"{label} must be a normalized relative path")
    return Path(*candidate.parts)


def _real_child(root: Path, relative: Path, label: str) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} escapes the project root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} cannot use a symlink")
        if not current.exists():
            break
    return candidate


def _require_file(root: Path, relative: Path, errors: list[str]) -> None:
    try:
        target = _real_child(root, relative, str(relative))
    except ValueError as exc:
        errors.append(str(exc))
        return
    if not target.is_file():
        errors.append(f"missing regular file: {relative}")


def _require_directory(root: Path, relative: Path, errors: list[str]) -> None:
    try:
        target = _real_child(root, relative, str(relative))
    except ValueError as exc:
        errors.append(str(exc))
        return
    if not target.is_dir():
        errors.append(f"missing directory: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_root", type=Path)
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--work-packages-dir", default="work-packages")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--infra-dir", default="infra")
    parser.add_argument(
        "--authority-file",
        action="append",
        default=["AGENTS.md", "README.md"],
        help="normalized relative authority path; may be repeated",
    )
    args = parser.parse_args()
    configured_root = args.project_root.expanduser()
    try:
        if configured_root.is_symlink():
            raise ValueError("project root cannot be a symlink")
        root = configured_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("project root must be a directory")
        infra_dir = _relative_path(args.infra_dir, "infra directory")
        work_packages_dir = _relative_path(
            args.work_packages_dir, "work-packages directory"
        )
        runs_dir = _relative_path(args.runs_dir, "runs directory")
        authority_files = [
            _relative_path(item, "authority file") for item in args.authority_file
        ]
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    errors: list[str] = []
    for relative in authority_files:
        _require_file(root, relative, errors)
    manifest = infra_dir / "ENVIRONMENT_MANIFEST.json"
    _require_file(root, manifest, errors)
    _require_directory(root, infra_dir, errors)
    _require_directory(root, work_packages_dir, errors)
    _require_directory(root, runs_dir, errors)
    if args.require_ready:
        _require_file(root, infra_dir / "READY", errors)

    wp_root = root / work_packages_dir
    packages: list[Path] = []
    if wp_root.is_dir() and not wp_root.is_symlink():
        try:
            entries = sorted(wp_root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            errors.append(f"cannot inspect work-packages directory: {exc}")
            entries = []
        for entry in entries:
            if entry.is_symlink():
                errors.append(
                    f"work package entry {entry.name!r} cannot use a symlink"
                )
            elif entry.is_dir():
                packages.append(entry)
    if not packages:
        errors.append("no work package CONTRACT.md found")
    for package in packages:
        try:
            # Keep the lexical path until _real_child has rejected aliases.
            relative_package = package.relative_to(root)
            _real_child(root, relative_package, f"work package {package.name}")
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        for name in ("CONTRACT.md", "HANDOFF.md", "STATUS.md"):
            _require_file(root, relative_package / name, errors)

    manifest_path = root / manifest
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("infra manifest must be an object")
            if args.require_ready and data.get("status") != "READY":
                errors.append("infra manifest is not READY")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"invalid infra manifest: {exc}")

    if errors:
        for error in errors:
            print("ERROR:", error)
        return 1
    print("PASS: project layout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
