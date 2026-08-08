"""Harbor 0.20 backend adapter with native trial-result ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from MagentaBench.schemas import (
    EnvironmentReceipt,
    EvidenceBundle,
    ProvenanceRecord,
    RunStatus,
    UsageRecord,
    VerifierEvidence,
)

from ..compiler import CompiledRun
from ..evidence import artifact_ref, atomic_write_bytes, atomic_write_json, sha256_file
from .fake import CaseExecution

DEFAULT_HARBOR_EXECUTABLE = "/root/.local/share/uv/tools/harbor/bin/harbor"
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class HarborConfigurationError(ValueError):
    """A resolved manifest cannot be represented as a Harbor JobConfig."""


class HarborExecutionError(RuntimeError):
    """Retained for callers that choose to promote an infra bundle to an error."""


def harbor_agent_name(subject: Any) -> str:
    adapter = str(getattr(subject, "adapter", "")).strip().lower().replace("_", "-")
    aliases = {
        "claude": "claude-code",
        "claude-code": "claude-code",
        "codex-cli": "codex",
        "codex": "codex",
        "pi": "pi",
        "antigravity-cli": "antigravity-cli",
    }
    if adapter in aliases:
        return aliases[adapter]
    raise HarborConfigurationError(
        f"subject adapter {adapter!r} has no Harbor built-in agent mapping"
    )


def build_job_config(
    run: CompiledRun,
    *,
    agent_name: str | None = None,
    dataset_name: str | None = None,
    attempts: int | None = None,
    max_retries: int = 0,
    environment_type: str | None = None,
    quiet: bool = True,
) -> dict[str, Any]:
    """Build the native Harbor JobConfig projection."""

    protocol = run.manifest.execution.protocol
    backend = run.manifest.execution.backend
    if protocol is None:
        raise HarborConfigurationError("Harbor runs require a resolved protocol")
    defaults = dict(backend.defaults)
    shim_override = defaults.get("agent_override")
    if any(key in defaults for key in ("authoritative_reward_metric", "reward_pass_value")):
        raise HarborConfigurationError(
            "verifier scoring semantics belong to the benchmark contract, not backend defaults"
        )
    if backend.adapter == "harbor" and (agent_name is not None or dataset_name is not None):
        raise HarborConfigurationError(
            "real Harbor derives agent and dataset solely from resolved manifest"
        )
    if backend.adapter == "harbor" and shim_override is not None:
        raise HarborConfigurationError(
            "agent_override is shim-only and forbidden for the real Harbor backend"
        )
    if backend.adapter not in {"harbor", "harbor-shim"}:
        raise HarborConfigurationError(
            f"unsupported Harbor backend adapter: {backend.adapter!r}"
        )
    selected_agent = agent_name or (
        str(shim_override)
        if backend.adapter == "harbor-shim" and shim_override is not None
        else harbor_agent_name(run.manifest.subject)
    )
    if not selected_agent.strip():
        raise HarborConfigurationError("Harbor agent name must not be empty")
    attempts = protocol.rollouts_per_case if attempts is None else attempts
    if attempts < 1 or max_retries < 0:
        raise HarborConfigurationError("attempts must be positive and retries non-negative")
    kwargs = defaults.get("agent_kwargs", {})
    if not isinstance(kwargs, Mapping):
        raise HarborConfigurationError("backend.defaults.agent_kwargs must be a table")
    config: dict[str, Any] = {
        "job_name": run.manifest.metadata.run_id,
        "n_concurrent_trials": protocol.parallelism,
        "n_attempts": attempts,
        "quiet": quiet,
        "retry": {"max_retries": max_retries},
        "environment": {
            "type": environment_type or str(defaults.get("environment_type", "docker")),
        },
        "agents": [
            {
                "name": selected_agent,
                "model_name": run.manifest.execution.model,
                "kwargs": dict(kwargs),
            }
        ],
        "datasets": [
            {
                "name": (
                    dataset_name
                    if backend.adapter == "harbor-shim" and dataset_name is not None
                    else run.manifest.benchmark.id
                ),
                "ref": run.manifest.benchmark.artifact_digest,
            }
        ],
    }
    # A benchmark adapter may resolve its native timeout baseline into this
    # backend default. Never derive a generic multiplier from a hard-coded
    # 3600-second assumption.
    if "agent_timeout_multiplier" in defaults:
        config["agent_timeout_multiplier"] = float(defaults["agent_timeout_multiplier"])
    return config


def render_job_yaml(config: Mapping[str, Any]) -> str:
    """Render deterministic JSON, valid YAML 1.2 without a PyYAML dependency."""

    return json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _verifier_rewards(verifier: Mapping[str, Any]) -> dict[str, float]:
    value = verifier.get("rewards")
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): float(item)
        for key, item in value.items()
        if isinstance(item, (int, float))
    }


def _verifier_score(
    verifier: Mapping[str, Any], authoritative_key: str | None = None
) -> float | None:
    rewards = _verifier_rewards(verifier)
    if authoritative_key is not None:
        return rewards.get(authoritative_key)
    if len(rewards) == 1:
        return next(iter(rewards.values()))
    # A named multi-metric reward has no universal BMP projection. The full
    # mapping remains in VerifierEvidence and scoring_valid is blocked.
    return None


def _incomplete_phase(result: Mapping[str, Any]) -> str | None:
    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        timing = result.get(phase)
        if isinstance(timing, Mapping) and timing.get("started_at") and not timing.get("finished_at"):
            return phase
    return None


def _exception_status(exception_type: str) -> RunStatus:
    """Classify a completed native phase from Harbor's exception family.

    Harbor records ``finished_at`` even when a phase terminates by exception,
    so exception ownership must take precedence over a generic ``Timeout``
    suffix.  In particular, ``VerifierTimeoutError`` is verifier failure, not
    an agent wall-time exhaustion.
    """

    value = exception_type.lower()
    if any(word in value for word in ("docker", "infra", "environment", "sandbox")):
        return RunStatus.infra_error
    if "agentsetup" in value or "agent_setup" in value or "harness" in value:
        return RunStatus.harness_fault
    if "verifier" in value or "reward" in value:
        return RunStatus.verifier_error
    if "agent" in value or "authentication" in value:
        if "timeout" in value or "timedout" in value:
            return RunStatus.timeout
        return RunStatus.agent_error
    if "output" in value or "parse" in value:
        return RunStatus.invalid_output
    if "timeout" in value or "timedout" in value:
        return RunStatus.timeout
    return RunStatus.invalid_output


def _status_from_result(
    result: Mapping[str, Any],
    *,
    authoritative_reward_key: str | None = None,
    reward_pass_value: float | None = None,
) -> RunStatus:
    """Map native TrialResult fields, preserving verifier/agent/infra phases."""

    if result.get("_bmp_parse_error"):
        return RunStatus.invalid_output
    if result.get("_bmp_missing_trial_dir") or result.get("_bmp_job_without_trials"):
        return RunStatus.infra_error
    incomplete_phase = _incomplete_phase(result)
    exception = result.get("exception_info")
    exception_type = (
        str(exception.get("exception_type", "")).lower()
        if isinstance(exception, Mapping)
        else ""
    )
    if incomplete_phase == "environment_setup":
        return RunStatus.infra_error
    if incomplete_phase == "agent_setup":
        return RunStatus.harness_fault
    if incomplete_phase == "agent_execution":
        return RunStatus.timeout if "timeout" in exception_type else RunStatus.agent_error
    if incomplete_phase == "verifier":
        return RunStatus.verifier_error
    if isinstance(exception, Mapping):
        return _exception_status(exception_type)

    verifier = result.get("verifier_result")
    agent = result.get("agent_result")
    if isinstance(verifier, Mapping):
        # Native VerifierResult has only named rewards. Without an adapter-
        # declared authoritative key and outcome semantics, preserve the map
        # and block scoring rather than guessing pass/fail from a threshold.
        rewards = _verifier_rewards(verifier)
        if rewards:
            if (
                authoritative_reward_key is not None
                and reward_pass_value is not None
                and authoritative_reward_key in rewards
            ):
                return (
                    RunStatus.pass_
                    if rewards[authoritative_reward_key] == reward_pass_value
                    else RunStatus.verified_fail
                )
            return RunStatus.unsupported
        return RunStatus.verifier_error
    if isinstance(agent, Mapping):
        agent_status = str(agent.get("status", agent.get("state", ""))).lower()
        if "timeout" in agent_status:
            return RunStatus.timeout
        if any(word in agent_status for word in ("error", "crash", "fail")):
            return RunStatus.agent_error
        output = agent.get("output", agent.get("final_output", agent.get("answer")))
        if not output:
            return RunStatus.no_output
    # Native fields were present but insufficient to classify the trial.
    return RunStatus.invalid_output


def _native_wall_seconds(result: Mapping[str, Any]) -> float | None:
    raw = result.get("wall_clock_seconds")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw)

    def parsed(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            observed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return observed

    started = parsed(result.get("started_at"))
    finished = parsed(result.get("finished_at"))
    if started is None or finished is None:
        return None
    elapsed = (finished - started).total_seconds()
    return elapsed if elapsed >= 0 else None


def _inside(root: Path, source: Path) -> Path:
    if source.is_symlink():
        raise HarborConfigurationError(f"symlink artifact is not accepted: {source}")
    resolved_root = root.resolve()
    resolved = source.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise HarborConfigurationError(f"artifact escapes Harbor result root: {source}") from exc
    return resolved


def _copy_atomic(source: Path, destination: Path, root: Path) -> Path:
    source = _inside(root, source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with source.open("rb") as src, temporary.open("wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(temporary, destination)
    return destination


def _find_result_files(result_root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(result_root.rglob("result.json")):
        if "bmp_cases" in path.parts:
            continue
        _inside(result_root, path)
        found.append(path)
    return found


def _trial_payloads(result_root: Path) -> list[tuple[str, Mapping[str, Any], Path]]:
    loaded_files: list[tuple[Path, Mapping[str, Any]]] = []
    for result_path in _find_result_files(result_root):
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            loaded_files.append(
                (
                    result_path,
                    {
                        "_bmp_parse_error": f"{type(exc).__name__}: {exc}",
                        "_bmp_result_path": str(result_path),
                    },
                )
            )
            continue
        if isinstance(loaded, Mapping):
            loaded_files.append((result_path, loaded))
    # Older Harbor JobResult projections embedded ``trial_results``.  Harbor
    # 0.20's on-disk JobResult instead contains aggregate ``stats`` while each
    # authoritative TrialResult lives in a child directory.  Never turn that
    # aggregate into a second synthetic trial.
    embedded_job_files = [
        item
        for item in loaded_files
        if isinstance(item[1].get("trial_results"), list)
    ]
    aggregate_job_files = [
        item
        for item in loaded_files
        if isinstance(item[1].get("n_total_trials"), int)
        and isinstance(item[1].get("stats"), Mapping)
        and "trial_name" not in item[1]
    ]
    aggregate_job_paths = tuple(path for path, _ in aggregate_job_files)
    if embedded_job_files:
        source_files = embedded_job_files
    else:
        aggregate_paths = set(aggregate_job_paths)
        source_files = [item for item in loaded_files if item[0] not in aggregate_paths]
        if not source_files and aggregate_job_files:
            result_path, loaded = aggregate_job_files[0]
            payload = dict(loaded)
            payload["_bmp_result_path"] = str(result_path)
            payload["_bmp_job_without_trials"] = True
            source_files = [(result_path, payload)]
    payloads: list[tuple[str, Mapping[str, Any], Path]] = []
    for result_path, loaded in source_files:
        trials = loaded.get("trial_results")
        if isinstance(trials, list):
            for index, trial in enumerate(trials):
                if isinstance(trial, Mapping):
                    label = str(trial.get("trial_name", trial.get("id", f"trial-{index:04d}")))
                    candidate_dir = result_path.parent / label
                    payload = dict(trial)
                    payload["_bmp_job_result_paths"] = [str(result_path)]
                    if candidate_dir.is_dir():
                        base_dir = candidate_dir
                        payload["_bmp_result_path"] = str(candidate_dir / "result.json")
                    else:
                        payload["_bmp_result_path"] = str(result_path)
                        base_dir = result_path.parent
                        payload["_bmp_missing_trial_dir"] = True
                    payloads.append((label, payload, base_dir))
        else:
            payload = dict(loaded)
            payload.setdefault("_bmp_result_path", str(result_path))
            if aggregate_job_paths:
                payload["_bmp_job_result_paths"] = [
                    str(path) for path in aggregate_job_paths
                ]
            payloads.append((str(payload.get("trial_name", result_path.parent.name)), payload, result_path.parent))
    return payloads


def parse_harbor_results(
    run: CompiledRun,
    *,
    result_root: str | Path,
    case_id: str = "case-001",
    runner_digest: str = "0" * 64,
    executable: str | None = None,
    observed_version: str = "0.20.0",
    observed_backend_digest: str | None = None,
    environment_receipt: EnvironmentReceipt | None = None,
    authoritative_reward_key: str | None = None,
    reward_pass_value: float | None = None,
    allow_test_parse: bool = False,
) -> tuple[CaseExecution, ...]:
    """Ingest every native trial into immutable, case-owned evidence."""

    if run.manifest.metadata.test_override is not None and not allow_test_parse:
        raise HarborConfigurationError(
            "test-override Harbor parsing requires allow_test_parse=true"
        )
    result_root = Path(result_root).resolve()
    if not _CASE_ID.fullmatch(case_id):
        raise HarborConfigurationError(f"invalid case id: {case_id!r}")
    payloads = _trial_payloads(result_root)
    if not payloads:
        payloads = [(case_id, {}, result_root)]
    cases: list[CaseExecution] = []
    sanitized_names: dict[str, str] = {}
    for index, (trial_name, result, trial_root) in enumerate(payloads):
        safe_trial = re.sub(r"[^A-Za-z0-9_.-]+", "_", trial_name).strip("_") or f"trial-{index:04d}"
        previous_name = sanitized_names.get(safe_trial)
        if previous_name is not None and previous_name != trial_name:
            raise HarborConfigurationError(
                "native trial names collide after sanitization: "
                f"{previous_name!r} and {trial_name!r} -> {safe_trial!r}"
            )
        sanitized_names[safe_trial] = trial_name
        combined_case = f"{case_id}__{safe_trial}"
        if not _CASE_ID.fullmatch(combined_case):
            raise HarborConfigurationError(f"invalid native trial identity: {combined_case!r}")
        case_dir = result_root / "bmp_cases" / combined_case
        if case_dir.exists() and any(case_dir.iterdir()):
            raise HarborConfigurationError(
                f"native trial evidence already exists and is immutable: {combined_case!r}"
            )
        case_dir.mkdir(parents=True, exist_ok=True)
        artifact_dir = case_dir / "harbor_artifacts"
        result_source = Path(str(result.get("_bmp_result_path", trial_root / "result.json")))
        if result.get("_bmp_missing_trial_dir") or result.get("_bmp_parse_error"):
            sources = [result_source]
        else:
            sources = [
                path
                for path in trial_root.rglob("*")
                if path.is_file() and "bmp_cases" not in path.parts
            ]
        sources.extend(
            Path(str(path)) for path in result.get("_bmp_job_result_paths", ())
        )
        refs: dict[Path, Any] = {}
        for source in sorted(set(sources)):
            relative = _inside(result_root, source).relative_to(result_root)
            destination = _copy_atomic(source, artifact_dir / relative, result_root)
            refs[source] = artifact_ref(destination)
        result_ref = refs.get(result_source)
        output_sources = [
            source
            for source in sources
            if source.name in {"answer.txt", "final_answer.md"}
        ]
        output_refs = tuple(refs[source] for source in output_sources if source in refs)
        status = _status_from_result(
            result,
            authoritative_reward_key=authoritative_reward_key,
            reward_pass_value=reward_pass_value,
        )
        if status in {RunStatus.pass_, RunStatus.verified_fail} and not output_refs:
            if result_ref is not None:
                output_refs = (result_ref,)
            else:
                status = RunStatus.no_output
        verifier = result.get("verifier_result")
        if not isinstance(verifier, Mapping):
            verifier = result
        score_value = _verifier_score(verifier, authoritative_reward_key)
        verifier_evidence = None
        if (
            status in {RunStatus.pass_, RunStatus.verified_fail, RunStatus.unsupported}
            and _verifier_rewards(verifier)
        ) or (
            status == RunStatus.verifier_error
            and ("exception_info" in result or "verifier_result" in result)
        ):
            verifier_evidence = VerifierEvidence(
                verifier=str(verifier.get("verifier", "harbor.native")),
                passed=status == RunStatus.pass_,
                score=score_value,
                metrics=_verifier_rewards(verifier),
                artifact_refs=(result_ref,) if result_ref else (),
                details={
                    "trial_name": trial_name,
                    "rewards": _verifier_rewards(verifier),
                    "authoritative_reward_metric": authoritative_reward_key,
                    "scoring_semantics_declared": (
                        authoritative_reward_key is not None
                        and reward_pass_value is not None
                    ),
                    "exception_info": result.get("exception_info"),
                    "incomplete_phase": _incomplete_phase(result),
                },
            )
        status_path = case_dir / "status.json"
        atomic_write_json(status_path, {"case_id": combined_case, "status": status.value})
        log_refs = tuple(
            ref for source, ref in refs.items()
            if source.name not in {"answer.txt", "final_answer.md"}
        ) + (artifact_ref(status_path),)
        backend = run.manifest.execution.backend
        agent_result = result.get("agent_result")
        usage_source = agent_result if isinstance(agent_result, Mapping) else {}
        bundle = EvidenceBundle(
            run_id=f"{run.manifest.metadata.run_id}__{safe_trial}",
            status=status,
            output_refs=output_refs,
            log_refs=log_refs,
            verifier_evidence=verifier_evidence,
            usage=UsageRecord(
                input_tokens=usage_source.get("n_input_tokens"),
                output_tokens=usage_source.get("n_output_tokens"),
                total_tokens=(
                    int(usage_source.get("n_input_tokens") or 0)
                    + int(usage_source.get("n_output_tokens") or 0)
                    if usage_source.get("n_input_tokens") is not None
                    and usage_source.get("n_output_tokens") is not None
                    else None
                ),
                cost=usage_source.get("cost_usd"),
                wall_clock_seconds=_native_wall_seconds(result),
            ),
            provenance=ProvenanceRecord(
                manifest_digest=run.manifest_digest,
                runner_digest=runner_digest,
                benchmark_digest=run.manifest.benchmark.artifact_digest,
                subject_digest=run.manifest.subject.artifact_digest,
                backend_digest=observed_backend_digest or backend.digest,
                executable=executable,
                distribution="harbor",
                version=observed_version,
                backend_kind="harbor",
                network_mode=str(backend.defaults.get("network_mode", "none")),
                workspace_namespace=str(result_root.parent),
                environment_receipt=environment_receipt,
                test_override=run.manifest.metadata.test_override,
            ),
        )
        bundle_path = case_dir / "evidence_bundle.json"
        atomic_write_json(bundle_path, bundle)
        cases.append(
            CaseExecution(
                case_id=combined_case,
                bundle=bundle,
                bundle_path=bundle_path,
                bundle_digest=sha256_file(bundle_path),
            )
        )
    return tuple(cases)


def parse_harbor_result(
    run: CompiledRun,
    *,
    result_root: str | Path,
    case_id: str = "case-001",
    runner_digest: str = "0" * 64,
    executable: str | None = None,
    observed_version: str = "0.20.0",
    observed_backend_digest: str | None = None,
    environment_receipt: EnvironmentReceipt | None = None,
    authoritative_reward_key: str | None = None,
    reward_pass_value: float | None = None,
    allow_test_parse: bool = False,
) -> CaseExecution:
    return parse_harbor_results(
        run,
        result_root=result_root,
        case_id=case_id,
        runner_digest=runner_digest,
        executable=executable,
        observed_version=observed_version,
        observed_backend_digest=observed_backend_digest,
        environment_receipt=environment_receipt,
        authoritative_reward_key=authoritative_reward_key,
        reward_pass_value=reward_pass_value,
        allow_test_parse=allow_test_parse,
    )[0]


@dataclass(frozen=True)
class HarborExecution:
    case: CaseExecution
    config_path: Path
    invocation_stdout: Path
    invocation_stderr: Path
    cases: tuple[CaseExecution, ...] = ()


class HarborBackend:
    """Invoke a pinned Harbor executable and ingest all native trials."""

    def __init__(
        self,
        record_root: str | Path,
        *,
        harbor_executable: str | Path = DEFAULT_HARBOR_EXECUTABLE,
        timeout_seconds: float = 3600.0,
        environment_receipt: EnvironmentReceipt | None = None,
        allow_test_shim: bool = False,
    ) -> None:
        candidate = Path(harbor_executable).expanduser()
        if not candidate.is_absolute():
            raise HarborConfigurationError("Harbor executable must be an absolute pinned path")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise HarborConfigurationError(f"Harbor executable is missing: {candidate}") from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise HarborConfigurationError(f"Harbor executable is not executable: {resolved}")
        self.record_root = Path(record_root).resolve()
        self.harbor_executable = str(resolved)
        self.timeout_seconds = timeout_seconds
        self.environment_receipt = environment_receipt
        self.allow_test_shim = allow_test_shim
        self.runner_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def _validate_generated_config(self, config_path: Path) -> None:
        """Use Harbor's own parser while enforcing a stricter no-extra projection."""

        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarborConfigurationError(f"generated JobConfig is not JSON/YAML data: {exc}") from exc
        allowed = {
            "job_name", "jobs_dir", "n_attempts", "install_only", "timeout_multiplier",
            "agent_timeout_multiplier", "verifier_timeout_multiplier",
            "agent_setup_timeout_multiplier", "environment_build_timeout_multiplier",
            "debug", "n_concurrent_trials", "quiet", "retry", "environment", "verifier",
            "metrics", "agents", "datasets", "tasks", "artifacts", "extra_instruction_paths",
        }
        unknown = set(config) - allowed
        if unknown:
            raise HarborConfigurationError(f"generated JobConfig has unknown fields: {sorted(unknown)}")
        for agent in config.get("agents", []):
            if set(agent) - {"name", "model_name", "kwargs"}:
                raise HarborConfigurationError("generated AgentConfig has unknown fields")
        try:
            checked = subprocess.run(
                [self.harbor_executable, "run", "--config", str(config_path), "--print-config"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except OSError as exc:
            raise HarborConfigurationError(f"cannot validate Harbor JobConfig: {exc}") from exc
        if checked.returncode != 0:
            raise HarborConfigurationError(
                f"Harbor rejected generated JobConfig ({checked.returncode}): {checked.stderr[-2000:]}"
            )
        if not self.allow_test_shim:
            try:
                resolved = json.loads(checked.stdout)
            except json.JSONDecodeError as exc:
                raise HarborConfigurationError(
                    "Harbor --print-config did not return a JSON projection for comparison"
                ) from exc

            def assert_projection(expected: Any, actual: Any, path: str) -> None:
                if isinstance(expected, Mapping):
                    if not isinstance(actual, Mapping):
                        raise HarborConfigurationError(f"Harbor projection changed {path}")
                    for key, value in expected.items():
                        if key not in actual:
                            raise HarborConfigurationError(f"Harbor projection omitted {path}.{key}")
                        assert_projection(value, actual[key], f"{path}.{key}")
                elif isinstance(expected, list):
                    if not isinstance(actual, list) or len(actual) < len(expected):
                        raise HarborConfigurationError(f"Harbor projection changed {path}")
                    for index, value in enumerate(expected):
                        assert_projection(value, actual[index], f"{path}[{index}]")
                elif actual != expected:
                    raise HarborConfigurationError(
                        f"Harbor projection changed {path}: expected {expected!r}, got {actual!r}"
                    )

            assert_projection(config, resolved, "JobConfig")

    @staticmethod
    def _inspect_executable(executable: str) -> tuple[str, str]:
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarborConfigurationError(f"cannot inspect Harbor version: {exc}") from exc
        text = (result.stdout or result.stderr).strip()
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
        if not match:
            raise HarborConfigurationError(f"Harbor --version was not parseable: {text!r}")
        digest = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        return match.group(1), digest

    def _failure_case(
        self,
        run: CompiledRun,
        *,
        evidence_root: Path,
        case_id: str,
        status: RunStatus,
        stdout_path: Path,
        stderr_path: Path,
        executable: str,
        version: str,
        digest: str,
        message: str,
    ) -> HarborExecution:
        case_dir = evidence_root / "bmp_cases" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        status_path = case_dir / "status.json"
        atomic_write_json(status_path, {"case_id": case_id, "status": status.value, "error": message})
        bundle = EvidenceBundle(
            run_id=run.manifest.metadata.run_id,
            status=status,
            log_refs=(artifact_ref(stdout_path), artifact_ref(stderr_path), artifact_ref(status_path)),
            usage=UsageRecord(),
            provenance=ProvenanceRecord(
                manifest_digest=run.manifest_digest,
                runner_digest=self.runner_digest,
                benchmark_digest=run.manifest.benchmark.artifact_digest,
                subject_digest=run.manifest.subject.artifact_digest,
                backend_digest=digest,
                executable=executable,
                distribution="harbor",
                version=version,
                backend_kind="harbor",
                network_mode="none",
                workspace_namespace=str(evidence_root.parent),
                environment_receipt=self.environment_receipt,
                test_override=run.manifest.metadata.test_override,
            ),
        )
        bundle_path = case_dir / "evidence_bundle.json"
        atomic_write_json(bundle_path, bundle)
        case = CaseExecution(
            case_id=case_id,
            bundle=bundle,
            bundle_path=bundle_path,
            bundle_digest=sha256_file(bundle_path),
        )
        return HarborExecution(case, evidence_root / "job.yaml", stdout_path, stderr_path, (case,))

    def run(
        self,
        run: CompiledRun,
        *,
        agent_name: str | None = None,
        dataset_name: str | None = None,
        case_id: str = "case-001",
    ) -> HarborExecution:
        backend = run.manifest.execution.backend
        if backend.adapter == "harbor-shim" and not self.allow_test_shim:
            raise HarborConfigurationError(
                "harbor-shim is test-only; real Harbor backend cannot select it"
            )
        if backend.adapter == "harbor" and "agent_override" in backend.defaults:
            raise HarborConfigurationError(
                "real Harbor backend rejects shim-only agent_override"
            )
        if backend.adapter not in {"harbor", "harbor-shim"}:
            raise HarborConfigurationError(
                f"unsupported Harbor backend adapter: {backend.adapter!r}"
            )
        if backend.executable != self.harbor_executable:
            raise HarborConfigurationError(
                "Harbor executable does not match the manifest's pinned backend executable"
            )
        observed_version, observed_digest = self._inspect_executable(self.harbor_executable)
        if observed_version != backend.version:
            raise HarborConfigurationError(
                f"Harbor version drift: manifest {backend.version}, observed {observed_version}"
            )
        if backend.digest != observed_digest:
            raise HarborConfigurationError(
                f"Harbor executable digest drift: manifest {backend.digest}, observed {observed_digest}"
            )
        evidence_root = self.record_root / run.manifest.metadata.experiment_id / run.manifest_digest
        evidence_root.mkdir(parents=True, exist_ok=True)
        config = build_job_config(run, agent_name=agent_name, dataset_name=dataset_name)
        config_path = evidence_root / "job.yaml"
        atomic_write_bytes(config_path, render_job_yaml(config).encode("utf-8"))
        self._validate_generated_config(config_path)
        stdout_path = evidence_root / "harbor.stdout.log"
        stderr_path = evidence_root / "harbor.stderr.log"
        command = [
            self.harbor_executable,
            "run",
            "--agent",
            config["agents"][0]["name"],
            "--config",
            str(config_path),
            "--jobs-dir",
            str(evidence_root),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
            atomic_write_bytes(stdout_path, completed.stdout.encode("utf-8"))
            atomic_write_bytes(stderr_path, completed.stderr.encode("utf-8"))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            atomic_write_bytes(stdout_path, stdout.encode("utf-8"))
            atomic_write_bytes(stderr_path, stderr.encode("utf-8"))
            return self._failure_case(
                run,
                evidence_root=evidence_root,
                case_id=case_id,
                status=RunStatus.timeout,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                executable=self.harbor_executable,
                version=observed_version,
                digest=observed_digest,
                message=f"Harbor timed out after {self.timeout_seconds:.1f}s",
            )
        except OSError as exc:
            atomic_write_bytes(stdout_path, b"")
            atomic_write_bytes(stderr_path, str(exc).encode("utf-8"))
            return self._failure_case(
                run,
                evidence_root=evidence_root,
                case_id=case_id,
                status=RunStatus.infra_error,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                executable=self.harbor_executable,
                version=observed_version,
                digest=observed_digest,
                message=str(exc),
            )
        if completed.returncode != 0 and not _find_result_files(evidence_root):
            return self._failure_case(
                run,
                evidence_root=evidence_root,
                case_id=case_id,
                status=RunStatus.infra_error,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                executable=self.harbor_executable,
                version=observed_version,
                digest=observed_digest,
                message=f"Harbor exited {completed.returncode}: {completed.stderr[-2000:]}",
            )
        cases = parse_harbor_results(
            run,
            result_root=evidence_root,
            case_id=case_id,
            runner_digest=self.runner_digest,
            executable=self.harbor_executable,
            observed_version=observed_version,
            observed_backend_digest=observed_digest,
            environment_receipt=self.environment_receipt,
            authoritative_reward_key=run.manifest.benchmark.authoritative_reward_metric,
            reward_pass_value=run.manifest.benchmark.reward_pass_value,
            allow_test_parse=self.allow_test_shim,
        )
        updated_cases: list[CaseExecution] = []
        for case in cases:
            bundle = case.bundle.model_copy(
                update={
                    "log_refs": (
                        *case.bundle.log_refs,
                        artifact_ref(config_path),
                        artifact_ref(stdout_path),
                        artifact_ref(stderr_path),
                    )
                }
            )
            atomic_write_json(case.bundle_path, bundle)
            updated_cases.append(replace(case, bundle=bundle, bundle_digest=sha256_file(case.bundle_path)))
        return HarborExecution(
            case=updated_cases[0],
            config_path=config_path,
            invocation_stdout=stdout_path,
            invocation_stderr=stderr_path,
            cases=tuple(updated_cases),
        )


__all__ = [
    "DEFAULT_HARBOR_EXECUTABLE",
    "HarborBackend",
    "HarborConfigurationError",
    "HarborExecution",
    "HarborExecutionError",
    "build_job_config",
    "harbor_agent_name",
    "parse_harbor_result",
    "parse_harbor_results",
    "render_job_yaml",
]
