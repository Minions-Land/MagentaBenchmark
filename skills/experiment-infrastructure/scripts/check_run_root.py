#!/usr/bin/env python3
"""Check that a proposed run root is inside the authorized project root."""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_root")
    parser.add_argument("run_root")
    args = parser.parse_args()
    project = Path(args.project_root).expanduser().resolve()
    run = Path(args.run_root).expanduser().resolve()
    try:
        run.relative_to(project)
    except ValueError:
        print(f"OUTSIDE_AUTHORIZED_ROOT: {run}")
        return 2
    print(f"AUTHORIZED_RUN_ROOT: {run}")
    print(f"EXISTS: {run.exists()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
