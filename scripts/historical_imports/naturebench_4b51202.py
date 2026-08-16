#!/usr/bin/env python3
"""Build the approved NatureBench declaration and reference projection."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date
from hashlib import sha256
from io import StringIO
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

SOURCE_ID = "aosebench-naturebench-4b51202"
SOURCE_COMMIT = "4b512029f3ad37746502ce377e4fcc2027fd46db"
SOURCE_TREE = "e11636f88a5d74e9cb4dcaa06518b9a3a71c87ea"
DECISION_SHA256 = "5ade9379443b403714de768de47abb934a1a41e0d58f227028d2602d56ed14f4"
NORMALIZER_ID = "naturebench-4b51202.v1"

FILES = {
    "README.md": {
        "blob": "5e2484a6e6b49c494d488ddd5698320bae159afa",
        "sha256": "a28950e7d0d35805d70c697d119b81632b2d1c5ed7fa39b88f542abed7b7a024",
        "size": 2529,
    },
    "bench-env.template.sh": {
        "blob": "6177723a5079e6f26d1c03a7fb0fbdb6a7377ddc",
        "sha256": "b1b40d91e4d21c2848ff1cfb4fd22ff156e02f632631fbe9edc3ffbfed2d84d7",
        "size": 10671,
    },
    "docs/REBUILD.md": {
        "blob": "14f1cd8239607c46fa427a3f3e0a0e1f1afdf07c",
        "sha256": "f9c80ffa6fb92ed83157432678015d56f7b9b8287611caa845a8a8339fd6cbec",
        "size": 13424,
    },
    "docs/OPUS48_DEV_COHORT.md": {
        "blob": "9455a168bf645822ba16e443d0b25363936990ae",
        "sha256": "ed777f3a09be409db1df00864ee5478613c03d9b7be18320c299eacf5740bdd0",
        "size": 7363,
    },
    "scripts/aggregate_results.py": {
        "blob": "c302cc6d46f54e73bd7fa09883f875fc8f2dce23",
        "sha256": "eb86495388fd19c61992b73ce8a409f622f4ee8a94ce7c3fe84e7bbfa82c3b2f",
        "size": 11725,
    },
    "task-set/cellomics.txt": {
        "blob": "18c02dafc5759479c2d91904402b6ce3099e1cd9",
        "sha256": "10020a26ab2c5d510a916a72db668123396555ec5352661d79f856370f084ac2",
        "size": 589,
    },
    "manifests/tasks.tsv": {
        "blob": "f5821c63e3575fefb501c6e02d3c5cfeddf91cfb",
        "sha256": "24d667df9d14a433a460a6115e6c4c24e8d6b7186cc2c528be648c199f7dfc09",
        "size": 5829,
    },
    "vendor/naturebench/VENDOR.md": {
        "blob": "8611acec9c7fbe3fd206b7c0c8335ecb887f87e1",
        "sha256": "eefa3fccf5db57e36e1bb506016032df9062e212d6dcacf8d2ff2ffc7baa6f15",
        "size": 5467,
    },
    "vendor/naturebench/submit_results/compute_scores.py": {
        "blob": "860dfb66603572dd1720e1e4244f688af7275044",
        "sha256": "a8a4a3579548b9097f063d2575dcf9452e81e74a79cda763a8766039026bef2e",
        "size": 9392,
    },
    "vendor/naturebench/submit_results/case_metadata.csv": {
        "blob": "694778143483d629a12df5e878c766378f89e8da",
        "sha256": "85a064dc6389ce4148667701cfc99b92e330618811975a8288254b8b78e59145",
        "size": 3252,
    },
}

METRIC_IDS = (
    "completion-rate",
    "match-sota",
    "mean-g-all",
    "median-g-all",
    "median-g-valid",
    "score-rate",
    "surpass-sota",
)

DECLARATIONS = (
    {
        "slug": "codex",
        "method_id": "codex-cli",
        "method_name": "Codex CLI",
        "model_id": "openai-gpt-5.5",
        "model_name": "openai/gpt-5.5",
        "provider_id": "openrouter",
        "provider_name": "OpenRouter",
    },
    {
        "slug": "pantheonos",
        "method_id": "pantheonos",
        "method_name": "PantheonOS",
        "model_id": "openai-gpt-5.5",
        "model_name": "openai/gpt-5.5",
        "provider_id": "openrouter",
        "provider_name": "OpenRouter",
    },
    {
        "slug": "biomni",
        "method_id": "biomni",
        "method_name": "Biomni",
        "model_id": "openai-gpt-5.5",
        "model_name": "openai/gpt-5.5",
        "provider_id": "openrouter",
        "provider_name": "OpenRouter",
    },
    {
        "slug": "magenta-aose",
        "method_id": "magenta-aose",
        "method_name": "Magenta+aose",
        "model_id": "openai-gpt-5.5",
        "model_name": "openai/gpt-5.5",
        "provider_id": "openrouter",
        "provider_name": "OpenRouter",
    },
    {
        "slug": "magenta-aose-opus48",
        "method_id": "magenta-aose",
        "method_name": "Magenta+aose",
        "model_id": "claude-opus-4-8",
        "model_name": "Claude Opus 4.8",
        "provider_id": "anthropic",
        "provider_name": "Anthropic subscription",
        "development": True,
    },
)

REFERENCE_ROWS = (
    {
        "slug": "opus47-claude-code",
        "model_id": "claude-opus-4-7",
        "model_name": "Opus 4.7",
        "method_id": "claude-code",
        "method_name": "Claude Code",
        "surpass_sota": 19.4,
        "match_sota": 45.2,
        "completion_rate": 87.1,
        "median_g_all": -0.011,
    },
    {
        "slug": "gpt54-codex",
        "model_id": "gpt-5.4",
        "model_name": "GPT-5.4",
        "method_id": "codex",
        "method_name": "Codex",
        "surpass_sota": 9.7,
        "match_sota": 22.6,
        "completion_rate": 64.5,
        "median_g_all": -0.148,
    },
    {
        "slug": "deepseek-v4-pro-claude-code",
        "model_id": "deepseek-v4-pro",
        "model_name": "DeepSeek-V4-Pro",
        "method_id": "claude-code",
        "method_name": "Claude Code",
        "surpass_sota": 9.7,
        "match_sota": 29.0,
        "completion_rate": 83.9,
        "median_g_all": -0.261,
    },
)

_RECORD_ADAPTER = TypeAdapter(HistoricalRecord)


def _load_verified(root: Path) -> dict[str, bytes]:
    raw: dict[str, bytes] = {}
    for relative, expected in FILES.items():
        content = (root / relative).read_bytes()
        if len(content) != expected["size"]:
            raise ValueError(f"size mismatch for {relative}")
        if sha256(content).hexdigest() != expected["sha256"]:
            raise ValueError(f"SHA-256 mismatch for {relative}")
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


def _validate_source_facts(raw: dict[str, bytes]) -> None:
    cases = [
        line.strip()
        for line in raw["task-set/cellomics.txt"].decode().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if len(cases) != 31 or len(set(cases)) != 31:
        raise ValueError("expected 31 unique Cellular Omics cases")

    metadata = {
        row["case_id"]: row["domain"]
        for row in csv.DictReader(
            StringIO(
                raw["vendor/naturebench/submit_results/case_metadata.csv"].decode()
            )
        )
    }
    if {metadata.get(case) for case in cases} != {"Cellular Omics"}:
        raise ValueError(
            "the selected cases are not the complete Cellular Omics domain"
        )

    task_rows = list(
        csv.DictReader(StringIO(raw["manifests/tasks.tsv"].decode()), delimiter="\t")
    )
    if {row["case_id"] for row in task_rows} != set(cases):
        raise ValueError("task manifest and Cellular Omics case set disagree")
    if Counter(row["tier"] for row in task_rows) != Counter(
        {"cpu": 3, "gpu_low": 24, "gpu_high": 4}
    ):
        raise ValueError("unexpected CPU/GPU tier counts")

    readme = raw["README.md"].decode()
    for fact in (
        "31 CellularOmics tasks",
        "PantheonOS",
        "Codex CLI",
        "Biomni",
        "Magenta+aose",
    ):
        if fact not in readme:
            raise ValueError(f"README declaration missing: {fact}")

    environment = raw["bench-env.template.sh"].decode()
    for fact in (
        "BBDA_MODEL:-openai/gpt-5.5",
        "BBDA_PROVIDER:-openrouter",
        "JUDGE_MODEL:-openai/gpt-5.5",
        "AOSE_AGENT_TIMEOUT_SEC:-14400",
    ):
        if fact not in environment:
            raise ValueError(f"configuration declaration missing: {fact}")

    development = raw["docs/OPUS48_DEV_COHORT.md"].decode()
    for fact in ("Claude Opus 4.8", "It is not a benchmark result"):
        if fact not in development:
            raise ValueError(f"development-cohort declaration missing: {fact}")

    aggregate = raw["scripts/aggregate_results.py"].decode()
    for fact in (
        "g > 0.1",
        "invalid and unscored counted as g = -1",
        'DEFAULT_COHORTS = ["codex", "PantheonOS", "Biomni", "Magenta-aose"]',
    ):
        if fact not in aggregate:
            raise ValueError(f"metric definition missing: {fact}")

    table = raw["docs/REBUILD.md"].decode().replace("\u2212", "-")
    for row in REFERENCE_ROWS:
        expected = (
            f"| {row['model_name']} | {row['method_name']} | "
            f"{row['surpass_sota']:.1f} % | {row['match_sota']:.1f} % | "
            f"{row['completion_rate']:.1f} % | {row['median_g_all']:.3f} |"
        )
        if table.count(expected) != 1:
            raise ValueError(
                f"reference aggregate row missing or duplicated: {row['slug']}"
            )


def _definition(metric_id: str, source_field: str, policy: str) -> str:
    return sha256(
        canonical_json_bytes(
            {
                "format": "naturebench-imported-metric-definition-v1",
                "metric_id": metric_id,
                "normalizer_id": NORMALIZER_ID,
                "policy": policy,
                "source_field": source_field,
            }
        )
    ).hexdigest()


def _metric(
    metric_id: str,
    value: float,
    *,
    source_field: str,
    unit: str,
    aggregation: str,
    policy: str,
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "definition_sha256": _definition(metric_id, source_field, policy),
        "state": "observed",
        "value": value,
        "unit": unit,
        "direction": "higher-is-better",
        "aggregation": aggregation,
        "denominator": {
            "unit": "cases",
            "planned_count": 31,
            "observed_count": 31,
            "excluded_count": 0,
        },
        "uncertainty": None,
        "missing_count": 0,
        "invalid_count": 0,
        "zero_filled_count": 0,
    }


def _dataset() -> dict[str, Any]:
    return {
        "id": "naturebench-cellular-omics-31",
        "name": "NatureBench Cellular Omics 31-case domain",
        "version": "source-task-set-4b51202",
        "split": "Cellular Omics",
        "commit_sha": None,
        "content_sha256": None,
    }


def _hardware() -> dict[str, Any]:
    return {
        "architecture": "unknown",
        "cpu_count": None,
        "memory_bytes": None,
        "accelerator": None,
        "accelerator_count": None,
    }


def _declaration_conditions(spec: dict[str, Any]) -> dict[str, Any]:
    development = bool(spec.get("development"))
    experiment_id = f"naturebranch-cellular-omics-{spec['slug']}"
    configuration = {
        "experiment_id": experiment_id,
        "method": spec["method_id"],
        "model": spec["model_id"],
        "provider": spec["provider_id"],
        "runtime": "rootless-apptainer",
        "task_count": 31,
        "timeout_seconds": 14400,
        "web_search": False,
    }
    factors = [
        {"id": "agent-timeout", "value": 14400, "unit": "seconds"},
        {"id": "cpu-cases", "value": 3, "unit": "cases"},
        {"id": "gpu-high-cases", "value": 4, "unit": "cases"},
        {"id": "gpu-low-cases", "value": 24, "unit": "cases"},
        {"id": "judge-model", "value": "openai/gpt-5.5", "unit": None},
        {"id": "rootless", "value": True, "unit": None},
        {"id": "web-search", "value": False, "unit": None},
    ]
    limitations = [
        "dataset-bytes-unbound",
        "declaration-only",
        "hardware-unbound",
        "legacy-not-bmp-verified",
        "model-revision-unbound",
        "no-naturebranch-results",
        "official-docker-cohort-not-directly-comparable",
        "source-private-approved-projection",
    ]
    if development:
        factors.append({"id": "thinking-level", "value": "medium", "unit": None})
        limitations.extend(
            [
                "development-cohort",
                "different-model",
                "different-provider-path",
                "different-tool-surface",
                "not-benchmark-result",
            ]
        )
    return {
        "experiment_id": experiment_id,
        "benchmark": {
            "id": "naturebench",
            "name": "NatureBench",
            "version": "snapshot-4b51202",
        },
        "dataset": _dataset(),
        "method": {
            "id": spec["method_id"],
            "name": spec["method_name"],
            "version": None,
            "subject_id": spec["method_id"],
        },
        "model": {
            "id": spec["model_id"],
            "name": spec["model_name"],
            "version": None,
            "revision": None,
        },
        "provider": {
            "id": spec["provider_id"],
            "name": spec["provider_name"],
            "version": None,
            "region": None,
        },
        "harness": {
            "id": "aosebench-nature-apptainer-v1",
            "name": "AOSEBench-Nature rootless Apptainer harness",
            "version": "1",
            "protocol_id": "naturebench-cellular-omics.v1",
            "configuration_sha256": sha256(
                canonical_json_bytes(configuration)
            ).hexdigest(),
        },
        "evaluator": {
            "id": "naturebench-score-and-gpt55-validity-judge",
            "name": "NatureBench score evaluator and GPT-5.5 validity judge",
            "version": None,
            "kind": "hybrid",
            "independent": True,
        },
        "execution": {
            "mode": "apptainer",
            "backend_id": "aosebench-nature-apptainer",
            "isolation": "container",
            "network_policy": "benchmark-defined",
            "case_count": 31,
            "repetitions_per_case": 1,
            "seeds": [],
            "order_policy": "source-defined",
            "hardware": _hardware(),
            "image_sha256": None,
            "configuration_id": f"naturebranch-{spec['slug']}",
            "configuration_sha256": sha256(
                canonical_json_bytes(configuration)
            ).hexdigest(),
            "configuration_profiles": [spec["slug"]],
            "factors": factors,
            "budget": {"max_cases": 31},
        },
        "purpose": "exploratory" if development else "benchmark",
        "comparability": {
            "status": "not-comparable" if development else "conditional",
            "comparison_group": (
                None if development else "naturebench-cellular-omics-apptainer"
            ),
            "protocol_sha256": FILES["scripts/aggregate_results.py"]["sha256"],
            "case_set_sha256": FILES["task-set/cellomics.txt"]["sha256"],
            "evaluator_sha256": FILES[
                "vendor/naturebench/submit_results/compute_scores.py"
            ]["sha256"],
        },
        "limitations": limitations,
    }


def _reference_conditions(row: dict[str, Any]) -> dict[str, Any]:
    configuration = {
        "model": row["model_id"],
        "official_harness": row["method_id"],
        "scope": "Cellular Omics",
        "task_count": 31,
        "judge_model": "GPT-5.5",
        "web_search": False,
    }
    return {
        "experiment_id": f"naturebench-cellular-omics-upstream-{row['slug']}",
        "benchmark": {
            "id": "naturebench",
            "name": "NatureBench",
            "version": "official-reference",
        },
        "dataset": _dataset(),
        "method": {
            "id": row["method_id"],
            "name": row["method_name"],
            "version": None,
            "subject_id": row["method_id"],
        },
        "model": {
            "id": row["model_id"],
            "name": row["model_name"],
            "version": None,
            "revision": None,
        },
        "provider": None,
        "harness": {
            "id": f"naturebench-official-{row['method_id']}",
            "name": f"NatureBench official {row['method_name']} harness",
            "version": None,
            "protocol_id": "naturebench-official-reference",
            "configuration_sha256": sha256(
                canonical_json_bytes(configuration)
            ).hexdigest(),
        },
        "evaluator": {
            "id": "naturebench-gpt-5.5-judged",
            "name": "NatureBench deterministic scoring with GPT-5.5 validity judge",
            "version": None,
            "kind": "hybrid",
            "independent": True,
        },
        "execution": {
            "mode": "docker",
            "backend_id": "naturebench-reference-docker",
            "isolation": "container",
            "network_policy": "benchmark-defined",
            "case_count": 31,
            "repetitions_per_case": 1,
            "seeds": [],
            "order_policy": "unknown",
            "hardware": _hardware(),
            "image_sha256": None,
            "configuration_id": f"naturebench-reference-{row['slug']}",
            "configuration_sha256": sha256(
                canonical_json_bytes(configuration)
            ).hexdigest(),
            "configuration_profiles": [row["method_id"], row["model_id"]],
            "factors": [
                {"id": "judge-model", "value": "GPT-5.5", "unit": None},
                {"id": "reference-harness", "value": row["method_name"], "unit": None},
                {"id": "web-search", "value": False, "unit": None},
            ],
            "budget": {"max_cases": 31},
        },
        "purpose": "evaluation",
        "comparability": {
            "status": "conditional",
            "comparison_group": "naturebench-cellular-omics-upstream-reference",
            "protocol_sha256": FILES["scripts/aggregate_results.py"]["sha256"],
            "case_set_sha256": FILES["task-set/cellomics.txt"]["sha256"],
            "evaluator_sha256": FILES[
                "vendor/naturebench/submit_results/compute_scores.py"
            ]["sha256"],
        },
        "limitations": [
            "aggregate-only",
            "dataset-bytes-unbound",
            "evaluator-revision-unbound",
            "hardware-unbound",
            "invalid-missing-counts-unavailable",
            "legacy-not-bmp-verified",
            "model-revision-unbound",
            "not-aosebench-output",
            "per-task-results-unavailable",
            "provider-unbound",
            "rounded-source-value",
            "source-private-approved-projection",
            "upstream-reference",
        ],
    }


def _validated_record(payload: dict[str, Any]) -> HistoricalRecord:
    payload["record_id"] = compute_record_id(payload)
    return _RECORD_ADAPTER.validate_json(canonical_json_bytes(payload), strict=True)


def _declaration_record(spec: dict[str, Any], snapshot_sha: str) -> HistoricalRecord:
    experiment = _declaration_conditions(spec)
    provenance = [
        _provenance("declaration", "README.md"),
        _provenance("configuration", "bench-env.template.sh"),
        _provenance("dataset", "task-set/cellomics.txt"),
        _provenance("manifest", "manifests/tasks.tsv"),
        _provenance("metric", "scripts/aggregate_results.py"),
        _provenance("evaluator", "vendor/naturebench/submit_results/compute_scores.py"),
    ]
    if spec.get("development"):
        provenance.append(_provenance("declaration", "docs/OPUS48_DEV_COHORT.md"))
    return _validated_record(
        {
            "kind": "declaration",
            "source_id": SOURCE_ID,
            "source_snapshot_sha256": snapshot_sha,
            "logical_key": experiment["experiment_id"],
            "supersedes": [],
            "evidence_tier": "declaration-only",
            "claim_eligible": False,
            "provenance": provenance,
            "experiment": experiment,
            "metric_ids": METRIC_IDS,
        }
    )


def _reference_record(row: dict[str, Any], snapshot_sha: str) -> HistoricalRecord:
    experiment = _reference_conditions(row)
    metrics = [
        _metric(
            "completion-rate",
            row["completion_rate"],
            source_field="CR",
            unit="percent",
            aggregation="rate",
            policy="valid-scored cases divided by all 31 Cellular Omics cases",
        ),
        _metric(
            "match-sota",
            row["match_sota"],
            source_field="Match-SOTA",
            unit="percent",
            aggregation="rate",
            policy="valid-scored cases with g >= 0 divided by all 31 cases",
        ),
        _metric(
            "median-g-all",
            row["median_g_all"],
            source_field="median g",
            unit="aggregate-improvement",
            aggregation="median",
            policy="median g over all 31 cases with invalid and unscored cases set to -1",
        ),
        _metric(
            "surpass-sota",
            row["surpass_sota"],
            source_field="Surpass-SOTA",
            unit="percent",
            aggregation="rate",
            policy="valid-scored cases with g > 0.1 divided by all 31 cases",
        ),
    ]
    run_id = f"naturebench-upstream-reference-{row['slug']}"
    return _validated_record(
        {
            "kind": "run",
            "source_id": SOURCE_ID,
            "source_snapshot_sha256": snapshot_sha,
            "logical_key": run_id,
            "supersedes": [],
            "evidence_tier": "legacy-evaluated",
            "claim_eligible": False,
            "provenance": [
                _provenance("result", "docs/REBUILD.md"),
                _provenance("dataset", "task-set/cellomics.txt"),
                _provenance(
                    "manifest", "vendor/naturebench/submit_results/case_metadata.csv"
                ),
                _provenance("metric", "scripts/aggregate_results.py"),
                _provenance(
                    "evaluator", "vendor/naturebench/submit_results/compute_scores.py"
                ),
                _provenance("harness", "vendor/naturebench/VENDOR.md"),
            ],
            "experiment": experiment,
            "run_id": run_id,
            "parent_run_id": None,
            "terminal_state": "completed",
            "metrics": metrics,
        }
    )


def build(source_root: Path, output_root: Path) -> None:
    raw = _load_verified(source_root)
    _validate_source_facts(raw)
    normalizer_sha = sha256(Path(__file__).read_bytes()).hexdigest()
    source = HistoricalSource.model_validate(
        {
            "source_id": SOURCE_ID,
            "repository": "Minions-Land/AOSEBench-NatureBench",
            "commit_sha": SOURCE_COMMIT,
            "root_tree": {"algorithm": "sha1", "digest": SOURCE_TREE},
            "normalizer_id": NORMALIZER_ID,
            "normalizer_sha256": normalizer_sha,
            "visibility": "private",
            "license_status": "not-detected",
            "license_id": None,
            "ref_hint": "NatureBranch",
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
    records = [
        *(_declaration_record(spec, snapshot_sha) for spec in DECLARATIONS),
        *(_reference_record(row, snapshot_sha) for row in REFERENCE_ROWS),
    ]
    if len(records) != 8:
        raise ValueError("expected five declarations and three reference runs")

    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError(f"output directory is not empty: {output_root}")
    records_root = output_root / "records"
    records_root.mkdir(parents=True, exist_ok=True)
    (output_root / "source.json").write_text(
        json.dumps(source.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for record in sorted(records, key=lambda item: item.record_id):
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
