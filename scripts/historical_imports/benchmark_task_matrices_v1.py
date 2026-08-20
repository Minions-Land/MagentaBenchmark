#!/usr/bin/env python3
"""Deterministically project the approved task-matrix safe fields.

The historical import normalizers in this directory produce typed aggregate
records.  This module is a separate projector: it consumes the pinned source
bytes, checks their identities, and emits the per-task view used by the
repository report.  It never writes the BMP ledger or promotes a claim.
"""

from __future__ import annotations

import argparse
import csv
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping

PROJECTOR_ID = "task-matrix-projector-v1"
REPORT_FORMAT = "magentabench-task-matrix-v1"

CMT_PATH = "experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/per_answer_regrade.csv"
DA_DEFAULT_PATH = "results/default/_summary/llm_judge_summary.tsv"
DA_XHIGH_PATH = "results/xhigh/_summary/llm_judge_summary.tsv"
NATURE_TASKS_PATH = "manifests/tasks.tsv"
NATURE_CELLOMICS_PATH = "task-set/cellomics.txt"

EXPECTED_SOURCES: dict[str, dict[str, Any]] = {
    "cmtbench": {
        "commit_sha": "150fa100ead4ab51acdfc24ed246a8c5b2141466",
        "tree_sha": "3deaec22a778564ae37cbea396765268f959fee5",
        "repository": "Minions-Land/MinionsOS2-Bench",
        "paths": {
            CMT_PATH: {
                "git_blob_sha1": "88fc2f305f97ef1fcaab247602eb20c948c17945",
                "size_bytes": 221376,
                "sha256": "cde0aa20311f255fcc4892d69ec0b58702d16f8e27473481276c2cdad4cdcbad",
            }
        },
    },
    "biomnibench-da": {
        "commit_sha": "def4dae7520807d254612b3590eb32b9aa977924",
        "tree_sha": "50e8fe57a14d8f4c89b8357ab91827fe8bfe60ee",
        "repository": "Minions-Land/AOSEBench",
        "paths": {
            DA_DEFAULT_PATH: {
                "git_blob_sha1": "0e5d4e04c830b60d1a54b5f4f0171e1c96382c5f",
                "size_bytes": 35972,
                "sha256": "af5b16706bf758b79c090ecab73df46d3a23cc1defe6917839a5871b7dd54f5d",
            },
            DA_XHIGH_PATH: {
                "git_blob_sha1": "68e7f39dd8692c904a3a8cc775b5bef2f50b0648",
                "size_bytes": 37959,
                "sha256": "1d587f28659a5fc026f8d0b6c14e6ef361991484d24d22cf59725afeee117373",
            },
        },
    },
    "naturebench": {
        "commit_sha": "4b512029f3ad37746502ce377e4fcc2027fd46db",
        "tree_sha": "e11636f88a5d74e9cb4dcaa06518b9a3a71c87ea",
        "repository": "Minions-Land/AOSEBench-NatureBench",
        "paths": {
            NATURE_TASKS_PATH: {
                "git_blob_sha1": "f5821c63e3575fefb501c6e02d3c5cfeddf91cfb",
                "size_bytes": 5829,
                "sha256": "24d667df9d14a433a460a6115e6c4c24e8d6b7186cc2c528be648c199f7dfc09",
            },
            NATURE_CELLOMICS_PATH: {
                "git_blob_sha1": "18c02dafc5759479c2d91904402b6ce3099e1cd9",
                "size_bytes": 589,
                "sha256": "10020a26ab2c5d510a916a72db668123396555ec5352661d79f856370f084ac2",
            },
        },
    },
}

CMT_METHODS = (
    ("purellm_gpt54", "gpt-5.4"),
    ("purellm", "gpt-5.5"),
    ("codex_gpt54", "gpt-5.4"),
    ("codex", "gpt-5.5"),
    ("purellm_sonnet46_1m", "claude-sonnet-4-6"),
    ("claudecode", "claude-sonnet-4-6[1m]"),
    ("autoscientist", "claude-sonnet-4-6[1m]"),
    ("minionsos2", "claude-sonnet-4-6[1m]"),
)
DA_METHODS = ("Biomni", "PantheonOS", "CellVoyager", "CodexCLI", "BaseLLM")


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()


