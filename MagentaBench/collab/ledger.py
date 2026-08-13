"""Read-only experiment ledger derived from repository evidence.

The ledger deliberately does not persist a global progress board.  It joins
the independently owned bundle, lab, BMP declaration, and report records into
stable rows that can be regenerated on any checkout.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from pydantic import ValidationError

from MagentaBench.lab import LabArtifactRef, LabRunState, LabStore
from MagentaBench.schemas.models import ClaimReport, ResolvedBmpManifest
from MagentaBench.schemas.verification import (
    ReportVerificationError,
    verify_run_report,
)

from .models import ExperimentBundle
from .repository import CollaborationError, ExperimentRepository


LEDGER_FORMAT = "magentabench-experiment-ledger-v1"
_REPORT_NAMES = frozenset({"claim_report.json", "observation_report.json"})
_CSV_COLUMNS = {
    "experiments": (
        "experiment_id",
        "bundle_id",
        "bmp_spec",
        "bmp_spec_sha256",
        "benchmark_id",
        "dataset_id",
        "evaluator_id",
        "subject_id",
        "model",
        "backend_id",
        "protocol_id",
        "regime_id",
        "stage_id",
        "factors",
        "configuration_profiles",
        "metric_ids",
        "primary_metric_ids",
        "case_ids",
        "repetitions_per_case",
        "seeds",
        "max_tokens",
        "max_wall_seconds",
        "max_cost",
        "purpose",
        "evidence_classification",
        "execution_mode",
        "question",
        "hypothesis",
        "summary",
        "lab_issue",
        "lab_status",
        "owner",
        "lease_holder",
        "blocker_count",
        "dependencies_complete",
        "available",
        "latest_run_id",
        "latest_run_state",
        "lab_revision",
        "updated_at",
    ),
    "runs": (
        "experiment_id",
        "lab_issue",
        "lab_run_id",
        "run_state",
        "record_root",
        "report_ref",
        "manifest_digest",
        "purpose",
        "standalone_verification",
        "claim_eligible",
        "protocol_valid",
        "isolation_valid",
        "validity_gates",
        "failure_breakdown",
        "metric_row_count",
        "verified_manifest_paths",
    ),
    "metrics": (
        "experiment_id",
        "lab_run_id",
        "parent_run_id",
        "manifest_digest",
        "method_id",
        "subject_id",
        "model",
        "benchmark_id",
        "dataset_id",
        "dataset_commit",
        "dataset_digest",
        "dataset_split",
        "backend_id",
        "purpose",
        "metric_id",
        "metric_digest",
        "metric_state",
        "value",
        "reason",
        "planned_rollout_count",
        "task_count",
        "rollouts_per_task",
        "observed_count",
        "zero_filled_count",
        "excluded_count",
        "missing_count",
        "invalid_count",
        "uncertainty_method",
        "uncertainty_confidence_level",
        "uncertainty_lower",
        "uncertainty_upper",
    ),
}


@dataclass(frozen=True)
class ExperimentLedger:
    experiments: tuple[dict[str, Any], ...]
    runs: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "errors": list(self.errors),
            "experiment_count": len(self.experiments),
            "experiments": list(self.experiments),
            "format": LEDGER_FORMAT,
            "metric_row_count": len(self.metrics),
            "metrics": list(self.metrics),
            "ok": self.ok,
            "run_count": len(self.runs),
            "runs": list(self.runs),
        }


def _load_toml(path: Path) -> Mapping[str, Any]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CollaborationError(f"cannot parse experiment TOML {path}: {exc}") from exc
    if not isinstance(document, dict):  # pragma: no cover - tomllib contract
        raise CollaborationError(f"experiment TOML is not a table: {path}")
    return document


def _id_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _relative_if_inside(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _experiment_row(
    bundle: ExperimentBundle,
    summary: Any,
    document: Mapping[str, Any],
    state: Any,
) -> dict[str, Any]:
    experiment = document.get("experiment")
    execution = document.get("execution")
    if not isinstance(experiment, Mapping) or not isinstance(execution, Mapping):
        raise CollaborationError(f"bundle {bundle.id!r} has an incomplete BMP declaration")
    protocol = experiment.get("protocol")
    configuration = experiment.get("configuration")
    design = experiment.get("design")
    budget = execution.get("budget")
    if not isinstance(configuration, Mapping):
        configuration = {}
    if not isinstance(design, Mapping):
        design = {}
    if not isinstance(budget, Mapping):
        budget = {}
    latest = state.runs[-1] if state.runs else None
    return {
        "available": summary.available,
        "backend_id": execution.get("backend"),
        "benchmark_id": experiment.get("benchmark"),
        "blocker_count": len(state.blockers),
        "bmp_spec": bundle.bmp_spec,
        "bmp_spec_sha256": bundle.bmp_spec_sha256,
        "bundle_id": bundle.id,
        "case_ids": list(bundle.design.planned_case_ids),
        "configuration_profiles": _id_list(configuration.get("profiles")),
        "dataset_id": experiment.get("dataset"),
        "dependencies_complete": summary.dependencies_complete,
        "evidence_classification": bundle.evidence.classification,
        "evaluator_id": experiment.get("evaluator"),
        "execution_mode": bundle.execution.mode.value,
        "experiment_id": bundle.id,
        "factors": _id_list(experiment.get("factors")),
        "hypothesis": bundle.design.hypothesis,
        "lab_issue": bundle.lab_issue,
        "lab_revision": state.revision,
        "lab_status": state.status.value,
        "latest_run_id": None if latest is None else latest.run_id,
        "latest_run_state": None if latest is None else latest.state.value,
        "lease_holder": summary.lease_holder,
        "max_cost": budget.get("max_cost"),
        "max_tokens": budget.get("max_tokens"),
        "max_wall_seconds": budget.get("max_wall_seconds"),
        "metric_ids": _id_list(experiment.get("metrics")),
        "model": execution.get("model"),
        "owner": state.owner,
        "primary_metric_ids": list(bundle.design.primary_metrics),
        "protocol_id": protocol,
        "purpose": design.get("purpose", bundle.purpose.value),
        "question": bundle.design.question,
        "regime_id": experiment.get("regime"),
        "repetitions_per_case": bundle.design.repetitions_per_case,
        "seeds": list(bundle.design.seeds),
        "stage_id": experiment.get("stage"),
        "subject_id": experiment.get("subject"),
        "summary": bundle.summary,
        "updated_at": state.updated_at.isoformat().replace("+00:00", "Z"),
    }


def _declaration_row(
    root: Path,
    path: Path,
    document: Mapping[str, Any],
    states: tuple[Any, ...],
) -> dict[str, Any]:
    experiment = document.get("experiment")
    execution = document.get("execution")
    if not isinstance(experiment, Mapping) or not isinstance(execution, Mapping):
        raise CollaborationError(f"experiment declaration is incomplete: {path}")
    experiment_id = experiment.get("id")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise CollaborationError(f"experiment declaration has no id: {path}")
    design = experiment.get("design")
    configuration = experiment.get("configuration")
    budget = execution.get("budget")
    if not isinstance(design, Mapping):
        design = {}
    if not isinstance(configuration, Mapping):
        configuration = {}
    if not isinstance(budget, Mapping):
        budget = {}
    relative = path.relative_to(root).as_posix()
    linked = tuple(state for state in states if state.issue.experiment == relative)
    latest_run = None
    linked_runs = [run for state in linked for run in state.runs]
    if linked_runs:
        latest_run = linked_runs[-1]
    statuses = sorted({state.status.value for state in linked})
    owners = sorted({state.owner for state in linked if state.owner is not None})
    blockers = sum(len(state.blockers) for state in linked)
    return {
        "available": None,
        "backend_id": execution.get("backend"),
        "benchmark_id": experiment.get("benchmark"),
        "blocker_count": blockers,
        "bmp_spec": relative,
        "bmp_spec_sha256": None,
        "bundle_id": None,
        "case_ids": [],
        "configuration_profiles": _id_list(configuration.get("profiles")),
        "dataset_id": experiment.get("dataset"),
        "dependencies_complete": None,
        "evaluator_id": experiment.get("evaluator"),
        "evidence_classification": "unmanaged",
        "execution_mode": None,
        "experiment_id": experiment_id,
        "factors": _id_list(experiment.get("factors")),
        "hypothesis": None,
        "lab_issue": None if not linked else ",".join(state.issue.issue_id for state in linked),
        "lab_revision": None if not linked else ",".join(state.revision for state in linked),
        "lab_status": "unmanaged" if not statuses else ",".join(statuses),
        "latest_run_id": None if latest_run is None else latest_run.run_id,
        "latest_run_state": None if latest_run is None else latest_run.state.value,
        "lease_holder": None,
        "max_cost": budget.get("max_cost"),
        "max_tokens": budget.get("max_tokens"),
        "max_wall_seconds": budget.get("max_wall_seconds"),
        "metric_ids": _id_list(experiment.get("metrics")),
        "model": execution.get("model"),
        "owner": None if not owners else ",".join(owners),
        "primary_metric_ids": [],
        "protocol_id": experiment.get("protocol"),
        "purpose": design.get("purpose"),
        "question": None,
        "regime_id": experiment.get("regime"),
        "repetitions_per_case": None,
        "seeds": ([] if execution.get("seed") is None else [execution.get("seed")]),
        "stage_id": experiment.get("stage"),
        "subject_id": experiment.get("subject"),
        "summary": None,
        "updated_at": (
            None
            if not linked
            else max(state.updated_at for state in linked).isoformat().replace("+00:00", "Z")
        ),
    }


def _relocate(path: str, path_map: Mapping[str, str]) -> Path:
    matches = [
        prefix
        for prefix in path_map
        if path == prefix or path.startswith(prefix.rstrip("/") + "/")
    ]
    if not matches:
        return Path(path)
    prefix = max(matches, key=len)
    suffix = path[len(prefix) :].lstrip("/")
    if any(part in {".", ".."} for part in suffix.split("/") if part):
        raise CollaborationError(f"mapped artifact path may traverse: {path!r}")
    return Path(path_map[prefix]) / suffix


def parse_path_maps(values: tuple[str, ...] | list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise CollaborationError("--map values must use absolute OLD=NEW paths")
        old, new = value.split("=", 1)
        if not old or not new or not Path(old).is_absolute() or not Path(new).is_absolute():
            raise CollaborationError("--map values must use absolute OLD=NEW paths")
        normalized_old = old.rstrip("/") or "/"
        normalized_new = new.rstrip("/") or "/"
        if normalized_old in result:
            raise CollaborationError(f"duplicate --map source prefix: {normalized_old}")
        result[normalized_old] = normalized_new
    return result


def _resolve_report_locator(
    root: Path,
    ref: LabArtifactRef,
    path_map: Mapping[str, str],
) -> Path:
    locator = ref.locator
    if "://" in locator:
        raise CollaborationError("remote report locators require local materialization")
    pure = PurePosixPath(locator)
    if pure.name not in _REPORT_NAMES:
        raise CollaborationError(
            "lab report_ref must name claim_report.json or observation_report.json"
        )
    lexical = (
        _relocate(locator, path_map)
        if pure.is_absolute()
        else root.joinpath(*pure.parts)
    )
    if lexical.is_symlink() or not lexical.is_file():
        raise CollaborationError(f"linked report is missing or non-regular: {locator}")
    resolved = lexical.resolve(strict=True)
    if not pure.is_absolute():
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CollaborationError(f"repository report locator escapes project root: {locator}") from exc
    return resolved


def _report_ref_matches(path: Path, ref: LabArtifactRef) -> bool:
    content = path.read_bytes()
    return len(content) == ref.size_bytes and hashlib.sha256(content).hexdigest() == ref.sha256


def _manifest_rows(
    verified: Any,
    root: Path,
    path_map: Mapping[str, str],
) -> tuple[dict[str, ResolvedBmpManifest], list[str]]:
    manifests: dict[str, ResolvedBmpManifest] = {}
    paths: list[str] = []
    for ref in verified.record_index.manifest_refs:
        manifest_path = _relocate(ref.path, path_map).expanduser().resolve()
        try:
            manifest = ResolvedBmpManifest.model_validate_json(manifest_path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:  # verifier already passed
            raise CollaborationError(f"cannot reload verified manifest {ref.path}: {exc}") from exc
        manifests[manifest.metadata.run_id] = manifest
        paths.append(_relative_if_inside(manifest_path, root))
    return manifests, paths


def _method_id(manifest: ResolvedBmpManifest) -> str:
    configuration = manifest.metadata.configuration
    if configuration is not None:
        return configuration.id
    if manifest.metadata.meta_evolver is not None:
        return manifest.metadata.meta_evolver.id
    if manifest.metadata.evolver is not None:
        return manifest.metadata.evolver.id
    return manifest.subject.id


def _metric_row(
    *,
    bundle: ExperimentBundle,
    lab_run_id: str,
    report: Any,
    result: Any,
    manifest: ResolvedBmpManifest,
) -> dict[str, Any]:
    uncertainty = result.uncertainty
    return {
        "backend_id": manifest.execution.backend.id,
        "benchmark_id": manifest.benchmark.id,
        "dataset_commit": manifest.dataset.commit,
        "dataset_digest": manifest.dataset.source_content_digest,
        "dataset_id": manifest.dataset.id,
        "dataset_split": manifest.dataset.split,
        "excluded_count": result.excluded_count,
        "experiment_id": bundle.id,
        "invalid_count": result.invalid_count,
        "lab_run_id": lab_run_id,
        "manifest_digest": result.manifest_digest,
        "method_id": _method_id(manifest),
        "metric_digest": result.metric_digest,
        "metric_id": result.metric_id,
        "metric_state": result.state.value,
        "missing_count": result.missing_count,
        "model": manifest.execution.model,
        "observed_count": result.observed_count,
        "parent_run_id": result.parent_run_id,
        "planned_rollout_count": result.planned_rollout_count,
        "purpose": report.purpose.value,
        "reason": result.reason,
        "rollouts_per_task": result.rollouts_per_task,
        "subject_id": manifest.subject.id,
        "task_count": result.task_count,
        "uncertainty_confidence_level": (
            None if uncertainty is None else uncertainty.confidence_level
        ),
        "uncertainty_lower": None if uncertainty is None else uncertainty.lower,
        "uncertainty_method": None if uncertainty is None else uncertainty.method.value,
        "uncertainty_upper": None if uncertainty is None else uncertainty.upper,
        "value": result.value,
        "zero_filled_count": result.zero_filled_count,
    }


def _run_rows(
    root: Path,
    bundle: ExperimentBundle,
    state: Any,
    *,
    path_map: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    relocation = {} if path_map is None else dict(path_map)
    runs: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for lab_run in state.runs:
        base = {
            "claim_eligible": None,
            "experiment_id": bundle.id,
            "failure_breakdown": {},
            "isolation_valid": None,
            "lab_issue": bundle.lab_issue,
            "lab_run_id": lab_run.run_id,
            "manifest_digest": lab_run.manifest_digest,
            "metric_row_count": 0,
            "protocol_valid": None,
            "purpose": bundle.purpose.value,
            "record_root": lab_run.record_root,
            "report_ref": (
                None if lab_run.report_ref is None else lab_run.report_ref.locator
            ),
            "run_state": lab_run.state.value,
            "standalone_verification": "not-applicable",
            "validity_gates": {},
            "verified_manifest_paths": [],
        }
        if lab_run.state != LabRunState.finished:
            runs.append(base)
            continue
        assert lab_run.report_ref is not None
        base["standalone_verification"] = "failed"
        try:
            report_path = _resolve_report_locator(root, lab_run.report_ref, relocation)
            if not _report_ref_matches(report_path, lab_run.report_ref):
                raise CollaborationError("lab report_ref digest or size does not match linked bytes")
            verified = verify_run_report(report_path, path_map=relocation)
            report = verified.report
            if report.experiment_id != bundle.id:
                raise CollaborationError(
                    f"report experiment_id {report.experiment_id!r} differs from bundle {bundle.id!r}"
                )
            if lab_run.manifest_digest is not None and report.manifest_digest != lab_run.manifest_digest:
                raise CollaborationError("lab run manifest digest differs from verified report")
            manifests, manifest_paths = _manifest_rows(verified, root, relocation)
            run_metric_rows = []
            for result in report.metric_results:
                manifest = manifests.get(result.parent_run_id)
                if manifest is None:
                    raise CollaborationError(
                        f"verified metric parent run has no manifest: {result.parent_run_id}"
                    )
                run_metric_rows.append(
                    _metric_row(
                        bundle=bundle,
                        lab_run_id=lab_run.run_id,
                        report=report,
                        result=result,
                        manifest=manifest,
                    )
                )
            base.update(
                {
                    "claim_eligible": (
                        report.claim_eligible if isinstance(report, ClaimReport) else None
                    ),
                    "failure_breakdown": {
                        key.value: value for key, value in report.failure_breakdown.items()
                    },
                    "isolation_valid": (
                        None if isinstance(report, ClaimReport) else report.isolation_valid
                    ),
                    "metric_row_count": len(run_metric_rows),
                    "protocol_valid": (
                        None if isinstance(report, ClaimReport) else report.protocol_valid
                    ),
                    "purpose": report.purpose.value,
                    "standalone_verification": "verified",
                    "validity_gates": (
                        {
                            key.value: value.valid
                            for key, value in report.gates.items()
                        }
                        if isinstance(report, ClaimReport)
                        else {
                            "isolation_valid": report.isolation_valid,
                            "protocol_valid": report.protocol_valid,
                        }
                    ),
                    "verified_manifest_paths": manifest_paths,
                }
            )
            metrics.extend(run_metric_rows)
        except (CollaborationError, ReportVerificationError, OSError, ValueError) as exc:
            errors.append(
                {
                    "code": "run-verification",
                    "message": str(exc),
                    "source": f"{bundle.lab_issue}/{lab_run.run_id}",
                }
            )
        runs.append(base)
    return runs, metrics, errors


def build_experiment_ledger(
    project_root: str | Path,
    *,
    at: datetime | None = None,
    path_map: Mapping[str, str] | None = None,
) -> ExperimentLedger:
    """Join every checked-in experiment bundle with lab and verified run facts."""

    root = Path(os.path.abspath(os.fspath(Path(project_root).expanduser()))).resolve(strict=True)
    repository = ExperimentRepository(root)
    validation = repository.validate(at=at)
    if not validation.ok:
        return ExperimentLedger(
            experiments=(),
            runs=(),
            metrics=(),
            errors=tuple(
                {
                    "code": finding.code,
                    "message": finding.message,
                    "source": finding.path or "repository",
                }
                for finding in validation.errors
            ),
        )
    summaries = {item.id: item for item in validation.bundles}
    store = LabStore(root)
    states = store.list()
    experiments: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    bundle_ids: set[str] = set()
    for path in repository.bundle_paths():
        bundle = repository.load_bundle(path)
        bundle_ids.add(bundle.id)
        document = _load_toml(root / bundle.bmp_spec)
        state = store.load(bundle.lab_issue)
        experiments.append(
            _experiment_row(bundle, summaries[bundle.id], document, state)
        )
        run_rows, metric_rows, run_errors = _run_rows(
            root,
            bundle,
            state,
            path_map=path_map,
        )
        runs.extend(run_rows)
        metrics.extend(metric_rows)
        errors.extend(run_errors)
    declarations_root = root / "MagentaBench/conformance/experiments"
    for declaration in sorted(declarations_root.glob("*.toml")):
        document = _load_toml(declaration)
        experiment = document.get("experiment")
        experiment_id = experiment.get("id") if isinstance(experiment, Mapping) else None
        if experiment_id in bundle_ids:
            continue
        try:
            experiments.append(
                _declaration_row(root, declaration, document, states)
            )
        except CollaborationError as exc:
            errors.append(
                {
                    "code": "declaration-invalid",
                    "message": str(exc),
                    "source": declaration.relative_to(root).as_posix(),
                }
            )
    return ExperimentLedger(
        experiments=tuple(sorted(experiments, key=lambda item: item["experiment_id"])),
        runs=tuple(sorted(runs, key=lambda item: (item["experiment_id"], item["lab_run_id"]))),
        metrics=tuple(
            sorted(
                metrics,
                key=lambda item: (
                    item["experiment_id"],
                    item["lab_run_id"],
                    item["parent_run_id"],
                    item["metric_id"],
                ),
            )
        ),
        errors=tuple(errors),
    )


def render_csv(ledger: ExperimentLedger, table: str) -> str:
    rows = getattr(ledger, table)
    columns = _CSV_COLUMNS[table]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: (
                    json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if isinstance(value, (dict, list))
                    else value
                )
                for key, value in row.items()
            }
        )
    return output.getvalue()


__all__ = [
    "LEDGER_FORMAT",
    "ExperimentLedger",
    "build_experiment_ledger",
    "parse_path_maps",
    "render_csv",
]
