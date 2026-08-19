#!/usr/bin/env python3
"""Verify exact task/trial coverage against a frozen JSON grid."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import TypeAlias


CellValue: TypeAlias = tuple[str, str | int]


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable or malformed") from exc


def _cell_value(value: object, label: str) -> CellValue:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label} must be a string or integer")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{label} must be non-empty")
    prefix = "string" if isinstance(value, str) else "integer"
    return prefix, value


def _row_value(row: dict[str, object], primary: str, fallback: str) -> CellValue:
    if primary in row:
        value = row[primary]
    elif fallback in row:
        value = row[fallback]
    else:
        raise ValueError(f"row requires {primary} or {fallback}")
    return _cell_value(value, f"row {primary}/{fallback}")


def _expected_grid(path: Path) -> tuple[set[CellValue], set[CellValue]]:
    value = _load_json(path, "expected grid")
    if not isinstance(value, dict) or set(value) != {"task_ids", "trial_ids"}:
        raise ValueError("expected grid requires only task_ids and trial_ids")
    task_values = value["task_ids"]
    trial_values = value["trial_ids"]
    if not isinstance(task_values, list) or not task_values:
        raise ValueError("expected task_ids must be a non-empty list")
    if not isinstance(trial_values, list) or not trial_values:
        raise ValueError("expected trial_ids must be a non-empty list")
    task_ids = {
        _cell_value(item, "expected task id") for item in task_values
    }
    trial_ids = {
        _cell_value(item, "expected trial id") for item in trial_values
    }
    if len(task_ids) != len(task_values) or len(trial_ids) != len(trial_values):
        raise ValueError("expected grid identifiers must be unique")
    return task_ids, trial_ids


def _rows(path: Path) -> list[object]:
    value = _load_json(path, "results")
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("simulations"), list):
        return value["simulations"]
    raise ValueError("results must be a list or contain a simulations list")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--expected-grid", type=Path, required=True)
    parser.add_argument(
        "--required-field",
        action="append",
        required=True,
        help="top-level result field that must exist and be non-null; repeatable",
    )
    args = parser.parse_args()
    if any(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", field) is None
        for field in args.required_field
    ):
        print("ERROR: required field names must be safe normalized identifiers")
        return 2
    if len(set(args.required_field)) != len(args.required_field):
        print("ERROR: required field names must be unique")
        return 2
    try:
        task_ids, trial_ids = _expected_grid(args.expected_grid)
        keys: list[tuple[CellValue, CellValue]] = []
        for item in _rows(args.results):
            if not isinstance(item, dict):
                raise ValueError("every result row must be an object")
            for field in args.required_field:
                if field not in item or item[field] is None or item[field] == "":
                    raise ValueError(f"result row requires non-empty field {field}")
            keys.append(
                (
                    _row_value(item, "task_id", "task"),
                    _row_value(item, "trial", "trial_id"),
                )
            )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    expected_cells = {(task, trial) for task in task_ids for trial in trial_ids}
    counts = Counter(keys)
    observed_cells = set(counts)
    duplicates = sum(count - 1 for count in counts.values() if count > 1)
    missing = len(expected_cells - observed_cells)
    unexpected = len(observed_cells - expected_cells)
    print(f"expected={len(expected_cells)}")
    print(f"records={len(keys)}")
    print(f"unique_cells={len(observed_cells)}")
    print(f"duplicates={duplicates}")
    print(f"missing_cells={missing}")
    print(f"unexpected_cells={unexpected}")
    if duplicates or missing or unexpected:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
