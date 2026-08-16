#!/usr/bin/env python3
"""Build the approved BiomniBench-DA projection from pinned AOSEBench bytes."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
from datetime import date
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from MagentaBench.collab.import_models import (
    HistoricalRecord,
    HistoricalSource,
    canonical_json_bytes,
    compute_record_id,
    source_snapshot_identity,
)

SOURCE_ID = "aosebench-biomnibench-da-def4dae7"
SOURCE_COMMIT = "def4dae7520807d254612b3590eb32b9aa977924"
SOURCE_TREE = "50e8fe57a14d8f4c89b8357ab91827fe8bfe60ee"
DECISION_SHA256 = "5ade9379443b403714de768de47abb934a1a41e0d58f227028d2602d56ed14f4"
NORMALIZER_ID = "biomnibench-da-def4dae7.v1"

FILES = {
    "results/SNAPSHOT.md": {
        "blob": "449c1e0aee9d837fa86b3760b2af9ff2bf2becd6",
        "sha256": "eb8dfa998da520639973e7dcac574416f3c53ab847ed8131a89b6b034435459e",
        "size": 2462,
    },
    "results/default/_summary/llm_judge_method_summary.tsv": {
        "blob": "01532f1a6f51243d5be0bf60fcc3388808c91848",
        "sha256": "cb17f75051654736c0231302f0531e958701660123ba663c5d9cde11f7027a7c",
        "size": 562,
    },
    "results/default/_summary/llm_judge_summary.tsv": {
        "blob": "0e5d4e04c830b60d1a54b5f4f0171e1c96382c5f",
        "sha256": "af5b16706bf758b79c090ecab73df46d3a23cc1defe6917839a5871b7dd54f5d",
        "size": 35972,
    },
    "results/xhigh/_summary/llm_judge_method_summary.tsv": {
        "blob": "ebee2315c547970d71253ccc350dcf12391ce24e",
        "sha256": "423bd424e5628750035a90d496cfb277b0bc390ba3bd834f370a002cb8029c25",
        "size": 577,
    },
    "results/xhigh/_summary/llm_judge_summary.tsv": {
        "blob": "68e7f39dd8692c904a3a8cc775b5bef2f50b0648",
        "sha256": "1d587f28659a5fc026f8d0b6c14e6ef361991484d24d22cf59725afeee117373",
        "size": 37959,
    },
    "results/live-magenta/_summary/magenta_schedule.json": {
        "blob": "a376dcc06fb427a15ecc884458994b33c1a44582",
        "sha256": "cb8963744e242d3daef7a0716bd7aa12b076029ebc67ed83a9e897313b8e30a4",
        "size": 9867,
    },
    "results/live-magenta/_summary/magenta_task_summary.tsv": {
        "blob": "fb9faff1a859a98071b4fcba8135c3d589b9fcf3",
        "sha256": "ccac90cbf4a5edf9a94e73be1c8320514e670b7215d790f8e42fcbd518f420f4",
        "size": 3528,
    },
    "manifests/data_files.tsv": {
        "blob": "ba728ab894a69dd272267ab355e71660596f3f1e",
        "sha256": "e922e7d71ab729c7439dd7a4409c446f1af924019431532b594756d7dc9dcdaf",
        "size": 37415,
    },
    "docs/EVALUATION.md": {
        "blob": "6220282334cc8a6d52b4761272bc669fe04e3afd",
        "sha256": "700fc06085831998a71f2990fa227c748684ad3dbc06851d592290c2947d653a",
        "size": 1938,
    },
}

REGIMES = ("default", "xhigh")
METHODS = ("Biomni", "PantheonOS", "CellVoyager", "CodexCLI", "BaseLLM")
METHOD_IDS = {
    "Biomni": "biomni",
    "PantheonOS": "pantheonos",
    "CellVoyager": "cellvoyager",
    "CodexCLI": "codexcli",
    "BaseLLM": "basellm",
}
OFFICIAL_EXPECTED = {
    ("default", "Biomni"): (28.58, 15.0, 32.08, 50, 0),
    ("default", "PantheonOS"): (58.18, 58.5, 25.66, 49, 1),
    ("default", "CellVoyager"): (2.40, 0.0, 10.13, 3, 47),
    ("default", "CodexCLI"): (57.10, 60.0, 26.90, 47, 3),
    ("default", "BaseLLM"): (1.16, 0.0, 3.56, 50, 0),
    ("xhigh", "Biomni"): (48.92, 48.0, 33.82, 48, 2),
    ("xhigh", "PantheonOS"): (26.00, 0.0, 34.46, 32, 18),
    ("xhigh", "CellVoyager"): (0.0, 0.0, 0.0, 0, 50),
    ("xhigh", "CodexCLI"): (59.16, 67.0, 32.72, 44, 6),
    ("xhigh", "BaseLLM"): (1.04, 0.0, 4.09, 50, 0),
}
USAGE_CONFLICTS = {
    ("default", "Biomni"): (930007, 0, 85471),
    ("default", "PantheonOS"): (677595, 0, 133953),
    ("default", "CellVoyager"): (22125, 0, 6239),
}
LEGACY_EXPECTED = {
    "OldAose": ("aose", 53.86, 52.0, 22.59854609735515),
    "PureGPT54": ("pure", 25.10, 20.0, 18.958870628552855),
}
PARTIAL_EXPECTED = {
    "Magenta-medium": (10, 5, 3, 3_666_782),
    "Magenta-xhigh": (8, 3, 3, 5_708_573),
}
OFFICIAL_DETAIL_MANIFEST_SHA256 = (
    "5067d4d24ca17f2e4bcf45c990c615f9105ede49bd22c3bdce4c4523989e0be0"
)
LEGACY_GRADE_MANIFEST_SHA256 = (
    "10b992ad83e0999ae008c10787ae980068cacb857db5f0d2a9f5b84df4590ae9"
)

_RECORD_ADAPTER = TypeAdapter(HistoricalRecord)


def _git_blob_oid(content: bytes) -> str:
    header = b"blob " + str(len(content)).encode("ascii") + b"\0"
    return sha1(header + content).hexdigest()


def _entry(root: Path, path: Path) -> tuple[dict[str, Any], bytes]:
    content = path.read_bytes()
    return (
        {
            "path": path.relative_to(root).as_posix(),
            "git_blob_oid": _git_blob_oid(content),
            "content_sha256": sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
        content,
    )


def _manifest_digest(entries: list[dict[str, Any]]) -> str:
    return sha256(canonical_json_bytes(entries)).hexdigest()


def _load_verified_files(root: Path) -> dict[str, bytes]:
    raw: dict[str, bytes] = {}
    for relative, expected in FILES.items():
        content = (root / relative).read_bytes()
        if len(content) != expected["size"]:
            raise ValueError(f"size mismatch for {relative}")
        if sha256(content).hexdigest() != expected["sha256"]:
            raise ValueError(f"SHA-256 mismatch for {relative}")
        if _git_blob_oid(content) != expected["blob"]:
            raise ValueError(f"Git blob mismatch for {relative}")
        raw[relative] = content
    return raw


def _provenance(role: str, path: str) -> dict[str, Any]:
    expected = FILES[path]
    return {
        "role": role,
        "path": path,
        "git_blob_oid": {"algorithm": "sha1", "digest": expected["blob"]},
        "content_sha256": expected["sha256"],
        "size_bytes": expected["size"],
    }


def _entry_provenance(role: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": role,
        "path": entry["path"],
        "git_blob_oid": {
            "algorithm": "sha1",
            "digest": entry["git_blob_oid"],
        },
        "content_sha256": entry["content_sha256"],
        "size_bytes": entry["size_bytes"],
    }


def _tsv(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8")), delimiter="\t"))


def _metric_definition(metric_id: str, source_field: str, policy: str) -> str:
    return sha256(
        canonical_json_bytes(
            {
                "format": "biomnibench-imported-metric-definition-v1",
                "metric_id": metric_id,
                "normalizer_id": NORMALIZER_ID,
                "policy": policy,
                "source_field": source_field,
            }
        )
    ).hexdigest()


def _metric(
    metric_id: str,
    value: int | float | None,
    *,
    source_field: str,
    policy: str,
    unit: str,
    direction: str,
    aggregation: str,
    planned: int,
    observed: int,
    missing: int = 0,
    zero_filled: int = 0,
    state: str = "observed",
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "definition_sha256": _metric_definition(metric_id, source_field, policy),
        "state": state,
        "value": value,
        "unit": unit,
        "direction": direction,
        "aggregation": aggregation,
        "denominator": {
            "unit": "cases",
            "planned_count": planned,
            "observed_count": observed,
            "excluded_count": 0,
        },
        "uncertainty": None,
        "missing_count": missing,
        "invalid_count": 0,
        "zero_filled_count": zero_filled,
    }


def _score_metrics(
    mean: float,
    median: float,
    sample_sd: float,
    *,
    observed: int,
    missing: int,
    legacy: bool = False,
) -> list[dict[str, Any]]:
    missing_only = observed == 0
    state = "missing" if missing_only else "observed"
    values: tuple[float | None, ...] = (
        (None, None, None) if missing_only else (mean, median, sample_sd)
    )
    policy = (
        "aggregate over 50 legacy grade records"
        if legacy
        else "aggregate over 50 tasks; absent outputs are retained as missing and zero-filled by the official scorer"
    )
    specs = (
        ("score-mean", "mean_score_all_tasks", "higher-is-better", "mean"),
        ("score-median", "median_score_all_tasks", "higher-is-better", "median"),
        (
            "score-sample-standard-deviation",
            "per_task_sd_all_tasks",
            "neutral",
            "none",
        ),
    )
    return [
        _metric(
            metric_id,
            value,
            source_field=source_field,
            policy=policy,
            unit="points",
            direction=direction,
            aggregation=aggregation,
            planned=50,
            observed=observed,
            missing=missing,
            zero_filled=0 if legacy else missing,
            state=state,
        )
        for value, (metric_id, source_field, direction, aggregation) in zip(
            values, specs, strict=True
        )
    ]


def _usage_metrics(
    usage: tuple[int, int, int], *, observed: int, missing: int
) -> list[dict[str, Any]]:
    specs = (
        ("judge-total-tokens", "judge_total_tokens_sum"),
        ("judge-cached-tokens", "judge_cached_tokens_sum"),
        ("judge-reasoning-tokens", "judge_reasoning_tokens_sum"),
    )
    return [
        _metric(
            metric_id,
            value,
            source_field=source_field,
            policy="judge usage summed across evaluated outputs; no-output cases contribute zero",
            unit="tokens",
            direction="neutral",
            aggregation="sum",
            planned=50,
            observed=observed,
            missing=missing,
            zero_filled=missing,
        )
        for value, (metric_id, source_field) in zip(usage, specs, strict=True)
    ]


def _descriptor_digest(format_id: str, **values: Any) -> str:
    return sha256(canonical_json_bytes({"format": format_id, **values})).hexdigest()


def _base_factors(
    regime: str, data_count: int, data_bytes: int
) -> list[dict[str, Any]]:
    return [
        {"id": "dataset-byte-count", "value": data_bytes, "unit": "bytes"},
        {"id": "dataset-file-count", "value": data_count, "unit": "files"},
        {"id": "reasoning-effort", "value": regime, "unit": None},
        {"id": "task-memory-profile", "value": "mixed", "unit": None},
        {"id": "task-storage-profile", "value": "mixed", "unit": None},
    ]


def _conditions(
    *,
    experiment_id: str,
    method_id: str,
    method_name: str,
    regime: str,
    model: dict[str, Any] | None,
    mode: str,
    isolation: str,
    purpose: str,
    comparison_group: str,
    protocol_sha: str,
    evaluator_sha: str,
    case_set_sha: str,
    data_count: int,
    data_bytes: int,
    limitations: list[str],
    configuration_sha: str | None = None,
    evaluator_id: str = "biomnibench-da-rubric-judge",
    evaluator_name: str = "BiomniBench DA rubric judge",
    extra_factors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "benchmark": {
            "id": "biomnibench-da",
            "name": "BiomniBench DA",
            "version": "snapshot-def4dae7",
        },
        "dataset": {
            "id": "biomnibench-da-50",
            "name": "BiomniBench DA 50-case test set",
            "version": "snapshot-def4dae7",
            "split": "test",
            "commit_sha": None,
            "content_sha256": None,
        },
        "method": {
            "id": method_id,
            "name": method_name,
            "version": None,
            "subject_id": method_id,
        },
        "model": model,
        "provider": None,
        "harness": {
            "id": evaluator_id,
            "name": evaluator_name,
            "version": "snapshot-def4dae7",
            "protocol_id": f"{evaluator_id}.v1",
            "configuration_sha256": evaluator_sha,
        },
        "evaluator": {
            "id": evaluator_id,
            "name": evaluator_name,
            "version": "snapshot-def4dae7",
            "kind": "model",
            "independent": True,
        },
        "execution": {
            "mode": mode,
            "backend_id": None,
            "isolation": isolation,
            "network_policy": "unknown",
            "case_count": 50,
            "repetitions_per_case": 1,
            "seeds": [],
            "order_policy": "source-defined",
            "hardware": {
                "architecture": "unknown",
                "cpu_count": None,
                "memory_bytes": None,
                "accelerator": None,
                "accelerator_count": None,
            },
            "image_sha256": None,
            "configuration_id": f"biomnibench-{regime}-{method_id}",
            "configuration_sha256": configuration_sha,
            "configuration_profiles": [regime, method_id],
            "factors": [
                *_base_factors(regime, data_count, data_bytes),
                *(extra_factors or []),
            ],
            "budget": None,
        },
        "purpose": purpose,
        "comparability": {
            "status": "conditional",
            "comparison_group": comparison_group,
            "protocol_sha256": protocol_sha,
            "case_set_sha256": case_set_sha,
            "evaluator_sha256": evaluator_sha,
        },
        "limitations": limitations,
    }


def _data_manifest(raw: dict[str, bytes]) -> tuple[int, int]:
    rows = _tsv(raw["manifests/data_files.tsv"])
    paths = [row["relative_path"] for row in rows]
    if len(rows) != 463 or len(paths) != len(set(paths)):
        raise ValueError("expected 463 unique data-manifest paths")
    total_bytes = sum(int(row["size_bytes"]) for row in rows)
    if total_bytes != 82_946_366_733:
        raise ValueError("unexpected data-manifest byte count")
    return len(rows), total_bytes


def _official_details(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    facts: dict[tuple[str, str], dict[str, Any]] = {}
    for regime in REGIMES:
        for method in METHODS:
            method_root = root / "results" / regime / method
            task_dirs = sorted(
                (path for path in method_root.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            )
            if len(task_dirs) != 50:
                raise ValueError(f"expected 50 {regime}/{method} task directories")
            scores: list[float] = []
            statuses: dict[str, str] = {}
            usage = [0, 0, 0]
            for task_root in task_dirs:
                documents: dict[str, Any] = {}
                for filename in (
                    "reward.json",
                    "evaluation.json",
                    "evaluation_status.json",
                ):
                    entry, content = _entry(root, task_root / filename)
                    entries.append(entry)
                    documents[filename] = json.loads(content)
                reward = documents["reward.json"]
                evaluation = documents["evaluation.json"]
                status = documents["evaluation_status.json"]
                score = reward.get("score")
                if score != evaluation.get("total_score") or score != status.get(
                    "score"
                ):
                    raise ValueError(
                        f"score disagreement for {regime}/{method}/{task_root.name}"
                    )
                if (
                    status.get("task") != task_root.name
                    or status.get("method") != method
                ):
                    raise ValueError("evaluation status identity disagreement")
                judge_status = status.get("status")
                if judge_status not in {"evaluated", "no_output"}:
                    raise ValueError("unexpected judge status")
                if judge_status == "no_output" and score != 0:
                    raise ValueError("no-output score must be zero")
                scores.append(float(score))
                statuses[task_root.name] = judge_status
                status_usage = status.get("usage") or {}
                usage[0] += int(status_usage.get("total_tokens") or 0)
                usage[1] += int(
                    (status_usage.get("prompt_tokens_details") or {}).get(
                        "cached_tokens"
                    )
                    or 0
                )
                usage[2] += int(
                    (status_usage.get("completion_tokens_details") or {}).get(
                        "reasoning_tokens"
                    )
                    or 0
                )
            facts[(regime, method)] = {
                "tasks": tuple(path.name for path in task_dirs),
                "scores": tuple(scores),
                "statuses": statuses,
                "usage": tuple(usage),
            }
    if _manifest_digest(entries) != OFFICIAL_DETAIL_MANIFEST_SHA256:
        raise ValueError("official per-task result manifest differs from pinned bytes")
    return facts


def _official_facts(
    root: Path, raw: dict[str, bytes]
) -> tuple[dict[tuple[str, str], dict[str, Any]], tuple[str, ...]]:
    details = _official_details(root)
    case_sets = {fact["tasks"] for fact in details.values()}
    if len(case_sets) != 1:
        raise ValueError("official regimes and methods do not share one case set")
    case_set = next(iter(case_sets))

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for regime in REGIMES:
        method_path = f"results/{regime}/_summary/llm_judge_method_summary.tsv"
        detail_path = f"results/{regime}/_summary/llm_judge_summary.tsv"
        method_rows = {row["method"]: row for row in _tsv(raw[method_path])}
        detail_rows = {
            (row["method"], row["task"]): row for row in _tsv(raw[detail_path])
        }
        if set(method_rows) != set(METHODS) or len(detail_rows) != 250:
            raise ValueError(f"unexpected {regime} summary dimensions")
        for method in METHODS:
            key = (regime, method)
            expected = OFFICIAL_EXPECTED[key]
            detail = details[key]
            row = method_rows[method]
            scores = detail["scores"]
            evaluated = sum(
                status == "evaluated" for status in detail["statuses"].values()
            )
            missing = sum(
                status == "no_output" for status in detail["statuses"].values()
            )
            observed = (
                float(row["mean_score_all_tasks"]),
                float(row["median_score_all_tasks"]),
                float(row["per_task_sd_all_tasks"]),
                int(row["evaluated_outputs"]),
                int(row["no_output_scored_zero"]),
            )
            if observed != expected:
                raise ValueError(f"published aggregate changed for {regime}/{method}")
            if int(row["tasks"]) != 50 or int(row["scores_available"]) != 50:
                raise ValueError("official summary denominator changed")
            if int(row["failed_judge"]) or int(row["missing_eval_files"]):
                raise ValueError("unexpected official judge failure or missing file")
            recomputed = (
                round(statistics.mean(scores), 2),
                round(statistics.median(scores), 2),
                round(statistics.stdev(scores), 2),
                evaluated,
                missing,
            )
            if recomputed != expected:
                raise ValueError(
                    f"per-task recomputation changed for {regime}/{method}"
                )
            for task, status in detail["statuses"].items():
                summary_row = detail_rows[(method, task)]
                if summary_row["judge_status"] != status:
                    raise ValueError("per-task judge summary status disagreement")
                score = scores[detail["tasks"].index(task)]
                if float(summary_row["score"]) != score:
                    raise ValueError("per-task judge summary score disagreement")

            reported_usage = (
                int(row["judge_total_tokens_sum"]),
                int(row["judge_cached_tokens_sum"]),
                int(row["judge_reasoning_tokens_sum"]),
            )
            usage = detail["usage"]
            usage_valid = evaluated > 0
            if key in USAGE_CONFLICTS:
                if reported_usage != (0, 0, 0) or usage != USAGE_CONFLICTS[key]:
                    raise ValueError("expected judge usage conflict changed")
            elif reported_usage != usage:
                raise ValueError(f"judge usage summary changed for {regime}/{method}")
            result[key] = {
                "mean": expected[0],
                "median": expected[1],
                "sample_sd": expected[2],
                "evaluated": evaluated,
                "missing": missing,
                "usage": usage,
                "usage_valid": usage_valid,
                "method_path": method_path,
                "detail_path": detail_path,
            }
    return result, case_set


def _legacy_facts(root: Path, case_set: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    result: dict[str, dict[str, Any]] = {}
    for method, (
        subject,
        expected_mean,
        expected_median,
        expected_sd,
    ) in LEGACY_EXPECTED.items():
        grade_paths = sorted(
            (root / "results" / "default" / method).glob("*/grade.json")
        )
        if len(grade_paths) != 50:
            raise ValueError(f"expected 50 legacy grades for {method}")
        method_entries: list[dict[str, Any]] = []
        scores: list[float] = []
        tasks: list[str] = []
        for path in grade_paths:
            entry, content = _entry(root, path)
            entries.append(entry)
            method_entries.append(entry)
            row = json.loads(content)
            if (
                row.get("subject") != subject
                or row.get("subject_model") != "gpt-5.4"
                or row.get("judge_model") != "configured-model"
                or row.get("grade_mode") != "deepseek"
            ):
                raise ValueError(f"legacy evaluator identity changed for {method}")
            raw_total = float(row["raw_total"])
            if not math.isclose(float(row["score"]) * 100, raw_total, abs_tol=1e-9):
                raise ValueError(f"legacy score scale disagreement for {method}")
            scores.append(raw_total)
            tasks.append(row["task_id"])
        if tuple(sorted(tasks)) != case_set:
            raise ValueError(f"legacy case set changed for {method}")
        observed = (
            statistics.mean(scores),
            statistics.median(scores),
            statistics.stdev(scores),
        )
        expected = (expected_mean, expected_median, expected_sd)
        if any(
            not math.isclose(left, right, rel_tol=0, abs_tol=1e-12)
            for left, right in zip(observed, expected, strict=True)
        ):
            raise ValueError(f"legacy aggregate changed for {method}")
        result[method] = {
            "mean": observed[0],
            "median": observed[1],
            "sample_sd": observed[2],
            "entries": method_entries,
        }
    if _manifest_digest(entries) != LEGACY_GRADE_MANIFEST_SHA256:
        raise ValueError("legacy grade manifest differs from pinned bytes")
    return result


def _partial_facts(
    raw: dict[str, bytes], case_set: tuple[str, ...]
) -> dict[str, dict[str, int]]:
    schedule = json.loads(raw["results/live-magenta/_summary/magenta_schedule.json"])
    if (
        tuple(sorted(schedule["tasks"])) != case_set
        or set(schedule["methods"]) != set(PARTIAL_EXPECTED)
        or schedule["image"] != "biomnibench-da-magenta-0.0.22:latest"
    ):
        raise ValueError("live Magenta schedule identity changed")
    rows = _tsv(raw["results/live-magenta/_summary/magenta_task_summary.tsv"])
    if len(rows) != 18 or len({(row["method"], row["task"]) for row in rows}) != 18:
        raise ValueError("unexpected live Magenta status dimensions")
    result: dict[str, dict[str, int]] = {}
    for method, expected in PARTIAL_EXPECTED.items():
        method_rows = [row for row in rows if row["method"] == method]
        terminal = sum(row["terminal"] == "True" for row in method_rows)
        success = sum(row["ok"] == "True" for row in method_rows)
        timeout = sum(row["timeout"] == "True" for row in method_rows)
        total_tokens = sum(int(row["total_tokens"]) for row in method_rows)
        if (terminal, success, timeout, total_tokens) != expected:
            raise ValueError(f"live Magenta status changed for {method}")
        if any(row["task"] not in case_set for row in method_rows):
            raise ValueError("live Magenta contains an unknown task")
        result[method] = {
            "terminal": terminal,
            "success": success,
            "timeout": timeout,
            "total_tokens": total_tokens,
        }
    return result


def _record(payload: dict[str, Any]):
    payload["record_id"] = compute_record_id(payload)
    return _RECORD_ADAPTER.validate_json(canonical_json_bytes(payload), strict=True)


def build(source_root: Path, output_root: Path) -> None:
    raw = _load_verified_files(source_root)
    data_count, data_bytes = _data_manifest(raw)
    official, case_set = _official_facts(source_root, raw)
    legacy = _legacy_facts(source_root, case_set)
    partial = _partial_facts(raw, case_set)

    normalizer_sha = sha256(Path(__file__).read_bytes()).hexdigest()
    source = HistoricalSource.model_validate(
        {
            "source_id": SOURCE_ID,
            "repository": "Minions-Land/AOSEBench",
            "commit_sha": SOURCE_COMMIT,
            "root_tree": {"algorithm": "sha1", "digest": SOURCE_TREE},
            "normalizer_id": NORMALIZER_ID,
            "normalizer_sha256": normalizer_sha,
            "visibility": "private",
            "license_status": "not-detected",
            "license_id": None,
            "ref_hint": "main",
            "publication_approval": {
                "approval_id": "github-issue-85",
                "approved_by": "PoorOtterBob",
                "approved_at": date(2026, 8, 16),
                "decision_ref": "Minions-Land/MagentaBenchmark#85",
                "decision_sha256": DECISION_SHA256,
                "destination_repository": "Minions-Land/MagentaBenchmark",
                "scope": "typed-results-only",
            },
        }
    )
    snapshot_sha = source_snapshot_identity(source)
    case_set_sha = sha256(canonical_json_bytes(case_set)).hexdigest()
    evaluator_sha = FILES["docs/EVALUATION.md"]["sha256"]
    records = []

    official_limitations = [
        "aggregate-only",
        "budget-unbound",
        "dataset-content-unbound",
        "dataset-revision-unbound",
        "image-digest-unbound",
        "legacy-not-bmp-verified",
        "mixed-task-memory-profile",
        "mixed-task-storage-profile",
        "model-identity-redacted",
        "no-run-repetitions",
        "provider-identity-redacted",
        "source-private-approved-projection",
    ]
    for regime in REGIMES:
        for method in METHODS:
            fact = official[(regime, method)]
            limitations = list(official_limitations)
            if (regime, method) in USAGE_CONFLICTS:
                limitations.append("judge-usage-summary-conflicts-per-task-status")
            metrics = _score_metrics(
                fact["mean"],
                fact["median"],
                fact["sample_sd"],
                observed=fact["evaluated"],
                missing=fact["missing"],
            )
            if fact["usage_valid"]:
                metrics.extend(
                    _usage_metrics(
                        fact["usage"],
                        observed=fact["evaluated"],
                        missing=fact["missing"],
                    )
                )
            method_id = METHOD_IDS[method]
            run_id = f"biomnibench-da-{regime}-{method_id}"
            records.append(
                _record(
                    {
                        "kind": "run",
                        "source_id": SOURCE_ID,
                        "source_snapshot_sha256": snapshot_sha,
                        "logical_key": run_id,
                        "supersedes": [],
                        "evidence_tier": "legacy-evaluated",
                        "claim_eligible": False,
                        "provenance": [
                            _provenance("declaration", "results/SNAPSHOT.md"),
                            _provenance("dataset", "manifests/data_files.tsv"),
                            _provenance("evaluator", "docs/EVALUATION.md"),
                            _provenance("metric", fact["method_path"]),
                            _provenance("result", fact["detail_path"]),
                        ],
                        "experiment": _conditions(
                            experiment_id="biomnibench-da-official-20260716",
                            method_id=method_id,
                            method_name=method,
                            regime=regime,
                            model=None,
                            mode="docker",
                            isolation="container",
                            purpose="evaluation",
                            comparison_group="biomnibench-da-official-rubric-20260716",
                            protocol_sha=evaluator_sha,
                            evaluator_sha=evaluator_sha,
                            case_set_sha=case_set_sha,
                            data_count=data_count,
                            data_bytes=data_bytes,
                            limitations=limitations,
                        ),
                        "run_id": run_id,
                        "parent_run_id": None,
                        "terminal_state": "completed",
                        "metrics": metrics,
                    }
                )
            )

    legacy_protocol_sha = _descriptor_digest(
        "biomnibench-legacy-grade-protocol-v1",
        grade_mode="deepseek",
        judge_model="redacted",
        score_field="raw_total",
    )
    for method in ("OldAose", "PureGPT54"):
        method_id = "old-aose" if method == "OldAose" else "pure-gpt54"
        fact = legacy[method]
        run_id = f"biomnibench-da-legacy-{method_id}"
        records.append(
            _record(
                {
                    "kind": "run",
                    "source_id": SOURCE_ID,
                    "source_snapshot_sha256": snapshot_sha,
                    "logical_key": run_id,
                    "supersedes": [],
                    "evidence_tier": "legacy-evaluated",
                    "claim_eligible": False,
                    "provenance": [
                        _provenance("declaration", "results/SNAPSHOT.md"),
                        _provenance("dataset", "manifests/data_files.tsv"),
                        *(
                            _entry_provenance("metric", entry)
                            for entry in fact["entries"]
                        ),
                    ],
                    "experiment": _conditions(
                        experiment_id="biomnibench-da-legacy-grades",
                        method_id=method_id,
                        method_name=method,
                        regime="legacy",
                        model={
                            "id": "gpt-5.4",
                            "name": "gpt-5.4",
                            "version": None,
                            "revision": None,
                        },
                        mode="unknown",
                        isolation="unknown",
                        purpose="evaluation",
                        comparison_group="biomnibench-da-legacy-deepseek-grades",
                        protocol_sha=legacy_protocol_sha,
                        evaluator_sha=legacy_protocol_sha,
                        case_set_sha=case_set_sha,
                        data_count=data_count,
                        data_bytes=data_bytes,
                        evaluator_id="biomnibench-da-legacy-deepseek-judge",
                        evaluator_name="Legacy BiomniBench DA rubric judge",
                        limitations=[
                            "aggregate-only",
                            "budget-unbound",
                            "dataset-content-unbound",
                            "dataset-revision-unbound",
                            "evaluator-lineage-distinct",
                            "execution-conditions-unbound",
                            "judge-model-identity-redacted",
                            "legacy-not-bmp-verified",
                            "model-revision-unbound",
                            "no-run-repetitions",
                            "provider-identity-redacted",
                            "source-private-approved-projection",
                        ],
                    ),
                    "run_id": run_id,
                    "parent_run_id": None,
                    "terminal_state": "completed",
                    "metrics": _score_metrics(
                        fact["mean"],
                        fact["median"],
                        fact["sample_sd"],
                        observed=50,
                        missing=0,
                        legacy=True,
                    ),
                }
            )
        )

    live_schedule_sha = FILES["results/live-magenta/_summary/magenta_schedule.json"][
        "sha256"
    ]
    for method in ("Magenta-medium", "Magenta-xhigh"):
        regime = method.removeprefix("Magenta-")
        method_id = "magenta"
        run_id = f"biomnibench-da-magenta-{regime}"
        fact = partial[method]
        configuration_sha = _descriptor_digest(
            "biomnibench-magenta-live-configuration-v1",
            schedule_sha256=live_schedule_sha,
            method=method,
            terminal_count=fact["terminal"],
            success_count=fact["success"],
            timeout_count=fact["timeout"],
        )
        records.append(
            _record(
                {
                    "kind": "run",
                    "source_id": SOURCE_ID,
                    "source_snapshot_sha256": snapshot_sha,
                    "logical_key": run_id,
                    "supersedes": [],
                    "evidence_tier": "candidate",
                    "claim_eligible": False,
                    "provenance": [
                        _provenance("declaration", "results/SNAPSHOT.md"),
                        _provenance("dataset", "manifests/data_files.tsv"),
                        _provenance(
                            "configuration",
                            "results/live-magenta/_summary/magenta_schedule.json",
                        ),
                        _provenance(
                            "result",
                            "results/live-magenta/_summary/magenta_task_summary.tsv",
                        ),
                    ],
                    "experiment": _conditions(
                        experiment_id="biomnibench-da-magenta-live-20260716",
                        method_id=method_id,
                        method_name="Magenta",
                        regime=regime,
                        model=None,
                        mode="docker",
                        isolation="container",
                        purpose="exploratory",
                        comparison_group="biomnibench-da-magenta-live-20260716",
                        protocol_sha=live_schedule_sha,
                        evaluator_sha=evaluator_sha,
                        case_set_sha=case_set_sha,
                        data_count=data_count,
                        data_bytes=data_bytes,
                        configuration_sha=configuration_sha,
                        extra_factors=[
                            {
                                "id": "other-terminal-error-count",
                                "value": fact["terminal"]
                                - fact["success"]
                                - fact["timeout"],
                                "unit": "cases",
                            },
                            {
                                "id": "recorded-total-token-count",
                                "value": fact["total_tokens"],
                                "unit": "tokens",
                            },
                            {
                                "id": "successful-case-count",
                                "value": fact["success"],
                                "unit": "cases",
                            },
                            {
                                "id": "terminal-case-count",
                                "value": fact["terminal"],
                                "unit": "cases",
                            },
                            {
                                "id": "timeout-case-count",
                                "value": fact["timeout"],
                                "unit": "cases",
                            },
                        ],
                        limitations=[
                            "aggregate-only",
                            "budget-unbound",
                            "dataset-content-unbound",
                            "dataset-revision-unbound",
                            "evaluation-not-run",
                            "floating-image-reference",
                            "image-digest-unbound",
                            "in-progress-snapshot",
                            "legacy-not-bmp-verified",
                            "metrics-unavailable",
                            "mixed-task-memory-profile",
                            "mixed-task-storage-profile",
                            "model-identity-redacted",
                            "no-run-repetitions",
                            "provider-identity-redacted",
                            "source-private-approved-projection",
                        ],
                    ),
                    "run_id": run_id,
                    "parent_run_id": None,
                    "terminal_state": "partial",
                    "metrics": [],
                }
            )
        )

    if len(records) != 14:
        raise ValueError("expected exactly 14 BiomniBench run records")
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError(f"output directory is not empty: {output_root}")
    records_root = output_root / "records"
    records_root.mkdir(parents=True, exist_ok=True)
    (output_root / "source.json").write_text(
        json.dumps(source.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for record in records:
        (records_root / f"{record.record_id}.json").write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    build(args.source_root.resolve(), args.output_root.resolve())


if __name__ == "__main__":
    main()
