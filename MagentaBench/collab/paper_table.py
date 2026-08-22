"""Deterministic paper-facing views over the generated experiment ledger.

The experiment ledger is the source of truth.  This module only projects its
long-form rows into a stable, publication-friendly shape; it never writes a
ledger row, changes verification state, or infers claim eligibility.  It is
intentionally usable as a standalone module so a paper build can run::

    python -m MagentaBench.collab.paper_table --project-root . --format markdown

The command emits a disposable view.  The checked-in bundle, lab event chain,
verified report, and imported source records remain authoritative.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .ledger import ExperimentLedger, build_experiment_ledger, parse_path_maps


PAPER_TABLE_FORMAT = "magentabench-paper-experiment-table-v2"

# Keep this tuple explicit.  Adding a source field is a reviewed contract
# change, while row order and structured-cell encoding remain reproducible.
PAPER_COLUMNS: tuple[str, ...] = (
    "row_id",
    "row_kind",
    "record_origin",
    "lab_issue",
    "evidence_tier",
    "source_evidence_class",
    "benchmark_id",
    "dataset_id",
    "dataset_split",
    "case_id",
    "question",
    "case_or_question",
    "unit_id",
    "unit_kind",
    "attempt_id",
    "experiment_id",
    "run_id",
    "source_run_id",
    "source_run_record_id",
    "parent_run_id",
    "aggregate_run_id",
    "aggregate_run_record_id",
    "aggregate_reconciliation_status",
    "result_granularity",
    "purpose",
    "terminal_state",
    "method_id",
    "subject_id",
    "model",
    "code_commit",
    "provider_id",
    "harness_id",
    "protocol_id",
    "metric_id",
    "metric_state",
    "result_status",
    "result_reason",
    "value",
    "unit",
    "direction",
    "aggregation",
    "denominator",
    "denominator_unit",
    "denominator_planned",
    "denominator_observed",
    "denominator_zero_filled",
    "denominator_excluded",
    "denominator_missing",
    "denominator_invalid",
    "planned_rollout_count",
    "task_count",
    "rollouts_per_task",
    "uncertainty",
    "uncertainty_method",
    "uncertainty_confidence_level",
    "uncertainty_lower",
    "uncertainty_upper",
    "evaluator_id",
    "verification_status",
    "standalone_verification",
    "claim_eligible",
    "claim_status",
    "configuration_id",
    "configuration_digest",
    "configuration_profiles",
    "condition_digest",
    "factor_values",
    "conditions",
    "backend_id",
    "execution_mode",
    "image_digest",
    "budget",
    "comparability",
    "record_root",
    "report_ref",
    "validity_gates",
    "failure_breakdown",
    "verified_manifest_refs",
    "manifest_digest",
    "metric_digest",
    "dataset_commit",
    "dataset_digest",
    "provenance_paths",
    "provenance_refs",
    "limitations",
    "source_id",
    "record_id",
    "logical_key_sha256",
    "supersedes",
)


class PaperTableError(ValueError):
    """Raised when a projection input cannot be represented deterministically."""


def _canonical_json(value: Any) -> str:
    """Encode a structured cell without locale or insertion-order variance."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PaperTableError(f"value is not deterministic JSON: {exc}") from exc


