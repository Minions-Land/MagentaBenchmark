"""Content-addressed uv environment construction and provenance receipts."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

from MagentaBench.schemas import (
    EnvironmentReceipt,
    EnvironmentSpec,
    PackageRecord,
)

from ..evidence import atomic_write_json

try:  # pragma: no cover - Windows uses the fallback process-local behavior
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class EnvManagerError(RuntimeError):
    """Base error for reproducible environment management."""


class EnvironmentBuildError(EnvManagerError):
    """A pinned environment could not be built within its declared contract."""

    def __init__(
        self,
        message: str,
        *,
        timeout_seconds: float | None = None,
        partial_output: str = "",
        command: Sequence[str] = (),
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.partial_output = partial_output
        self.command = tuple(command)
        super().__init__(message)


class EnvironmentDriftError(EnvManagerError):
    """A cached environment no longer matches its content-addressed receipt."""


def mount_content_digest(path: str | Path) -> str:
    """Hash one mounted file or a complete, symlink-free directory tree."""

    declared = Path(path).expanduser()
    if declared.is_symlink():
        raise EnvironmentDriftError(
            f"mount content cannot be a symlink: {declared}"
        )
    try:
        root = declared.resolve(strict=True)
    except OSError as exc:
        raise EnvironmentDriftError(
            f"mount content is missing or unreadable: {declared}: {exc}"
        ) from exc
    if root.is_file():
        digest = hashlib.sha256()
        with root.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    if not root.is_dir():
        raise EnvironmentDriftError(
            f"mount content must be a regular file or directory: {root}"
        )

    entries: list[dict[str, object]] = []
    for candidate in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise EnvironmentDriftError(
                f"mount directory contains a symlink: {candidate}"
            )
        if candidate.is_dir():
            entries.append({"path": relative, "type": "directory"})
            continue
        if not candidate.is_file():
            raise EnvironmentDriftError(
                f"mount directory contains a non-regular entry: {candidate}"
            )
        file_digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(block)
        entries.append(
            {
                "path": relative,
                "type": "file",
                "size_bytes": candidate.stat().st_size,
                "sha256": file_digest.hexdigest(),
            }
        )
    payload = json.dumps(
        entries,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class EnvManager:
    """Build and cache uv virtual environments by canonical spec digest."""

    def __init__(
        self,
        cache_root: str | Path | None = None,
        *,
        link_mode: str = "copy",
        uv_executable: str | Path | None = None,
        output_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.cache_root = Path(
            cache_root if cache_root is not None else "~/.cache/bmp/envs"
        ).expanduser().resolve()
        if link_mode not in {"copy", "clone", "hardlink", "symlink"}:
            raise EnvManagerError(
                "link_mode must be one of copy, clone, hardlink, symlink"
            )
        self.link_mode = link_mode
        selected_uv = str(uv_executable) if uv_executable is not None else shutil.which("uv")
        if not selected_uv:
            raise EnvironmentBuildError("uv executable was not found")
        resolved_uv = shutil.which(selected_uv) or selected_uv
        self.uv_executable = str(Path(resolved_uv).resolve())
        self.output_sink = output_sink

    @staticmethod
    def spec_digest(spec: EnvironmentSpec) -> str:
        return spec.canonical_digest()

    @staticmethod
    def _validate_mounts(spec: EnvironmentSpec) -> None:
        for mount in spec.mounts:
            if mount.content_sha256 is None:
                raise EnvironmentDriftError(
                    f"mount {mount.name!r} lacks required content_sha256"
                )
            observed = mount_content_digest(mount.host_path)
            if observed != mount.content_sha256:
                raise EnvironmentDriftError(
                    f"mount {mount.name!r} content digest drift: "
                    f"expected {mount.content_sha256}, got {observed}"
                )

    def environment_directory(self, spec: EnvironmentSpec) -> Path:
        return self.cache_root / self.spec_digest(spec)

    @staticmethod
    def _python_path(environment_dir: Path) -> Path:
        if os.name == "nt":  # pragma: no cover
            return environment_dir / "Scripts" / "python.exe"
        return environment_dir / "bin" / "python"

    @staticmethod
    def _validate_version_pin(version: str) -> tuple[int, ...]:
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            raise EnvironmentBuildError(
                "python_version must be an explicit numeric pin such as '3.11' "
                "or '3.11.13'; implicit/default interpreter selection is forbidden"
            )
        return tuple(int(part) for part in version.split("."))

    def _build_environment(self) -> dict[str, str]:
        retained = (
            "PATH",
            "HOME",
            "TMPDIR",
            "UV_CACHE_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
        )
        environment = {name: os.environ[name] for name in retained if name in os.environ}
        environment["UV_LINK_MODE"] = self.link_mode
        return environment

    def _emit(self, line: str) -> None:
        if self.output_sink is not None:
            self.output_sink(line)

    def _run_streaming(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        cwd: Path,
    ) -> str:
        """Run a build step while forwarding output and retaining failure context."""

        try:
            process = subprocess.Popen(
                tuple(command),
                cwd=cwd,
                env=self._build_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise EnvironmentBuildError(
                f"cannot launch environment build command {' '.join(command)}: {exc}",
                timeout_seconds=timeout,
                command=command,
            ) from exc
        lines: list[str] = []

        def consume() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                self._emit(line.rstrip("\n"))

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=5)
            output = "".join(lines)
            raise EnvironmentBuildError(
                f"environment build timed out after {timeout:.1f}s: "
                f"{' '.join(command)}\n{output}",
                timeout_seconds=timeout,
                partial_output=output,
                command=command,
            ) from exc
        reader.join(timeout=5)
        output = "".join(lines)
        if returncode != 0:
            raise EnvironmentBuildError(
                f"environment build command exited {returncode}: "
                f"{' '.join(command)}\n{output}",
                partial_output=output,
                command=command,
            )
        return output

    @contextmanager
    def _digest_lock(self, digest: str) -> Iterator[None]:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache_root / f"{digest}.lock"
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _inspect_python(python: Path) -> tuple[str, str]:
        command = (
            str(python),
            "-c",
            "import json,sys; print(json.dumps({"
            "'executable':sys.executable,'version':'.'.join(map(str,sys.version_info[:3]))}))",
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
                check=True,
            )
            payload = json.loads(completed.stdout)
            return str(payload["executable"]), str(payload["version"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            raise EnvironmentDriftError(
                f"cannot inspect environment interpreter {python}: {exc}"
            ) from exc

    @staticmethod
    def _installed_packages(python: Path) -> tuple[PackageRecord, ...]:
        script = r'''
import hashlib, importlib.metadata as metadata, json
records = []
for dist in sorted(metadata.distributions(), key=lambda item: (item.metadata.get("Name") or "").lower()):
    name = dist.metadata.get("Name") or "unknown"
    digest = hashlib.sha256()
    file_count = 0
    for relative in sorted(dist.files or (), key=str):
        path = dist.locate_file(relative)
        if not path.is_file():
            continue
        file_count += 1
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    records.append({"name": name, "version": dist.version, "sha256": digest.hexdigest() if file_count else None})
print(json.dumps(records, sort_keys=True))
'''
        try:
            completed = subprocess.run(
                (str(python), "-c", script),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=300,
                check=True,
            )
            payload = json.loads(completed.stdout)
            return tuple(PackageRecord.model_validate(item) for item in payload)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise EnvironmentBuildError(
                f"cannot inventory installed packages in {python}: {exc}"
            ) from exc

    def _validate_cached(
        self, spec: EnvironmentSpec, directory: Path
    ) -> EnvironmentReceipt:
        receipt_path = directory / "environment_receipt.json"
        try:
            receipt = EnvironmentReceipt.model_validate_json(receipt_path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise EnvironmentDriftError(
                f"cached environment receipt is missing or invalid: {receipt_path}: {exc}"
            ) from exc
        digest = self.spec_digest(spec)
        if receipt.spec_id != spec.id or receipt.spec_digest != digest:
            raise EnvironmentDriftError("cached environment receipt spec identity drift")
        expected_python = self._python_path(directory).absolute()
        if Path(receipt.python_executable).absolute() != expected_python:
            raise EnvironmentDriftError("cached environment interpreter path drift")
        executable, version = self._inspect_python(expected_python)
        if Path(executable).resolve() != expected_python.resolve() or version != receipt.python_version:
            raise EnvironmentDriftError("cached environment interpreter version drift")
        requested = self._validate_version_pin(spec.python_version)
        actual = tuple(int(part) for part in version.split("."))
        if actual[: len(requested)] != requested:
            raise EnvironmentDriftError(
                f"cached interpreter {version} violates pin {spec.python_version}"
            )
        return receipt

    def ensure(
        self,
        spec: EnvironmentSpec,
        *,
        expected_digest: str | None = None,
    ) -> EnvironmentReceipt:
        """Return a validated cached receipt, building the environment once.

        ``expected_digest`` is supplied by a resume record. A changed spec is
        a drift error, never an implicit new environment lineage.
        """

        requested = self._validate_version_pin(spec.python_version)
        self._validate_mounts(spec)
        digest = self.spec_digest(spec)
        if expected_digest is not None and digest != expected_digest:
            raise EnvironmentDriftError(
                f"environment spec digest drift: expected {expected_digest}, got {digest}"
            )
        final_directory = self.cache_root / digest
        with self._digest_lock(digest):
            if final_directory.exists():
                return self._validate_cached(spec, final_directory)

            staging = self.cache_root / f".{digest}.tmp-{uuid.uuid4().hex}"
            started = time.monotonic()
            deadline = started + spec.build_timeout_seconds
            try:
                remaining = max(0.001, deadline - time.monotonic())
                self._run_streaming(
                    (
                        self.uv_executable,
                        "venv",
                        "--python",
                        spec.python_version,
                        str(staging),
                    ),
                    timeout=remaining,
                    cwd=self.cache_root,
                )
                python = self._python_path(staging)
                if spec.packages:
                    remaining = max(0.001, deadline - time.monotonic())
                    self._run_streaming(
                        (
                            self.uv_executable,
                            "pip",
                            "install",
                            "--python",
                            str(python),
                            *spec.packages,
                        ),
                        timeout=remaining,
                        cwd=self.cache_root,
                    )
                executable, version = self._inspect_python(python)
                actual = tuple(int(part) for part in version.split("."))
                if actual[: len(requested)] != requested:
                    raise EnvironmentBuildError(
                        f"uv selected Python {version}, violating explicit pin "
                        f"{spec.python_version}"
                    )
                packages = self._installed_packages(python)
                final_python = self._python_path(final_directory).resolve()
                receipt = EnvironmentReceipt(
                    spec_id=spec.id,
                    spec_digest=digest,
                    python_executable=str(final_python),
                    python_version=version,
                    installed_packages=packages,
                    build_duration_seconds=time.monotonic() - started,
                    built_at=datetime.now(timezone.utc).isoformat(),
                )
                atomic_write_json(staging / "environment_receipt.json", receipt)
                os.replace(staging, final_directory)
                return receipt
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise


# Backward-compatible descriptive alias; ``EnvManager`` is the public contract.
EnvironmentManager = EnvManager

__all__ = [
    "EnvManager",
    "EnvManagerError",
    "EnvironmentBuildError",
    "EnvironmentDriftError",
    "EnvironmentManager",
    "mount_content_digest",
]
