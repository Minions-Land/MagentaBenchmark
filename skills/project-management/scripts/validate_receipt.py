#!/usr/bin/env python3
"""Validate the required sections and privacy boundaries of a Markdown receipt."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


REQUIRED = (
    "conclusion",
    "frozen protocol",
    "results and denominator",
    "sentinel",
    "fidelity",
    "cost",
    "evidence and next action",
)
SECRET = re.compile(r"(?im)^\s*(?:API_KEY|SECRET|PASSWORD|ACCESS_TOKEN|OPENAI_API_KEY)\s*=[^<${\s#]+")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("receipt", type=Path)
    args = p.parse_args()
    path = args.receipt.expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    lower = text.lower()
    errors = [f"missing section: {name}" for name in REQUIRED if name not in lower]
    if SECRET.search(text):
        errors.append("possible secret assignment")
    if "complete" in lower or "reproduced" in lower:
        for token in ("expected", "unique", "commit", "sha256", "owner"):
            if token not in lower:
                errors.append(f"claim-bearing receipt missing: {token}")
    if errors:
        for error in errors:
            print("ERROR:", error)
        return 1
    print("PASS:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