def _digest_identity(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _coalesce(*values: Any) -> Any:
    """Return the first present value without treating zero/false as missing."""

    for value in values:
        if value is not None:
            return value
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _code_commit(row: Mapping[str, Any]) -> str | None:
    """Read an explicitly supplied code identity, never the dataset commit."""

    direct = _first(
        row,
        "code_commit",
        "code_sha",
        "agent_commit",
        "subject_commit",
        "method_commit",
    )
    if isinstance(direct, str):
        return direct
    conditions = _mapping(row.get("conditions"))
    for path in (
        ("code", "commit"),
        ("subject", "commit"),
        ("method", "commit"),
        ("agent", "commit"),
    ):
        value = _nested(conditions, *path)
        if isinstance(value, str):
            return value
    return None


def _case_and_question(
    row: Mapping[str, Any], experiment: Mapping[str, Any]
) -> tuple[str | None, str | None, str | None]:
    case_id = _first(row, "case_id", "case", "question_id", "unit_id")
    question = _first(row, "question", "question_text")
    if case_id is None:
        conditions = _mapping(row.get("conditions"))
        case_id = _first(conditions, "case_id", "case", "question_id")
    if question is None:
        question = _first(_mapping(experiment), "question")
    if case_id is None:
        declared = experiment.get("case_ids")
        if isinstance(declared, (list, tuple)) and len(declared) == 1:
            case_id = declared[0]
    if case_id is not None and not isinstance(case_id, str):
        case_id = _canonical_json(case_id)
    if question is not None and not isinstance(question, str):
        question = _canonical_json(question)
    return case_id, question, case_id or question


def _denominator_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    denominator = row.get("denominator")
    denominator_map = _mapping(denominator)

    def count(name: str, *aliases: str) -> Any:
        value = _first(denominator_map, name, *aliases)
        if value is not None:
            return value
        return _first(row, name, *aliases)

    return {
        "denominator": denominator,
        "denominator_unit": _first(denominator_map, "unit")
        or row.get("denominator_unit"),
        "denominator_planned": count(
            "planned_count", "planned", "planned_rollout_count"
        ),
        "denominator_observed": count("observed_count", "observed"),
        "denominator_zero_filled": count("zero_filled_count", "zero_filled"),
        "denominator_excluded": count("excluded_count", "excluded"),
        "denominator_missing": count("missing_count", "missing"),
        "denominator_invalid": count("invalid_count", "invalid"),
        "planned_rollout_count": count("planned_rollout_count", "planned_count"),
        "task_count": count("task_count", "tasks"),
        "rollouts_per_task": count("rollouts_per_task", "repetitions_per_case"),
    }


def _claim_fields(row: Mapping[str, Any]) -> tuple[bool, str]:
    """Preserve an authoritative boolean without deriving a new claim.

    Missing/non-boolean values are represented as ineligible in the compact
    boolean column and retained as ``not-derived`` in ``claim_status``.  This
    keeps a paper table safe to filter while making the missing gate visible.
    """

    value = row.get("claim_eligible")
    if value is True:
        return True, "eligible"
    if value is False:
        return False, "ineligible"
    return False, "not-derived"


def _cell_value(value: Any) -> Any:
    """Convert nested cells to deterministic CSV/Markdown-safe scalars."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value)
    if isinstance(value, (str, int, float)):
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            raise PaperTableError("non-finite numeric value cannot enter paper table")
        return value
    return _canonical_json(value)


def _run_key(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return (row.get("experiment_id"), _first(row, "lab_run_id", "run_id"))


def _base_row(
    source: Mapping[str, Any],
    *,
    experiment: Mapping[str, Any],
    run: Mapping[str, Any] | None,
    row_kind: str,
) -> dict[str, Any]:
    denominator = _denominator_fields(source)
    case_id, question, case_or_question = _case_and_question(source, experiment)
    claim_eligible, claim_status = _claim_fields(source)
    run = {} if run is None else run
    record_origin = source.get("record_origin")
    if not isinstance(record_origin, str) or not record_origin:
        record_origin = "unknown"
    observation_id = source.get("observation_id")
    identity = {
        "record_origin": record_origin,
        "experiment_id": source.get("experiment_id") or experiment.get("experiment_id"),
        "run_id": _first(source, "run_id", "lab_run_id") or run.get("lab_run_id"),
        "unit_id": source.get("unit_id"),
        "attempt_id": source.get("attempt_id"),
        "metric_id": source.get("metric_id"),
        "row_kind": row_kind,
        "record_id": source.get("record_id"),
    }
    row_id = (
        observation_id
        if isinstance(observation_id, str) and observation_id
        else (f"{row_kind}:{_digest_identity(identity)}")
    )
    experiment_id = _first(source, "experiment_id") or experiment.get("experiment_id")
    run_id = _first(source, "run_id", "lab_run_id") or run.get("lab_run_id")
    terminal_state = _first(source, "terminal_state") or run.get("run_state")
    verification_status = _first(
        source,
        "verification_status",
        "standalone_verification",
    ) or run.get("standalone_verification")
    standalone_verification = _first(source, "standalone_verification") or run.get(
        "standalone_verification"
    )
    metric_state = source.get("metric_state")
    result_status = _first(source, "result_status", "reason", "status")
    if result_status is None:
        result_status = metric_state or verification_status or terminal_state
    code_commit = _coalesce(
        _code_commit(source), _code_commit(run), _code_commit(experiment)
    )

    # Context is sourced from the observation first, then from its experiment
    # and run.  Missing context stays empty; it is never guessed from a name.
    values: dict[str, Any] = {
        "row_id": row_id,
        "row_kind": row_kind,
        "record_origin": record_origin,
        "lab_issue": _coalesce(
            _first(source, "lab_issue"),
            _first(run, "lab_issue"),
            _first(experiment, "lab_issue"),
        ),
        "evidence_tier": source.get("evidence_tier"),
        "source_evidence_class": source.get("source_evidence_class"),
        "benchmark_id": _coalesce(
            _first(source, "benchmark_id"), experiment.get("benchmark_id")
        ),
        "dataset_id": _coalesce(
            _first(source, "dataset_id"), experiment.get("dataset_id")
        ),
        "dataset_split": _first(source, "dataset_split"),
        "case_id": case_id,
        "question": question,
        "case_or_question": case_or_question,
        "unit_id": source.get("unit_id"),
        "unit_kind": source.get("unit_kind"),
        "attempt_id": source.get("attempt_id"),
        "experiment_id": experiment_id,
        "run_id": run_id,
        "source_run_id": source.get("source_run_id"),
        "source_run_record_id": source.get("source_run_record_id"),
        "parent_run_id": source.get("parent_run_id"),
        "aggregate_run_id": source.get("aggregate_run_id"),
        "aggregate_run_record_id": source.get("aggregate_run_record_id"),
        "aggregate_reconciliation_status": source.get(
            "aggregate_reconciliation_status"
        ),
        "result_granularity": source.get("result_granularity"),
        "purpose": _coalesce(
            _first(source, "purpose"),
            _first(run, "purpose"),
            _first(experiment, "purpose"),
        ),
        "terminal_state": terminal_state,
        "method_id": _coalesce(
            _first(source, "method_id"), experiment.get("subject_id")
        ),
        "subject_id": _coalesce(
            _first(source, "subject_id"), experiment.get("subject_id")
        ),
        "model": _coalesce(_first(source, "model"), experiment.get("model")),
        "code_commit": code_commit,
        "provider_id": _coalesce(
            _first(source, "provider_id"),
            _first(run, "provider_id"),
            _first(experiment, "provider_id"),
        ),
        "harness_id": _coalesce(
            _first(source, "harness_id"),
            _first(run, "harness_id"),
            _first(experiment, "harness_id"),
        ),
        "protocol_id": _coalesce(
            _first(source, "protocol_id"),
            _first(run, "protocol_id"),
            _first(experiment, "protocol_id"),
        ),
        "metric_id": source.get("metric_id"),
        "metric_state": metric_state,
        "result_status": result_status,
        "result_reason": source.get("result_reason"),
        "value": source.get("value"),
        "unit": source.get("unit"),
        "direction": source.get("direction"),
        "aggregation": source.get("aggregation"),
        **denominator,
        "uncertainty": source.get("uncertainty"),
        "uncertainty_method": source.get("uncertainty_method"),
        "uncertainty_confidence_level": source.get("uncertainty_confidence_level"),
        "uncertainty_lower": source.get("uncertainty_lower"),
        "uncertainty_upper": source.get("uncertainty_upper"),
        "evaluator_id": _coalesce(
            _first(source, "evaluator_id"), experiment.get("evaluator_id")
        ),
        "verification_status": verification_status,
        "standalone_verification": standalone_verification,
        "claim_eligible": claim_eligible,
        "claim_status": claim_status,
        "configuration_id": _first(source, "configuration_id")
        or _first(run, "configuration_id")
        or _first(experiment, "configuration_id"),
        "configuration_digest": _first(source, "configuration_digest")
        or _first(run, "configuration_digest")
        or _first(experiment, "configuration_digest"),
        "configuration_profiles": _coalesce(
            _first(source, "configuration_profiles"),
            _first(run, "configuration_profiles"),
            _first(experiment, "configuration_profiles"),
        ),
        "condition_digest": _coalesce(
            _first(source, "condition_digest"),
            _first(run, "condition_digest"),
            _first(experiment, "condition_digest"),
        ),
        "factor_values": _coalesce(
            _first(source, "factor_values"),
            _first(run, "factor_values"),
            _first(experiment, "factor_values"),
        ),
        "conditions": _coalesce(
            _first(source, "conditions"),
            _first(run, "conditions"),
            _first(experiment, "conditions"),
        ),
        "backend_id": _coalesce(
            _first(source, "backend_id"), experiment.get("backend_id")
        ),
        "execution_mode": _coalesce(
            _first(source, "execution_mode"), experiment.get("execution_mode")
        ),
        "image_digest": _coalesce(
            _first(source, "image_digest"),
            _first(run, "image_digest"),
            _first(experiment, "image_digest"),
        ),
        "budget": _coalesce(
            _first(source, "budget"),
            _first(run, "budget"),
            _first(experiment, "budget"),
        ),
        "comparability": _coalesce(
            _first(source, "comparability"),
            _first(run, "comparability"),
            _first(experiment, "comparability"),
        ),
        "record_root": _coalesce(_first(source, "record_root"), run.get("record_root")),
        "report_ref": _coalesce(_first(source, "report_ref"), run.get("report_ref")),
        "validity_gates": run.get("validity_gates"),
        "failure_breakdown": run.get("failure_breakdown"),
        "verified_manifest_refs": run.get("verified_manifest_refs"),
        "manifest_digest": _coalesce(
            _first(source, "manifest_digest"), run.get("manifest_digest")
        ),
        "metric_digest": source.get("metric_digest"),
        "dataset_commit": _coalesce(
            _first(source, "dataset_commit"),
            _first(run, "dataset_commit"),
            _first(experiment, "dataset_commit"),
        ),
        "dataset_digest": _coalesce(
            _first(source, "dataset_digest"),
            _first(run, "dataset_digest"),
            _first(experiment, "dataset_digest"),
        ),
        "provenance_paths": source.get("provenance_paths"),
        "provenance_refs": source.get("provenance_refs"),
        "limitations": source.get("limitations"),
        "source_id": source.get("source_id"),
        "record_id": source.get("record_id"),
        "logical_key_sha256": source.get("logical_key_sha256"),
        "supersedes": source.get("supersedes"),
    }
    return {column: values.get(column) for column in PAPER_COLUMNS}


def project_paper_table(ledger: ExperimentLedger) -> "PaperTable":
    """Project observations and result-less runs into a stable paper table.

    Every generated observation is retained, including zero, missing, invalid,
    and legacy rows.  A run without an observation gets a ``row_kind=run``
    row, preserving failed/non-terminal attempts instead of dropping them.
    Declaration-only experiments are intentionally not result rows; they remain
    visible through the ordinary ledger's ``experiments``/``catalog`` tables.
    """

    experiments = {
        row.get("experiment_id"): row
        for row in getattr(ledger, "experiments", ())
        if row.get("experiment_id") is not None
    }
    runs = tuple(getattr(ledger, "runs", ()))
    observations = tuple(getattr(ledger, "observations", ()))
    run_by_key = {_run_key(row): row for row in runs}
    observed_run_keys: set[tuple[Any, Any]] = set()
    rows: list[dict[str, Any]] = []
    for observation in observations:
        experiment = experiments.get(observation.get("experiment_id"), {})
        run = run_by_key.get(_run_key(observation))
        if run is not None:
            observed_run_keys.add(_run_key(observation))
        rows.append(
            _base_row(
                observation,
                experiment=experiment,
                run=run,
                row_kind="observation",
            )
        )
    for run in runs:
        key = _run_key(run)
        if key in observed_run_keys:
            continue
        experiment = experiments.get(run.get("experiment_id"), {})
        rows.append(_base_row(run, experiment=experiment, run=run, row_kind="run"))

    def sort_key(row: Mapping[str, Any]) -> tuple[str, ...]:
        kind = row.get("row_kind")
        return tuple(
            "" if row.get(name) is None else str(row.get(name))
            for name in (
                "benchmark_id",
                "dataset_id",
                "dataset_split",
                "case_or_question",
                "method_id",
                "model",
                "metric_id",
                "run_id",
            )
        ) + (
            "0" if kind == "observation" else "1",
            "" if row.get("row_id") is None else str(row["row_id"]),
        )

    normalized = tuple(
        sorted(
            ({column: row.get(column) for column in PAPER_COLUMNS} for row in rows),
            key=sort_key,
        )
    )
    errors = tuple(
        sorted(
            (
                dict(item)
                for item in getattr(ledger, "errors", ())
                if isinstance(item, Mapping)
            ),
            key=lambda item: (
                str(item.get("code", "")),
                str(item.get("source", "")),
                str(item.get("message", "")),
            ),
        )
    )
    return PaperTable(rows=normalized, errors=errors)


def build_paper_table(
    project_root: str | Path,
    *,
    path_map: Mapping[str, str] | None = None,
    imports_dir: str | Path | None = None,
) -> "PaperTable":
    """Build the generated ledger, then return its paper projection."""

    return project_paper_table(
        build_experiment_ledger(
            project_root,
            path_map=path_map,
            imports_dir=imports_dir,
        )
    )


def select_paper_table(
    table: "PaperTable",
    *,
    metric_ids: Sequence[str] = (),
    benchmark_ids: Sequence[str] = (),
    dataset_ids: Sequence[str] = (),
) -> "PaperTable":
    """Select rows without changing their schema, values, or provenance."""

    metric_set = frozenset(metric_ids)
    benchmark_set = frozenset(benchmark_ids)
    dataset_set = frozenset(dataset_ids)
    rows = tuple(
        row
        for row in table.rows
        if (not metric_set or row.get("metric_id") in metric_set)
        and (not benchmark_set or row.get("benchmark_id") in benchmark_set)
        and (not dataset_set or row.get("dataset_id") in dataset_set)
    )
    return PaperTable(rows=rows, errors=table.errors)


@dataclass(frozen=True)
class PaperTable:
    """Immutable rows and source errors for one projection snapshot."""

    rows: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": list(PAPER_COLUMNS),
            "errors": [dict(item) for item in self.errors],
            "format": PAPER_TABLE_FORMAT,
            "row_count": len(self.rows),
            "rows": [dict(row) for row in self.rows],
        }


def render_csv(table: PaperTable) -> str:
    """Render a fixed-header CSV with canonical JSON structured cells."""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=PAPER_COLUMNS,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in table.rows:
        writer.writerow(
            {column: _cell_value(row.get(column)) for column in PAPER_COLUMNS}
        )
    return output.getvalue()


def render_markdown(table: PaperTable) -> str:
    """Render a deterministic pipe table suitable for a paper appendix."""

    labels = tuple(PAPER_COLUMNS)

    def cell(value: Any) -> str:
        rendered = _cell_value(value)
        if rendered == "":
            return "-"
        return str(rendered).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "| " + " | ".join(labels) + " |",
        "| " + " | ".join("---" for _ in labels) + " |",
    ]
    lines.extend(
        "| " + " | ".join(cell(row.get(column)) for column in labels) + " |"
        for row in table.rows
    )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m MagentaBench.collab.paper_table",
        description="Render a deterministic paper-facing view of the BMP ledger",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--format", choices=("markdown", "csv", "json"), default="markdown"
    )
    parser.add_argument("--imports-dir", type=Path, default=None)
    parser.add_argument("--map", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--metric-id", action="append", default=[])
    parser.add_argument("--benchmark-id", action="append", default=[])
    parser.add_argument("--dataset-id", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        table = build_paper_table(
            args.project_root,
            path_map=parse_path_maps(args.map),
            imports_dir=args.imports_dir,
        )
        table = select_paper_table(
            table,
            metric_ids=args.metric_id,
            benchmark_ids=args.benchmark_id,
            dataset_ids=args.dataset_id,
        )
        if args.format == "csv":
            output = render_csv(table)
        elif args.format == "json":
            output = _canonical_json(table.as_dict()) + "\n"
        else:
            output = render_markdown(table)
        print(output, end="")
        for finding in table.errors:
            print(
                f"ERROR {finding.get('code', 'ledger')} "
                f"[{finding.get('source', 'ledger')}]: {finding.get('message', '')}",
                file=sys.stderr,
            )
        return 0 if table.ok else 1
    except (PaperTableError, OSError, ValueError) as exc:
        print(f"paper table failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by smoke tests
    raise SystemExit(main())


__all__ = [
    "PAPER_COLUMNS",
    "PAPER_TABLE_FORMAT",
    "PaperTable",
    "PaperTableError",
    "build_paper_table",
    "main",
    "project_paper_table",
    "render_csv",
    "render_markdown",
    "select_paper_table",
]
