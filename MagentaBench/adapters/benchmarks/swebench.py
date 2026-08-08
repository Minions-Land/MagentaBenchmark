"""Content-addressed SWE-bench dataset loader for BMP adapters."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from MagentaBench.runner.adapter_registry import (
    AdapterRegistryError,
    LoadedCaseSet,
    ResolvedCaseSet,
    write_immutable_json,
)
from MagentaBench.runner.compiler import CompiledRun
from MagentaBench.runner.evidence import artifact_ref, sha256_file, source_closure_digest
from MagentaBench.schemas import ArtifactRef, CaseArtifact, CaseSetArtifact


_REQUIRED_FIELDS = frozenset(
    {
        "instance_id",
        "repo",
        "base_commit",
        "problem_statement",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
    }
)
_CASE_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


@dataclass(frozen=True)
class SweBenchCase:
    """Activated case split into public, execution, and verifier projections."""

    public: Mapping[str, Any]
    execution_contract: Mapping[str, Any]
    verifier_contract: Mapping[str, Any]
    case_set_digest: str

    @property
    def task_id(self) -> str:
        return str(self.public["instance_id"])


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _relative_file(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AdapterRegistryError(f"SWE-bench {label} must be a relative file")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise AdapterRegistryError(f"SWE-bench {label} must stay under benchmark source")
    candidate = root / relative
    for path in (candidate, *candidate.parents):
        if path == root.parent:
            break
        if path.is_symlink():
            raise AdapterRegistryError(f"SWE-bench {label} must not contain symlinks")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AdapterRegistryError(f"SWE-bench {label} is missing or escapes source") from exc
    if not resolved.is_file():
        raise AdapterRegistryError(f"SWE-bench {label} is not a file")
    return resolved


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AdapterRegistryError(f"SWE-bench {label} is malformed JSON") from exc
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise AdapterRegistryError(f"SWE-bench {label} must be a string list")
    if len(set(value)) != len(value):
        raise AdapterRegistryError(f"SWE-bench {label} contains duplicate tests")
    return tuple(value)


class SweBenchLoader:
    """Load a pinned SWE-bench JSON split without exposing oracle patches."""

    adapter = "swebench"

    def __init__(self) -> None:
        self.digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    @staticmethod
    def _config(run: CompiledRun) -> Mapping[str, Any]:
        benchmark = run.manifest.benchmark
        if benchmark.kind != "custom" or benchmark.adapter != "swebench":
            raise AdapterRegistryError("SWE-bench loader requires a custom swebench artifact")
        config = benchmark.config
        if not isinstance(config, Mapping):
            raise AdapterRegistryError("SWE-bench benchmark config must be a table")
        allowed = {"dataset_file", "dataset_name", "image_template"}
        unknown = sorted(set(config) - allowed)
        if unknown:
            raise AdapterRegistryError(f"unsupported SWE-bench config keys: {unknown}")
        return config

    @classmethod
    def _dataset_file(cls, run: CompiledRun) -> Path:
        config = cls._config(run)
        return _relative_file(
            Path(run.manifest.benchmark.source),
            config.get("dataset_file"),
            label="dataset_file",
        )

    @staticmethod
    def _parse_rows(content: bytes) -> tuple[dict[str, Any], ...]:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterRegistryError("SWE-bench dataset is malformed JSON") from exc
        if not isinstance(value, list) or not value:
            raise AdapterRegistryError("SWE-bench dataset must be a non-empty JSON list")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(value):
            if not isinstance(raw, Mapping):
                raise AdapterRegistryError(f"SWE-bench row {index} must be an object")
            missing = sorted(_REQUIRED_FIELDS - set(raw))
            if missing:
                raise AdapterRegistryError(
                    f"SWE-bench row {index} lacks required fields: {missing}"
                )
            instance_id = raw.get("instance_id")
            if (
                not isinstance(instance_id, str)
                or not instance_id.strip()
                or any(
                    character not in _CASE_ID_CHARACTERS
                    for character in instance_id
                )
            ):
                raise AdapterRegistryError(f"SWE-bench row {index} has invalid instance_id")
            if instance_id in seen:
                raise AdapterRegistryError(f"duplicate SWE-bench instance_id: {instance_id}")
            seen.add(instance_id)
            for field in ("repo", "base_commit", "problem_statement"):
                if not isinstance(raw.get(field), str) or not str(raw[field]).strip():
                    raise AdapterRegistryError(
                        f"SWE-bench {instance_id} has invalid {field}"
                    )
            normalized = dict(raw)
            normalized["FAIL_TO_PASS"] = _string_list(
                raw["FAIL_TO_PASS"], label=f"{instance_id}.FAIL_TO_PASS"
            )
            normalized["PASS_TO_PASS"] = _string_list(
                raw["PASS_TO_PASS"], label=f"{instance_id}.PASS_TO_PASS"
            )
            rows.append(normalized)
        return tuple(rows)

    @staticmethod
    def _ordered_rows(
        run: CompiledRun, rows: Sequence[dict[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        protocol = run.manifest.execution.protocol
        if protocol is None:
            raise AdapterRegistryError("SWE-bench case resolution requires a protocol")
        ordered = list(rows)
        if protocol.case_order == "seeded_random":
            if run.manifest.execution.seed is None:
                raise AdapterRegistryError("seeded SWE-bench order requires an execution seed")
            random.Random(run.manifest.execution.seed).shuffle(ordered)
        elif protocol.case_order == "random":
            random.SystemRandom().shuffle(ordered)
        elif protocol.case_order in {"custom", "explicit"}:
            requested = tuple(protocol.explicit_case_ids)
            by_id = {str(row["instance_id"]): row for row in ordered}
            missing = [case_id for case_id in requested if case_id not in by_id]
            if missing:
                raise AdapterRegistryError(
                    "explicit SWE-bench cases are absent from the split: "
                    + ", ".join(missing)
                )
            ordered = [by_id[case_id] for case_id in requested]
        elif protocol.case_order != "fixed":
            raise AdapterRegistryError(
                f"unsupported SWE-bench order policy: {protocol.case_order!r}"
            )
        return tuple(ordered)

    @classmethod
    def _projections(
        cls, run: CompiledRun, row: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        config = cls._config(run)
        instance_id = str(row["instance_id"])
        image_template = config.get(
            "image_template", "sweb.eval.x86_64.{instance_id}:latest"
        )
        if (
            not isinstance(image_template, str)
            or image_template.count("{instance_id}") != 1
            or "{" in image_template.replace("{instance_id}", "")
            or "}" in image_template.replace("{instance_id}", "")
        ):
            raise AdapterRegistryError(
                "SWE-bench image_template must contain only one {instance_id} field"
            )
        image = image_template.replace("{instance_id}", instance_id)
        if not image.strip() or any(character.isspace() for character in image):
            raise AdapterRegistryError("SWE-bench image_template produces an invalid image")
        public = {
            "instance_id": instance_id,
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "problem_statement": row["problem_statement"],
            "hints_text": row.get("hints_text", ""),
            "version": row.get("version", ""),
        }
        execution = {
            "instance_id": instance_id,
            "container_image": image,
            "workspace_path": "/testbed",
        }
        verifier = {
            "instance_id": instance_id,
            "test_patch": row["test_patch"],
            "fail_to_pass": list(row["FAIL_TO_PASS"]),
            "pass_to_pass": list(row["PASS_TO_PASS"]),
        }
        return public, execution, verifier

    def resolve(self, run: CompiledRun, artifact_root: Path) -> ResolvedCaseSet:
        dataset_file = self._dataset_file(run)
        dataset_bytes = dataset_file.read_bytes()
        source_ref = ArtifactRef(
            path=str(dataset_file),
            sha256=hashlib.sha256(dataset_bytes).hexdigest(),
            size_bytes=len(dataset_bytes),
        )
        source_root = Path(run.manifest.benchmark.source)
        if (
            source_closure_digest(source_root, (source_ref,))
            != run.manifest.benchmark.source_content_digest
        ):
            raise AdapterRegistryError(
                "SWE-bench dataset closure differs from compiled benchmark"
            )
        rows = self._ordered_rows(run, self._parse_rows(dataset_bytes))
        cases: list[CaseArtifact] = []
        for row in rows:
            instance_id = str(row["instance_id"])
            public, execution, verifier = self._projections(run, row)
            refs: list[ArtifactRef] = []
            for label, payload in (
                ("public", public),
                ("execution", execution),
                ("verifier", verifier),
            ):
                encoded = _json_bytes(payload)
                digest = hashlib.sha256(encoded).hexdigest()
                path = artifact_root / "content" / f"{instance_id}-{label}-{digest}.json"
                write_immutable_json(path, payload, label=f"SWE-bench {label} contract")
                refs.append(artifact_ref(path))
            cases.append(
                CaseArtifact(
                    case_id=instance_id,
                    public_input_ref=refs[0],
                    task_contract_refs=(refs[1],),
                    verifier_contract_refs=(refs[2],),
                )
            )
        protocol = run.manifest.execution.protocol
        assert protocol is not None
        artifact = CaseSetArtifact(
            benchmark_id=run.manifest.benchmark.id,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            loader_adapter=self.adapter,
            loader_digest=self.digest,
            selection_method=(
                "explicit_case_ids"
                if protocol.case_order in {"custom", "explicit"}
                else "all_cases"
            ),
            case_order=protocol.case_order,
            order_seed=(
                run.manifest.execution.seed
                if protocol.case_order == "seeded_random"
                else None
            ),
            source_content_digest=run.manifest.benchmark.source_content_digest,
            source_content_refs=(source_ref,),
            ordered_case_ids=tuple(case.case_id for case in cases),
            cases=tuple(cases),
        )
        artifact_path = artifact_root / artifact.canonical_digest() / "case_set.json"
        write_immutable_json(artifact_path, artifact, label="SWE-bench case-set artifact")
        return ResolvedCaseSet(
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_sha256=sha256_file(artifact_path),
        )

    def load(self, run: CompiledRun, resolved: ResolvedCaseSet) -> LoadedCaseSet:
        loaded: list[SweBenchCase] = []
        for case in resolved.artifact.cases:
            if len(case.task_contract_refs) != 1 or len(case.verifier_contract_refs) != 1:
                raise AdapterRegistryError(
                    f"SWE-bench case contracts are incomplete: {case.case_id}"
                )
            try:
                public = json.loads(Path(case.public_input_ref.path).read_bytes())
                execution = json.loads(Path(case.task_contract_refs[0].path).read_bytes())
                verifier = json.loads(Path(case.verifier_contract_refs[0].path).read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AdapterRegistryError(
                    f"activated SWE-bench case is unreadable: {case.case_id}"
                ) from exc
            if any(
                not isinstance(value, Mapping)
                or value.get("instance_id") != case.case_id
                for value in (public, execution, verifier)
            ):
                raise AdapterRegistryError(
                    f"activated SWE-bench contract identity drift: {case.case_id}"
                )
            if "patch" in public or "patch" in execution or "patch" in verifier:
                raise AdapterRegistryError(
                    f"activated SWE-bench contract exposes an oracle patch: {case.case_id}"
                )
            loaded.append(
                SweBenchCase(
                    public=public,
                    execution_contract=execution,
                    verifier_contract=verifier,
                    case_set_digest=resolved.artifact.canonical_digest(),
                )
            )
        if tuple(case.task_id for case in loaded) != resolved.artifact.ordered_case_ids:
            raise AdapterRegistryError("loaded SWE-bench order differs from case set")
        return LoadedCaseSet(
            artifact=resolved.artifact,
            artifact_path=resolved.artifact_path,
            artifact_sha256=resolved.artifact_sha256,
            cases=tuple(loaded),
        )


__all__ = ["SweBenchCase", "SweBenchLoader"]
