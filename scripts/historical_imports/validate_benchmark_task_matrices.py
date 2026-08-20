#!/usr/bin/env python3
"""Fail-closed validator for the published task-matrix projection."""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

try:
    from scripts.historical_imports.benchmark_task_matrices_v1 import (
        EXPECTED_SOURCES,
        PROJECTOR_ID,
        REPORT_FORMAT,
        _parse_root,
        projector_sha256,
        read_pinned,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` invocation.
    from benchmark_task_matrices_v1 import (  # type: ignore[import-not-found, no-redef]
        EXPECTED_SOURCES,
        PROJECTOR_ID,
        REPORT_FORMAT,
        _parse_root,
        projector_sha256,
        read_pinned,
    )

FORBIDDEN_KEY_PARTS = (
    "answer",
    "credential",
    "gold",
    "model_output",
    "password",
    "private_test",
    "prompt",
    "secret",
    "stderr",
    "stdout",
    "token",
    "trace",
)
FORBIDDEN_TEXT = re.compile(
    r"(?:^|/)(?:mnt|tmp|home|root)/|[A-Za-z]:\\|https?://[^ ]+[?&](?:token|key|sig)=",
    re.I,
)
DECLARATION_BENCHMARKS = {"naturebench", "bioml-bench"}
EXPECTED_COUNTS = {
    "cmtbench": 50,
    "biomnibench-da": 50,
    "naturebench": 31,
    "bioml-bench": 8,
    "swe-bench-lite-(not-verified)": 1,
}


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def _walk(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in FORBIDDEN_KEY_PARTS):
                _fail(errors, f"forbidden field at {path}.{key}")
            _walk(child, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]", errors)
    elif isinstance(value, str) and FORBIDDEN_TEXT.search(value):
        _fail(errors, f"machine-private or authenticated locator at {path}")
    elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        _fail(errors, f"non-finite number at {path}")


def _counts(cells: list[Mapping[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(cell.get("verdict")) for cell in cells).items()))


def _summary_map(items: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(item.get("method")): item for item in items}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric summary: {value!r}") from exc


def _validate_sources(
    report: Mapping[str, Any], roots: Mapping[str, Path], errors: list[str]
) -> None:
    source_names = {item["benchmark_id"] for item in report.get("benchmarks", [])}
    for name in ("cmtbench", "biomnibench-da", "naturebench"):
        if name not in source_names:
            continue
        if name == "biomnibench-da":
            root_names: tuple[str, ...] = (
                "biomnibench-da-default",
                "biomnibench-da-xhigh",
            )
        else:
            root_names = (name,)
        for root_name in root_names:
            root = roots.get(root_name)
            if root is None:
                _fail(errors, f"missing required source root: {root_name}")
                continue
            source = EXPECTED_SOURCES[
                "biomnibench-da" if name == "biomnibench-da" else name
            ]
            relatives = (
                ("results/default/_summary/llm_judge_summary.tsv",)
                if root_name == "biomnibench-da-default"
                else ("results/xhigh/_summary/llm_judge_summary.tsv",)
                if root_name == "biomnibench-da-xhigh"
                else tuple(source["paths"])
            )
            for relative in relatives:
                try:
                    read_pinned(root, name, relative)
                except (OSError, ValueError) as exc:
                    _fail(errors, str(exc))


def validate_report(
    report: Mapping[str, Any],
    *,
    roots: Mapping[str, Path] | None = None,
    require_sources: bool = False,
) -> list[str]:
    errors: list[str] = []
    if report.get("format") != REPORT_FORMAT:
        _fail(errors, "unexpected report format")
    projector = report.get("projector")
    if not isinstance(projector, Mapping):
        _fail(errors, "projector identity is missing")
    else:
        if projector.get("id") != PROJECTOR_ID:
            _fail(errors, "projector id mismatch")
        if (
            projector.get("path")
            != "scripts/historical_imports/benchmark_task_matrices_v1.py"
        ):
            _fail(errors, "projector path mismatch")
        if projector.get("sha256") != projector_sha256():
            _fail(errors, "projector digest mismatch")
    _walk(report, "$", errors)
    if report.get("claim_eligible") is True:
        _fail(errors, "report claim_eligible must be false")
    contract = report.get("projection_contract")
    if not isinstance(contract, Mapping) or contract.get("claim_eligible") is not False:
        _fail(errors, "projection contract must be non-claim")
    allowed = (
        set(contract.get("allowed_fields", ()))
        if isinstance(contract, Mapping)
        else set()
    )
    if "claim_eligible" not in allowed:
        _fail(errors, "projection contract omits claim_eligible")

    benchmarks = report.get("benchmarks")
    if not isinstance(benchmarks, list):
        return errors + ["benchmarks must be a list"]
    seen_benchmarks: set[str] = set()
    for benchmark in benchmarks:
        if not isinstance(benchmark, Mapping):
            _fail(errors, "benchmark entry is not an object")
            continue
        benchmark_id = str(benchmark.get("benchmark_id"))
        if benchmark_id in seen_benchmarks:
            _fail(errors, f"duplicate benchmark: {benchmark_id}")
        seen_benchmarks.add(benchmark_id)
        if benchmark.get("claim_eligible") is not False:
            _fail(errors, f"benchmark {benchmark_id} is claim-eligible")
        rows = benchmark.get("rows")
        columns = benchmark.get("method_columns")
        if not isinstance(rows, list) or not isinstance(columns, list):
            _fail(errors, f"benchmark {benchmark_id} rows/columns malformed")
            continue
        if benchmark.get("row_count") != len(rows) or benchmark.get(
            "row_count"
        ) != EXPECTED_COUNTS.get(benchmark_id):
            _fail(errors, f"benchmark {benchmark_id} row count mismatch")
        if len(columns) != len(set(columns)):
            _fail(errors, f"benchmark {benchmark_id} has duplicate method columns")
        task_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                _fail(errors, f"benchmark {benchmark_id} row is not an object")
                continue
            task_id = str(row.get("task_id"))
            if task_id in task_ids:
                _fail(errors, f"benchmark {benchmark_id} duplicate task id {task_id}")
            task_ids.add(task_id)
            if row.get("claim_eligible") is not False:
                _fail(
                    errors,
                    f"benchmark {benchmark_id}/{task_id} missing false claim_eligible",
                )
            methods = row.get("methods")
            if not isinstance(methods, Mapping) or set(methods) != set(columns):
                _fail(
                    errors,
                    f"benchmark {benchmark_id}/{task_id} method coverage mismatch",
                )
                continue
            for method, cell in methods.items():
                if not isinstance(cell, Mapping):
                    _fail(
                        errors,
                        f"benchmark {benchmark_id}/{task_id}/{method} cell is not an object",
                    )
                    continue
                if cell.get("claim_eligible") is not False:
                    _fail(
                        errors,
                        f"benchmark {benchmark_id}/{task_id}/{method} missing false claim_eligible",
                    )
                if benchmark_id in DECLARATION_BENCHMARKS:
                    if any(
                        isinstance(value, (int, float)) and not isinstance(value, bool)
                        for value in cell.values()
                    ):
                        _fail(
                            errors,
                            f"declaration benchmark has numeric value: {benchmark_id}/{task_id}/{method}",
                        )
                    if cell.get("verdict") is not None:
                        _fail(
                            errors,
                            f"declaration benchmark has a verdict: {benchmark_id}/{task_id}/{method}",
                        )
        summary = _summary_map(benchmark.get("method_summaries", []))
        if set(summary) != set(columns):
            _fail(errors, f"benchmark {benchmark_id} method summaries mismatch")
        if benchmark_id in {"cmtbench", "biomnibench-da"}:
            for method in columns:
                cells = [
                    row["methods"][method]
                    for row in rows
                    if isinstance(row.get("methods"), Mapping)
                ]
                actual_counts = _counts(cells)
                expected = dict(summary.get(method, {}).get("verdict_counts", {}))
                if actual_counts != expected:
                    _fail(
                        errors,
                        f"benchmark {benchmark_id}/{method} verdict aggregate drift",
                    )
                if benchmark_id == "biomnibench-da" and method in summary:
                    scores = [_decimal(cell["score"]) for cell in cells]
                    numeric = (
                        summary[method].get("numeric_summaries", {}).get("score", {})
                    )
                    if numeric.get("observed_count") != len(scores):
                        _fail(
                            errors,
                            f"benchmark {benchmark_id}/{method} observed count drift",
                        )
                    if _decimal(numeric.get("mean")) != _decimal(
                        round(float(sum(scores) / len(scores)), 2)
                    ):
                        _fail(errors, f"benchmark {benchmark_id}/{method} mean drift")
                    if _decimal(numeric.get("min")) != min(scores) or _decimal(
                        numeric.get("max")
                    ) != max(scores):
                        _fail(errors, f"benchmark {benchmark_id}/{method} range drift")

        source = benchmark.get("source")
        if benchmark_id in EXPECTED_SOURCES and not isinstance(source, Mapping):
            _fail(errors, f"benchmark {benchmark_id} source metadata missing")
        elif isinstance(source, Mapping) and benchmark_id in EXPECTED_SOURCES:
            if (
                source.get("projector_id") != PROJECTOR_ID
                or source.get("projector_sha256") != projector_sha256()
            ):
                _fail(
                    errors,
                    f"benchmark {benchmark_id} source projector identity mismatch",
                )
            expected_source = EXPECTED_SOURCES[benchmark_id]
            for field, expected_value in (
                ("repository", expected_source["repository"]),
                ("commit_sha", expected_source["commit_sha"]),
                ("root_tree_sha1", expected_source["tree_sha"]),
            ):
                source_value: Any = source.get(field)
                if field == "root_tree_sha1" and isinstance(source_value, Mapping):
                    source_value = source_value.get("digest")
                if source_value != expected_value:
                    _fail(errors, f"benchmark {benchmark_id} source {field} mismatch")
            paths = {
                item.get("path"): item
                for item in source.get("paths", [])
                if isinstance(item, Mapping)
            }
            for relative, expected_path in expected_source["paths"].items():
                actual_path = paths.get(relative)
                if actual_path is None:
                    _fail(
                        errors,
                        f"benchmark {benchmark_id} source path missing: {relative}",
                    )
                    continue
                for field in ("git_blob_sha1", "size_bytes", "sha256"):
                    if actual_path.get(field) != expected_path[field]:
                        _fail(
                            errors,
                            f"benchmark {benchmark_id} source {relative} {field} mismatch",
                        )
    if seen_benchmarks != set(EXPECTED_COUNTS):
        _fail(errors, "benchmark set mismatch")
    if require_sources:
        _validate_sources(report, roots or {}, errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source-root", action="append", type=_parse_root, default=[])
    parser.add_argument("--require-sources", action="store_true")
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        errors = validate_report(
            report, roots=dict(args.source_root), require_sources=args.require_sources
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors = [str(exc)]
    if errors:
        print(
            json.dumps(
                {"ok": False, "errors": errors}, ensure_ascii=False, sort_keys=True
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "projector": PROJECTOR_ID,
                "rows": sum(EXPECTED_COUNTS.values()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
