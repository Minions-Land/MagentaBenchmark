"""Terminal-Bench 2.x adapter for the native Harbor backend.

The adapter deliberately keeps Terminal-Bench's task implementation opaque to
BMP.  It only records the task source closure, exposes a stable case id and
passes the activated local task directory to Harbor.  Harbor remains the
authority for container setup, agent execution and verifier semantics.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from MagentaBench.runner.adapter_registry import (
    AdapterRegistryError,
    LoadedCaseSet,
    ResolvedCaseSet,
    _write_immutable,
    write_immutable_json,
)
from MagentaBench.runner.backend.fake import CaseExecution
from MagentaBench.runner.backend.harbor import HarborBackend, HarborConfigurationError
from MagentaBench.runner.compiler import CompiledRun
from MagentaBench.runner.case_order import (
    CaseOrderError,
    custom_order_binding,
    selected_case_ids,
)
from MagentaBench.runner.evidence import (
    artifact_ref,
    atomic_write_json,
    sha256_file,
    source_closure_digest,
)
from MagentaBench.runner.network import (
    record_active_network_probe,
    record_unobservable_network,
)
from MagentaBench.schemas import (
    ArtifactRef,
    CaseArtifact,
    CaseSetArtifact,
    NetworkBoundary,
    NetworkEndpointRecord,
    NetworkPolicySource,
    RunStatus,
    VerifierEvidence,
)
from tools.mirror_acquisition.mirror import (
    AcquisitionError,
    DockerClient,
    PinnedExecutable,
    SubprocessRunner,
    acquisition_ref,
    verify_cached_image,
)
from tools.mirror_acquisition.models import (
    ImageSpecError,
    LoadedImageSpec,
    load_image_spec,
)

# Keep the custom Harbor Agent in the statically audited source closure without
# importing Harbor in BMP's lightweight compiler/test environment. Harbor loads
# the class through the digest-bound ``AgentConfig.import_path`` at runtime.
if TYPE_CHECKING:
    from .magenta_agent import MagentaAgent


_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_PYTEST_COMPLETION = re.compile(
    r"^=+\s+(?P<body>.+?)\s+in\s+[0-9]+(?:\.[0-9]+)?s\s+=+$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}$")
_HOSTNAME = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_RUNTIME_IDENTITY_FORMAT = "magentabench-harbor-runtime-identity-v1"
_CONTAINER_RECEIPT_FORMAT = "magentabench-harbor-container-receipt-v1"
_RUNTIME_FAILURE_FORMAT = "magentabench-harbor-runtime-identity-failure-v1"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_IDENTITY_KEYS = frozenset(
    {
        "format",
        "required",
        "retain_task_container",
        "docker_executable",
        "docker_executable_sha256",
        "docker_client_version",
        "docker_server_version",
        "oci_mirror_registry",
        "network_probe_host",
        "network_probe_port",
        "network_probe_timeout_seconds",
        "task_image_specs",
    }
)
_MODULE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class TerminalBenchCase:
    """One activated Terminal-Bench task and its immutable source binding."""

    task_id: str
    task_name: str
    task_path: str
    task_manifest_ref: ArtifactRef
    task_contract_refs: tuple[ArtifactRef, ...]
    verifier_contract_refs: tuple[ArtifactRef, ...]
    case_set_digest: str
    allow_internet: bool
    verifier_completion_artifact: str | None


class TerminalBenchRuntimeIdentityError(HarborConfigurationError):
    """A safe, classified failure at the retained task-container boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _RuntimeIdentityPolicy:
    docker_executable: Path
    docker_executable_sha256: str
    docker_client_version: str
    docker_server_version: str
    oci_mirror_registry: str
    network_probe_host: str
    network_probe_port: int
    network_probe_timeout_seconds: int
    image_spec_path: Path
    image_spec_relative_path: str
    loaded_image_spec: LoadedImageSpec


@dataclass(frozen=True)
class _RuntimePreflight:
    policy: _RuntimeIdentityPolicy
    docker_version: Mapping[str, str]
    docker_executable_size: int
    cache_verification: Mapping[str, Any]


def _runtime_failure(code: str, message: str) -> TerminalBenchRuntimeIdentityError:
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) is None:
        code = "RUNTIME_IDENTITY_FAILED"
    return TerminalBenchRuntimeIdentityError(code, message)


def _registered_spec_path(value: Any) -> tuple[Path, str]:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or Path(value).is_absolute()
        or any(part in {"", ".", ".."} for part in Path(value).parts)
    ):
        raise _runtime_failure(
            "IMAGE_SPEC_PATH_INVALID",
            "registered OCI image spec path is invalid",
        )
    candidate = _PROJECT_ROOT
    for part in Path(value).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise _runtime_failure(
                "IMAGE_SPEC_PATH_INVALID",
                "registered OCI image spec path is invalid",
            )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _runtime_failure(
            "IMAGE_SPEC_UNAVAILABLE",
            "registered OCI image spec is unavailable",
        ) from exc
    try:
        resolved.relative_to(_PROJECT_ROOT)
    except ValueError as exc:
        raise _runtime_failure(
            "IMAGE_SPEC_PATH_INVALID",
            "registered OCI image spec path is invalid",
        ) from exc
    if not resolved.is_file():
        raise _runtime_failure(
            "IMAGE_SPEC_UNAVAILABLE",
            "registered OCI image spec is unavailable",
        )
    return resolved, value


