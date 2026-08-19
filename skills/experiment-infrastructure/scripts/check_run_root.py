#!/usr/bin/env python3
"""Check that a proposed run root is a safe project descendant."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def _reject_controls(value: str, label: str) -> None:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{label} contains control characters")


def _uses_symlink(path: Path) -> bool:
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def _resolve_project_root(value: str) -> Path:
    _reject_controls(value, "project root")
    configured = Path(value).expanduser()
    if _uses_symlink(configured):
        raise ValueError("project root cannot use a symlink")
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise ValueError("project root is unavailable") from exc
    if not resolved.is_dir():
        raise ValueError("project root must be a directory")
    return resolved


def _resolve_run_root(value: str) -> tuple[Path, bool]:
    _reject_controls(value, "run root")
    configured = Path(value).expanduser()
    existed = os.path.lexists(configured)
    if _uses_symlink(configured):
        raise ValueError("run root cannot use a symlink")
    try:
        resolved = configured.resolve(strict=False)
    except OSError as exc:
        raise ValueError("run root is unavailable") from exc
    if existed and not resolved.is_dir():
        raise ValueError("existing run root must be a real directory")
    return resolved, existed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_root")
    parser.add_argument("run_root")
    parser.add_argument(
        "--require-new",
        action="store_true",
        help="fail when the proposed run root already exists",
    )
    args = parser.parse_args()
    try:
        project = _resolve_project_root(args.project_root)
        run, existed = _resolve_run_root(args.run_root)
        relative = run.relative_to(project)
        if not relative.parts:
            raise ValueError("run root must be strictly below the project root")
        if args.require_new and existed:
            raise ValueError("run root already exists")
        if Path(os.path.commonpath((project, run))) != project:
            raise ValueError("run root escapes the project root")
    except ValueError as exc:
        print(f"UNAUTHORIZED_RUN_ROOT: {exc}")
        return 2
    print(f"AUTHORIZED_RUN_ROOT: {relative.as_posix()}")
    print(f"EXISTS: {existed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
