#!/usr/bin/env python3
"""Verify unique task/trial coverage in a JSON list of simulation records."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("results")
    p.add_argument("--tasks", type=int, required=True)
    p.add_argument("--trials", type=int, required=True)
    args = p.parse_args()
    data = json.loads(Path(args.results).read_text(encoding="utf-8-sig"))
    rows = data if isinstance(data, list) else data.get("simulations", [])
    keys = []
    for row in rows:
        task = row.get("task_id", row.get("task"))
        trial = row.get("trial", row.get("trial_id"))
        keys.append((str(task), str(trial)))
    counts = Counter(keys)
    expected = args.tasks * args.trials
    unique = len(counts)
    duplicates = sum(n - 1 for n in counts.values() if n > 1)
    print(f"expected={expected}")
    print(f"records={len(keys)}")
    print(f"unique_cells={unique}")
    print(f"duplicates={duplicates}")
    if unique != expected or duplicates:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
