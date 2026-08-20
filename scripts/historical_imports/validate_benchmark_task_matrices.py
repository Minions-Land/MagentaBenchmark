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
        canonical_json_bytes,
        project_report,
        projector_sha256,
        read_pinned,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` invocation.
    from benchmark_task_matrices_v1 import (  # type: ignore[import-not-found, no-redef]
        EXPECTED_SOURCES,
        PROJECTOR_ID,
        REPORT_FORMAT,
        _parse_root,
        canonical_json_bytes,
        project_report,
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
    r"(?:^|/)(?:mnt|tmp|home|root)/"
    r"|[A-Za-z]:\\"
    r"|https?://[^/\s:@]+:[^/@\s]+@"
    r"|(?:^|[?#&])(?:access[_-]?token|api[_-]?key|auth[_-]?token|key|password|secret|sig|token)=[^\s&#]+",
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

TOP_LEVEL_FIELDS = {
    "benchmarks",
    "format",
    "generated_at",
    "generated_from",
    "projection_contract",
    "projector",
}
CONTRACT_FIELDS = {
    "allowed_fields",
    "claim_eligible",
    "excluded_fields",
    "id",
    "publication_decision",
    "source_of_truth",
}
BENCHMARK_FIELDS = {
    "benchmark_id",
    "claim_eligible",
    "limitations",
    "method_columns",
    "method_summaries",
    "name",
    "observation_status",
    "record_origin",
    "result_route",
    "row_count",
    "rows",
    "source",
}
ROW_FIELDS = {"category", "claim_eligible", "methods", "task_id"}
CELL_FIELDS = {"claim_eligible", "reason", "score", "status", "verdict"}
SUMMARY_FIELDS = {"method", "numeric_summaries", "verdict_counts"}
NUMERIC_SUMMARY_FIELDS = {"score"}
SOURCE_PATH_FIELDS = {"git_blob_sha1", "path", "sha256", "size_bytes"}
SOURCE_FIELDS = {
    "branch",
    "commit_sha",
    "fixed_snapshot",
    "git_blob_sha1",
    "kind",
    "normalizer_id",
    "normalizer_role",
    "normalizer_sha256",
    "path_hint",
    "paths",
    "publication_decision",
    "projection",
    "projector_id",
    "projector_sha256",
    "repository",
    "repository_hint",
    "result_available",
    "result_paths",
    "revision_hint",
    "root_tree_sha1",
    "sha256",
    "size_bytes",
    "visibility",
}


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def _unknown_fields(
    value: Mapping[str, Any], allowed: set[str], path: str, errors: list[str]
) -> None:
    for key in value:
        if str(key) not in allowed:
            _fail(errors, f"unknown field at {path}.{key}")


def _contains_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(
            re.fullmatch(
                r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
                value.strip(),
            )
        )
    if isinstance(value, Mapping):
        return any(_contains_numeric(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_numeric(child) for child in value)
    return False


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


def _summary_map(items: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("method")): item for item in items if isinstance(item, Mapping)
    }


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric summary: {value!r}") from exc


def _validate_sources(
    report: Mapping[str, Any], roots: Mapping[str, Path], errors: list[str]
) -> None:
    source_names = {
        item.get("benchmark_id")
        for item in report.get("benchmarks", [])
        if isinstance(item, Mapping)
    }
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
    _unknown_fields(report, TOP_LEVEL_FIELDS, "$", errors)
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
    if isinstance(contract, Mapping):
        _unknown_fields(contract, CONTRACT_FIELDS, "$.projection_contract", errors)

    benchmarks = report.get("benchmarks")
    if not isinstance(benchmarks, list):
        return errors + ["benchmarks must be a list"]
    seen_benchmarks: set[str] = set()
    for benchmark in benchmarks:
        if not isinstance(benchmark, Mapping):
            _fail(errors, "benchmark entry is not an object")
            continue
        benchmark_id = str(benchmark.get("benchmark_id"))
        _unknown_fields(
            benchmark, BENCHMARK_FIELDS, f"$.benchmarks[{benchmark_id}]", errors
        )
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
        columns_valid = all(isinstance(column, str) for column in columns)
        column_names = set(columns) if columns_valid else set()
        if benchmark.get("row_count") != len(rows) or benchmark.get(
            "row_count"
        ) != EXPECTED_COUNTS.get(benchmark_id):
            _fail(errors, f"benchmark {benchmark_id} row count mismatch")
        if not columns_valid:
            _fail(errors, f"benchmark {benchmark_id} method columns must be strings")
        elif len(columns) != len(column_names):
            _fail(errors, f"benchmark {benchmark_id} has duplicate method columns")
        task_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                _fail(errors, f"benchmark {benchmark_id} row is not an object")
                continue
            task_id = str(row.get("task_id"))
            _unknown_fields(
                row, ROW_FIELDS, f"$.benchmarks[{benchmark_id}].rows[{task_id}]", errors
            )
            if task_id in task_ids:
                _fail(errors, f"benchmark {benchmark_id} duplicate task id {task_id}")
            task_ids.add(task_id)
            if row.get("claim_eligible") is not False:
                _fail(
                    errors,
                    f"benchmark {benchmark_id}/{task_id} missing false claim_eligible",
                )
            methods = row.get("methods")
            if not isinstance(methods, Mapping) or (
                columns_valid and set(methods) != column_names
            ):
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
                _unknown_fields(
                    cell,
                    CELL_FIELDS,
                    f"$.benchmarks[{benchmark_id}].rows[{task_id}].methods[{method}]",
                    errors,
                )
                if cell.get("claim_eligible") is not False:
                    _fail(
                        errors,
                        f"benchmark {benchmark_id}/{task_id}/{method} missing false claim_eligible",
                    )
                if benchmark_id in DECLARATION_BENCHMARKS:
                    if _contains_numeric(cell):
                        _fail(
                            errors,
                            f"declaration benchmark has numeric value: {benchmark_id}/{task_id}/{method}",
                        )
                    if cell.get("verdict") is not None:
                        _fail(
                            errors,
                            f"declaration benchmark has a verdict: {benchmark_id}/{task_id}/{method}",
                        )
        summary_items = benchmark.get("method_summaries", [])
        if not isinstance(summary_items, list):
            _fail(errors, f"benchmark {benchmark_id} method summaries malformed")
        for index, item in enumerate(
            summary_items if isinstance(summary_items, list) else []
        ):
            if isinstance(item, Mapping):
                _unknown_fields(
                    item,
                    SUMMARY_FIELDS,
                    f"$.benchmarks[{benchmark_id}].method_summaries[{index}]",
                    errors,
                )
                numeric_summaries = item.get("numeric_summaries", {})
                if not isinstance(numeric_summaries, Mapping):
                    _fail(
                        errors,
                        f"benchmark {benchmark_id} method summary numeric_summaries malformed",
                    )
                else:
                    _unknown_fields(
                        numeric_summaries,
                        NUMERIC_SUMMARY_FIELDS,
                        f"$.benchmarks[{benchmark_id}].method_summaries[{index}].numeric_summaries",
                        errors,
                    )
        summary = _summary_map(summary_items)
        if columns_valid and set(summary) != column_names:
            _fail(errors, f"benchmark {benchmark_id} method summaries mismatch")
        if benchmark_id in {"cmtbench", "biomnibench-da"} and columns_valid:
            for method in columns:
                cells = []
                for row in rows:
                    methods = row.get("methods") if isinstance(row, Mapping) else None
                    cell = methods.get(method) if isinstance(methods, Mapping) else None
                    if isinstance(cell, Mapping):
                        cells.append(cell)
                actual_counts = _counts(cells)
                expected = dict(summary.get(method, {}).get("verdict_counts", {}))
                if actual_counts != expected:
                    _fail(
                        errors,
                        f"benchmark {benchmark_id}/{method} verdict aggregate drift",
                    )
                if benchmark_id == "biomnibench-da" and method in summary:
                    try:
                        scores = [_decimal(cell["score"]) for cell in cells]
                    except (KeyError, TypeError, ValueError) as exc:
                        _fail(
                            errors,
                            f"benchmark {benchmark_id}/{method} score malformed: {exc}",
                        )
                        continue
                    numeric_summaries = summary[method].get("numeric_summaries", {})
                    if not isinstance(numeric_summaries, Mapping):
                        _fail(
                            errors,
                            f"benchmark {benchmark_id}/{method} numeric_summaries malformed",
                        )
                        continue
                    numeric = numeric_summaries.get("score", {})
                    if not isinstance(numeric, Mapping):
                        _fail(
                            errors,
                            f"benchmark {benchmark_id}/{method} score summary malformed",
                        )
                        continue
                    if numeric.get("observed_count") != len(scores):
                        _fail(
                            errors,
                            f"benchmark {benchmark_id}/{method} observed count drift",
                        )
                    try:
                        mean_drift = _decimal(numeric.get("mean")) != _decimal(
                            round(float(sum(scores) / len(scores)), 2)
                        )
                        range_drift = _decimal(numeric.get("min")) != min(
                            scores
                        ) or _decimal(numeric.get("max")) != max(scores)
                    except (TypeError, ValueError, ZeroDivisionError) as exc:
                        _fail(
                            errors,
                            f"benchmark {benchmark_id}/{method} numeric summary malformed: {exc}",
                        )
                        continue
                    if mean_drift:
                        _fail(errors, f"benchmark {benchmark_id}/{method} mean drift")
                    if range_drift:
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
            _unknown_fields(
                source, SOURCE_FIELDS, f"$.benchmarks[{benchmark_id}].source", errors
            )
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
            source_paths = source.get("paths", [])
            if not isinstance(source_paths, list):
                _fail(errors, f"benchmark {benchmark_id} source paths malformed")
                source_paths = []
            paths = {
                item.get("path"): item
                for item in source_paths
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
            for index, item in enumerate(source_paths):
                if isinstance(item, Mapping):
                    _unknown_fields(
                        item,
                        SOURCE_PATH_FIELDS,
                        f"$.benchmarks[{benchmark_id}].source.paths[{index}]",
                        errors,
                    )
    if seen_benchmarks != set(EXPECTED_COUNTS):
        _fail(errors, "benchmark set mismatch")
    if require_sources:
        _validate_sources(report, roots or {}, errors)
        if not errors:
            _validate_canonical_projection(report, roots or {}, errors)
    return errors


def _validate_canonical_projection(
    report: Mapping[str, Any], roots: Mapping[str, Path], errors: list[str]
) -> None:
    try:
        projected = project_report(report, roots, require_sources=True)
        if canonical_json_bytes(projected) != canonical_json_bytes(report):
            _fail(errors, "report does not match canonical source projection")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        _fail(errors, f"canonical source projection failed: {exc}")


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
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        RecursionError,
        json.JSONDecodeError,
    ) as exc:
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