def projector_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _root_for(roots: Mapping[str, Path], name: str) -> Path:
    try:
        return roots[name]
    except KeyError as exc:
        raise ValueError(f"source root is required for {name}") from exc


def _candidate(root: Path, relative: str, *, git_checkout: bool = False) -> Path:
    direct = root / relative
    if direct.is_file():
        return direct
    if git_checkout:
        raise ValueError(f"source file is missing at repository path: {relative}")
    basename = root / Path(relative).name
    if basename.is_file():
        return basename
    raise ValueError(f"source file is missing: {relative} under {root}")


def _git_blob_sha1(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


def _git_checkout_identity(root: Path) -> tuple[str, str] | None:
    """Return ``(HEAD, root-tree)`` when *root* is a Git checkout.

    Portable evidence exports may omit ``.git``; their pinned byte size,
    SHA-256, and Git blob IDs remain the source identity. A checkout must also
    prove the commit and root tree declared by the source descriptor.
    """
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None
    if probe.returncode != 0:
        if "not a git repository" in probe.stderr.lower():
            return None
        raise ValueError(f"cannot inspect Git source root: {root}")
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"cannot read Git source identity: {root}") from exc
    return head, tree


def _validate_git_source_identity(root: Path, source_name: str) -> None:
    identity = _git_checkout_identity(root)
    if identity is None:
        return
    expected = EXPECTED_SOURCES[source_name]
    if identity[0] != expected["commit_sha"]:
        raise ValueError(f"Git commit mismatch for {source_name}")
    if identity[1] != expected["tree_sha"]:
        raise ValueError(f"Git root tree mismatch for {source_name}")


def read_pinned(root: Path, source_name: str, relative: str) -> bytes:
    source = EXPECTED_SOURCES[source_name]
    expected = source["paths"][relative]
    identity = _git_checkout_identity(root)
    if identity is not None:
        _validate_git_source_identity(root, source_name)
    path = _candidate(root, relative, git_checkout=identity is not None)
    content = path.read_bytes()
    if len(content) != expected["size_bytes"]:
        raise ValueError(f"size mismatch for {relative}")
    if hashlib.sha256(content).hexdigest() != expected["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {relative}")
    if _git_blob_sha1(content) != expected["git_blob_sha1"]:
        raise ValueError(f"Git blob mismatch for {relative}")
    return content


def _bool(value: str) -> bool:
    return value.strip().lower() == "true"


def _cmt_cell(record: Mapping[str, str]) -> dict[str, Any]:
    if not _bool(record["adopted_success"]):
        verdict = "未解析"
    elif _bool(record["adopted_correct"]):
        verdict = "正确"
    else:
        verdict = "错误"
    return {"verdict": verdict, "claim_eligible": False}


def project_cmt(root: Path) -> dict[str, Any]:
    content = read_pinned(root, "cmtbench", CMT_PATH)
    records = list(csv.DictReader(content.decode().splitlines()))
    required = {
        "task",
        "branch",
        "method",
        "model",
        "adopted_success",
        "adopted_correct",
    }
    if not records or not required <= set(records[0]):
        raise ValueError("CMT source header is incomplete")
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        key = f"{record['method']} · {record['model']}"
        if (record["method"], record["model"]) not in CMT_METHODS:
            raise ValueError(f"unexpected CMT method: {key}")
        task = record["task"]
        row = grouped.setdefault(
            task, {"task_id": task, "category": record["branch"], "methods": {}}
        )
        if key in row["methods"]:
            raise ValueError(f"duplicate CMT cell: {task}/{key}")
        row["methods"][key] = _cmt_cell(record)
    expected_methods = [f"{method} · {model}" for method, model in CMT_METHODS]
    rows = list(grouped.values())
    for row in rows:
        if set(row["methods"]) != set(expected_methods):
            raise ValueError(f"incomplete CMT row: {row['task_id']}")
        row["methods"] = {method: row["methods"][method] for method in expected_methods}
    summaries = []
    for method in expected_methods:
        counts: dict[str, int] = {}
        for row in rows:
            verdict = row["methods"][method]["verdict"]
            counts[verdict] = counts.get(verdict, 0) + 1
        summaries.append(
            {"method": method, "verdict_counts": counts, "numeric_summaries": {}}
        )
    return {
        "rows": rows,
        "row_count": len(rows),
        "method_columns": expected_methods,
        "method_summaries": summaries,
    }