def _task_docker_image(case: TerminalBenchCase) -> str:
    path = Path(case.task_manifest_ref.path)
    if (
        not path.is_file()
        or path.stat().st_size != case.task_manifest_ref.size_bytes
        or sha256_file(path) != case.task_manifest_ref.sha256
    ):
        raise _runtime_failure(
            "TASK_MANIFEST_DRIFT",
            "Terminal-Bench task manifest identity drifted before runtime preflight",
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        environment = document["environment"]
        image = environment["docker_image"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise _runtime_failure(
            "TASK_IMAGE_MISSING",
            "Terminal-Bench task image declaration is unavailable",
        ) from exc
    if (
        not isinstance(environment, Mapping)
        or not isinstance(image, str)
        or not image
        or image != image.strip()
        or not image.isascii()
        or any(marker in image for marker in ("=", "@", "://", "\\"))
    ):
        raise _runtime_failure(
            "TASK_IMAGE_INVALID",
            "Terminal-Bench task image declaration is invalid",
        )
    return image


def _runtime_identity_policy(
    run: CompiledRun,
    case: TerminalBenchCase,
) -> _RuntimeIdentityPolicy:
    declared = run.manifest.execution.backend.defaults.get("runtime_identity")
    if not isinstance(declared, Mapping):
        raise _runtime_failure(
            "RUNTIME_POLICY_MISSING",
            "registered Harbor runtime identity policy is missing",
        )
    if set(declared) != _RUNTIME_IDENTITY_KEYS:
        raise _runtime_failure(
            "RUNTIME_POLICY_KEYS_INVALID",
            "registered Harbor runtime identity policy has invalid keys",
        )
    if declared.get("format") != _RUNTIME_IDENTITY_FORMAT:
        raise _runtime_failure(
            "RUNTIME_POLICY_FORMAT_INVALID",
            "registered Harbor runtime identity policy format is invalid",
        )
    if declared.get("required") is not True:
        raise _runtime_failure(
            "RUNTIME_POLICY_NOT_REQUIRED",
            "registered Harbor runtime identity policy must fail closed",
        )
    if declared.get("retain_task_container") is not True:
        raise _runtime_failure(
            "CONTAINER_RETENTION_DISABLED",
            "registered Harbor runtime identity policy must retain the task container",
        )

    executable_value = declared.get("docker_executable")
    if not isinstance(executable_value, str):
        raise _runtime_failure(
            "DOCKER_EXECUTABLE_INVALID",
            "registered Docker executable path is invalid",
        )
    executable = Path(executable_value)
    executable_digest = declared.get("docker_executable_sha256")
    if (
        not executable.is_absolute()
        or executable.name != "docker"
        or executable_value != executable.as_posix()
        or any(part in {"", ".", ".."} for part in executable.parts[1:])
        or not isinstance(executable_digest, str)
        or _SHA256.fullmatch(executable_digest) is None
    ):
        raise _runtime_failure(
            "DOCKER_EXECUTABLE_INVALID",
            "registered Docker executable identity is invalid",
        )

    client_version = declared.get("docker_client_version")
    server_version = declared.get("docker_server_version")
    if (
        not isinstance(client_version, str)
        or _VERSION.fullmatch(client_version) is None
        or not isinstance(server_version, str)
        or _VERSION.fullmatch(server_version) is None
    ):
        raise _runtime_failure(
            "DOCKER_VERSION_POLICY_INVALID",
            "registered Docker version policy is invalid",
        )
    mirror = declared.get("oci_mirror_registry")
    if (
        not isinstance(mirror, str)
        or not mirror
        or not mirror.isascii()
        or any(marker in mirror for marker in ("/", "@", "=", "://", "\\"))
    ):
        raise _runtime_failure(
            "OCI_MIRROR_INVALID",
            "registered OCI mirror registry is invalid",
        )

    probe_host = declared.get("network_probe_host")
    probe_port = declared.get("network_probe_port")
    probe_timeout = declared.get("network_probe_timeout_seconds")
    if not isinstance(probe_host, str) or _HOSTNAME.fullmatch(probe_host) is None:
        raise _runtime_failure(
            "NETWORK_PROBE_POLICY_INVALID",
            "registered network probe host is invalid",
        )
    if (
        not isinstance(probe_port, int)
        or isinstance(probe_port, bool)
        or not 1 <= probe_port <= 65535
        or not isinstance(probe_timeout, int)
        or isinstance(probe_timeout, bool)
        or not 1 <= probe_timeout <= 30
    ):
        raise _runtime_failure(
            "NETWORK_PROBE_POLICY_INVALID",
            "registered network probe parameters are invalid",
        )

    mappings = declared.get("task_image_specs")
    if not isinstance(mappings, Mapping) or not mappings:
        raise _runtime_failure(
            "IMAGE_SPEC_MAP_INVALID",
            "registered task image spec map is invalid",
        )
    normalized_specs: dict[str, tuple[Path, str]] = {}
    for task_id, relative_path in mappings.items():
        if not isinstance(task_id, str) or _ID.fullmatch(task_id) is None:
            raise _runtime_failure(
                "IMAGE_SPEC_MAP_INVALID",
                "registered task image spec map is invalid",
            )
        normalized_specs[task_id] = _registered_spec_path(relative_path)
    if case.task_id not in normalized_specs:
        raise _runtime_failure(
            "IMAGE_SPEC_NOT_REGISTERED",
            "Terminal-Bench task has no registered OCI image spec",
        )
    image_spec_path, image_spec_relative_path = normalized_specs[case.task_id]
    try:
        loaded = load_image_spec(image_spec_path)
    except ImageSpecError as exc:
        raise _runtime_failure(
            "IMAGE_SPEC_INVALID",
            "registered OCI image spec is invalid",
        ) from exc
    task_image = _task_docker_image(case)
    spec_task_image = loaded.spec.canonical_tag_ref.removeprefix("docker.io/")
    if task_image != spec_task_image:
        raise _runtime_failure(
            "TASK_IMAGE_SPEC_MISMATCH",
            "Terminal-Bench task image does not match its registered OCI spec",
        )
    return _RuntimeIdentityPolicy(
        docker_executable=executable,
        docker_executable_sha256=executable_digest,
        docker_client_version=client_version,
        docker_server_version=server_version,
        oci_mirror_registry=mirror,
        network_probe_host=probe_host,
        network_probe_port=probe_port,
        network_probe_timeout_seconds=probe_timeout,
        image_spec_path=image_spec_path,
        image_spec_relative_path=image_spec_relative_path,
        loaded_image_spec=loaded,
    )


def _runtime_preflight(
    run: CompiledRun,
    case: TerminalBenchCase,
) -> _RuntimePreflight:
    policy = _runtime_identity_policy(run, case)
    try:
        with PinnedExecutable.open(policy.docker_executable) as pinned:
            if pinned.identity.get("sha256") != policy.docker_executable_sha256:
                raise _runtime_failure(
                    "DOCKER_EXECUTABLE_DRIFT",
                    "registered Docker executable identity drifted",
                )
            runner = SubprocessRunner(pass_fds=(pinned.descriptor,))
            docker = DockerClient(
                runner,
                pinned.invocation_path,
                pinned_executable=pinned,
            )
            version = docker.version()
            if (
                version.get("client_version") != policy.docker_client_version
                or version.get("server_version") != policy.docker_server_version
            ):
                raise _runtime_failure(
                    "DOCKER_VERSION_DRIFT",
                    "Docker runtime version drifted from the registered policy",
                )
            cache = verify_cached_image(
                policy.loaded_image_spec,
                policy.oci_mirror_registry,
                docker,
            )
            pinned.require_unchanged()
            size = pinned.identity.get("size_bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise _runtime_failure(
                    "DOCKER_EXECUTABLE_DRIFT",
                    "registered Docker executable identity drifted",
                )
    except TerminalBenchRuntimeIdentityError:
        raise
    except AcquisitionError as exc:
        raise _runtime_failure(
            f"DOCKER_{exc.code}",
            "Docker and OCI runtime preflight failed",
        ) from exc
    return _RuntimePreflight(
        policy=policy,
        docker_version=version,
        docker_executable_size=size,
        cache_verification=cache,
    )


def _compose_project_name(trial_name: str) -> str:
    name = trial_name.lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return re.sub(r"[^a-z0-9_-]", "-", name)


def _native_trial_name(native: CaseExecution, attempt_id: str) -> str:
    prefix = f"{attempt_id}__"
    if not native.case_id.startswith(prefix):
        raise _runtime_failure(
            "NATIVE_TRIAL_IDENTITY_MISSING",
            "native Harbor trial identity is unavailable",
        )
    trial_name = native.case_id.removeprefix(prefix)
    if _ID.fullmatch(trial_name) is None:
        raise _runtime_failure(
            "NATIVE_TRIAL_IDENTITY_INVALID",
            "native Harbor trial identity is invalid",
        )
    verifier = native.bundle.verifier_evidence
    if verifier is not None and "trial_name" in verifier.details:
        observed = verifier.details.get("trial_name")
        if observed != trial_name:
            raise _runtime_failure(
                "NATIVE_TRIAL_IDENTITY_DRIFT",
                "native Harbor trial identity drifted",
            )
    return trial_name


def _network_probe_endpoint(
    policy: _RuntimeIdentityPolicy,
    *,
    allow_internet: bool,
    succeeded: bool,
) -> NetworkEndpointRecord:
    return NetworkEndpointRecord(
        protocol="tcp",
        host=policy.network_probe_host,
        port=policy.network_probe_port,
        outcome=(
            "connected"
            if succeeded and allow_internet
            else "policy_violation"
            if succeeded
            else "blocked"
        ),
    )


def _container_projection(
    payload_text: str,
    *,
    expected_container_id: str,
    expected_project: str,
    expected_task_image: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(payload_text)
        if not isinstance(payload, list) or len(payload) != 1:
            raise ValueError
        container = payload[0]
        config = container["Config"]
        labels = config["Labels"]
        state = container["State"]
        host = container["HostConfig"]
        networks = container["NetworkSettings"]["Networks"]
        container_id = container["Id"]
        name = container["Name"]
        image_id = container["Image"]
        task_image = config["Image"]
        status = state["Status"]
        running = state["Running"]
        exit_code = state["ExitCode"]
        network_mode = host["NetworkMode"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _runtime_failure(
            "CONTAINER_INSPECT_INVALID",
            "retained task container inspection is invalid",
        ) from exc
    if (
        not isinstance(container, Mapping)
        or not isinstance(config, Mapping)
        or not isinstance(labels, Mapping)
        or not isinstance(state, Mapping)
        or not isinstance(host, Mapping)
        or not isinstance(networks, Mapping)
        or container_id != expected_container_id
        or _CONTAINER_ID.fullmatch(container_id) is None
        or name != f"/{expected_project}-main-1"
        or image_id != labels.get("com.docker.compose.image")
        or _OCI_DIGEST.fullmatch(image_id) is None
        or task_image != expected_task_image
        or labels.get("com.docker.compose.project") != expected_project
        or labels.get("com.docker.compose.service") != "main"
        or labels.get("com.docker.compose.container-number") != "1"
        or labels.get("com.docker.compose.oneoff") != "False"
        or status not in {"created", "running", "exited", "dead"}
        or not isinstance(running, bool)
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not isinstance(network_mode, str)
        or network_mode != f"{expected_project}_default"
        or set(networks) != {f"{expected_project}_default"}
    ):
        raise _runtime_failure(
            "CONTAINER_IDENTITY_MISMATCH",
            "retained task container identity does not match the Harbor trial",
        )
    return {
        "container_id": container_id,
        "container_name": name.removeprefix("/"),
        "image_id": image_id,
        "task_image": task_image,
        "compose_project": expected_project,
        "compose_service": "main",
        "state": status,
        "running": running,
        "exit_code": exit_code,
        "network_mode": network_mode,
        "network_names": sorted(networks),
    }


class TerminalBenchLoader:
    """Load local Terminal-Bench tasks without importing Harbor internals."""

    adapter = "terminal_bench"
    digest = _MODULE_DIGEST

    @staticmethod
    def _source(run: CompiledRun) -> Path:
        source = run.manifest.dataset.source
        if not source:
            raise AdapterRegistryError("Terminal-Bench benchmark source is missing")
        root = Path(source).resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise AdapterRegistryError(f"Terminal-Bench source is not a real directory: {root}")
        return root

    @classmethod
    def _source_refs(cls, run: CompiledRun) -> tuple[ArtifactRef, ...]:
        source_artifact = run.manifest.dataset
        source = cls._source(run)
        patterns = tuple(getattr(source_artifact, "content_globs", ()))
        if not patterns:
            raise AdapterRegistryError("Terminal-Bench content_globs must be non-empty")
        files: set[Path] = set()
        for pattern in patterns:
            for path in source.glob(pattern):
                if path.is_file():
                    resolved = path.resolve(strict=True)
                    try:
                        relative = resolved.relative_to(source)
                    except ValueError as exc:
                        raise AdapterRegistryError(
                            f"Terminal-Bench content path escapes source: {path}"
                        ) from exc
                    if any(part in {"", ".", ".."} for part in relative.parts):
                        raise AdapterRegistryError(
                            f"Terminal-Bench content path is not normalized: {path}"
                        )
                    if path.is_symlink():
                        raise AdapterRegistryError(
                            f"Terminal-Bench content dependency is a symlink: {path}"
                        )
                    files.add(resolved)
        if not files:
            raise AdapterRegistryError("Terminal-Bench content_globs matched no files")
        refs = tuple(artifact_ref(path) for path in sorted(files))
        observed = source_closure_digest(source, refs)
        if observed != source_artifact.source_content_digest:
            raise AdapterRegistryError(
                "Terminal-Bench source closure differs from compiled dataset"
            )
        return refs

    @classmethod
    def _tasks(
        cls, run: CompiledRun
    ) -> tuple[
        tuple[
            str,
            str,
            Path,
            tuple[ArtifactRef, ...],
            tuple[ArtifactRef, ...],
            bool,
            str | None,
        ],
        ...,
    ]:
        source = cls._source(run)
        dataset = run.manifest.dataset
        task_source = dataset.config.get("task_source")
        if not isinstance(task_source, str) or not task_source:
            raise AdapterRegistryError("Terminal-Bench dataset task_source is missing")
        task_root = source / task_source
        if not task_root.is_dir() or task_root.is_symlink():
            raise AdapterRegistryError(f"Terminal-Bench task root is missing: {task_root}")
        tasks: list[
            tuple[
                str,
                str,
                Path,
                tuple[ArtifactRef, ...],
                tuple[ArtifactRef, ...],
                bool,
                str | None,
            ]
        ] = []
        for task_dir in sorted(task_root.iterdir(), key=lambda p: p.name):
            if not task_dir.is_dir() or task_dir.is_symlink():
                continue
            manifest_path = task_dir / "task.toml"
            instruction_path = task_dir / "instruction.md"
            if not manifest_path.is_file() or not instruction_path.is_file():
                continue
            try:
                document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
                task_name = str(document["task"]["name"])
            except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
                raise AdapterRegistryError(
                    f"malformed Terminal-Bench task manifest: {manifest_path}"
                ) from exc
            slug = task_name.rsplit("/", 1)[-1]
            if _ID.fullmatch(slug) is None:
                raise AdapterRegistryError(f"invalid Terminal-Bench case id: {slug!r}")
            task_paths = tuple(
                path.resolve()
                for path in sorted(task_dir.rglob("*"))
                if path.is_file()
                and not path.is_symlink()
                and (
                    path.name in {"task.toml", "instruction.md"}
                    or path.relative_to(task_dir).parts[0] == "environment"
                )
            )
            task_refs = tuple(artifact_ref(path) for path in task_paths)
            verifier_paths = tuple(
                path.resolve()
                for path in sorted((task_dir / "tests").rglob("*"))
                if path.is_file() and not path.is_symlink()
            )
            verifier_refs = tuple(artifact_ref(path) for path in verifier_paths)
            environment = document.get("environment", {})
            if not isinstance(environment, Mapping):
                raise AdapterRegistryError(
                    f"Terminal-Bench [environment] must be a table: {manifest_path}"
                )
            raw_allow_internet = environment.get("allow_internet")
            network_mode = environment.get("network_mode")
            if raw_allow_internet is not None and not isinstance(raw_allow_internet, bool):
                raise AdapterRegistryError(
                    f"Terminal-Bench environment.allow_internet must be boolean: {manifest_path}"
                )
            if network_mode is not None:
                if not isinstance(network_mode, str):
                    raise AdapterRegistryError(
                        f"Terminal-Bench environment.network_mode must be a string: {manifest_path}"
                    )
                mode = network_mode.casefold()
                if mode not in {"no-network", "public", "allowlist"}:
                    raise AdapterRegistryError(
                        f"unsupported Terminal-Bench network_mode {network_mode!r}: {manifest_path}"
                    )
                mode_allow = mode != "no-network"
                if raw_allow_internet is not None and raw_allow_internet != mode_allow:
                    raise AdapterRegistryError(
                        f"conflicting Terminal-Bench network policy fields: {manifest_path}"
                    )
                raw_allow_internet = mode_allow
            if raw_allow_internet is None:
                raw_allow_internet = True
            verifier_completion_artifact = None
            test_script = task_dir / "tests" / "test.sh"
            if test_script.is_file():
                test_text = test_script.read_text(encoding="utf-8")
                if "--ctrf /logs/verifier/ctrf.json" in test_text:
                    verifier_completion_artifact = "verifier/ctrf.json"
            if not task_refs or not verifier_refs:
                raise AdapterRegistryError(
                    f"Terminal-Bench task lacks task/verifier contract files: {task_dir}"
                )
            tasks.append(
                (
                    slug,
                    task_name,
                    task_dir.resolve(),
                    task_refs,
                    verifier_refs,
                    raw_allow_internet,
                    verifier_completion_artifact,
                )
            )
        if not tasks:
            raise AdapterRegistryError("Terminal-Bench task root contains no valid tasks")
        ids = [item[0] for item in tasks]
        if len(ids) != len(set(ids)):
            raise AdapterRegistryError("Terminal-Bench task names collide after slugging")
        return tuple(tasks)

    @staticmethod
    def _ordered(
        run: CompiledRun,
        tasks: tuple[
            tuple[
                str,
                str,
                Path,
                tuple[ArtifactRef, ...],
                tuple[ArtifactRef, ...],
                bool,
                str | None,
            ],
            ...,
        ],
    ) -> tuple[
        tuple[
            str,
            str,
            Path,
            tuple[ArtifactRef, ...],
            tuple[ArtifactRef, ...],
            bool,
            str | None,
        ],
        ...,
    ]:
        protocol = run.manifest.execution.protocol
        if protocol is None:
            raise AdapterRegistryError("Terminal-Bench case resolution requires a protocol")
        values = list(tasks)
        if protocol.case_order == "seeded_random":
            if run.manifest.execution.seed is None:
                raise AdapterRegistryError("seeded Terminal-Bench order requires a seed")
            random.Random(run.manifest.execution.seed).shuffle(values)
        elif protocol.case_order == "random":
            random.SystemRandom().shuffle(values)
        elif protocol.case_order in {"custom", "explicit"}:
            try:
                requested = selected_case_ids(protocol)
            except CaseOrderError as exc:
                raise AdapterRegistryError(str(exc)) from exc
            assert requested is not None
            by_id = {item[0]: item for item in values}
            missing = [case_id for case_id in requested if case_id not in by_id]
            if missing:
                raise AdapterRegistryError(
                    "Terminal-Bench explicit case ids are missing: " + ", ".join(missing)
                )
            values = [by_id[case_id] for case_id in requested]
        elif protocol.case_order != "fixed":
            raise AdapterRegistryError(f"unsupported Terminal-Bench case order: {protocol.case_order}")
        return tuple(values)

    def resolve(self, run: CompiledRun, artifact_root: Path) -> ResolvedCaseSet:
        source_refs = self._source_refs(run)
        ordered = self._ordered(run, self._tasks(run))
        cases: list[CaseArtifact] = []
        for task_id, task_name, task_path, task_refs, verifier_refs, _, _ in ordered:
            instruction_path = task_path / "instruction.md"
            instruction = instruction_path.read_text(encoding="utf-8")
            public_payload = _json_bytes(
                {
                    "case_id": task_id,
                    "task_name": task_name,
                    "task_relpath": task_path.relative_to(self._source(run)).as_posix(),
                    "instruction": instruction,
                }
            )
            public_digest = hashlib.sha256(public_payload).hexdigest()
            public_path = artifact_root / "content" / f"{task_id}-{public_digest}.json"
            _write_immutable(public_path, public_payload, label="Terminal-Bench public input")
            cases.append(
                CaseArtifact(
                    case_id=task_id,
                    public_input_ref=artifact_ref(public_path),
                    task_contract_refs=task_refs,
                    verifier_contract_refs=verifier_refs,
                )
            )
        artifact = CaseSetArtifact(
            benchmark_id=run.manifest.benchmark.id,
            benchmark_digest=run.manifest.benchmark.artifact_digest,
            dataset_id=run.manifest.dataset.id,
            dataset_digest=run.manifest.dataset.artifact_digest,
            loader_adapter=self.adapter,
            loader_digest=self.digest,
            selection_method={
                "custom": "custom_order_artifact",
                "explicit": "explicit_case_ids",
            }.get(run.manifest.execution.protocol.case_order, "all_cases"),
            case_order=run.manifest.execution.protocol.case_order,
            order_seed=(
                run.manifest.execution.seed
                if run.manifest.execution.protocol.case_order == "seeded_random"
                else None
            ),
            order_strategy_adapter=(
                run.manifest.execution.protocol.custom_order.adapter
                if run.manifest.execution.protocol.case_order == "custom"
                and run.manifest.execution.protocol.custom_order is not None
                else None
            ),
            order_strategy_ref=(
                custom_order_binding(run.manifest.execution.protocol)[2]
                if run.manifest.execution.protocol.case_order == "custom"
                else None
            ),
            source_content_digest=run.manifest.dataset.source_content_digest,
            source_content_refs=source_refs,
            ordered_case_ids=tuple(case.case_id for case in cases),
            cases=tuple(cases),
        )
        artifact_path = artifact_root / artifact.canonical_digest() / "case_set.json"
        write_immutable_json(artifact_path, artifact, label="Terminal-Bench case-set artifact")
        return ResolvedCaseSet(
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_sha256=sha256_file(artifact_path),
        )

    def load(self, run: CompiledRun, resolved: ResolvedCaseSet) -> LoadedCaseSet:
        source = self._source(run)
        by_id = {item[0]: item for item in self._tasks(run)}
        loaded: list[TerminalBenchCase] = []
        case_set_digest = resolved.artifact.canonical_digest()
        for case in resolved.artifact.cases:
            try:
                public = json.loads(Path(case.public_input_ref.path).read_text(encoding="utf-8"))
                (
                    task_id,
                    task_name,
                    task_path,
                    task_refs,
                    verifier_refs,
                    allow_internet,
                    verifier_completion_artifact,
                ) = by_id[case.case_id]
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
                raise AdapterRegistryError(
                    f"Terminal-Bench activated case is unreadable: {case.case_id}"
                ) from exc
            if public.get("case_id") != task_id or public.get("task_name") != task_name:
                raise AdapterRegistryError(f"Terminal-Bench public input drift: {case.case_id}")
            if (
                tuple(case.task_contract_refs) != task_refs
                or tuple(case.verifier_contract_refs) != verifier_refs
            ):
                raise AdapterRegistryError(f"Terminal-Bench task contract drift: {case.case_id}")
            loaded.append(
                TerminalBenchCase(
                    task_id=task_id,
                    task_name=task_name,
                    task_path=str(task_path),
                    task_manifest_ref=next(
                        ref
                        for ref in task_refs
                        if Path(ref.path).name == "task.toml"
                    ),
                    task_contract_refs=task_refs,
                    verifier_contract_refs=verifier_refs,
                    case_set_digest=case_set_digest,
                    allow_internet=allow_internet,
                    verifier_completion_artifact=verifier_completion_artifact,
                )
            )
        if tuple(item.task_id for item in loaded) != resolved.artifact.ordered_case_ids:
            raise AdapterRegistryError("Terminal-Bench activated case order drift")
        return LoadedCaseSet(
            artifact=resolved.artifact,
            artifact_path=resolved.artifact_path,
            artifact_sha256=resolved.artifact_sha256,
            cases=tuple(loaded),
        )


class TerminalBenchHarborBackend:
    """Pipeline-facing wrapper that launches exactly one native Harbor task."""

    adapter = "harbor"

    def __init__(self, record_root: Path, *, timeout_seconds: float | None = None) -> None:
        self._backend = HarborBackend(
            record_root,
            timeout_seconds=timeout_seconds or 3600.0,
        )
        self.runner_digest = self._backend.runner_digest

    def run_directory(self, run: CompiledRun) -> Path:
        return self._backend.run_directory(run)

    def reset_state(self, case_id: str, policy: str) -> Any:
        return self._backend.reset_state(case_id, policy)

    def load_completed(
        self, run: CompiledRun, bundle_path: Path, *, expected_runner_digest: str
    ) -> CaseExecution | None:
        return self._backend.load_completed(
            run, bundle_path, expected_runner_digest=expected_runner_digest
        )

    def execute(
        self,
        run: CompiledRun,
        case: TerminalBenchCase,
        attempt: Any,
    ) -> CaseExecution:
        preflight = _runtime_preflight(run, case)
        staged_path, staging_receipt = self._stage_task(run, case, attempt.attempt_id)
        execution = self._backend.run(
            run,
            task_path=staged_path,
            case_id=attempt.attempt_id,
            execution_id=attempt.attempt_id,
            attempts=1,
            timeout_seconds=attempt.remaining_wall_seconds,
        )
        if len(execution.cases) != 1:
            raise HarborConfigurationError(
                "Terminal-Bench adapter expected exactly one Harbor trial per attempt"
            )
        native = execution.cases[0]
        bundle = self._validate_verifier_completion(native, case)
        # Scheduler identity belongs to BMP's attempt, while the native trial
        # name remains in VerifierEvidence.details and copied artifacts.
        network_receipt_path = native.bundle_path.parent / "network_observation.json"
        try:
            container, network = self._observe_runtime(
                case,
                native,
                attempt.attempt_id,
                preflight,
                network_receipt_path,
            )
        except TerminalBenchRuntimeIdentityError as exc:
            failure_path = native.bundle_path.parent / "runtime_identity_failure.json"
            atomic_write_json(
                failure_path,
                {
                    "format": _RUNTIME_FAILURE_FORMAT,
                    "case_id": case.task_id,
                    "attempt_id": attempt.attempt_id,
                    "status": "unobservable",
                    "reason_code": exc.code,
                },
            )
            network = record_unobservable_network(
                network_receipt_path,
                resolver_adapter="terminal_bench",
                execution_adapter="harbor",
                case_id=case.task_id,
                boundary=NetworkBoundary.task_container,
                allow_internet=case.allow_internet,
                source=NetworkPolicySource.case_set_artifact,
                source_artifact_digest=case.case_set_digest,
                reason=f"runtime identity observation failed ({exc.code})",
            )
            container = None
        bundle = bundle.model_copy(
            update={
                "run_id": attempt.attempt_id,
                "log_refs": (
                    *bundle.log_refs,
                    artifact_ref(staging_receipt),
                    artifact_ref(network_receipt_path),
                    *(
                        (artifact_ref(failure_path),)
                        if container is None
                        else ()
                    ),
                ),
                "network_policy": network.policy,
                "network_observation": network.observation,
            }
        )
        if container is not None:
            receipt_path = native.bundle_path.parent / "container_receipt.json"
            atomic_write_json(receipt_path, container)
            provenance = bundle.provenance.model_copy(
                update={
                    "image_digest": container["image_id"],
                    "container_receipt_ref": artifact_ref(receipt_path),
                }
            )
            bundle = bundle.model_copy(
                update={
                    "log_refs": (*bundle.log_refs, artifact_ref(receipt_path)),
                    "provenance": provenance,
                }
            )
        atomic_write_json(native.bundle_path, bundle)
        return CaseExecution(
            case_id=case.task_id,
            bundle=bundle,
            bundle_path=native.bundle_path,
            bundle_digest=sha256_file(native.bundle_path),
        )

    @staticmethod
    def _docker_call(
        preflight: _RuntimePreflight,
        argv: tuple[str, ...],
        *,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        try:
            with PinnedExecutable.open(preflight.policy.docker_executable) as pinned:
                if pinned.identity.get("sha256") != preflight.policy.docker_executable_sha256:
                    raise _runtime_failure(
                        "DOCKER_EXECUTABLE_DRIFT",
                        "registered Docker executable identity drifted",
                    )
                result = SubprocessRunner(pass_fds=(pinned.descriptor,))(
                    (pinned.invocation_path, *argv), timeout
                )
                pinned.require_unchanged()
        except TerminalBenchRuntimeIdentityError:
            raise
        except (OSError, AcquisitionError) as exc:
            raise _runtime_failure(
                "DOCKER_COMMAND_FAILED",
                "Docker runtime command failed",
            ) from exc
        return result.returncode, result.stdout, result.stderr

    def _locate_container(
        self,
        preflight: _RuntimePreflight,
        *,
        trial_name: str,
        expected_image: str,
    ) -> tuple[str, dict[str, Any]]:
        project = _compose_project_name(f"{trial_name}__env")
        code, stdout, _ = self._docker_call(
            preflight,
            (
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=main",
                "--no-trunc",
                "--format",
                "{{.ID}}",
            ),
        )
        if code != 0:
            raise _runtime_failure("CONTAINER_DISCOVERY_FAILED", "retained task container discovery failed")
        identifiers = tuple(item.strip() for item in stdout.splitlines() if item.strip())
        if len(identifiers) != 1 or _CONTAINER_ID.fullmatch(identifiers[0]) is None:
            raise _runtime_failure(
                "CONTAINER_DISCOVERY_AMBIGUOUS",
                "retained Harbor task container identity is missing or ambiguous",
            )
        container_id = identifiers[0]
        code, inspect, _ = self._docker_call(preflight, ("inspect", container_id))
        if code != 0:
            raise _runtime_failure("CONTAINER_INSPECT_FAILED", "retained task container inspection failed")
        projection = _container_projection(
            inspect,
            expected_container_id=container_id,
            expected_project=project,
            expected_task_image=expected_image,
        )
        return container_id, projection

    def _observe_runtime(
        self,
        case: TerminalBenchCase,
        native: CaseExecution,
        attempt_id: str,
        preflight: _RuntimePreflight,
        network_receipt_path: Path,
    ) -> tuple[dict[str, Any], Any]:
        trial_name = _native_trial_name(native, attempt_id)
        expected_image = preflight.policy.loaded_image_spec.spec.canonical_tag_ref.removeprefix(
            "docker.io/"
        )
        container_id, projection = self._locate_container(
            preflight,
            trial_name=trial_name,
            expected_image=expected_image,
        )
        if (
            native.bundle.provenance.executable_digest
            != native.bundle.provenance.backend_digest
            or native.bundle.provenance.executable_digest is None
        ):
            raise _runtime_failure(
                "HARBOR_EXECUTABLE_IDENTITY_MISSING",
                "native Harbor executable identity is missing",
            )
        started = False
        try:
            with PinnedExecutable.open(preflight.policy.docker_executable) as pinned:
                runner = SubprocessRunner(pass_fds=(pinned.descriptor,))
                docker = DockerClient(
                    runner,
                    pinned.invocation_path,
                    pinned_executable=pinned,
                )
                image = docker.inspect_optional(projection["image_id"])
                if image is None:
                    raise _runtime_failure(
                        "CONTAINER_IMAGE_MISSING",
                        "retained task container image is unavailable",
                    )
                try:
                    verify_cached_image(
                        preflight.policy.loaded_image_spec,
                        preflight.policy.oci_mirror_registry,
                        docker,
                    )
                except AcquisitionError as exc:
                    raise _runtime_failure(
                        f"DOCKER_{exc.code}",
                        "retained task container image failed OCI identity verification",
                    ) from exc
                spec = preflight.policy.loaded_image_spec.spec
                expected_ref = acquisition_ref(spec, preflight.policy.oci_mirror_registry)
                if (
                    image.image_id != spec.config.digest
                    or image.os != spec.platform.os
                    or image.architecture != spec.platform.architecture
                    or image.rootfs_diff_ids != spec.rootfs_diff_ids
                    or expected_ref not in image.repo_digests
                ):
                    raise _runtime_failure(
                        "CONTAINER_IMAGE_MISMATCH",
                        "retained task container image does not match its OCI identity",
                    )
                was_running = projection["running"]
                if was_running:
                    raise _runtime_failure(
                        "CONTAINER_STATE_UNEXPECTED",
                        "retained task container was still running after Harbor completion",
                    )
                code, _, _ = self._docker_call(preflight, ("start", container_id))
                if code != 0:
                    raise _runtime_failure("CONTAINER_START_FAILED", "retained task container could not be started")
                started = True
                probe_argv = (
                    "exec",
                    container_id,
                    "/bin/bash",
                    "-c",
                    "exec 3<>/dev/tcp/$1/$2",
                    "--",
                    preflight.policy.network_probe_host,
                    str(preflight.policy.network_probe_port),
                )
                code, _, _ = self._docker_call(
                    preflight,
                    probe_argv,
                    timeout=float(preflight.policy.network_probe_timeout_seconds),
                )
                egress_succeeded = code == 0
                endpoint = _network_probe_endpoint(
                    preflight.policy,
                    allow_internet=case.allow_internet,
                    succeeded=egress_succeeded,
                )
                pinned.require_unchanged()
        except TerminalBenchRuntimeIdentityError:
            raise
        except AcquisitionError as exc:
            raise _runtime_failure(
                f"DOCKER_{exc.code}",
                "retained task container identity observation failed",
            ) from exc
        finally:
            if started:
                code, _, _ = self._docker_call(preflight, ("stop", "--time", "10", container_id))
                if code != 0:
                    raise _runtime_failure(
                        "CONTAINER_RESTORE_FAILED",
                        "retained task container could not be restored to stopped state",
                    )
        code, inspect, _ = self._docker_call(preflight, ("inspect", container_id))
        if code != 0:
            raise _runtime_failure("CONTAINER_RESTORE_FAILED", "retained task container final state is unavailable")
        final_projection = _container_projection(
            inspect,
            expected_container_id=container_id,
            expected_project=projection["compose_project"],
            expected_task_image=expected_image,
        )
        if final_projection["running"] or final_projection["state"] not in {"exited", "dead"}:
            raise _runtime_failure("CONTAINER_RESTORE_FAILED", "retained task container was not stopped after observation")
        network = record_active_network_probe(
            network_receipt_path,
            resolver_adapter="terminal_bench",
            execution_adapter="harbor",
            case_id=case.task_id,
            boundary=NetworkBoundary.task_container,
            allow_internet=case.allow_internet,
            source=NetworkPolicySource.case_set_artifact,
            source_artifact_digest=case.case_set_digest,
            egress_succeeded=egress_succeeded,
            reached_endpoints=(endpoint,),
        )
        code, _, _ = self._docker_call(preflight, ("rm", container_id))
        if code != 0:
            raise _runtime_failure(
                "CONTAINER_REMOVE_FAILED",
                "retained task container could not be removed after observation",
            )
        code, stdout, _ = self._docker_call(
            preflight,
            (
                "ps",
                "-a",
                "--filter",
                f"id={container_id}",
                "--no-trunc",
                "--format",
                "{{.ID}}",
            ),
        )
        if code != 0 or stdout.strip():
            raise _runtime_failure(
                "CONTAINER_REMOVE_UNCONFIRMED",
                "retained task container removal could not be confirmed",
            )
        network_name = final_projection["network_names"][0]
        code, _, _ = self._docker_call(
            preflight,
            ("network", "rm", network_name),
        )
        if code != 0:
            raise _runtime_failure(
                "CONTAINER_NETWORK_REMOVE_FAILED",
                "retained task container network could not be removed",
            )
        code, stdout, _ = self._docker_call(
            preflight,
            (
                "network",
                "ls",
                "--filter",
                f"name=^{network_name}$",
                "--format",
                "{{.Name}}",
            ),
        )
        if code != 0 or stdout.strip():
            raise _runtime_failure(
                "CONTAINER_NETWORK_REMOVE_UNCONFIRMED",
                "retained task container network removal could not be confirmed",
            )
        receipt = {
            "format": _CONTAINER_RECEIPT_FORMAT,
            "protocol_version": 1,
            "case_id": case.task_id,
            "attempt_id": attempt_id,
            "native_trial_name": trial_name,
            "image_id": final_projection["image_id"],
            "agent_executable_sha256": native.bundle.provenance.executable_digest,
            "harbor": {
                "executable_sha256": native.bundle.provenance.executable_digest,
                "version": native.bundle.provenance.version,
            },
            "docker": {
                "executable_sha256": preflight.policy.docker_executable_sha256,
                "executable_size_bytes": preflight.docker_executable_size,
                "client_version": preflight.docker_version["client_version"],
                "server_version": preflight.docker_version["server_version"],
                "cache_verification_format": preflight.cache_verification["format"],
            },
            "image": {
                "spec_id": preflight.policy.loaded_image_spec.spec.spec_id,
                "spec_path": preflight.policy.image_spec_relative_path,
                "spec_file_sha256": preflight.policy.loaded_image_spec.file_sha256,
                "canonical_digest_ref": preflight.policy.loaded_image_spec.spec.canonical_digest_ref,
                "canonical_tag_ref": preflight.policy.loaded_image_spec.spec.canonical_tag_ref,
                "acquisition_ref": acquisition_ref(
                    preflight.policy.loaded_image_spec.spec,
                    preflight.policy.oci_mirror_registry,
                ),
                "config_digest": preflight.policy.loaded_image_spec.spec.config.digest,
                "manifest_digest": preflight.policy.loaded_image_spec.spec.manifest.digest,
                "image_id": final_projection["image_id"],
                "repo_digests": sorted(image.repo_digests),
            },
            "container": final_projection,
            "harbor_completion_container": projection,
            "lifecycle": {
                "observed_stopped": True,
                "probe_start_stop": True,
                "removed": True,
                "network_removed": True,
            },
            "network": {
                "allow_internet": case.allow_internet,
                "probe_host": preflight.policy.network_probe_host,
                "probe_port": preflight.policy.network_probe_port,
                "probe_timeout_seconds": preflight.policy.network_probe_timeout_seconds,
                "observation_sha256": sha256_file(network_receipt_path),
                "observation_size_bytes": network_receipt_path.stat().st_size,
            },
        }
        return receipt, network

    @staticmethod
    def _validate_ctrf(path: Path) -> tuple[bool, dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, {"reason": f"invalid CTRF JSON: {type(exc).__name__}"}
        if not isinstance(payload, Mapping):
            return False, {"reason": "CTRF document must be an object"}
        results = payload.get("results")
        if not isinstance(results, Mapping):
            return False, {"reason": "CTRF results object is missing"}
        tool = results.get("tool")
        summary = results.get("summary")
        tests = results.get("tests")
        if not isinstance(tool, Mapping) or tool.get("name") != "pytest":
            return False, {"reason": "CTRF pytest tool identity is missing"}
        if not isinstance(summary, Mapping) or not isinstance(tests, list):
            return False, {"reason": "CTRF summary or tests are missing"}
        counts: dict[str, int] = {}
        for key in ("tests", "passed", "failed", "skipped", "pending", "other"):
            value = summary.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return False, {"reason": f"CTRF summary.{key} is invalid"}
            counts[key] = value
        if counts["tests"] < 1 or counts["tests"] != len(tests):
            return False, {"reason": "CTRF contains no complete test inventory"}
        if (
            sum(
                counts[key]
                for key in ("passed", "failed", "skipped", "pending", "other")
            )
            != counts["tests"]
        ):
            return False, {"reason": "CTRF summary counts are inconsistent"}
        observed: dict[str, int] = {}
        for item in tests:
            if not isinstance(item, Mapping):
                return False, {"reason": "CTRF test entry is invalid"}
            status = item.get("status")
            if status not in {"passed", "failed", "skipped", "pending", "other"}:
                return False, {"reason": "CTRF test status is invalid"}
            observed[str(status)] = observed.get(str(status), 0) + 1
        if any(
            observed.get(key, 0) != counts[key]
            for key in ("passed", "failed", "skipped", "pending", "other")
        ):
            return False, {"reason": "CTRF test statuses disagree with the summary"}
        return True, {
            "tool": "pytest",
            "tool_version": tool.get("version"),
            "tests": counts["tests"],
            "passed": counts["passed"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "pending": counts["pending"],
            "other": counts["other"],
        }

    @staticmethod
    def _validate_pytest_stdout(
        path: Path,
        *,
        reward_passed: bool,
        expected_counts: Mapping[str, int],
    ) -> tuple[bool, dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            return False, {"reason": f"invalid verifier stdout: {type(exc).__name__}"}
        completions: list[dict[str, int]] = []
        for line in lines:
            match = _PYTEST_COMPLETION.fullmatch(line.strip())
            if match is None:
                continue
            counts = {key: 0 for key in ("passed", "failed", "skipped")}
            for amount, status in re.findall(
                r"(\d+)\s+(passed|failed|skipped)",
                match.group("body"),
            ):
                counts[status] += int(amount)
            if sum(counts.values()) > 0:
                completions.append(counts)
        if not completions:
            return False, {
                "reason": "expected at least one completed pytest summary in verifier stdout"
            }
        # A small number of official Terminal-Bench verifiers intentionally run
        # pytest more than once.  The invocation that writes the declared CTRF
        # artifact is the last one, so bind that artifact to the last completed
        # pytest summary rather than rejecting the verifier outright.
        observed = completions[-1]
        if any(observed[key] != expected_counts[key] for key in observed):
            return False, {"reason": "verifier stdout counts disagree with CTRF"}
        stdout_passed = all(completion["failed"] == 0 for completion in completions)
        if stdout_passed != reward_passed:
            return False, {"reason": "verifier stdout outcome disagrees with native reward"}
        return True, {
            "stdout_summary": observed,
            "stdout_completed_pytest_runs": len(completions),
        }

    def _validate_verifier_completion(
        self, native: CaseExecution, case: TerminalBenchCase
    ) -> Any:
        bundle = native.bundle
        expected = case.verifier_completion_artifact
        if expected is None or bundle.status not in {
            RunStatus.pass_,
            RunStatus.verified_fail,
        }:
            return bundle
        matches = [
            ref
            for ref in (*bundle.output_refs, *bundle.log_refs)
            if Path(ref.path).as_posix().endswith(f"/{expected}")
        ]
        stdout_matches = [
            ref
            for ref in bundle.log_refs
            if Path(ref.path).as_posix().endswith("/verifier/test-stdout.txt")
        ]
        details: dict[str, Any]
        valid = len(matches) == 1
        if valid:
            completion_path = Path(matches[0].path)
            if (
                not completion_path.is_file()
                or completion_path.stat().st_size != matches[0].size_bytes
                or sha256_file(completion_path) != matches[0].sha256
            ):
                valid = False
                details = {"reason": "CTRF artifact reference digest drift"}
            else:
                valid, details = self._validate_ctrf(completion_path)
        else:
            details = {
                "reason": (
                    f"expected exactly one {expected} artifact, found {len(matches)}"
                )
            }
        if valid:
            if len(stdout_matches) != 1:
                valid = False
                details = {
                    "reason": (
                        "expected exactly one verifier/test-stdout.txt artifact, "
                        f"found {len(stdout_matches)}"
                    )
                }
            else:
                stdout_path = Path(stdout_matches[0].path)
                if (
                    not stdout_path.is_file()
                    or stdout_path.stat().st_size != stdout_matches[0].size_bytes
                    or sha256_file(stdout_path) != stdout_matches[0].sha256
                ):
                    valid = False
                    details = {"reason": "verifier stdout artifact reference digest drift"}
                else:
                    stdout_valid, stdout_details = self._validate_pytest_stdout(
                        stdout_path,
                        reward_passed=bundle.status == RunStatus.pass_,
                        expected_counts=details,
                    )
                    valid = stdout_valid
                    if valid:
                        details = {**details, **stdout_details}
                    else:
                        details = stdout_details
        if valid:
            verifier = bundle.verifier_evidence
            if verifier is None:
                valid = False
                details = {"reason": "native verifier evidence is missing"}
        if valid:
            assert bundle.verifier_evidence is not None
            verifier = bundle.verifier_evidence
            verifier_details = dict(verifier.details)
            verifier_details["completion_evidence"] = {
                "kind": "ctrf+pytest-stdout",
                "artifact": expected,
                **details,
            }
            return bundle.model_copy(
                update={
                    "verifier_evidence": verifier.model_copy(
                        update={
                            "artifact_refs": (
                                *verifier.artifact_refs,
                                matches[0],
                                stdout_matches[0],
                            ),
                            "details": verifier_details,
                        }
                    )
                }
            )
        status_path = native.bundle_path.parent / "verifier_completion_status.json"
        atomic_write_json(
            status_path,
            {
                "case_id": case.task_id,
                "status": RunStatus.verifier_error.value,
                "required_artifact": expected,
                **details,
            },
        )
        verifier = bundle.verifier_evidence
        verifier_details = {} if verifier is None else dict(verifier.details)
        verifier_details["completion_evidence"] = {
            "kind": "ctrf+pytest-stdout",
            "artifact": expected,
            "valid": False,
            **details,
        }
        verifier_evidence = VerifierEvidence(
            verifier="harbor.native",
            passed=False,
            artifact_refs=(artifact_ref(status_path),),
            details=verifier_details,
        )
        return bundle.model_copy(
            update={
                "status": RunStatus.verifier_error,
                "output_refs": (),
                "log_refs": (*bundle.log_refs, artifact_ref(status_path)),
                "verifier_evidence": verifier_evidence,
            }
        )

    def _stage_task(
        self,
        run: CompiledRun,
        case: TerminalBenchCase,
        attempt_id: str,
    ) -> tuple[Path, Path]:
        """Create a task view that cannot expose Terminal-Bench solutions."""

        source = Path(case.task_path).resolve(strict=True)
        if not source.is_dir() or source.is_symlink():
            raise HarborConfigurationError(f"Terminal-Bench task path is invalid: {source}")
        destination = (
            self._backend.run_directory(run)
            / "staged_tasks"
            / re.sub(r"[^A-Za-z0-9_.-]+", "_", attempt_id)
        )
        if destination.exists():
            raise HarborConfigurationError(
                f"staged task path already exists and is immutable: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=False)
        copied: list[Path] = []
        seen_paths: set[Path] = set()
        # Revalidate exactly the activated source refs before copying.  The
        # loader's resolution and this staging step are separate trust
        # boundaries, so a source mutation must fail closed.
        for ref in (*case.task_contract_refs, *case.verifier_contract_refs):
            raw_path = Path(ref.path)
            current = raw_path.anchor and Path(raw_path.anchor) or Path("/")
            for component in raw_path.parts[1:]:
                current = current / component
                if current.is_symlink():
                    raise HarborConfigurationError(
                        f"symlink in activated task ref: {raw_path}"
                    )
            path = raw_path.resolve(strict=True)
            try:
                relative = path.relative_to(source)
            except ValueError as exc:
                raise HarborConfigurationError(
                    f"activated task ref escapes task root: {ref.path}"
                ) from exc
            if path in seen_paths:
                raise HarborConfigurationError("activated task refs contain duplicate paths")
            seen_paths.add(path)
            if path.is_symlink() or not path.is_file():
                raise HarborConfigurationError(
                    f"activated task ref is not a regular file: {path}"
                )
            if path.stat().st_size != ref.size_bytes or sha256_file(path) != ref.sha256:
                raise HarborConfigurationError(
                    f"activated task ref byte drift: {relative.as_posix()}"
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(target)
        required = (destination / "task.toml", destination / "instruction.md")
        if any(not path.is_file() for path in required):
            raise HarborConfigurationError("staged Terminal-Bench task is missing required files")
        staged_refs = tuple(artifact_ref(path) for path in sorted(copied))
        staged_digest = source_closure_digest(destination, staged_refs)
        receipt_path = destination.parent / f"{destination.name}-staging-receipt.json"
        receipt = {
            "protocol_version": 1,
            "case_id": case.task_id,
            "attempt_id": attempt_id,
            "source_task_path": str(source),
            "source_task_manifest_ref": case.task_manifest_ref.model_dump(mode="json"),
            "staged_task_path": str(destination),
            "staged_content_digest": staged_digest,
            "staged_content_refs": [ref.model_dump(mode="json") for ref in staged_refs],
            "excluded_paths": ["solution", "README.md", ".git"],
            "solution_excluded": (source / "solution").exists()
            and not (destination / "solution").exists(),
        }
        atomic_write_json(receipt_path, receipt)
        return destination, receipt_path


class TerminalBenchHarborFactory:
    adapter = "harbor"
    digest = _MODULE_DIGEST

    def build(
        self,
        run: CompiledRun,
        *,
        record_root: Path,
        workspace_root: Path,
    ) -> TerminalBenchHarborBackend:
        if run.manifest.execution.backend.adapter != self.adapter:
            raise AdapterRegistryError("Terminal-Bench Harbor factory received a non-Harbor run")
        return TerminalBenchHarborBackend(
            record_root,
            timeout_seconds=run.manifest.execution.budget.max_wall_seconds,
        )


class TerminalBenchHarborExecutionAdapter:
    benchmark_adapter = "terminal_bench"
    backend_adapter = "harbor"
    subject_interface = "terminal-bench-harbor-v1"
    digest = _MODULE_DIGEST

    def execute(
        self,
        backend: TerminalBenchHarborBackend,
        run: CompiledRun,
        case: TerminalBenchCase,
        attempt: Any,
    ) -> CaseExecution:
        return backend.execute(run, case, attempt)

    def reset_state(self, backend: TerminalBenchHarborBackend, case_id: str, policy: str) -> Any:
        return backend.reset_state(case_id, policy)


__all__ = [
    "TerminalBenchCase",
    "TerminalBenchLoader",
    "TerminalBenchHarborBackend",
    "TerminalBenchHarborFactory",
    "TerminalBenchHarborExecutionAdapter",
]
