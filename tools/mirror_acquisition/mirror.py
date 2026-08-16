"""Secret-safe Git/Python diagnostics and digest-bound OCI acquisition."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import secrets
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit

from .models import LoadedImageSpec, OciImageSpec


CANONICAL_GIT_URL = "https://github.com/Minions-Land/MagentaBenchmark.git"
GIT_MIRROR_URL = (
    "https://ghfast.top/https://github.com/Minions-Land/MagentaBenchmark.git"
)
GIT_MIRROR_REFSPEC = "+refs/heads/*:refs/remotes/mirror/*"
GIT_MIRROR_PUSH_URL = "disabled://magentabench-fetch-only"
PYTHON_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"
DEFAULT_OCI_MIRROR = "docker.1ms.run"
DOCTOR_FORMAT = "magentabench-mirror-doctor-v1"
RECEIPT_FORMAT = "magentabench-oci-acquisition-receipt-v1"

_MAX_COMMAND_OUTPUT = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 1024 * 1024 * 1024
_TERMINATION_GRACE_SECONDS = 5.0
_REGISTRY_PATTERN = re.compile(
    r"^(?:localhost|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)"
    r"(?::(?:[1-9][0-9]{0,4}))?$"
)
_DOCKER_VERSION_PATTERN = re.compile(
    r"^(?P<core>[0-9]{1,4}(?:\.[0-9]{1,4}){1,3})"
    r"(?:[-+][0-9A-Za-z.-]{1,32})?$"
)
_DOCKER_API_PATTERN = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}$")
_DOCKER_OS_VALUES = frozenset({"darwin", "freebsd", "linux", "windows"})
_DOCKER_ARCH_VALUES = frozenset(
    {"386", "amd64", "arm", "arm64", "ppc64le", "riscv64", "s390x"}
)


class AcquisitionError(RuntimeError):
    """A classified failure whose text never contains untrusted command output."""

    def __init__(self, code: str, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], float], CommandResult]


class SubprocessRunner:
    """Run public acquisition commands without ambient credential variables."""

    def __init__(
        self,
        *,
        docker_config: Path | None = None,
        inherit_git_config: bool = False,
        pass_fds: Sequence[int] = (),
    ) -> None:
        self._environment = {
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if inherit_git_config:
            self._environment.pop("GIT_CONFIG_GLOBAL")
            self._environment.pop("GIT_CONFIG_NOSYSTEM")
            for name, value in os.environ.items():
                if name in {"GIT_CONFIG", "HOME", "XDG_CONFIG_HOME"} or name.startswith(
                    "GIT_CONFIG_"
                ):
                    self._environment[name] = value
        if docker_config is not None:
            self._environment["DOCKER_CONFIG"] = str(docker_config)
        self._pass_fds = tuple(pass_fds)

    def __call__(self, argv: Sequence[str], timeout: float) -> CommandResult:
        try:
            process = subprocess.Popen(
                tuple(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment,
                pass_fds=self._pass_fds,
                start_new_session=True,
            )
        except OSError:
            return CommandResult(127)
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        exceeded = False
        timed_out = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = selector.select(min(remaining, 1.0))
                for key, _ in events:
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    streams[key.fd].extend(chunk)
                    if (
                        sum(len(value) for value in streams.values())
                        > _MAX_COMMAND_OUTPUT
                    ):
                        exceeded = True
                        break
                if exceeded:
                    break
        finally:
            selector.close()
        if not exceeded and not timed_out:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if exceeded or timed_out:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        else:
            process.wait()
        if timed_out:
            return CommandResult(124)
        if exceeded:
            return CommandResult(125)
        return CommandResult(
            process.returncode,
            bytes(streams[stdout_fd]).decode("utf-8", errors="replace"),
            bytes(streams[stderr_fd]).decode("utf-8", errors="replace"),
        )


@dataclass(frozen=True)
class LocalImageObservation:
    image_id: str
    os: str
    architecture: str
    variant: str | None
    rootfs_diff_ids: tuple[str, ...]
    repo_digests: tuple[str, ...]


def _json_sha256(value: Any) -> str:
    data = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError
        output[key] = value
    return output


def _descriptor_identity(descriptor: int) -> dict[str, int | str]:
    digest = hashlib.sha256()
    size = 0
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > _MAX_EXECUTABLE_BYTES
    ):
        raise ValueError
    while size < before.st_size:
        block = os.pread(descriptor, min(1024 * 1024, before.st_size - size), size)
        if not block:
            break
        size += len(block)
        digest.update(block)
    after = os.fstat(descriptor)
    if size != before.st_size or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError
    return {"sha256": digest.hexdigest(), "size_bytes": size}


@dataclass
class PinnedExecutable:
    """An opened executable whose exact inode is inherited by child processes."""

    descriptor: int
    identity: dict[str, int | str]

    @classmethod
    def open(cls, path: Path) -> "PinnedExecutable":
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = -1
        try:
            resolved = path.resolve(strict=True)
            descriptor = os.open(resolved, flags)
            identity = _descriptor_identity(descriptor)
            descriptor_stat = os.fstat(descriptor)
            if (
                not descriptor_stat.st_mode & 0o111
                or not Path("/proc/self/fd").is_dir()
            ):
                raise OSError
        except (OSError, ValueError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise AcquisitionError(
                "DOCKER_IDENTITY_FAILED", "Docker executable identity is unavailable"
            ) from exc
        return cls(descriptor=descriptor, identity=identity)

    @property
    def invocation_path(self) -> str:
        return f"/proc/self/fd/{self.descriptor}"

    def require_unchanged(self) -> None:
        if self.descriptor < 0:
            raise AcquisitionError(
                "DOCKER_IDENTITY_CHANGED", "Docker executable identity changed"
            )
        try:
            observed = _descriptor_identity(self.descriptor)
        except (OSError, ValueError):
            raise AcquisitionError(
                "DOCKER_IDENTITY_CHANGED", "Docker executable identity changed"
            ) from None
        if observed != self.identity:
            raise AcquisitionError(
                "DOCKER_IDENTITY_CHANGED", "Docker executable identity changed"
            )

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> "PinnedExecutable":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _require_executable_unchanged(
    docker: "DockerClient", expected: Mapping[str, int | str] | None
) -> None:
    if expected is None:
        return
    if docker.pinned_executable is None:
        raise AcquisitionError(
            "DOCKER_IDENTITY_CHANGED", "Docker executable identity changed"
        )
    docker.pinned_executable.require_unchanged()
    if docker.pinned_executable.identity != expected:
        raise AcquisitionError(
            "DOCKER_IDENTITY_CHANGED", "Docker executable identity changed"
        )


def _observe_invoked_executable(
    docker: "DockerClient",
) -> dict[str, int | str] | None:
    pinned = docker.pinned_executable
    if pinned is None:
        return None
    if docker.executable != pinned.invocation_path:
        raise AcquisitionError(
            "DOCKER_IDENTITY_FAILED", "Docker executable identity is unavailable"
        )
    pinned.require_unchanged()
    return dict(pinned.identity)


def _safe_https_url(value: str) -> str:
    if (
        not value
        or not value.isascii()
        or any(character in value for character in ("\x00", "\r", "\n", "\\"))
    ):
        raise AcquisitionError(
            "INVALID_POLICY", "mirror policy is invalid", exit_code=2
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AcquisitionError(
            "INVALID_POLICY", "mirror policy is invalid", exit_code=2
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise AcquisitionError(
            "INVALID_POLICY", "mirror policy is invalid", exit_code=2
        )
    return value


def validate_mirror_registry(value: str) -> str:
    """Accept a registry authority, never a URL, repository, or credential."""

    if (
        not value
        or not value.isascii()
        or value != value.lower()
        or any(character.isspace() for character in value)
        or any(character in value for character in ("\x00", "@", "/", "\\"))
        or "://" in value
        or _REGISTRY_PATTERN.fullmatch(value) is None
    ):
        raise AcquisitionError(
            "INVALID_MIRROR_REGISTRY",
            "OCI mirror registry is invalid",
            exit_code=2,
        )
    if value.split(":", 1)[0] in {"docker.io", "registry-1.docker.io"}:
        raise AcquisitionError(
            "CANONICAL_MIRROR_AMBIGUITY",
            "OCI mirror must differ from the canonical registry",
            exit_code=2,
        )
    if ":" in value:
        port = int(value.rsplit(":", 1)[1])
        if port > 65535:
            raise AcquisitionError(
                "INVALID_MIRROR_REGISTRY",
                "OCI mirror registry is invalid",
                exit_code=2,
            )
    return value


def acquisition_ref(spec: OciImageSpec, mirror_registry: str) -> str:
    registry = validate_mirror_registry(mirror_registry)
    return f"{registry}/{spec.repository_path}@{spec.manifest.digest}"


def acquisition_plan(
    loaded: LoadedImageSpec, mirror_registry: str = DEFAULT_OCI_MIRROR
) -> dict[str, Any]:
    spec = loaded.spec
    source_ref = acquisition_ref(spec, mirror_registry)
    return {
        "format": "magentabench-oci-acquisition-plan-v1",
        "spec": {
            "id": spec.spec_id,
            "file_sha256": loaded.file_sha256,
            "identity_sha256": spec.identity_sha256(),
        },
        "identity": {
            "canonical_digest_ref": spec.canonical_digest_ref,
            "canonical_tag_ref": spec.canonical_tag_ref,
            "manifest_digest": spec.manifest.digest,
            "platform": spec.platform.model_dump(mode="json"),
        },
        "transport": {
            "acquisition_ref": source_ref,
            "mirror_registry": validate_mirror_registry(mirror_registry),
            "mirror_is_experiment_identity": False,
        },
        "operations": (
            "verify remote raw manifest",
            "pull immutable digest if absent",
            "verify local config, platform, and rootfs",
            "tag canonical reference if non-conflicting",
            "write explicit receipt",
        ),
    }


def _git_values(
    runner: CommandRunner, git: str, repository: Path, key: str
) -> tuple[str, ...]:
    result = runner(
        (git, "-C", str(repository), "config", "--local", "--get-all", key),
        10.0,
    )
    if result.returncode == 1 and not result.stdout:
        return ()
    if result.returncode != 0 or len(result.stdout) > 64 * 1024:
        return ()
    return tuple(result.stdout.splitlines())


def _git_remote_urls(
    runner: CommandRunner,
    git: str,
    repository: Path,
    remote: str,
    *,
    push: bool,
) -> tuple[str, ...]:
    argv = [git, "-C", str(repository), "remote", "get-url"]
    if push:
        argv.append("--push")
    argv.extend(("--all", remote))
    result = runner(tuple(argv), 10.0)
    if result.returncode != 0 or len(result.stdout) > 64 * 1024:
        return ()
    return tuple(result.stdout.splitlines())


def _git_has_local_url_rewrites(
    runner: CommandRunner, git: str, repository: Path
) -> bool:
    result = runner(
        (
            git,
            "-C",
            str(repository),
            "config",
            "--local",
            "--get-regexp",
            r"^url\..*\.(insteadOf|pushInsteadOf)$",
        ),
        10.0,
    )
    return result.returncode != 1 or bool(result.stdout)


def _git_state(
    runner: CommandRunner, git: str, repository: Path
) -> dict[str, tuple[str, ...]]:
    return {
        "origin_urls": _git_values(runner, git, repository, "remote.origin.url"),
        "origin_push_urls": _git_values(
            runner, git, repository, "remote.origin.pushurl"
        ),
        "mirror_urls": _git_values(runner, git, repository, "remote.mirror.url"),
        "mirror_push_urls": _git_values(
            runner, git, repository, "remote.mirror.pushurl"
        ),
        "mirror_fetch": _git_values(runner, git, repository, "remote.mirror.fetch"),
        "origin_effective_urls": _git_remote_urls(
            runner, git, repository, "origin", push=False
        ),
        "origin_effective_push_urls": _git_remote_urls(
            runner, git, repository, "origin", push=True
        ),
        "mirror_effective_urls": _git_remote_urls(
            runner, git, repository, "mirror", push=False
        ),
        "mirror_effective_push_urls": _git_remote_urls(
            runner, git, repository, "mirror", push=True
        ),
        "local_url_rewrites": (
            ("present",) if _git_has_local_url_rewrites(runner, git, repository) else ()
        ),
    }


def _docker_version(runner: CommandRunner, docker: str) -> dict[str, str] | None:
    result = runner((docker, "version", "--format", "{{json .}}"), 15.0)
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        client = payload["Client"]
        server = payload["Server"]
        client_version = _DOCKER_VERSION_PATTERN.fullmatch(client["Version"])
        server_version = _DOCKER_VERSION_PATTERN.fullmatch(server["Version"])
        if client_version is None or server_version is None:
            raise ValueError
        values = {
            "client_version": client_version.group("core"),
            "client_api_version": client["ApiVersion"],
            "server_version": server_version.group("core"),
            "server_api_version": server["ApiVersion"],
            "server_os": server["Os"],
            "server_architecture": server["Arch"],
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError
        if (
            any(
                _DOCKER_API_PATTERN.fullmatch(values[key]) is None
                for key in ("client_api_version", "server_api_version")
            )
            or values["server_os"] not in _DOCKER_OS_VALUES
            or values["server_architecture"] not in _DOCKER_ARCH_VALUES
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return values


def mirror_doctor(
    repository: Path,
    *,
    environment: Mapping[str, str] | None = None,
    git_runner: CommandRunner | None = None,
    docker_runner: CommandRunner | None = None,
    git_executable: str | None = None,
    docker_executable: str | None = None,
) -> dict[str, Any]:
    """Inspect configured mirrors without fetch, pull, tag, or credential output."""

    _safe_https_url(CANONICAL_GIT_URL)
    _safe_https_url(GIT_MIRROR_URL)
    _safe_https_url(PYTHON_INDEX_URL)
    validate_mirror_registry(DEFAULT_OCI_MIRROR)
    git_command_runner = git_runner or SubprocessRunner(inherit_git_config=True)
    docker_command_runner = docker_runner or SubprocessRunner()
    git = git_executable or shutil.which("git")
    docker = docker_executable or shutil.which("docker")
    env = environment if environment is not None else os.environ

    state = (
        _git_state(git_command_runner, git, repository.resolve())
        if git is not None
        else {}
    )
    origin_urls = state.get("origin_urls", ())
    origin_push_urls = state.get("origin_push_urls", ())
    docker_version = (
        _docker_version(docker_command_runner, docker) if docker is not None else None
    )
    checks: list[dict[str, Any]] = [
        {
            "id": "docker.cli_and_daemon",
            "required": True,
            "status": (
                "pass" if docker is not None and docker_version is not None else "fail"
            ),
        },
        {
            "id": "git.mirror.fetch_only",
            "required": True,
            "status": (
                "pass"
                if state.get("mirror_urls") == (GIT_MIRROR_URL,)
                and state.get("mirror_push_urls") == (GIT_MIRROR_PUSH_URL,)
                and state.get("mirror_fetch") == (GIT_MIRROR_REFSPEC,)
                and state.get("mirror_effective_urls") == (GIT_MIRROR_URL,)
                and state.get("mirror_effective_push_urls") == (GIT_MIRROR_PUSH_URL,)
                and not state.get("local_url_rewrites")
                else "fail"
            ),
        },
        {
            "id": "git.origin.authoritative",
            "required": True,
            "status": (
                "pass"
                if origin_urls == (CANONICAL_GIT_URL,)
                and origin_push_urls in ((), (CANONICAL_GIT_URL,))
                and state.get("origin_effective_urls") == (CANONICAL_GIT_URL,)
                and state.get("origin_effective_push_urls") == (CANONICAL_GIT_URL,)
                and not state.get("local_url_rewrites")
                else "fail"
            ),
        },
        {
            "id": "oci.mapping.digest_preserving",
            "required": True,
            "status": "pass",
        },
        {
            "id": "python.index.active",
            "required": True,
            "status": (
                "pass" if env.get("UV_DEFAULT_INDEX") == PYTHON_INDEX_URL else "fail"
            ),
        },
    ]
    checks.sort(key=lambda item: item["id"])
    failed = tuple(item["id"] for item in checks if item["status"] != "pass")
    return {
        "format": DOCTOR_FORMAT,
        "ok": not failed,
        "policy": {
            "git": {
                "canonical_remote": "origin",
                "canonical_url": CANONICAL_GIT_URL,
                "fetch_remote": "mirror",
                "fetch_url": GIT_MIRROR_URL,
                "push_disabled": True,
            },
            "oci": {
                "canonical_registry": "docker.io",
                "mirror_registry": DEFAULT_OCI_MIRROR,
                "requires_immutable_digest": True,
            },
            "python": {"index_url": PYTHON_INDEX_URL},
        },
        "checks": checks,
        "failure_codes": failed,
        "docker": docker_version,
        "limitations": (
            "Doctor performs no Git fetch, image pull, image tag, or container run.",
            "Mirror readiness is transport evidence, not benchmark evidence.",
            "Observed mismatched values are intentionally not returned.",
        ),
    }


def configure_git_mirror(
    repository: Path,
    *,
    runner: CommandRunner | None = None,
    git_executable: str | None = None,
) -> dict[str, Any]:
    """Idempotently configure a fetch-only mirror after validating origin."""

    command_runner = runner or SubprocessRunner(inherit_git_config=True)
    git = git_executable or shutil.which("git")
    if git is None:
        raise AcquisitionError("GIT_UNAVAILABLE", "Git is unavailable")
    root = repository.resolve()
    state = _git_state(command_runner, git, root)
    if (
        state["origin_urls"] != (CANONICAL_GIT_URL,)
        or state["origin_push_urls"] not in ((), (CANONICAL_GIT_URL,))
        or state["origin_effective_urls"] != (CANONICAL_GIT_URL,)
        or state["origin_effective_push_urls"] != (CANONICAL_GIT_URL,)
        or bool(state["local_url_rewrites"])
    ):
        raise AcquisitionError(
            "ORIGIN_NOT_CANONICAL",
            "origin must be canonical before mirror configuration",
            exit_code=2,
        )
    expected = {
        "mirror_urls": (GIT_MIRROR_URL,),
        "mirror_push_urls": (GIT_MIRROR_PUSH_URL,),
        "mirror_fetch": (GIT_MIRROR_REFSPEC,),
        "mirror_effective_urls": (GIT_MIRROR_URL,),
        "mirror_effective_push_urls": (GIT_MIRROR_PUSH_URL,),
    }
    if all(state[key] == value for key, value in expected.items()):
        return {
            "format": "magentabench-git-mirror-config-v1",
            "changed": False,
            "fetch_remote": "mirror",
            "push_disabled": True,
        }

    settings = (
        ("remote.mirror.pushurl", GIT_MIRROR_PUSH_URL),
        ("remote.mirror.url", GIT_MIRROR_URL),
        ("remote.mirror.fetch", GIT_MIRROR_REFSPEC),
    )
    for key, value in settings:
        result = command_runner(
            (
                git,
                "-C",
                str(root),
                "config",
                "--local",
                "--replace-all",
                key,
                value,
            ),
            10.0,
        )
        if result.returncode != 0:
            raise AcquisitionError(
                "GIT_CONFIGURATION_FAILED", "Git mirror configuration failed"
            )

    verified = _git_state(command_runner, git, root)
    if (
        verified["origin_urls"] != (CANONICAL_GIT_URL,)
        or verified["origin_push_urls"] not in ((), (CANONICAL_GIT_URL,))
        or verified["origin_effective_urls"] != (CANONICAL_GIT_URL,)
        or verified["origin_effective_push_urls"] != (CANONICAL_GIT_URL,)
        or bool(verified["local_url_rewrites"])
        or not all(verified[key] == value for key, value in expected.items())
    ):
        raise AcquisitionError(
            "GIT_CONFIGURATION_FAILED", "Git mirror configuration failed"
        )
    return {
        "format": "magentabench-git-mirror-config-v1",
        "changed": True,
        "fetch_remote": "mirror",
        "push_disabled": True,
    }


def _descriptor_from_raw(value: Any) -> dict[str, Any]:
    required = {"mediaType", "size", "digest"}
    optional = {"annotations", "artifactType", "data", "platform", "urls"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(required | optional)
    ):
        raise AcquisitionError("MANIFEST_INVALID", "OCI manifest is invalid")
    media_type = value.get("mediaType")
    size = value.get("size")
    digest = value.get("digest")
    if (
        not isinstance(media_type, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
    ):
        raise AcquisitionError("MANIFEST_INVALID", "OCI manifest is invalid")
    return {"media_type": media_type, "size_bytes": size, "digest": digest}


def _parse_manifest(
    output: str, *, spec: OciImageSpec, expected_ref: str
) -> dict[str, Any]:
    try:
        verbose = json.loads(output, object_pairs_hook=_reject_duplicate_pairs)
        raw_text = verbose["Raw"]
        if not isinstance(raw_text, str) or len(raw_text) > _MAX_COMMAND_OUTPUT:
            raise ValueError
        raw_bytes = base64.b64decode(raw_text, validate=True)
        raw = json.loads(raw_bytes, object_pairs_hook=_reject_duplicate_pairs)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcquisitionError("MANIFEST_INVALID", "OCI manifest is invalid") from exc

    if hashlib.sha256(raw_bytes).hexdigest() != spec.manifest.digest.removeprefix(
        "sha256:"
    ):
        raise AcquisitionError(
            "MANIFEST_DIGEST_MISMATCH", "OCI manifest digest does not match the spec"
        )
    required_manifest_keys = {"schemaVersion", "mediaType", "config", "layers"}
    optional_manifest_keys = {"annotations", "artifactType", "subject"}
    if (
        not isinstance(raw, dict)
        or not required_manifest_keys.issubset(raw)
        or not set(raw).issubset(required_manifest_keys | optional_manifest_keys)
    ):
        raise AcquisitionError("MANIFEST_INVALID", "OCI manifest is invalid")
    if raw["schemaVersion"] != 2 or raw["mediaType"] != spec.manifest.media_type:
        raise AcquisitionError(
            "MANIFEST_MEDIA_TYPE_MISMATCH", "OCI manifest type does not match the spec"
        )
    config = _descriptor_from_raw(raw["config"])
    layers_raw = raw["layers"]
    if not isinstance(layers_raw, list):
        raise AcquisitionError("MANIFEST_INVALID", "OCI manifest is invalid")
    layers = tuple(_descriptor_from_raw(item) for item in layers_raw)
    if config != spec.config.model_dump(mode="json"):
        raise AcquisitionError(
            "CONFIG_DESCRIPTOR_MISMATCH",
            "OCI config descriptor does not match the spec",
        )
    if layers != tuple(item.model_dump(mode="json") for item in spec.layers):
        raise AcquisitionError(
            "LAYER_DESCRIPTOR_MISMATCH", "OCI layer descriptors do not match the spec"
        )

    descriptor = verbose.get("Descriptor")
    expected_platform = {
        "architecture": spec.platform.architecture,
        "os": spec.platform.os,
    }
    if spec.platform.variant is not None:
        expected_platform["variant"] = spec.platform.variant
    if (
        verbose.get("Ref") != expected_ref
        or not isinstance(descriptor, dict)
        or descriptor.get("digest") != spec.manifest.digest
        or descriptor.get("mediaType") != spec.manifest.media_type
        or descriptor.get("size") != len(raw_bytes)
        or descriptor.get("size") != spec.manifest.size_bytes
        or descriptor.get("platform") != expected_platform
    ):
        raise AcquisitionError(
            "MANIFEST_DESCRIPTOR_MISMATCH",
            "OCI manifest descriptor does not match the spec",
        )
    return {
        "manifest": spec.manifest.model_dump(mode="json"),
        "config": config,
        "layers": layers,
        "platform": spec.platform.model_dump(mode="json"),
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


class DockerClient:
    def __init__(
        self,
        runner: CommandRunner,
        executable: str,
        *,
        pinned_executable: PinnedExecutable | None = None,
    ) -> None:
        self.runner = runner
        self.executable = executable
        self.pinned_executable = pinned_executable

    def version(self) -> dict[str, str]:
        result = _docker_version(self.runner, self.executable)
        if result is None:
            raise AcquisitionError("DOCKER_UNAVAILABLE", "Docker is unavailable")
        return result

    def verify_remote_manifest(
        self, spec: OciImageSpec, source_ref: str
    ) -> dict[str, Any]:
        result = self.runner(
            (self.executable, "manifest", "inspect", "--verbose", source_ref),
            60.0,
        )
        if result.returncode != 0:
            raise AcquisitionError(
                "DOCKER_MANIFEST_FAILED", "Docker manifest inspection failed"
            )
        return _parse_manifest(result.stdout, spec=spec, expected_ref=source_ref)

    def inspect_optional(self, reference: str) -> LocalImageObservation | None:
        result = self.runner(
            (self.executable, "image", "inspect", reference),
            20.0,
        )
        if result.returncode != 0:
            diagnostic = result.stderr.strip()
            missing_prefixes = (
                "Error response from daemon: No such image: ",
                "Error: No such object: ",
            )
            explicitly_missing = (
                result.returncode == 1
                and result.stdout.strip() in {"", "[]"}
                and "\n" not in diagnostic
                and any(diagnostic.startswith(prefix) for prefix in missing_prefixes)
                and any(
                    len(diagnostic.removeprefix(prefix)) > 0
                    for prefix in missing_prefixes
                    if diagnostic.startswith(prefix)
                )
            )
            if explicitly_missing:
                return None
            raise AcquisitionError(
                "DOCKER_INSPECT_FAILED", "Docker image inspection failed"
            )
        try:
            payload = json.loads(result.stdout)
            if not isinstance(payload, list) or len(payload) != 1:
                raise ValueError
            image = payload[0]
            rootfs = image["RootFS"]
            repo_digests = image.get("RepoDigests") or []
            variant = image.get("Variant")
            observation = LocalImageObservation(
                image_id=image["Id"],
                os=image["Os"],
                architecture=image["Architecture"],
                variant=variant,
                rootfs_diff_ids=tuple(rootfs["Layers"]),
                repo_digests=tuple(repo_digests),
            )
            if not all(
                isinstance(value, str)
                for value in (
                    observation.image_id,
                    observation.os,
                    observation.architecture,
                    *observation.rootfs_diff_ids,
                    *observation.repo_digests,
                )
            ) or (variant is not None and not isinstance(variant, str)):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AcquisitionError(
                "LOCAL_IMAGE_INVALID", "Docker image inspection is invalid"
            ) from exc
        return observation

    def pull(self, spec: OciImageSpec, source_ref: str) -> None:
        result = self.runner(
            (
                self.executable,
                "image",
                "pull",
                "--platform",
                spec.platform.docker_value,
                source_ref,
            ),
            1800.0,
        )
        if result.returncode != 0:
            raise AcquisitionError("DOCKER_PULL_FAILED", "Docker image pull failed")

    def tag(self, source_ref: str, canonical_tag_ref: str) -> None:
        result = self.runner(
            (self.executable, "image", "tag", source_ref, canonical_tag_ref),
            30.0,
        )
        if result.returncode != 0:
            raise AcquisitionError("DOCKER_TAG_FAILED", "Docker image tag failed")


def _local_matches(
    spec: OciImageSpec,
    observation: LocalImageObservation,
    *,
    required_repo_digest: str | None = None,
) -> bool:
    expected_variant = spec.platform.variant
    variant_matches = (
        observation.variant == expected_variant
        if expected_variant is not None
        else observation.variant in (None, "")
    )
    return (
        observation.image_id == spec.config.digest
        and observation.os == spec.platform.os
        and observation.architecture == spec.platform.architecture
        and variant_matches
        and observation.rootfs_diff_ids == spec.rootfs_diff_ids
        and (
            required_repo_digest is None
            or required_repo_digest in observation.repo_digests
        )
    )


def _require_local_match(
    spec: OciImageSpec,
    observation: LocalImageObservation | None,
    *,
    required_repo_digest: str | None = None,
) -> LocalImageObservation:
    if observation is None:
        raise AcquisitionError("LOCAL_IMAGE_MISSING", "required local image is missing")
    if observation.image_id != spec.config.digest:
        raise AcquisitionError(
            "LOCAL_CONFIG_MISMATCH", "local image config does not match the spec"
        )
    if (
        observation.os != spec.platform.os
        or observation.architecture != spec.platform.architecture
        or (
            observation.variant != spec.platform.variant
            if spec.platform.variant is not None
            else observation.variant not in (None, "")
        )
    ):
        raise AcquisitionError(
            "LOCAL_PLATFORM_MISMATCH", "local image platform does not match the spec"
        )
    if observation.rootfs_diff_ids != spec.rootfs_diff_ids:
        raise AcquisitionError(
            "LOCAL_ROOTFS_MISMATCH", "local image rootfs does not match the spec"
        )
    if (
        required_repo_digest is not None
        and required_repo_digest not in observation.repo_digests
    ):
        raise AcquisitionError(
            "LOCAL_MANIFEST_IDENTITY_MISSING",
            "local image is not bound to the acquisition manifest",
        )
    return observation


def verify_cached_image(
    loaded: LoadedImageSpec,
    mirror_registry: str,
    docker: DockerClient,
) -> dict[str, Any]:
    """Verify cached config/platform/rootfs identity without network activity."""

    spec = loaded.spec
    source_ref = acquisition_ref(spec, mirror_registry)
    source = _require_local_match(
        spec,
        docker.inspect_optional(source_ref),
        required_repo_digest=source_ref,
    )
    canonical = _require_local_match(
        spec, docker.inspect_optional(spec.canonical_tag_ref)
    )
    if canonical.image_id != source.image_id:
        raise AcquisitionError(
            "CANONICAL_TAG_CONFLICT", "canonical tag does not match the source image"
        )
    return {
        "format": "magentabench-oci-cache-verification-v1",
        "verified": True,
        "claim_eligible": False,
        "spec": {
            "id": spec.spec_id,
            "file_sha256": loaded.file_sha256,
            "identity_sha256": spec.identity_sha256(),
        },
        "identity": {
            "canonical_digest_ref": spec.canonical_digest_ref,
            "canonical_tag_ref": spec.canonical_tag_ref,
            "config_digest": spec.config.digest,
            "manifest_digest": spec.manifest.digest,
            "platform": spec.platform.model_dump(mode="json"),
            "rootfs_diff_ids": spec.rootfs_diff_ids,
        },
        "transport": {
            "acquisition_ref": source_ref,
            "mirror_registry": validate_mirror_registry(mirror_registry),
            "mirror_is_experiment_identity": False,
        },
        "verification": {
            "canonical_tag_matches": True,
            "compressed_layer_descriptors_verified": False,
            "config_verified": True,
            "manifest_repo_digest_observed": True,
            "platform_verified": True,
            "rootfs_diff_ids_verified": True,
        },
        "limitations": (
            "Cached verification performs no registry request or image pull.",
            "Compressed layer descriptors are verified only by acquire.",
            "This transport check is not benchmark result evidence.",
        ),
    }


def _receipt_path(value: str | Path) -> Path:
    text = os.fspath(value)
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise AcquisitionError(
            "UNSAFE_RECEIPT_PATH", "receipt path is unsafe", exit_code=2
        ) from exc
    if (
        not text
        or "\\" in text
        or any(character in text for character in ("\x00", "\r", "\n"))
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(part in {"", ".", ".."} for part in PurePath(text).parts)
    ):
        raise AcquisitionError(
            "UNSAFE_RECEIPT_PATH", "receipt path is unsafe", exit_code=2
        )
    path = Path(text)
    if path.suffix != ".json":
        raise AcquisitionError(
            "UNSAFE_RECEIPT_PATH", "receipt path is unsafe", exit_code=2
        )
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:] if absolute.is_absolute() else absolute.parts:
        current = current / part
        if current.is_symlink():
            raise AcquisitionError(
                "UNSAFE_RECEIPT_PATH", "receipt path is unsafe", exit_code=2
            )
    if absolute.exists() and not absolute.is_file():
        raise AcquisitionError(
            "UNSAFE_RECEIPT_PATH", "receipt path is unsafe", exit_code=2
        )
    return absolute


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _file_read_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


@dataclass
class _ExistingReceipt:
    destination: Path
    directory_fd: int
    directory_device: int
    directory_inode: int
    descriptor: int
    file_device: int
    file_inode: int
    size_bytes: int
    modified_ns: int
    data: bytes
    payload: dict[str, Any]
    sha256: str

    def close(self) -> None:
        if self.descriptor >= 0:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = -1
        if self.directory_fd >= 0:
            try:
                os.close(self.directory_fd)
            except OSError:
                pass
            self.directory_fd = -1


def _open_existing_receipt(path: Path) -> _ExistingReceipt | None:
    directory_fd = -1
    descriptor = -1
    try:
        try:
            directory_fd = os.open(path.parent, _directory_open_flags())
        except FileNotFoundError:
            return None
        directory_stat = os.fstat(directory_fd)
        try:
            descriptor = os.open(path.name, _file_read_flags(), dir_fd=directory_fd)
        except FileNotFoundError:
            os.close(directory_fd)
            directory_fd = -1
            return None
        before = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_RECEIPT_BYTES
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ValueError
        data = os.pread(descriptor, _MAX_RECEIPT_BYTES + 1, 0)
        after = os.fstat(descriptor)
        if (
            len(data) != before.st_size
            or len(data) > _MAX_RECEIPT_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError
        payload = json.loads(data, object_pairs_hook=_reject_duplicate_pairs)
        if not isinstance(payload, dict):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise AcquisitionError(
            "RECEIPT_CONFLICT", "existing receipt does not match this acquisition"
        ) from None
    return _ExistingReceipt(
        destination=path,
        directory_fd=directory_fd,
        directory_device=directory_stat.st_dev,
        directory_inode=directory_stat.st_ino,
        descriptor=descriptor,
        file_device=before.st_dev,
        file_inode=before.st_ino,
        size_bytes=before.st_size,
        modified_ns=before.st_mtime_ns,
        data=data,
        payload=payload,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _require_existing_receipt_unchanged(
    existing: _ExistingReceipt,
) -> None:
    path_descriptor = -1
    try:
        if _receipt_path(existing.destination) != existing.destination:
            raise OSError
        path_descriptor = os.open(existing.destination.parent, _directory_open_flags())
        directory_stat = os.fstat(path_descriptor)
        named = os.stat(
            existing.destination.name,
            dir_fd=path_descriptor,
            follow_symlinks=False,
        )
        opened = os.fstat(existing.descriptor)
        data = os.pread(existing.descriptor, _MAX_RECEIPT_BYTES + 1, 0)
    except (AcquisitionError, OSError):
        raise AcquisitionError(
            "RECEIPT_PATH_CHANGED", "receipt destination changed during acquisition"
        ) from None
    finally:
        if path_descriptor >= 0:
            os.close(path_descriptor)
    if (
        (directory_stat.st_dev, directory_stat.st_ino)
        != (existing.directory_device, existing.directory_inode)
        or (named.st_dev, named.st_ino) != (existing.file_device, existing.file_inode)
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (
            existing.file_device,
            existing.file_inode,
            existing.size_bytes,
            existing.modified_ns,
        )
        or data != existing.data
        or hashlib.sha256(data).hexdigest() != existing.sha256
    ):
        raise AcquisitionError(
            "RECEIPT_PATH_CHANGED", "receipt destination changed during acquisition"
        )


@dataclass
class _PreparedReceipt:
    destination: Path
    directory_fd: int
    directory_device: int
    directory_inode: int
    descriptor: int
    temporary_name: str

    def close(self) -> None:
        if self.descriptor >= 0:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = -1
        if self.directory_fd >= 0:
            try:
                os.unlink(self.temporary_name, dir_fd=self.directory_fd)
            except OSError:
                pass
            try:
                os.close(self.directory_fd)
            except OSError:
                pass
            self.directory_fd = -1


def _require_receipt_directory_unchanged(prepared: _PreparedReceipt) -> None:
    descriptor = -1
    try:
        descriptor = os.open(prepared.destination.parent, _directory_open_flags())
        observed = os.fstat(descriptor)
    except OSError:
        raise AcquisitionError(
            "RECEIPT_PATH_CHANGED", "receipt destination changed during acquisition"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (observed.st_dev, observed.st_ino) != (
        prepared.directory_device,
        prepared.directory_inode,
    ):
        raise AcquisitionError(
            "RECEIPT_PATH_CHANGED", "receipt destination changed during acquisition"
        )


def _prepare_receipt(path: Path) -> _PreparedReceipt:
    parent = path.parent
    directory_fd = -1
    descriptor = -1
    temporary_name = ""
    try:
        parent.mkdir(parents=True, exist_ok=True)
        directory_fd = os.open(parent, _directory_open_flags())
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise OSError
        temporary_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            temporary_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            temporary_flags |= os.O_CLOEXEC
        for _ in range(128):
            temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    temporary_name,
                    temporary_flags,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                continue
        if descriptor < 0:
            raise OSError
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(descriptor_stat.st_mode) or (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            raise OSError
        prepared = _PreparedReceipt(
            path,
            directory_fd,
            directory_stat.st_dev,
            directory_stat.st_ino,
            descriptor,
            temporary_name,
        )
        _require_receipt_directory_unchanged(prepared)
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory_fd >= 0:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise AcquisitionError(
            "RECEIPT_PREPARATION_FAILED",
            "receipt destination cannot be prepared safely",
        ) from None
    except AcquisitionError:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)
        raise
    return prepared


def _atomic_create_json(prepared: _PreparedReceipt, payload: Mapping[str, Any]) -> str:
    data = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    if len(data) > _MAX_RECEIPT_BYTES:
        raise AcquisitionError("RECEIPT_TOO_LARGE", "receipt exceeds its size limit")
    linked = False
    try:
        _require_receipt_directory_unchanged(prepared)
        os.lseek(prepared.descriptor, 0, os.SEEK_SET)
        os.ftruncate(prepared.descriptor, 0)
        with os.fdopen(os.dup(prepared.descriptor), "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(
                prepared.temporary_name,
                prepared.destination.name,
                src_dir_fd=prepared.directory_fd,
                dst_dir_fd=prepared.directory_fd,
                follow_symlinks=False,
            )
            linked = True
        except FileExistsError as exc:
            raise AcquisitionError(
                "RECEIPT_CONFLICT", "receipt already exists"
            ) from exc
        os.fsync(prepared.directory_fd)
        _require_receipt_directory_unchanged(prepared)
        destination_stat = os.stat(prepared.destination, follow_symlinks=False)
        temporary_stat = os.fstat(prepared.descriptor)
        if (destination_stat.st_dev, destination_stat.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ):
            raise AcquisitionError(
                "RECEIPT_PATH_CHANGED",
                "receipt destination changed during acquisition",
            )
    except AcquisitionError:
        if linked:
            _invalidate_linked_receipt(prepared)
        raise
    except OSError:
        if linked:
            _invalidate_linked_receipt(prepared)
        raise AcquisitionError(
            "RECEIPT_WRITE_FAILED", "receipt could not be created durably"
        ) from None
    return hashlib.sha256(data).hexdigest()


def _invalidate_linked_receipt(prepared: _PreparedReceipt) -> None:
    # Never unlink by pathname here: POSIX has no conditional unlink-by-inode,
    # so a non-cooperating writer could otherwise have its replacement removed.
    try:
        os.pwrite(prepared.descriptor, b"!", 0)
    except OSError:
        pass
    try:
        os.ftruncate(prepared.descriptor, 0)
    except OSError:
        pass
    try:
        os.fsync(prepared.descriptor)
    except OSError:
        pass


def _identity_block(spec: OciImageSpec) -> dict[str, Any]:
    return {
        "canonical_digest_ref": spec.canonical_digest_ref,
        "canonical_tag_ref": spec.canonical_tag_ref,
        "config": spec.config.model_dump(mode="json"),
        "layers": [layer.model_dump(mode="json") for layer in spec.layers],
        "local_image_id": spec.config.digest,
        "manifest": spec.manifest.model_dump(mode="json"),
        "platform": spec.platform.model_dump(mode="json"),
        "rootfs_diff_ids": list(spec.rootfs_diff_ids),
    }


_RECEIPT_LIMITATIONS = [
    "The mirror is untrusted transport and does not establish provenance.",
    "Docker pull verifies compressed layer content against the manifest.",
    "This receipt is acquisition evidence, not benchmark result evidence.",
]


def _valid_receipt_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(
        parsed
    )


def _valid_receipt_runtime(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "docker",
        "docker_executable",
    }:
        return False
    docker = value["docker"]
    if not isinstance(docker, dict) or set(docker) != {
        "client_api_version",
        "client_version",
        "server_api_version",
        "server_architecture",
        "server_os",
        "server_version",
    }:
        return False
    if (
        any(
            not isinstance(docker[key], str)
            or (match := _DOCKER_VERSION_PATTERN.fullmatch(docker[key])) is None
            or match.group("core") != docker[key]
            for key in ("client_version", "server_version")
        )
        or any(
            not isinstance(docker[key], str)
            or _DOCKER_API_PATTERN.fullmatch(docker[key]) is None
            for key in ("client_api_version", "server_api_version")
        )
        or docker["server_os"] not in _DOCKER_OS_VALUES
        or docker["server_architecture"] not in _DOCKER_ARCH_VALUES
    ):
        return False
    executable = value["docker_executable"]
    return executable is None or (
        isinstance(executable, dict)
        and set(executable) == {"sha256", "size_bytes"}
        and isinstance(executable["sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", executable["sha256"]) is not None
        and isinstance(executable["size_bytes"], int)
        and not isinstance(executable["size_bytes"], bool)
        and executable["size_bytes"] > 0
    )


def _expected_manifest_verification(spec: OciImageSpec) -> dict[str, Any]:
    return {
        "manifest": spec.manifest.model_dump(mode="json"),
        "config": spec.config.model_dump(mode="json"),
        "layers": [layer.model_dump(mode="json") for layer in spec.layers],
        "platform": spec.platform.model_dump(mode="json"),
        "raw_sha256": spec.manifest.digest.removeprefix("sha256:"),
    }


def _existing_receipt_matches(
    receipt: Mapping[str, Any],
    *,
    loaded: LoadedImageSpec,
    identity: Mapping[str, Any],
    identity_sha256: str,
    source_ref: str,
    mirror_registry: str,
) -> bool:
    spec = loaded.spec
    transport = receipt.get("transport")
    verification = receipt.get("verification")
    return (
        set(receipt)
        == {
            "claim_eligible",
            "format",
            "identity",
            "identity_sha256",
            "limitations",
            "observed_at",
            "runtime",
            "spec",
            "status",
            "transport",
            "verification",
        }
        and receipt.get("format") == RECEIPT_FORMAT
        and receipt.get("status") == "verified"
        and receipt.get("claim_eligible") is False
        and _valid_receipt_timestamp(receipt.get("observed_at"))
        and receipt.get("identity") == identity
        and receipt.get("identity_sha256") == identity_sha256
        and receipt.get("spec")
        == {
            "file_sha256": loaded.file_sha256,
            "id": spec.spec_id,
            "identity_sha256": spec.identity_sha256(),
            "size_bytes": loaded.size_bytes,
        }
        and isinstance(transport, dict)
        and set(transport)
        == {
            "acquisition_ref",
            "manifest_verified_from_raw",
            "mirror_is_experiment_identity",
            "mirror_registry",
            "pull_action",
            "tag_action",
        }
        and transport.get("acquisition_ref") == source_ref
        and transport.get("mirror_registry") == mirror_registry
        and transport.get("mirror_is_experiment_identity") is False
        and transport.get("manifest_verified_from_raw") is True
        and transport.get("pull_action") in {"cached", "pulled"}
        and transport.get("tag_action") in {"already-matched", "tagged"}
        and verification
        == {
            "canonical_tag_matches": True,
            "config_verified": True,
            "manifest": _expected_manifest_verification(spec),
            "platform_verified": True,
            "rootfs_diff_ids_verified": True,
        }
        and _valid_receipt_runtime(receipt.get("runtime"))
        and receipt.get("limitations") == _RECEIPT_LIMITATIONS
    )


@contextmanager
def _stable_host_lock(
    *, namespace: str, identity: str, busy_code: str, unavailable_code: str
):
    lock_digest = hashlib.sha256(
        f"{namespace}\x00{identity}".encode("utf-8")
    ).hexdigest()
    address = f"\x00magentabench-lock-v2-{lock_digest}"
    lock_socket: socket.socket | None = None
    try:
        socket_type = socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0)
        lock_socket = socket.socket(socket.AF_UNIX, socket_type)
        lock_socket.bind(address)
    except OSError as exc:
        if lock_socket is not None:
            lock_socket.close()
        if exc.errno == errno.EADDRINUSE:
            raise AcquisitionError(
                busy_code, "another acquisition owns this operation scope"
            ) from exc
        raise AcquisitionError(
            unavailable_code,
            "operation locking is unavailable",
        ) from exc
    try:
        yield
    finally:
        assert lock_socket is not None
        lock_socket.close()


@contextmanager
def _canonical_tag_lock(spec: OciImageSpec):
    with _stable_host_lock(
        namespace="oci-tag",
        identity=spec.canonical_tag_ref,
        busy_code="ACQUISITION_BUSY",
        unavailable_code="ACQUISITION_LOCK_UNAVAILABLE",
    ):
        yield


@contextmanager
def _receipt_path_lock(path: Path):
    with _stable_host_lock(
        namespace="receipt",
        identity=str(path),
        busy_code="RECEIPT_BUSY",
        unavailable_code="RECEIPT_LOCK_UNAVAILABLE",
    ):
        yield


def acquire_image(
    loaded: LoadedImageSpec,
    mirror_registry: str,
    receipt_path: str | Path,
    docker: DockerClient,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Serialize canonical tag ownership and run one strict acquisition."""

    with _canonical_tag_lock(loaded.spec):
        return _acquire_image_locked(
            loaded,
            mirror_registry,
            receipt_path,
            docker,
            now=now,
        )