def _da_records(root: Path, regime: str) -> list[dict[str, str]]:
    relative = DA_DEFAULT_PATH if regime == "default" else DA_XHIGH_PATH
    content = read_pinned(root, "biomnibench-da", relative)
    return list(csv.DictReader(content.decode().splitlines(), delimiter="\t"))


def project_da(default_root: Path, xhigh_root: Path) -> dict[str, Any]:
    records = [("default", record) for record in _da_records(default_root, "default")]
    records.extend(("xhigh", record) for record in _da_records(xhigh_root, "xhigh"))
    grouped: dict[str, dict[str, Any]] = {}
    for regime, record in records:
        if record["method"] not in DA_METHODS:
            raise ValueError(f"unexpected BiomniBench method: {record['method']}")
        task = record["task"]
        row = grouped.setdefault(
            task, {"task_id": task, "category": "DA", "methods": {}}
        )
        key = f"{record['method']} · {regime}"
        if key in row["methods"]:
            raise ValueError(f"duplicate DA cell: {task}/{key}")
        try:
            score = float(record["score"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid DA score for {task}/{key}") from exc
        if not math.isfinite(score):
            raise ValueError(f"non-finite DA score for {task}/{key}")
        row["methods"][key] = {
            "verdict": "成功" if record["judge_status"] == "evaluated" else "失败",
            "score": score,
            "claim_eligible": False,
        }
    expected_methods = [
        f"{method} · {regime}"
        for regime in ("default", "xhigh")
        for method in DA_METHODS
    ]
    rows = list(grouped.values())
    for row in rows:
        if list(row["methods"]) != expected_methods:
            raise ValueError(f"incomplete DA row: {row['task_id']}")
    summaries = []
    for method in expected_methods:
        cells = [row["methods"][method] for row in rows]
        scores = [cell["score"] for cell in cells]
        counts: dict[str, int] = {}
        for cell in cells:
            counts[cell["verdict"]] = counts.get(cell["verdict"], 0) + 1
        summaries.append(
            {
                "method": method,
                "verdict_counts": counts,
                "numeric_summaries": {
                    "score": {
                        "observed_count": len(scores),
                        "mean": round(sum(scores) / len(scores), 2),
                        "min": min(scores),
                        "max": max(scores),
                    }
                },
            }
        )
    return {
        "rows": rows,
        "row_count": len(rows),
        "method_columns": expected_methods,
        "method_summaries": summaries,
    }


def project_nature(root: Path) -> dict[str, Any]:
    tasks = list(
        csv.DictReader(
            read_pinned(root, "naturebench", NATURE_TASKS_PATH).decode().splitlines(),
            delimiter="\t",
        )
    )
    cellomics = {
        line.strip()
        for line in read_pinned(root, "naturebench", NATURE_CELLOMICS_PATH)
        .decode()
        .splitlines()
        if line.strip()
    }
    ids = [task["case_id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("NatureBench manifest contains duplicate case IDs")
    if set(ids) != cellomics:
        raise ValueError("NatureBench task-set and manifest disagree")
    method = "Magenta · Claude Opus 4.7 · medium"
    rows = [
        {
            "task_id": task["case_id"],
            "category": "Cellular Omics",
            "methods": {
                method: {
                    "status": "not-observed",
                    "verdict": None,
                    "reason": "no completed opus4.7_medium.csv at pinned NatureBranch snapshot",
                    "claim_eligible": False,
                }
            },
        }
        for task in tasks
    ]
    return {
        "rows": rows,
        "row_count": len(rows),
        "method_columns": [method],
        "method_summaries": [
            {
                "method": method,
                "verdict_counts": {"not-observed": len(rows)},
                "numeric_summaries": {},
            }
        ],
    }


def project_report(
    report: Mapping[str, Any],
    roots: Mapping[str, Path],
    *,
    require_sources: bool = True,
) -> dict[str, Any]:
    """Return a canonical report projection from fixed source bytes."""
    result = deepcopy(dict(report))
    result["projector"] = {
        "id": PROJECTOR_ID,
        "path": "scripts/historical_imports/benchmark_task_matrices_v1.py",
        "sha256": projector_sha256(),
    }
    benchmarks = {item["benchmark_id"]: item for item in result["benchmarks"]}
    if require_sources:
        benchmarks["cmtbench"].update(project_cmt(_root_for(roots, "cmtbench")))
        benchmarks["biomnibench-da"].update(
            project_da(
                _root_for(roots, "biomnibench-da-default"),
                _root_for(roots, "biomnibench-da-xhigh"),
            )
        )
        benchmarks["naturebench"].update(
            project_nature(_root_for(roots, "naturebench"))
        )
    for benchmark in result["benchmarks"]:
        benchmark["claim_eligible"] = False
        benchmark_source = benchmark.get("source")
        if isinstance(benchmark_source, dict):
            benchmark_source["projector_id"] = PROJECTOR_ID
            benchmark_source["projector_sha256"] = result["projector"]["sha256"]
            benchmark_source["projection"] = PROJECTOR_ID
            if benchmark["benchmark_id"] == "naturebench":
                benchmark_source["paths"] = [
                    {
                        "path": relative,
                        "git_blob_sha1": metadata["git_blob_sha1"],
                        "size_bytes": metadata["size_bytes"],
                        "sha256": metadata["sha256"],
                    }
                    for relative, metadata in EXPECTED_SOURCES["naturebench"][
                        "paths"
                    ].items()
                ]
                benchmark_source["result_paths"] = []
                benchmark_source["result_available"] = False
            elif benchmark["benchmark_id"] in {"cmtbench", "biomnibench-da"}:
                benchmark_source["normalizer_role"] = "historical-import"
        for row in benchmark.get("rows", []):
            row["claim_eligible"] = False
            for cell in row.get("methods", {}).values():
                if isinstance(cell, dict):
                    cell["claim_eligible"] = False
        if benchmark["benchmark_id"] == "swe-bench-lite-(not-verified)":
            for row in benchmark.get("rows", []):
                for cell in row.get("methods", {}).values():
                    cell.pop("metric_summary", None)
    contract = result.setdefault("projection_contract", {})
    allowed = list(contract.get("allowed_fields", []))
    if "claim_eligible" not in allowed:
        allowed.append("claim_eligible")
    contract["allowed_fields"] = allowed
    excluded = list(contract.get("excluded_fields", []))
    for field in ("absolute_paths", "authenticated_urls"):
        if field not in excluded:
            excluded.append(field)
    contract["excluded_fields"] = excluded
    return result


def _parse_root(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("source roots must use NAME=PATH")
    return name, Path(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-root", action="append", type=_parse_root, default=[])
    parser.add_argument(
        "--allow-unbound",
        action="store_true",
        help="only add projector identity and explicit false fields",
    )
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    roots = dict(args.source_root)
    projected = project_report(report, roots, require_sources=not args.allow_unbound)
    destination = args.output or args.report
    output_bytes = canonical_json_bytes(projected)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(output_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "projector": PROJECTOR_ID,
                "output": str(destination),
                "sha256": hashlib.sha256(output_bytes).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
