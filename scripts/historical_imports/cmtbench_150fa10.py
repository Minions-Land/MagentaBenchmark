#!/usr/bin/env python3
"""Build the approved CMTBench aggregate projection from pinned JSON bytes."""

from __future__ import annotations

import argparse
import json
from datetime import date
from hashlib import sha256
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

SOURCE_ID = "minionsos2-cmtbench-150fa10"
SOURCE_COMMIT = "150fa100ead4ab51acdfc24ed246a8c5b2141466"
SOURCE_TREE = "3deaec22a778564ae37cbea396765268f959fee5"
DECISION_SHA256 = "5ade9379443b403714de768de47abb934a1a41e0d58f227028d2602d56ed14f4"
NORMALIZER_ID = "cmtbench-150fa10.v1"

FILES = {
    "experiment_logs/cmt_bench/summary.json": {
        "blob": "7dd842535fec63c529348efe4bc9bddfb14e8032",
        "sha256": "e710a83605a5a22be0313fdaa0ea13f7b08a205d0efce3fa354cbd498c2e8a2d",
        "size": 3636,
    },
    "experiment_logs/cmt_bench/validation/summary.json": {
        "blob": "266e120fe437947e52b22afba16ab535badde158",
        "sha256": "453bd8d9848cff29b1ff0973245bebf94f57282dd17fc9db6f6ef69cee6489a3",
        "size": 1734,
    },
    "experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/branch_summary.json": {
        "blob": "98397c375c77d10ec873c85149f8b677c3259e39",
        "sha256": "6c64f4a8769f1d391035e7cfe0a83e0a90499a2bf6e598da0535daa34f7ba4d7",
        "size": 61884,
    },
    "experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/model_summary.json": {
        "blob": "f8210482d575bf66e5121dda4a15fb7562b12621",
        "sha256": "c299264adf7031d65c5dcc3b550db10e1653411e4e6a57e6fd049a02412b051d",
        "size": 7807,
    },
    "experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/environment.json": {
        "blob": "820e8f0dec9fbcc281e312bda1d447a5de8d5809",
        "sha256": "723e2f453f3199d73b9b7e8bd24054d0813a9c927d3a5880c529ad7fdf20f6bb",
        "size": 1131,
    },
}

MODEL_IDS = {
    "claude-sonnet-4-6[1m]": "claude-sonnet-4-6-1m",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.5": "gpt-5.5",
}
BRANCHES = {
    "DMRG": 4,
    "ED": 8,
    "HF": 5,
    "Other": 16,
    "PEPS": 3,
    "QMC": 6,
    "SM": 6,
    "VMC": 2,
}
_RECORD_ADAPTER = TypeAdapter(HistoricalRecord)