def _acquire_image_locked(
    loaded: LoadedImageSpec,
    mirror_registry: str,
    receipt_path: str | Path,
    docker: DockerClient,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Acquire one immutable image and emit a receipt only after all checks."""

    spec = loaded.spec
    registry = validate_mirror_registry(mirror_registry)
    source_ref = acquisition_ref(spec, registry)
    destination = _receipt_path(receipt_path)
    with _receipt_path_lock(destination):
        return _acquire_with_receipt_lock(
            loaded,
            registry,
            source_ref,
            destination,
            docker,
            now=now,
        )


def _acquire_with_receipt_lock(
    loaded: LoadedImageSpec,
    registry: str,
    source_ref: str,
    destination: Path,
    docker: DockerClient,
    *,
    now: datetime | None,
) -> dict[str, Any]:
    spec = loaded.spec
    identity = _identity_block(spec)
    identity_sha256 = _json_sha256(identity)
    existing = _open_existing_receipt(destination)
    if existing is not None:
        try:
            if not _existing_receipt_matches(
                existing.payload,
                loaded=loaded,
                identity=identity,
                identity_sha256=identity_sha256,
                source_ref=source_ref,
                mirror_registry=registry,
            ):
                raise AcquisitionError(
                    "RECEIPT_CONFLICT",
                    "existing receipt does not match this acquisition",
                )
            executable_identity = _observe_invoked_executable(docker)
            verify_cached_image(loaded, registry, docker)
            _require_executable_unchanged(docker, executable_identity)
            _require_existing_receipt_unchanged(existing)
            return {
                "format": "magentabench-oci-acquisition-result-v1",
                "status": "verified",
                "receipt_reused": True,
                "receipt_sha256": existing.sha256,
                "identity_sha256": identity_sha256,
            }
        finally:
            existing.close()

    prepared = _prepare_receipt(destination)
    try:
        return _acquire_new_image(
            loaded,
            registry,
            source_ref,
            identity,
            identity_sha256,
            prepared,
            docker,
            now=now,
        )
    finally:
        prepared.close()


def _acquire_new_image(
    loaded: LoadedImageSpec,
    registry: str,
    source_ref: str,
    identity: dict[str, Any],
    identity_sha256: str,
    prepared: _PreparedReceipt,
    docker: DockerClient,
    *,
    now: datetime | None,
) -> dict[str, Any]:
    spec = loaded.spec
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() != timezone.utc.utcoffset(
        observed_at
    ):
        raise AcquisitionError("INVALID_TIMESTAMP", "receipt timestamp must be UTC")
    executable_identity = _observe_invoked_executable(docker)

    docker_version = docker.version()
    _require_receipt_directory_unchanged(prepared)
    canonical_before = docker.inspect_optional(spec.canonical_tag_ref)
    if canonical_before is not None and not _local_matches(spec, canonical_before):
        raise AcquisitionError(
            "CANONICAL_TAG_CONFLICT", "canonical tag points to another image"
        )

    manifest = docker.verify_remote_manifest(spec, source_ref)
    source = docker.inspect_optional(source_ref)
    pull_action = "cached"
    if source is None:
        _require_receipt_directory_unchanged(prepared)
        _require_executable_unchanged(docker, executable_identity)
        docker.pull(spec, source_ref)
        pull_action = "pulled"
        source = docker.inspect_optional(source_ref)
    elif not _local_matches(spec, source, required_repo_digest=source_ref):
        raise AcquisitionError(
            "CACHED_IMAGE_CONFLICT", "cached mirror image does not match the spec"
        )
    source = _require_local_match(spec, source, required_repo_digest=source_ref)

    canonical_now = docker.inspect_optional(spec.canonical_tag_ref)
    if canonical_now is not None and not _local_matches(spec, canonical_now):
        raise AcquisitionError(
            "CANONICAL_TAG_CONFLICT", "canonical tag points to another image"
        )
    tag_action = "already-matched"
    if canonical_now is None:
        _require_receipt_directory_unchanged(prepared)
        _require_executable_unchanged(docker, executable_identity)
        docker.tag(source_ref, spec.canonical_tag_ref)
        tag_action = "tagged"
        canonical_now = docker.inspect_optional(spec.canonical_tag_ref)
    canonical_now = _require_local_match(spec, canonical_now)
    if canonical_now.image_id != source.image_id:
        raise AcquisitionError(
            "CANONICAL_TAG_CONFLICT", "canonical tag does not match the source image"
        )
    _require_receipt_directory_unchanged(prepared)
    _require_executable_unchanged(docker, executable_identity)

    receipt = {
        "format": RECEIPT_FORMAT,
        "status": "verified",
        "claim_eligible": False,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "spec": {
            "file_sha256": loaded.file_sha256,
            "id": spec.spec_id,
            "identity_sha256": spec.identity_sha256(),
            "size_bytes": loaded.size_bytes,
        },
        "identity": identity,
        "identity_sha256": identity_sha256,
        "transport": {
            "acquisition_ref": source_ref,
            "manifest_verified_from_raw": True,
            "mirror_is_experiment_identity": False,
            "mirror_registry": registry,
            "pull_action": pull_action,
            "tag_action": tag_action,
        },
        "verification": {
            "canonical_tag_matches": True,
            "config_verified": True,
            "manifest": manifest,
            "platform_verified": True,
            "rootfs_diff_ids_verified": True,
        },
        "runtime": {
            "docker": docker_version,
            "docker_executable": executable_identity,
        },
        "limitations": _RECEIPT_LIMITATIONS,
    }
    receipt_sha256 = _atomic_create_json(prepared, receipt)
    return {
        "format": "magentabench-oci-acquisition-result-v1",
        "status": "verified",
        "receipt_reused": False,
        "receipt_sha256": receipt_sha256,
        "identity_sha256": identity_sha256,
    }


def resolve_public_executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise AcquisitionError(f"{name.upper()}_UNAVAILABLE", f"{name} is unavailable")
    try:
        path = Path(value).resolve(strict=True)
    except OSError as exc:
        raise AcquisitionError(
            f"{name.upper()}_UNAVAILABLE", f"{name} is unavailable"
        ) from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AcquisitionError(f"{name.upper()}_UNAVAILABLE", f"{name} is unavailable")
    return path


__all__ = [
    "CANONICAL_GIT_URL",
    "DEFAULT_OCI_MIRROR",
    "DOCTOR_FORMAT",
    "GIT_MIRROR_REFSPEC",
    "GIT_MIRROR_URL",
    "PYTHON_INDEX_URL",
    "AcquisitionError",
    "CommandResult",
    "DockerClient",
    "PinnedExecutable",
    "SubprocessRunner",
    "acquire_image",
    "acquisition_plan",
    "acquisition_ref",
    "configure_git_mirror",
    "mirror_doctor",
    "resolve_public_executable",
    "validate_mirror_registry",
    "verify_cached_image",
]
