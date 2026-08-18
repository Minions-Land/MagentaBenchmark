"""Render verified memory-baseline evidence without dropping native metrics."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from MagentaBench.schemas import (
    ClaimReport,
    EvidenceBundle,
    GateName,
    ObservationReport,
    ResolvedBmpManifest,
    RunStatus,
    verify_run_report,
)


MATRIX_SCHEMA = "magentabench.memory-baseline-matrix.v1"
REPORT_SCHEMA = "magentabench.memory-baseline-report.v2"
SUPPORT_STATES = frozenset(
    {
        "pilot-ready",
        "needs-driver",
        "needs-corpus",
        "needs-artifact",
        "needs-data-prep",
        "service-required",
        "unsupported",
        "blocked",
    }
)
AVAILABILITY_STATES = frozenset({"runnable", "blocked"})
BASELINE_FAMILY_COUNT = 48
COMPLETED_STATUSES = frozenset(
    {RunStatus.pass_, RunStatus.verified_fail, RunStatus.scored}
)


class MemoryBaselineReportError(ValueError):
    """The matrix or verified report set is internally inconsistent."""


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MemoryBaselineReportError(f"{label} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemoryBaselineReportError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise MemoryBaselineReportError(f"{label} must be a JSON object")
    return value


def _unique_ids(values: Sequence[Mapping[str, Any]], *, label: str) -> tuple[str, ...]:
    ids: list[str] = []
    for value in values:
        item_id = value.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise MemoryBaselineReportError(f"{label} requires non-empty string ids")
        ids.append(item_id)
    if len(ids) != len(set(ids)):
        raise MemoryBaselineReportError(f"{label} ids must be unique")
    return tuple(ids)


def _support(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"status", "reason"}:
        raise MemoryBaselineReportError(f"{label} must contain status and reason")
    status = value.get("status")
    reason = value.get("reason")
    if status not in SUPPORT_STATES or not isinstance(reason, str) or not reason.strip():
        raise MemoryBaselineReportError(f"{label} has an invalid status or reason")
    return {"status": status, "reason": reason}


def load_matrix(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    matrix = _read_json_object(source, label="capability matrix")
    if matrix.get("schema_version") != MATRIX_SCHEMA:
        raise MemoryBaselineReportError("unsupported capability matrix schema")
    benchmarks = matrix.get("benchmarks")
    methods = matrix.get("methods")
    native_paths = matrix.get("paper_native_paths")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise MemoryBaselineReportError("capability matrix requires benchmarks")
    if not isinstance(methods, list) or not methods:
        raise MemoryBaselineReportError("capability matrix requires methods")
    if not isinstance(native_paths, list):
        raise MemoryBaselineReportError("capability matrix requires paper_native_paths")
    benchmark_ids = _unique_ids(benchmarks, label="benchmarks")
    method_ids = _unique_ids(methods, label="methods")
    _unique_ids(native_paths, label="paper_native_paths")
    for benchmark in benchmarks:
        if (
            not isinstance(benchmark.get("label"), str)
            or not isinstance(benchmark.get("registry_ids"), list)
            or not benchmark["registry_ids"]
            or any(not isinstance(item, str) or not item for item in benchmark["registry_ids"])
        ):
            raise MemoryBaselineReportError("benchmark labels and registry_ids are required")
    for method in methods:
        availability = method.get("availability")
        if availability not in AVAILABILITY_STATES:
            raise MemoryBaselineReportError(f"method {method['id']} has invalid availability")
        support = method.get("support")
        if not isinstance(support, dict) or set(support) != {"default", "overrides"}:
            raise MemoryBaselineReportError(f"method {method['id']} has invalid support")
        default = _support(support["default"], label=f"method {method['id']} default")
        overrides = support["overrides"]
        if not isinstance(overrides, dict) or not set(overrides).issubset(benchmark_ids):
            raise MemoryBaselineReportError(f"method {method['id']} has unknown overrides")
        for benchmark_id, value in overrides.items():
            _support(value, label=f"method {method['id']} {benchmark_id}")
        if availability == "blocked" and (
            default["status"] != "blocked"
            or any(value.get("status") != "blocked" for value in overrides.values())
        ):
            raise MemoryBaselineReportError(
                f"blocked method {method['id']} cannot declare runnable support"
            )
    for native in native_paths:
        support = native.get("support")
        if not isinstance(support, dict) or set(support) != set(benchmark_ids):
            raise MemoryBaselineReportError(
                f"paper-native path {native['id']} must cover every benchmark"
            )
        for benchmark_id, value in support.items():
            _support(value, label=f"paper-native path {native['id']} {benchmark_id}")
    if len(method_ids) != BASELINE_FAMILY_COUNT:
        raise MemoryBaselineReportError(
            "capability matrix must retain all "
            f"{BASELINE_FAMILY_COUNT} registered methods; observed {len(method_ids)}"
        )
    return matrix


def expand_capabilities(matrix: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for method in matrix["methods"]:
        default = method["support"]["default"]
        overrides = method["support"]["overrides"]
        for benchmark in matrix["benchmarks"]:
            support = overrides.get(benchmark["id"], default)
            rows.append(
                {
                    "kind": "coding-agent-arm",
                    "method_id": method["id"],
                    "method_label": method["label"],
                    "availability": method["availability"],
                    "equivalence": method["equivalence"],
                    "requirements": list(method["requirements"]),
                    "benchmark_id": benchmark["id"],
                    "benchmark_label": benchmark["label"],
                    "status": support["status"],
                    "reason": support["reason"],
                }
            )
    return rows


def expand_paper_native(matrix: Mapping[str, Any]) -> list[dict[str, Any]]:
    labels = {item["id"]: item["label"] for item in matrix["benchmarks"]}
    return [
        {
            "kind": "paper-native",
            "method_id": method["id"],
            "method_label": method["label"],
            "availability": "runnable",
            "equivalence": method["equivalence"],
            "requirements": [],
            "benchmark_id": benchmark_id,
            "benchmark_label": labels[benchmark_id],
            "status": support["status"],
            "reason": support["reason"],
        }
        for method in matrix["paper_native_paths"]
        for benchmark_id, support in method["support"].items()
    ]


def _artifact_bytes(ref: Any, *, label: str) -> bytes:
    path = Path(ref.path)
    if path.is_symlink() or not path.is_file():
        raise MemoryBaselineReportError(f"{label} is not a regular file")
    content = path.read_bytes()
    if len(content) != ref.size_bytes or hashlib.sha256(content).hexdigest() != ref.sha256:
        raise MemoryBaselineReportError(f"{label} differs from its artifact reference")
    return content


def _benchmark_id(matrix: Mapping[str, Any], registry_id: str) -> str:
    for benchmark in matrix["benchmarks"]:
        if registry_id in benchmark["registry_ids"]:
            return benchmark["id"]
    return registry_id


def _method_id(matrix: Mapping[str, Any], manifest: ResolvedBmpManifest) -> str:
    known = {
        item["id"] for item in (*matrix["methods"], *matrix["paper_native_paths"])
    }
    factor = manifest.metadata.factors.get("memory-method")
    if isinstance(factor, str) and factor in known:
        return factor
    subject_id = manifest.subject.id
    return subject_id if subject_id in known else subject_id


def _relative_evidence(path: str, base: Path) -> str:
    return Path(os.path.relpath(Path(path), start=base)).as_posix()


def _metric_order(values: Iterable[str]) -> list[str]:
    prefixes = {"native:": 0, "bmp:": 1, "usage:": 2}
    return sorted(
        values,
        key=lambda value: (
            prefixes.get(value.split(":", 1)[0] + ":", 9),
            value,
        ),
    )


def _report_validity(report: ObservationReport | ClaimReport) -> tuple[bool, bool]:
    if isinstance(report, ClaimReport):
        return (
            report.gates[GateName.protocol_valid].valid,
            report.gates[GateName.isolation_valid].valid,
        )
    return report.protocol_valid, report.isolation_valid


def _completion_eligibility(
    status: RunStatus,
    tool_errors: int | None,
    *,
    protocol_valid: bool,
) -> tuple[bool, list[str]]:
    exclusions: list[str] = []
    if not protocol_valid:
        exclusions.append("protocol_invalid")
    if status not in COMPLETED_STATUSES:
        exclusions.append(f"non_scoring_status:{status.value}")
    if tool_errors is None:
        exclusions.append("tool_errors_unobserved")
    elif tool_errors > 0:
        exclusions.append(f"tool_errors_nonzero:{tool_errors}")
    return not exclusions, exclusions


def collect_verified_results(
    matrix: Mapping[str, Any],
    report_paths: Sequence[str | Path],
    *,
    evidence_base: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    metric_columns: set[str] = set()
    for raw_path in report_paths:
        path = Path(raw_path).resolve()
        verified = verify_run_report(path)
        report = verified.report
        protocol_valid, isolation_valid = _report_validity(report)
        report_bytes = path.read_bytes()
        sources.append(
            {
                "name": path.name,
                "experiment_id": report.experiment_id,
                "purpose": report.purpose.value,
                "sha256": hashlib.sha256(report_bytes).hexdigest(),
                "protocol_valid": protocol_valid,
                "isolation_valid": isolation_valid,
            }
        )
        manifests: dict[str, ResolvedBmpManifest] = {}
        for ref in verified.record_index.manifest_refs:
            manifest = ResolvedBmpManifest.model_validate_json(
                _artifact_bytes(ref, label="resolved manifest")
            )
            if manifest.metadata.run_id in manifests:
                raise MemoryBaselineReportError("duplicate run id in record index")
            manifests[manifest.metadata.run_id] = manifest
        registered_by_run: dict[str, list[Any]] = {}
        for metric in report.metric_results:
            registered_by_run.setdefault(metric.parent_run_id, []).append(metric)
        for lineage in report.lineage:
            manifest = manifests.get(lineage.run_id)
            if manifest is None:
                raise MemoryBaselineReportError(
                    f"lineage run {lineage.run_id} has no resolved manifest"
                )
            bundle = EvidenceBundle.model_validate_json(
                _artifact_bytes(lineage.evidence_bundle_ref, label="evidence bundle")
            )
            values: dict[str, int | float | None] = {}
            metric_states: dict[str, str] = {}
            verifier = bundle.verifier_evidence
            if verifier is not None:
                for name, value in verifier.metrics.items():
                    key = f"native:{name}"
                    values[key] = value
                    metric_columns.add(key)
            for metric in registered_by_run.get(lineage.run_id, []):
                key = f"bmp:{metric.metric_id}"
                values[key] = metric.value
                metric_columns.add(key)
                if metric.state.value != "complete":
                    metric_states[key] = metric.reason or metric.state.value
            if bundle.usage is not None:
                for name, value in bundle.usage.model_dump(mode="json").items():
                    if value is not None:
                        key = f"usage:{name}"
                        values[key] = value
                        metric_columns.add(key)
            tool_errors = None if bundle.usage is None else bundle.usage.tool_errors
            completion_eligible, completion_exclusions = _completion_eligibility(
                bundle.status,
                tool_errors,
                protocol_valid=protocol_valid,
            )
            activation = bundle.provenance.model_activation
            rows.append(
                {
                    "experiment_id": report.experiment_id,
                    "run_id": lineage.run_id,
                    "attempt_id": lineage.attempt_id,
                    "case_id": lineage.case_id,
                    "benchmark_id": _benchmark_id(matrix, manifest.benchmark.id),
                    "benchmark_registry_id": manifest.benchmark.id,
                    "method_id": _method_id(matrix, manifest),
                    "subject_id": manifest.subject.id,
                    "model": manifest.execution.model,
                    "model_activation": (
                        None
                        if activation is None
                        else {
                            "status": activation.status,
                            "requested_provider_id": activation.requested_provider_id,
                            "requested_model_id": activation.requested_model_id,
                            "activated_provider_id": activation.activated_provider_id,
                            "activated_model_id": activation.activated_model_id,
                            "reason": list(activation.reason),
                        }
                    ),
                    "status": bundle.status.value,
                    "tool_error_count": tool_errors,
                    "completion_eligible": completion_eligible,
                    "completion_exclusions": completion_exclusions,
                    "passed": None if verifier is None else verifier.passed,
                    "score": None if verifier is None else verifier.score,
                    "values": values,
                    "metric_states": metric_states,
                    "evidence": {
                        "path": _relative_evidence(lineage.evidence_bundle_ref.path, evidence_base),
                        "sha256": lineage.evidence_bundle_ref.sha256,
                    },
                    "protocol_valid": protocol_valid,
                    "isolation_valid": isolation_valid,
                }
            )
    rows.sort(key=lambda row: (row["benchmark_id"], row["method_id"], row["case_id"], row["attempt_id"]))
    sources.sort(key=lambda value: (value["experiment_id"], value["sha256"]))
    return rows, sources, _metric_order(metric_columns)


def build_report(
    matrix: Mapping[str, Any],
    report_paths: Sequence[str | Path],
    *,
    evidence_base: Path,
) -> dict[str, Any]:
    capabilities = expand_capabilities(matrix)
    paper_native = expand_paper_native(matrix)
    results, sources, metric_columns = collect_verified_results(
        matrix, report_paths, evidence_base=evidence_base
    )
    all_cells = (*capabilities, *paper_native)
    return {
        "schema_version": REPORT_SCHEMA,
        "fairness_contract": matrix["fairness_contract"],
        "benchmarks": matrix["benchmarks"],
        "summary": {
            "benchmark_count": len(matrix["benchmarks"]),
            "registered_method_count": len(matrix["methods"]),
            "paper_native_path_count": len(matrix["paper_native_paths"]),
            "capability_cell_count": len(all_cells),
            "pilot_ready_cell_count": sum(item["status"] == "pilot-ready" for item in all_cells),
            "blocked_cell_count": sum(item["status"] == "blocked" for item in all_cells),
            "result_row_count": len(results),
            "completion_eligible_result_row_count": sum(
                row["completion_eligible"] for row in results
            ),
            "completion_ineligible_result_row_count": sum(
                not row["completion_eligible"] for row in results
            ),
        },
        "source_reports": sources,
        "metric_columns": metric_columns,
        "capability_rows": capabilities,
        "paper_native_rows": paper_native,
        "results": results,
    }


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _status_cell(status: str, reason: str) -> str:
    return (
        f'<div class="state state-{html.escape(status)}">'
        f"{html.escape(status.replace('-', ' '))}</div>"
        f'<div class="reason">{html.escape(reason)}</div>'
    )


def _matrix_table(
    rows: Sequence[Mapping[str, Any]], benchmarks: Sequence[Mapping[str, Any]]
) -> str:
    by_method: dict[str, dict[str, Mapping[str, Any]]] = {}
    labels: dict[str, str] = {}
    equivalence: dict[str, str] = {}
    for row in rows:
        by_method.setdefault(row["method_id"], {})[row["benchmark_id"]] = row
        labels[row["method_id"]] = row["method_label"]
        equivalence[row["method_id"]] = row["equivalence"]
    head = "".join(f"<th>{html.escape(item['label'])}</th>" for item in benchmarks)
    body: list[str] = []
    for method_id in by_method:
        cells = "".join(
            f"<td>{_status_cell(by_method[method_id][item['id']]['status'], by_method[method_id][item['id']]['reason'])}</td>"
            for item in benchmarks
        )
        body.append(
            "<tr>"
            f"<th><strong>{html.escape(labels[method_id])}</strong>"
            f'<span class="method-id">{html.escape(method_id)}</span>'
            f'<span class="equivalence">{html.escape(equivalence[method_id])}</span></th>'
            f"{cells}</tr>"
        )
    return (
        '<div class="table-wrap"><table class="matrix"><thead><tr>'
        f"<th>Method</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def render_html(document: Mapping[str, Any]) -> str:
    summary = document["summary"]
    metrics = document["metric_columns"]
    source_rows = "".join(
        "<tr>"
        f"<td>{html.escape(source['experiment_id'])}</td>"
        f"<td>{html.escape(source['purpose'])}</td>"
        f"<td>{html.escape(_format_value(source['protocol_valid']))}</td>"
        f"<td>{html.escape(_format_value(source['isolation_valid']))}</td>"
        f'<td class="digest">{html.escape(source["sha256"])}</td>'
        "</tr>"
        for source in document["source_reports"]
    )
    source_table = (
        '<div class="table-wrap"><table><thead><tr><th>Experiment</th>'
        "<th>Purpose</th><th>Protocol valid</th><th>Isolation valid</th>"
        f"<th>Report SHA-256</th></tr></thead><tbody>{source_rows}</tbody></table></div>"
        if source_rows
        else '<div class="empty">No verified source reports yet.</div>'
    )
    results: list[str] = []
    metric_head = "".join(f"<th>{html.escape(metric)}</th>" for metric in metrics)
    for row in document["results"]:
        values = "".join(
            f'<td title="{html.escape(row["metric_states"].get(metric, ""))}">'
            f"{html.escape(_format_value(row['values'].get(metric)))}</td>"
            for metric in metrics
        )
        evidence = row["evidence"]
        activation = row["model_activation"]
        activation_status = "missing" if activation is None else activation["status"]
        activated_model = (
            "-"
            if activation is None or activation["activated_model_id"] is None
            else activation["activated_model_id"]
        )
        results.append(
            f'<tr class="completion-{"yes" if row["completion_eligible"] else "no"}">'
            f"<td>{html.escape(row['benchmark_id'])}</td>"
            f"<td>{html.escape(row['method_id'])}</td>"
            f"<td>{html.escape(row['case_id'])}</td>"
            f'<td><span class="run-status">{html.escape(row["status"])}</span></td>'
            f"<td>{html.escape(row['model'])}</td>"
            f"<td>{html.escape(activation_status)}</td>"
            f"<td>{html.escape(activated_model)}</td>"
            f"<td>{html.escape(_format_value(row['score']))}</td>"
            f"<td>{html.escape(_format_value(row['passed']))}</td>"
            f"<td>{html.escape(_format_value(row['completion_eligible']))}</td>"
            f"<td>{html.escape(', '.join(row['completion_exclusions']) or '-')}</td>"
            f"{values}"
            f'<td><a href="{html.escape(evidence["path"], quote=True)}">evidence</a></td>'
            "</tr>"
        )
    result_table = (
        '<div class="table-wrap"><table><thead><tr><th>Benchmark</th><th>Method</th>'
        f"<th>Case</th><th>Status</th><th>Requested model</th><th>Activation</th>"
        f"<th>Activated model</th><th>Verifier score</th>"
        f"<th>Passed</th><th>Completion eligible</th><th>Completion exclusions</th>"
        f"{metric_head}<th>Evidence</th>"
        f"</tr></thead><tbody>{''.join(results)}</tbody></table></div>"
        if results
        else '<div class="empty">No verified run results yet.</div>'
    )
    css = """
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; color: #17212b; background: #f5f7f8; }
* { box-sizing: border-box; }
body { margin: 0; line-height: 1.45; }
header { background: #ffffff; border-bottom: 1px solid #d9dee3; padding: 28px 32px 22px; }
h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
main { max-width: 1800px; margin: 0 auto; padding: 24px 32px 48px; }
h2 { margin: 30px 0 12px; font-size: 19px; letter-spacing: 0; }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); border-bottom: 1px solid #d9dee3; background: #ffffff; }
.summary div { padding: 16px 20px; border-right: 1px solid #e4e8eb; }
.summary div:last-child { border-right: 0; }
.summary strong { display: block; font-size: 24px; }
.summary span { color: #53616d; font-size: 13px; }
.table-wrap { width: 100%; overflow: auto; border: 1px solid #d9dee3; background: #ffffff; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 12px; border-bottom: 1px solid #e5e9ec; border-right: 1px solid #edf0f2; text-align: left; vertical-align: top; }
thead th { position: sticky; top: 0; z-index: 2; background: #eef2f3; white-space: nowrap; }
tbody th { min-width: 210px; background: #fafbfb; }
.matrix td { min-width: 170px; }
.method-id, .equivalence { display: block; margin-top: 3px; color: #596772; font-weight: 400; overflow-wrap: anywhere; }
.state { display: inline-block; padding: 2px 6px; border-radius: 4px; font-weight: 700; text-transform: uppercase; font-size: 11px; }
.state-pilot-ready { background: #dcefe3; color: #176438; }
.state-blocked { background: #f7dede; color: #8f2525; }
.state-unsupported { background: #e8ebee; color: #46515a; }
.state-service-required { background: #e8e1f5; color: #5f3687; }
.state-needs-driver, .state-needs-corpus, .state-needs-artifact, .state-needs-data-prep { background: #f8e9c9; color: #76530d; }
.reason { margin-top: 6px; color: #46535e; }
.run-status { font-weight: 700; }
.completion-no { background: #fff8f1; }
.digest { max-width: 280px; overflow-wrap: anywhere; font-family: ui-monospace, monospace; }
.empty { border: 1px solid #d9dee3; background: #ffffff; padding: 20px; color: #53616d; }
a { color: #0b6254; font-weight: 700; }
@media (max-width: 760px) { header, main { padding-left: 16px; padding-right: 16px; } .summary { grid-template-columns: repeat(2, 1fr); } .summary div { border-bottom: 1px solid #e4e8eb; } }
"""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Memory Baseline Results</title><style>{css}</style></head><body>"
        "<header><h1>Memory Baseline Results</h1></header>"
        '<section class="summary">'
        f"<div><strong>{summary['benchmark_count']}</strong><span>Benchmarks</span></div>"
        f"<div><strong>{summary['registered_method_count']}</strong><span>Registered methods</span></div>"
        f"<div><strong>{summary['paper_native_path_count']}</strong><span>Paper-native paths</span></div>"
        f"<div><strong>{summary['pilot_ready_cell_count']}</strong><span>Pilot-ready cells</span></div>"
        f"<div><strong>{summary['blocked_cell_count']}</strong><span>Blocked cells</span></div>"
        f"<div><strong>{summary['result_row_count']}</strong><span>Verified result rows</span></div>"
        f"<div><strong>{summary['completion_eligible_result_row_count']}</strong><span>Completion eligible</span></div>"
        f"<div><strong>{summary['completion_ineligible_result_row_count']}</strong><span>Completion excluded</span></div>"
        "</section><main>"
        "<h2>Verified Reports</h2>"
        f"{source_table}"
        "<h2>Verified Results</h2>"
        f"{result_table}"
        "<h2>Paper-native Paths</h2>"
        f"{_matrix_table(document['paper_native_rows'], document['benchmarks'])}"
        "<h2>Coding-agent Arms</h2>"
        f"{_matrix_table(document['capability_rows'], document['benchmarks'])}"
        "</main></body></html>"
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise MemoryBaselineReportError(f"temporary output already exists: {temporary}")
    with temporary.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def generate(
    *,
    matrix_path: str | Path,
    report_paths: Sequence[str | Path],
    output_json: str | Path,
    output_html: str | Path,
) -> dict[str, Any]:
    html_path = Path(output_html).resolve()
    matrix = load_matrix(matrix_path)
    document = build_report(matrix, report_paths, evidence_base=html_path.parent)
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"
    _atomic_write(Path(output_json).resolve(), encoded)
    _atomic_write(html_path, render_html(document).encode("utf-8"))
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--matrix",
        default=str(Path(__file__).with_name("capability-matrix.json")),
    )
    parser.add_argument("--report", action="append", default=[])
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-html", required=True)
    args = parser.parse_args(argv)
    generate(
        matrix_path=args.matrix,
        report_paths=args.report,
        output_json=args.output_json,
        output_html=args.output_html,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