def _load_verified(root: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    raw: dict[str, bytes] = {}
    parsed: dict[str, Any] = {}
    for relative, expected in FILES.items():
        content = (root / relative).read_bytes()
        if len(content) != expected["size"]:
            raise ValueError(f"size mismatch for {relative}")
        if sha256(content).hexdigest() != expected["sha256"]:
            raise ValueError(f"SHA-256 mismatch for {relative}")
        raw[relative] = content
        parsed[relative] = json.loads(content)
    return raw, parsed


def _provenance(role: str, path: str) -> dict[str, Any]:
    expected = FILES[path]
    return {
        "role": role,
        "path": path,
        "git_blob_oid": {"algorithm": "sha1", "digest": expected["blob"]},
        "content_sha256": expected["sha256"],
        "size_bytes": expected["size"],
    }


def _definition(metric_id: str, source_field: str, policy: str) -> str:
    return sha256(
        canonical_json_bytes(
            {
                "format": "cmtbench-imported-metric-definition-v1",
                "metric_id": metric_id,
                "normalizer_id": NORMALIZER_ID,
                "policy": policy,
                "source_field": source_field,
            }
        )
    ).hexdigest()


def _metric(
    metric_id: str,
    value: int | float,
    *,
    source_field: str,
    unit: str,
    direction: str,
    aggregation: str,
    planned: int,
    observed: int,
    invalid: int = 0,
    zero_filled: int = 0,
    denominator_unit: str = "cases",
    policy: str,
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "definition_sha256": _definition(metric_id, source_field, policy),
        "state": "observed",
        "value": value,
        "unit": unit,
        "direction": direction,
        "aggregation": aggregation,
        "denominator": {
            "unit": denominator_unit,
            "planned_count": planned,
            "observed_count": observed,
            "excluded_count": 0,
        },
        "uncertainty": None,
        "missing_count": 0,
        "invalid_count": invalid,
        "zero_filled_count": zero_filled,
    }


def _accuracy_metrics(row: dict[str, Any], *, suffix: str = "") -> list[dict[str, Any]]:
    total = row["total"]
    result = []
    for prefix, success_field, failure_field in (
        ("adopted", "adopted_success", "unresolved_parser_failures"),
        ("raw-official", "raw_official_success", "raw_parser_failures"),
    ):
        source_prefix = prefix.replace("-", "_")
        metric_id = f"{prefix}-accuracy{suffix}"
        invalid = row[failure_field]
        result.append(
            _metric(
                metric_id,
                row[f"{source_prefix}_accuracy"],
                source_field=f"{source_prefix}_accuracy",
                unit="fraction",
                direction="higher-is-better",
                aggregation="rate",
                planned=total,
                observed=row[success_field],
                invalid=invalid,
                zero_filled=invalid,
                policy="parser failures remain in the denominator and score zero",
            )
        )
    result.append(
        _metric(
            f"legacy-accuracy{suffix}",
            row["legacy_accuracy"],
            source_field="legacy_accuracy",
            unit="fraction",
            direction="higher-is-better",
            aggregation="rate",
            planned=total,
            observed=total,
            policy="legacy verdict accuracy over every planned case",
        )
    )
    return result


def _diagnostic_metrics(row: dict[str, Any]) -> list[dict[str, Any]]:
    total = row["total"]
    specs = (
        (
            "adopted-success-rate",
            "adopted_success_rate",
            "fraction",
            "higher-is-better",
            "rate",
        ),
        (
            "raw-official-success-rate",
            "raw_official_success_rate",
            "fraction",
            "higher-is-better",
            "rate",
        ),
        (
            "unresolved-parser-failures",
            "unresolved_parser_failures",
            "count",
            "lower-is-better",
            "sum",
        ),
        (
            "raw-parser-failures",
            "raw_parser_failures",
            "count",
            "lower-is-better",
            "sum",
        ),
        ("format-repair-attempts", "format_repair_attempts", "count", "neutral", "sum"),
        ("format-repair-success", "format_repair_success", "count", "neutral", "sum"),
        ("format-repair-correct", "format_repair_correct", "count", "neutral", "sum"),
        ("changed-vs-legacy", "changed_vs_legacy", "count", "neutral", "sum"),
    )
    return [
        _metric(
            metric_id,
            row[field],
            source_field=field,
            unit=unit,
            direction=direction,
            aggregation=aggregation,
            planned=total,
            observed=total,
            policy="aggregate diagnostic over all planned cases",
        )
        for metric_id, field, unit, direction, aggregation in specs
    ]


def _resource_metrics(row: dict[str, Any]) -> list[dict[str, Any]]:
    specs = (
        ("work-time-seconds", "work_time_seconds", "seconds", "lower-is-better"),
        ("estimated-cost-usd", "cost_usd", "usd", "lower-is-better"),
        ("api-calls", "api_calls", "count", "neutral"),
        ("input-tokens", "input_tokens", "tokens", "neutral"),
        ("cache-read-input-tokens", "cache_read_input_tokens", "tokens", "neutral"),
        (
            "cache-creation-input-tokens",
            "cache_creation_input_tokens",
            "tokens",
            "neutral",
        ),
        ("cached-input-tokens", "cached_input_tokens", "tokens", "neutral"),
        ("output-tokens", "output_tokens", "tokens", "neutral"),
        ("reasoning-output-tokens", "reasoning_output_tokens", "tokens", "neutral"),
        (
            "total-tokens-excluding-cache-read",
            "total_tokens_excluding_cache_read",
            "tokens",
            "neutral",
        ),
        (
            "total-tokens-including-cache-read",
            "total_tokens_including_cache_read",
            "tokens",
            "neutral",
        ),
    )
    return [
        _metric(
            metric_id,
            row[field],
            source_field=field,
            unit=unit,
            direction=direction,
            aggregation="sum",
            planned=1,
            observed=1,
            denominator_unit="items",
            policy="source-reported aggregate with provider-specific coverage",
        )
        for metric_id, field, unit, direction in specs
        if row[field] is not None
    ]


def _conditions(
    row: dict[str, Any], environment_sha: str, case_set_sha: str
) -> dict[str, Any]:
    method = row["method"]
    model_name = row["model"]
    return {
        "experiment_id": "cmtbench-corrected-regrade-20260708",
        "benchmark": {"id": "cmtbench", "name": "CMTBench", "version": "2026-07-08"},
        "dataset": {
            "id": "cmtbench-50",
            "name": "CMTBench 50-task set",
            "version": "snapshot-150fa10",
            "split": "test",
            "commit_sha": None,
            "content_sha256": None,
        },
        "method": {"id": method, "name": method, "version": None, "subject_id": method},
        "model": {
            "id": MODEL_IDS[model_name],
            "name": model_name,
            "version": None,
            "revision": None,
        },
        "provider": None,
        "harness": {
            "id": "cmt-official-eval-format-repair-v1",
            "name": "CMT official evaluator with deterministic format repair",
            "version": "1",
            "protocol_id": "cmt-official-eval-format-repair.v1",
            "configuration_sha256": environment_sha,
        },
        "evaluator": {
            "id": "cmt-official-eval-format-repair-v1",
            "name": "CMT official evaluator with deterministic format repair",
            "version": "1",
            "kind": "deterministic",
            "independent": True,
        },
        "execution": {
            "mode": "unknown",
            "backend_id": None,
            "isolation": "unknown",
            "network_policy": "unknown",
            "case_count": 50,
            "repetitions_per_case": 1,
            "seeds": [],
            "order_policy": "unknown",
            "hardware": {
                "architecture": "unknown",
                "cpu_count": None,
                "memory_bytes": None,
                "accelerator": None,
                "accelerator_count": None,
            },
            "image_sha256": None,
            "configuration_id": f"cmtbench-150fa10-{method}",
            "configuration_sha256": sha256(canonical_json_bytes(row)).hexdigest(),
            "configuration_profiles": [method],
            "factors": [
                {
                    "id": "regrade-pipeline",
                    "value": "cmt-official-eval-format-repair-v1",
                    "unit": None,
                }
            ],
            "budget": None,
        },
        "purpose": "evaluation",
        "comparability": {
            "status": "conditional",
            "comparison_group": "cmtbench-corrected-regrade-20260708",
            "protocol_sha256": environment_sha,
            "case_set_sha256": case_set_sha,
            "evaluator_sha256": environment_sha,
        },
        "limitations": [
            "aggregate-only",
            "dataset-revision-unbound",
            "evaluator-upstream-unbound",
            "legacy-not-bmp-verified",
            "mixed-run-configuration",
            "model-revision-unbound",
            "resource-accounting-provider-specific",
            "source-private-approved-projection",
        ],
    }


def build(source_root: Path, output_root: Path) -> None:
    raw, documents = _load_verified(source_root)
    model_path = "experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/model_summary.json"
    branch_path = "experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/branch_summary.json"
    environment_path = "experiment_logs/cmt_bench/evaluation/regrade_reports/20260708-162616/environment.json"
    model_rows = documents[model_path]
    branch_rows = documents[branch_path]
    if len(model_rows) != 8 or len(branch_rows) != 64:
        raise ValueError("expected exactly 8 model rows and 64 method/branch rows")
    if {row["method"] for row in model_rows} != {row["method"] for row in branch_rows}:
        raise ValueError("method summaries and branch summaries disagree")
    for row in model_rows:
        if row["total"] != 50 or row["model"] not in MODEL_IDS:
            raise ValueError("unexpected model summary identity or denominator")
    observed_branches = {
        (row["method"], row["branch"]): row["total"] for row in branch_rows
    }
    expected_branches = {
        (row["method"], branch): total
        for row in model_rows
        for branch, total in BRANCHES.items()
    }
    if observed_branches != expected_branches:
        raise ValueError("branch denominators differ from the audited 50-task set")

    normalizer_sha = sha256(Path(__file__).read_bytes()).hexdigest()
    source = HistoricalSource.model_validate(
        {
            "source_id": SOURCE_ID,
            "repository": "Minions-Land/MinionsOS2-Bench",
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
    environment_sha = sha256(raw[environment_path]).hexdigest()
    case_set_path = "experiment_logs/cmt_bench/validation/summary.json"
    case_set_sha = sha256(raw[case_set_path]).hexdigest()
    branches_by_method: dict[str, list[dict[str, Any]]] = {}
    for row in branch_rows:
        branches_by_method.setdefault(row["method"], []).append(row)

    records = []
    for row in model_rows:
        method = row["method"]
        metrics = (
            _accuracy_metrics(row) + _diagnostic_metrics(row) + _resource_metrics(row)
        )
        for branch_row in sorted(
            branches_by_method[method], key=lambda item: item["branch"]
        ):
            suffix = f"-{branch_row['branch'].lower()}"
            metrics.extend(_accuracy_metrics(branch_row, suffix=suffix))
        run_id = f"cmtbench-20260708-{method.replace('_', '-')}"
        payload: dict[str, Any] = {
            "kind": "run",
            "source_id": SOURCE_ID,
            "source_snapshot_sha256": snapshot_sha,
            "logical_key": run_id,
            "supersedes": [],
            "evidence_tier": "legacy-evaluated",
            "claim_eligible": False,
            "provenance": [
                _provenance("result", "experiment_logs/cmt_bench/summary.json"),
                _provenance("dataset", case_set_path),
                _provenance("metric", model_path),
                _provenance("metric", branch_path),
                _provenance("configuration", environment_path),
            ],
            "experiment": _conditions(row, environment_sha, case_set_sha),
            "run_id": run_id,
            "parent_run_id": None,
            "terminal_state": "completed",
            "metrics": metrics,
        }
        payload["record_id"] = compute_record_id(payload)
        records.append(
            _RECORD_ADAPTER.validate_json(canonical_json_bytes(payload), strict=True)
        )

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
