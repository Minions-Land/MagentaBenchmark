#!/usr/bin/env python3
"""Check the owner/worker project spine without mutating it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("project_root", type=Path)
    p.add_argument("--require-ready", action="store_true")
    p.add_argument("--work-packages-dir", default="work-packages")
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--infra-dir", default="infra")
    args = p.parse_args()
    root = args.project_root.expanduser().resolve()
    infra_dir = Path(args.infra_dir)
    work_packages_dir = Path(args.work_packages_dir)
    runs_dir = Path(args.runs_dir)
    required = [
        Path("AGENTS.md"), Path("README.md"), Path("docs/CURRENT_PROJECT_DOCS.md"),
        infra_dir / "ENVIRONMENT_MANIFEST.json",
        work_packages_dir, runs_dir,
    ]
    if args.require_ready:
        required.append(infra_dir / "READY")
    errors = [f"missing: {x}" for x in required if not (root / x).exists()]
    wp_root = root / work_packages_dir
    packages = sorted(p.parent for p in wp_root.glob("*/CONTRACT.md")) if wp_root.is_dir() else []
    if not packages:
        errors.append("no work package CONTRACT.md found")
    for package in packages:
        for name in ("CONTRACT.md", "HANDOFF.md", "STATUS.md"):
            if not (package / name).is_file():
                errors.append(f"missing work-package file: {package.name}/{name}")
    manifest = root / infra_dir / "ENVIRONMENT_MANIFEST.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if args.require_ready and data.get("status") != "READY":
                errors.append("infra manifest is not READY")
        except Exception as exc:
            errors.append(f"invalid infra manifest: {exc}")
    if errors:
        for error in errors:
            print("ERROR:", error)
        return 1
    print("PASS:", root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
