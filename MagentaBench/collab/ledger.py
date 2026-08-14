"""Read-only experiment ledger derived from repository evidence.

The ledger deliberately does not persist a global progress board.  It joins
the independently owned bundle, lab, BMP declaration, and report records into
stable rows that can be regenerated on any checkout.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from pydantic import ValidationError

from MagentaBench.lab import LabArtifactRef, LabRunState, LabStatus, LabStore
from MagentaBench.runner.compiler import Compiler
from MagentaBench.schemas.models import ClaimReport, ResolvedBmpManifest
from MagentaBench.schemas.verification import (
    ReportVerificationError,
    verify_run_report,
)

from .models import ExperimentBundle
from .repository import CollaborationError, ExperimentRepository


LEDGER_FORMAT = "magentabench-experiment-ledger-v2"
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
        "run_count",
        "run_ids",
        "run_states",
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
        "verified_manifest_refs",
    ),
    "metrics": (
        "experiment_id",
        "lab_run_id",
        "parent_run_id",
        "manifest_digest",
        "method_id",
        "factor_values",
        "configuration_id",
        "configuration_digest",
        "configuration_profiles",
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
    "sources": (
        "source_id",
        "record_origin",
        "repository",
        "commit_sha",
        "tree_oid",
        "ref_hint",
        "visibility",
        "normalizer_id",
        "normalizer_digest",
        "source_digest",
        "snapshot_path",
        "record_count",
        "evidence_tiers",
    ),
    "catalog": (
        "catalog_id",
        "record_origin",
        "source_id",
        "record_id",
        "record_kind",
        "experiment_id",
        "benchmark_id",
        "dataset_id",
        "dataset_commit",
        "dataset_digest",
        "dataset_split",
        "method_id",
        "subject_id",
        "model",
        "provider_id",
        "harness_id",
        "evaluator_id",
        "execution_mode",
        "backend_id",
        "protocol_id",
        "purpose",
        "condition_digest",
        "metric_ids",
        "evidence_tier",
        "comparability",
        "claim_eligible",
        "limitations",
    ),
    "observations": (
        "observation_id",
        "record_origin",
        "source_id",
        "record_id",
        "experiment_id",
        "run_id",
        "parent_run_id",
        "method_id",
        "subject_id",
        "model",
        "provider_id",
        "harness_id",
        "benchmark_id",
        "dataset_id",
        "dataset_commit",
        "dataset_digest",
        "dataset_split",
        "evaluator_id",
        "execution_mode",
        "backend_id",
        "protocol_id",
        "purpose",
        "factor_values",
        "configuration_id",
        "configuration_digest",
        "configuration_profiles",
        "condition_digest",
        "metric_id",
        "metric_digest",
        "metric_state",
        "value",
        "unit",
        "direction",
        "aggregation",
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
        "evidence_tier",
        "comparability",
        "claim_eligible",
        "limitations",
        "provenance_paths",
    ),
    "assets": (
        "asset_id",
        "record_origin",
        "source_id",
        "record_id",
        "experiment_id",
        "run_id",
        "role",
        "status",
        "media_type",
        "locator",
        "content_sha256",
        "git_blob_oid",
        "size_bytes",
        "evidence_tier",
        "materialization_state",
        "limitations",
    ),
}


@dataclass(frozen=True)
class ExperimentLedger:
    experiments: tuple[dict[str, Any], ...]
    runs: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...] = ()
    catalog: tuple[dict[str, Any], ...] = ()
    observations: tuple[dict[str, Any], ...] = ()
    assets: tuple[dict[str, Any], ...] = ()
    errors: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_count": len(self.assets),
            "assets": list(self.assets),
            "catalog": list(self.catalog),
            "catalog_count": len(self.catalog),
            "errors": list(self.errors),
            "experiment_count": len(self.experiments),
            "experiments": list(self.experiments),
            "format": LEDGER_FORMAT,
            "metric_row_count": len(self.metrics),
            "metrics": list(self.metrics),
            "observation_count": len(self.observations),
            "observations": list(self.observations),
            "ok": self.ok,
            "run_count": len(self.runs),
            "runs": list(self.runs),
            "source_count": len(self.sources),
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class _ManifestSnapshot:
    by_run_id: Mapping[str, ResolvedBmpManifest]
    identities: tuple[tuple[str, str], ...]
    refs: tuple[str, ...]


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


def _stable_locator(path: str | Path, root: Path, *, digest: str | None = None) -> str:
    """Render a locator without making generated output host-dependent."""

    if isinstance(path, str) and "://" in path:
        return f"sha256:{digest}" if digest is not None else "<external>"
    candidate = Path(path)
    if not candidate.is_absolute():
        return PurePosixPath(candidate).as_posix()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        if digest is not None:
            return f"sha256:{digest}"
        return "<external>"


def _run_projection(
    runs: tuple[Any, ...] | list[Any],
) -> tuple[int, list[str], dict[str, list[str]]]:
    states: dict[str, set[str]] = {}
    for run in runs:
        states.setdefault(run.run_id, set()).add(run.state.value)
    run_ids = sorted(states)
    return (
        len(run_ids),
        run_ids,
        {run_id: sorted(states[run_id]) for run_id in run_ids},
    )


def _experiment_row(
    bundle: ExperimentBundle,
    document: Mapping[str, Any],
    state: Any,
    states: Mapping[str, Any],
    evaluated_at: datetime,
) -> dict[str, Any]:
    experiment = document.get("experiment")
    execution = document.get("execution")
    if not isinstance(experiment, Mapping) or not isinstance(execution, Mapping):
        raise CollaborationError(
            f"bundle {bundle.id!r} has an incomplete BMP declaration"
        )
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
    dependencies_complete = all(
        states[issue_id].status == LabStatus.done
        for issue_id in state.issue.dependencies
    )
    active_lease = state.active_lease(evaluated_at)
    available = (
        state.status in {LabStatus.open, LabStatus.planned, LabStatus.ready}
        and not state.blockers
        and dependencies_complete
        and active_lease is None
    )
    run_count, run_ids, run_states = _run_projection(list(state.runs))
    return {
        "available": available,
        "backend_id": execution.get("backend"),
        "benchmark_id": experiment.get("benchmark"),
        "blocker_count": len(state.blockers),
        "bmp_spec": bundle.bmp_spec,
        "bmp_spec_sha256": bundle.bmp_spec_sha256,
        "bundle_id": bundle.id,
        "case_ids": list(bundle.design.planned_case_ids),
        "configuration_profiles": _id_list(configuration.get("profiles")),
        "dataset_id": experiment.get("dataset"),
        "dependencies_complete": dependencies_complete,
        "evidence_classification": bundle.evidence.classification,
        "evaluator_id": experiment.get("evaluator"),
        "execution_mode": bundle.execution.mode.value,
        "experiment_id": bundle.id,
        "factors": _id_list(experiment.get("factors")),
        "hypothesis": bundle.design.hypothesis,
        "lab_issue": bundle.lab_issue,
        "lab_revision": state.revision,
        "lab_status": state.status.value,
        "lease_holder": None if active_lease is None else active_lease.owner,
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
        "run_count": run_count,
        "run_ids": run_ids,
        "run_states": run_states,
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
    linked_runs = [run for state in linked for run in state.runs]
    run_count, run_ids, run_states = _run_projection(linked_runs)
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
        "lab_issue": None
        if not linked
        else ",".join(state.issue.issue_id for state in linked),
        "lab_revision": None
        if not linked
        else ",".join(state.revision for state in linked),
        "lab_status": "unmanaged" if not statuses else ",".join(statuses),
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
        "run_count": run_count,
        "run_ids": run_ids,
        "run_states": run_states,
        "seeds": ([] if execution.get("seed") is None else [execution.get("seed")]),
        "stage_id": experiment.get("stage"),
        "subject_id": experiment.get("subject"),
        "summary": None,
        "updated_at": (
            None
            if not linked
            else max(state.updated_at for state in linked)
            .isoformat()
            .replace("+00:00", "Z")
        ),
    }


def _relocate(path: str, path_map: Mapping[str, str]) -> Path:
    matches = [
        prefix
        for prefix in path_map
        if path == prefix
        or (prefix == "/" and path.startswith("/"))
        or (prefix != "/" and path.startswith(prefix + "/"))
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
        if (
            not old
            or not new
            or not Path(old).is_absolute()
            or not Path(new).is_absolute()
        ):
            raise CollaborationError("--map values must use absolute OLD=NEW paths")
        normalized_old_path = PurePosixPath(old)
        normalized_new_path = PurePosixPath(new)
        if (
            old.startswith("//")
            or new.startswith("//")
            or old != normalized_old_path.as_posix()
            or new != normalized_new_path.as_posix()
            or any(part in {".", ".."} for part in normalized_old_path.parts)
            or any(part in {".", ".."} for part in normalized_new_path.parts)
        ):
            raise CollaborationError(
                "--map values must use normalized absolute OLD=NEW paths"
            )
        normalized_old = normalized_old_path.as_posix()
        normalized_new = normalized_new_path.as_posix()
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
            raise CollaborationError(
                f"repository report locator escapes project root: {locator}"
            ) from exc
    return resolved


def _read_verified_ref(path: Path, ref: Any, *, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        locator = getattr(ref, "path", getattr(ref, "locator", str(path)))
        raise CollaborationError(f"cannot read {label}: {locator}") from exc
    if (
        len(content) != ref.size_bytes
        or hashlib.sha256(content).hexdigest() != ref.sha256
    ):
        raise CollaborationError(
            f"{label} digest or size does not match its ArtifactRef"
        )
    return content


def _verify_report_snapshot(
    report_path: Path,
    ref: LabArtifactRef,
    *,
    path_map: Mapping[str, str],
) -> Any:
    """Verify an immutable copy of the report bytes bound by the lab link."""

    content = _read_verified_ref(report_path, ref, label="linked report")
    with tempfile.TemporaryDirectory(prefix="magentabench-ledger-") as directory:
        snapshot = Path(directory) / report_path.name
        snapshot.write_bytes(content)
        return verify_run_report(snapshot, path_map=path_map)


def _manifest_rows(
    verified: Any,
    root: Path,
    path_map: Mapping[str, str],
) -> _ManifestSnapshot:
    manifests: dict[str, ResolvedBmpManifest] = {}
    refs: list[str] = []
    identities: list[tuple[str, str]] = []
    for ref in verified.record_index.manifest_refs:
        manifest_path = _relocate(ref.path, path_map).expanduser().resolve()
        try:
            content = _read_verified_ref(manifest_path, ref, label="verified manifest")
            manifest = ResolvedBmpManifest.model_validate_json(content)
        except (OSError, ValidationError, ValueError) as exc:  # verifier already passed
            raise CollaborationError(
                f"cannot reload verified manifest {ref.path}: {exc}"
            ) from exc
        run_id = manifest.metadata.run_id
        if run_id in manifests:
            raise CollaborationError(
                f"verified manifests contain duplicate run_id: {run_id}"
            )
        manifests[run_id] = manifest
        identities.append((run_id, manifest.canonical_digest()))
        refs.append(_stable_locator(manifest_path, root, digest=ref.sha256))
    return _ManifestSnapshot(
        by_run_id=manifests,
        # Record-index order may reflect parallel completion order. Run IDs
        # remain the stable key, while duplicate IDs fail above.
        identities=tuple(sorted(identities)),
        refs=tuple(refs),
    )


def _method_id(manifest: ResolvedBmpManifest) -> str:
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
    configuration = manifest.metadata.configuration
    return {
        "backend_id": manifest.execution.backend.id,
        "benchmark_id": manifest.benchmark.id,
        "configuration_digest": (
            None if configuration is None else configuration.artifact_digest
        ),
        "configuration_id": None if configuration is None else configuration.id,
        "configuration_profiles": (
            [] if configuration is None else list(configuration.profiles)
        ),
        "dataset_commit": manifest.dataset.commit,
        "dataset_digest": manifest.dataset.source_content_digest,
        "dataset_id": manifest.dataset.id,
        "dataset_split": manifest.dataset.split,
        "excluded_count": result.excluded_count,
        "experiment_id": bundle.id,
        "factor_values": dict(manifest.metadata.factors),
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


def _expected_manifest_identities(
    root: Path,
    bundle: ExperimentBundle,
) -> tuple[tuple[str, str], ...]:
    """Resolve the pinned BMP declaration into the identities it permits."""

    spec_path = root.joinpath(*PurePosixPath(bundle.bmp_spec).parts)
    if spec_path.is_symlink() or not spec_path.is_file():
        raise CollaborationError(
            f"pinned BMP declaration is missing or non-regular: {bundle.bmp_spec}"
        )
    try:
        spec_bytes = spec_path.read_bytes()
    except OSError as exc:
        raise CollaborationError(
            f"cannot read pinned BMP declaration: {bundle.bmp_spec}"
        ) from exc
    observed = hashlib.sha256(spec_bytes).hexdigest()
    if observed != bundle.bmp_spec_sha256:
        raise CollaborationError(
            "pinned BMP declaration digest differs from the experiment bundle"
        )
    # Compile a private copy of the exact bytes whose digest was checked. The
    # compiler opens its input path itself, so passing the repository path
    # after hashing would reintroduce a replacement race.
    try:
        with tempfile.TemporaryDirectory(
            prefix="magentabench-ledger-spec-"
        ) as directory:
            snapshot = Path(directory) / spec_path.name
            snapshot.write_bytes(spec_bytes)
            compiled = Compiler(root).compile(snapshot)
    except ValueError as exc:
        raise CollaborationError(
            f"cannot compile pinned BMP declaration {bundle.bmp_spec}: {exc}"
        ) from exc
    identities = tuple(
        sorted((run.manifest.metadata.run_id, run.manifest_digest) for run in compiled)
    )
    if len({run_id for run_id, _ in identities}) != len(identities):
        raise CollaborationError(
            "pinned BMP declaration produced duplicate run identities"
        )
    return identities


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
    expected_identities: tuple[tuple[str, str], ...] | None = None
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
            "record_root": _stable_locator(lab_run.record_root, root),
            "report_ref": (
                None
                if lab_run.report_ref is None
                else _stable_locator(
                    lab_run.report_ref.locator,
                    root,
                    digest=lab_run.report_ref.sha256,
                )
            ),
            "run_state": lab_run.state.value,
            "standalone_verification": "not-applicable",
            "validity_gates": {},
            "verified_manifest_refs": [],
        }
        if lab_run.state != LabRunState.finished:
            runs.append(base)
            continue
        assert lab_run.report_ref is not None
        base["standalone_verification"] = "failed"
        try:
            report_path = _resolve_report_locator(root, lab_run.report_ref, relocation)
            verified = _verify_report_snapshot(
                report_path,
                lab_run.report_ref,
                path_map=relocation,
            )
            report = verified.report
            if report.experiment_id != bundle.id:
                raise CollaborationError(
                    f"report experiment_id {report.experiment_id!r} differs from bundle {bundle.id!r}"
                )
            if (
                lab_run.manifest_digest is not None
                and report.manifest_digest != lab_run.manifest_digest
            ):
                raise CollaborationError(
                    "lab run manifest digest differs from verified report"
                )
            manifest_snapshot = _manifest_rows(verified, root, relocation)
            if expected_identities is None:
                expected_identities = _expected_manifest_identities(root, bundle)
            if manifest_snapshot.identities != expected_identities:
                raise CollaborationError(
                    "verified manifest identities differ from the pinned BMP declaration"
                )
            run_metric_rows = []
            for result in report.metric_results:
                manifest = manifest_snapshot.by_run_id.get(result.parent_run_id)
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
                        report.claim_eligible
                        if isinstance(report, ClaimReport)
                        else None
                    ),
                    "failure_breakdown": {
                        key.value: value
                        for key, value in report.failure_breakdown.items()
                    },
                    "isolation_valid": (
                        None
                        if isinstance(report, ClaimReport)
                        else report.isolation_valid
                    ),
                    "metric_row_count": len(run_metric_rows),
                    "protocol_valid": (
                        None
                        if isinstance(report, ClaimReport)
                        else report.protocol_valid
                    ),
                    "purpose": report.purpose.value,
                    "standalone_verification": "verified",
                    "validity_gates": (
                        {key.value: value.valid for key, value in report.gates.items()}
                        if isinstance(report, ClaimReport)
                        else {
                            "isolation_valid": report.isolation_valid,
                            "protocol_valid": report.protocol_valid,
                        }
                    ),
                    "verified_manifest_refs": list(manifest_snapshot.refs),
                }
            )
            metrics.extend(run_metric_rows)
        except (
            CollaborationError,
            ReportVerificationError,
            OSError,
            ValueError,
        ) as exc:
            errors.append(
                {
                    "code": "run-verification",
                    "message": str(exc),
                    "source": f"{bundle.lab_issue}/{lab_run.run_id}",
                }
            )
        runs.append(base)
    return runs, metrics, errors


def _projection_id(prefix: str, payload: Mapping[str, Any]) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(content).hexdigest()}"


def _bmp_observation_rows(
    experiments: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    experiment_by_id = {item["experiment_id"]: item for item in experiments}
    run_by_id = {
        (item["experiment_id"], item["lab_run_id"]): item for item in runs
    }
    rows: list[dict[str, Any]] = []
    for metric in metrics:
        experiment = experiment_by_id[metric["experiment_id"]]
        run = run_by_id[(metric["experiment_id"], metric["lab_run_id"])]
        identity = {
            "experiment_id": metric["experiment_id"],
            "lab_run_id": metric["lab_run_id"],
            "metric_id": metric["metric_id"],
            "parent_run_id": metric["parent_run_id"],
            "record_origin": "bmp",
        }
        provenance_paths = [
            value
            for value in (
                experiment.get("bmp_spec"),
                run.get("report_ref"),
                *run.get("verified_manifest_refs", []),
            )
            if value is not None
        ]
        rows.append(
            {
                "aggregation": None,
                "backend_id": metric["backend_id"],
                "benchmark_id": metric["benchmark_id"],
                "claim_eligible": run.get("claim_eligible") is True,
                "comparability": "exact-identity",
                "condition_digest": metric["configuration_digest"],
                "configuration_digest": metric["configuration_digest"],
                "configuration_id": metric["configuration_id"],
                "configuration_profiles": metric["configuration_profiles"],
                "dataset_commit": metric["dataset_commit"],
                "dataset_digest": metric["dataset_digest"],
                "dataset_id": metric["dataset_id"],
                "dataset_split": metric["dataset_split"],
                "direction": None,
                "evaluator_id": experiment["evaluator_id"],
                "evidence_tier": "bmp-standalone",
                "excluded_count": metric["excluded_count"],
                "execution_mode": experiment["execution_mode"],
                "experiment_id": metric["experiment_id"],
                "factor_values": metric["factor_values"],
                "harness_id": None,
                "invalid_count": metric["invalid_count"],
                "limitations": [],
                "method_id": metric["method_id"],
                "metric_digest": metric["metric_digest"],
                "metric_id": metric["metric_id"],
                "metric_state": metric["metric_state"],
                "missing_count": metric["missing_count"],
                "model": metric["model"],
                "observation_id": _projection_id("bmp-observation", identity),
                "observed_count": metric["observed_count"],
                "parent_run_id": metric["parent_run_id"],
                "planned_rollout_count": metric["planned_rollout_count"],
                "protocol_id": experiment["protocol_id"],
                "provenance_paths": provenance_paths,
                "provider_id": None,
                "purpose": metric["purpose"],
                "record_id": None,
                "record_origin": "bmp",
                "rollouts_per_task": metric["rollouts_per_task"],
                "run_id": metric["lab_run_id"],
                "source_id": f"bmp:{metric['experiment_id']}",
                "subject_id": metric["subject_id"],
                "task_count": metric["task_count"],
                "uncertainty_confidence_level": metric[
                    "uncertainty_confidence_level"
                ],
                "uncertainty_lower": metric["uncertainty_lower"],
                "uncertainty_method": metric["uncertainty_method"],
                "uncertainty_upper": metric["uncertainty_upper"],
                "unit": None,
                "value": metric["value"],
                "zero_filled_count": metric["zero_filled_count"],
            }
        )
    return rows


def _bmp_source_rows(
    root: Path,
    experiments: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observed_ids = {item["experiment_id"] for item in observations}
    normalizer_id = "magentabench.bmp-ledger.v2"
    normalizer_digest = hashlib.sha256(normalizer_id.encode("utf-8")).hexdigest()
    rows: list[dict[str, Any]] = []
    for experiment in experiments:
        source_digest = experiment["bmp_spec_sha256"]
        if source_digest is None:
            source = root / experiment["bmp_spec"]
            if source.is_file() and not source.is_symlink():
                source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        tier = (
            "bmp-standalone"
            if experiment["experiment_id"] in observed_ids
            else "declaration-only"
        )
        rows.append(
            {
                "commit_sha": None,
                "evidence_tiers": [tier],
                "normalizer_digest": normalizer_digest,
                "normalizer_id": normalizer_id,
                "record_count": 1,
                "record_origin": "bmp",
                "ref_hint": None,
                "repository": ".",
                "snapshot_path": experiment["bmp_spec"],
                "source_digest": source_digest,
                "source_id": f"bmp:{experiment['experiment_id']}",
                "tree_oid": None,
                "visibility": "repository",
            }
        )
    return rows


def _bmp_catalog_rows(
    experiments: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observations_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        observations_by_experiment.setdefault(
            observation["experiment_id"], []
        ).append(observation)
    rows: list[dict[str, Any]] = []
    for experiment in experiments:
        observed = observations_by_experiment.get(experiment["experiment_id"], [])
        rows.append(
            {
                "backend_id": experiment["backend_id"],
                "benchmark_id": experiment["benchmark_id"],
                "catalog_id": f"bmp:{experiment['experiment_id']}",
                "claim_eligible": any(item["claim_eligible"] for item in observed),
                "comparability": (
                    "exact-identity" if observed else "declared-identity"
                ),
                "condition_digest": None,
                "dataset_commit": None,
                "dataset_digest": None,
                "dataset_id": experiment["dataset_id"],
                "dataset_split": None,
                "evaluator_id": experiment["evaluator_id"],
                "evidence_tier": (
                    "bmp-standalone" if observed else "declaration-only"
                ),
                "execution_mode": experiment["execution_mode"],
                "experiment_id": experiment["experiment_id"],
                "harness_id": None,
                "limitations": [],
                "method_id": experiment["subject_id"],
                "metric_ids": experiment["metric_ids"],
                "model": experiment["model"],
                "protocol_id": experiment["protocol_id"],
                "provider_id": None,
                "purpose": experiment["purpose"],
                "record_id": None,
                "record_kind": "experiment",
                "record_origin": "bmp",
                "source_id": f"bmp:{experiment['experiment_id']}",
                "subject_id": experiment["subject_id"],
            }
        )
    return rows


def _content_digest_from_locator(locator: str) -> str | None:
    prefix = "sha256:"
    return locator[len(prefix) :] if locator.startswith(prefix) else None


def _bmp_asset_rows(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        references: list[tuple[str, str]] = []
        if run["report_ref"] is not None:
            references.append(("report", run["report_ref"]))
        references.extend(
            ("manifest", locator) for locator in run["verified_manifest_refs"]
        )
        for role, locator in references:
            identity = {
                "experiment_id": run["experiment_id"],
                "locator": locator,
                "role": role,
                "run_id": run["lab_run_id"],
            }
            rows.append(
                {
                    "asset_id": _projection_id("bmp-asset", identity),
                    "content_sha256": _content_digest_from_locator(locator),
                    "evidence_tier": (
                        "bmp-standalone"
                        if run["standalone_verification"] == "verified"
                        else "candidate"
                    ),
                    "experiment_id": run["experiment_id"],
                    "git_blob_oid": None,
                    "limitations": [],
                    "locator": locator,
                    "materialization_state": (
                        "external" if locator.startswith("sha256:") else "materialized"
                    ),
                    "media_type": "application/json",
                    "record_id": None,
                    "record_origin": "bmp",
                    "role": role,
                    "run_id": run["lab_run_id"],
                    "size_bytes": None,
                    "source_id": f"bmp:{run['experiment_id']}",
                    "status": run["standalone_verification"],
                }
            )
    return rows


def build_experiment_ledger(
    project_root: str | Path,
    *,
    at: datetime | None = None,
    path_map: Mapping[str, str] | None = None,
    imports_dir: str | Path = "imports",
) -> ExperimentLedger:
    """Join every checked-in experiment bundle with lab and verified run facts."""

    root = Path(os.path.abspath(os.fspath(Path(project_root).expanduser()))).resolve(
        strict=True
    )
    evaluated_at = at or datetime.now(timezone.utc)
    repository = ExperimentRepository(root)
    validation = repository.validate(at=evaluated_at)
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
    store = LabStore(root)
    states = store.list()
    states_by_id = {state.issue.issue_id: state for state in states}
    experiments: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    bundle_ids: set[str] = set()
    managed_specs: dict[str, str] = {}
    loaded_bundles: list[tuple[Path, ExperimentBundle]] = []
    for path in repository.bundle_paths():
        bundle = repository.load_bundle(path)
        if bundle.id in bundle_ids:
            errors.append(
                {
                    "code": "duplicate-experiment-id",
                    "message": f"multiple experiment bundles declare id {bundle.id!r}",
                    "source": path.relative_to(root).as_posix(),
                }
            )
            continue
        bundle_ids.add(bundle.id)
        managed_specs[bundle.id] = bundle.bmp_spec
        loaded_bundles.append((path, bundle))
    for path, bundle in loaded_bundles:
        try:
            document = _load_toml(root / bundle.bmp_spec)
            state = states_by_id[bundle.lab_issue]
            experiments.append(
                _experiment_row(bundle, document, state, states_by_id, evaluated_at)
            )
        except (CollaborationError, KeyError) as exc:
            errors.append(
                {
                    "code": "lab-snapshot",
                    "message": str(exc),
                    "source": path.relative_to(root).as_posix(),
                }
            )
            continue
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
    declarations: list[tuple[Path, Mapping[str, Any], str]] = []
    declaration_sources: dict[str, list[str]] = {}
    for declaration in sorted(declarations_root.glob("*.toml")):
        try:
            document = _load_toml(declaration)
            experiment = document.get("experiment")
            experiment_id = (
                experiment.get("id") if isinstance(experiment, Mapping) else None
            )
            if not isinstance(experiment_id, str) or not experiment_id:
                raise CollaborationError(
                    f"experiment declaration has no id: {declaration}"
                )
            declarations.append((declaration, document, experiment_id))
            declaration_sources.setdefault(experiment_id, []).append(
                declaration.relative_to(root).as_posix()
            )
        except CollaborationError as exc:
            errors.append(
                {
                    "code": "declaration-invalid",
                    "message": str(exc),
                    "source": declaration.relative_to(root).as_posix(),
                }
            )
    for experiment_id, sources in sorted(declaration_sources.items()):
        if len(sources) > 1:
            errors.append(
                {
                    "code": "duplicate-experiment-id",
                    "message": (
                        f"experiment id {experiment_id!r} is declared by "
                        + ", ".join(sources)
                    ),
                    "source": sources[0],
                }
            )
    for declaration, document, experiment_id in declarations:
        if experiment_id in bundle_ids:
            if (
                managed_specs.get(experiment_id)
                == declaration.relative_to(root).as_posix()
            ):
                continue
            # A bundle owns its pinned declaration. Any other declaration with
            # the same id is handled by the duplicate-ID error above.
            continue
        if len(declaration_sources[experiment_id]) > 1:
            continue
        try:
            experiments.append(_declaration_row(root, declaration, document, states))
        except CollaborationError as exc:
            errors.append(
                {
                    "code": "declaration-invalid",
                    "message": str(exc),
                    "source": declaration.relative_to(root).as_posix(),
                }
            )
    experiments = sorted(experiments, key=lambda item: item["experiment_id"])
    runs = sorted(runs, key=lambda item: (item["experiment_id"], item["lab_run_id"]))
    metrics = sorted(
        metrics,
        key=lambda item: (
            item["experiment_id"],
            item["lab_run_id"],
            item["parent_run_id"],
            item["metric_id"],
        ),
    )
    observations = _bmp_observation_rows(experiments, runs, metrics)
    sources = _bmp_source_rows(root, experiments, observations)
    catalog = _bmp_catalog_rows(experiments, observations)
    assets = _bmp_asset_rows(runs)
    return ExperimentLedger(
        experiments=tuple(experiments),
        runs=tuple(runs),
        metrics=tuple(metrics),
        sources=tuple(sources),
        catalog=tuple(catalog),
        observations=tuple(observations),
        assets=tuple(assets),
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
                    json.dumps(
                        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
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
