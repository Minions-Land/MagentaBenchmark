#!/usr/bin/env python3
"""Read-only host readiness probes for non-local execution profiles.

The probes deliberately do not pull, build, inspect, or execute an image. They
establish host prerequisites only. Runtime identity and behavior still belong
in a backend receipt produced for a concrete execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence


_SAFE_SUBPROCESS_ENV = {
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}
_APPTAINER_VERSION_PREFIX = "apptainer version "


def _safe_text(value: str, *, label: str) -> str:
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} must be non-empty single-line text")
    return value


def _run(argv: Sequence[str], *, timeout: float = 15.0) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=_SAFE_SUBPROCESS_ENV,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_executable(value: str | None, fallback: str) -> Path | None:
    candidate = value or shutil.which(fallback)
    if candidate is None:
        return None
    candidate = _safe_text(candidate, label=f"{fallback} launcher")
    if os.sep not in candidate:
        resolved = shutil.which(candidate)
        if resolved is None:
            return None
        candidate = resolved
    try:
        path = Path(candidate).expanduser().resolve(strict=True)
    except OSError:
        return None
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return path


def _single_line(output: str, *, limit: int = 500) -> str:
    flattened = " ".join(output.split())
    return flattened[:limit]


def _path_observation(value: str | None, *, label: str) -> dict[str, Any]:
    if value is None:
        return {
            "label": label,
            "configured": False,
            "exists": False,
            "writable": False,
            "path": None,
            "filesystem": None,
        }
    safe = _safe_text(value, label=label)
    path = Path(safe).expanduser().resolve(strict=False)
    exists = path.is_dir()
    filesystem = None
    if exists and shutil.which("findmnt"):
        rc, output = _run(("findmnt", "-T", str(path), "-n", "-o", "FSTYPE"))
        filesystem = _single_line(output) if rc == 0 else None
    return {
        "label": label,
        "configured": True,
        "exists": exists,
        "writable": exists and os.access(path, os.W_OK | os.X_OK),
        "path": str(path),
        "filesystem": filesystem,
    }


def _subordinate_id_entry(path: Path, username: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split(":")
        if len(fields) != 3 or fields[0] != username:
            continue
        try:
            start, count = (int(value) for value in fields[1:])
        except ValueError:
            continue
        if start > 0 and count > 0:
            return True
    return False


def _executable_path(*candidates: str | None) -> str | None:
    return next(
        (
            str(Path(candidate).resolve())
            for candidate in candidates
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )


def _fuse_device_available(path: Path) -> bool:
    try:
        return path.exists() and stat.S_ISCHR(path.stat().st_mode)
    except OSError:
        return False


def _gpu_observation() -> dict[str, Any]:
    launcher = shutil.which("nvidia-smi")
    if launcher is None:
        return {"visible": False, "count": 0, "devices": []}
    rc, output = _run(
        (
            launcher,
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        )
    )
    if rc != 0:
        return {
            "visible": False,
            "count": 0,
            "devices": [],
            "error": _single_line(output),
        }
    devices: list[dict[str, Any]] = []
    for line in output.splitlines():
        name, separator, memory = line.rpartition(",")
        if not separator:
            continue
        try:
            memory_mib = int(memory.strip())
        except ValueError:
            continue
        devices.append({"name": name.strip(), "memory_mib": memory_mib})
    return {"visible": bool(devices), "count": len(devices), "devices": devices}


def probe_apptainer(
    *,
    launcher_value: str | None,
    cache_dir: str | None,
    tmp_dir: str | None,
    artifact_root: str | None,
    image: str | None,
    require_fakeroot: bool,
    require_cgroup_v2: bool,
    require_gpu: bool,
    fuse_device_path: Path = Path("/dev/fuse"),
    cgroup_controllers_path: Path = Path("/sys/fs/cgroup/cgroup.controllers"),
    fusermount_value: str | None = None,
    squashfuse_value: str | None = None,
) -> dict[str, Any]:
    launcher = _resolve_executable(launcher_value, "apptainer")
    installed = False
    version = None
    buildcfg: dict[str, str] = {}
    launcher_identity = None
    if launcher is not None:
        rc, output = _run((str(launcher), "--version"))
        version = _single_line(output) if rc == 0 else None
        rc, output = _run((str(launcher), "buildcfg"))
        if rc == 0:
            for line in output.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {
                    "PACKAGE_NAME",
                    "PACKAGE_VERSION",
                    "BINDIR",
                    "LIBEXECDIR",
                    "SYSCONFDIR",
                    "APPTAINER_CONFDIR",
                    "APPTAINER_SUID_INSTALL",
                }:
                    buildcfg[key] = _single_line(value)
        version_number = (
            version.removeprefix(_APPTAINER_VERSION_PREFIX)
            if version and version.startswith(_APPTAINER_VERSION_PREFIX)
            else None
        )
        installed = (
            version_number is not None
            and buildcfg.get("PACKAGE_NAME") == "apptainer"
            and buildcfg.get("PACKAGE_VERSION") == version_number
        )
        metadata = launcher.stat()
        launcher_identity = {
            "path": str(launcher),
            "sha256": _sha256(launcher),
            "size_bytes": metadata.st_size,
            "mode": stat.S_IMODE(metadata.st_mode),
        }

    uid = os.geteuid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    rootless_principal = uid != 0
    unshare = shutil.which("unshare")
    userns_ok = False
    userns_detail = "unshare unavailable"
    if unshare is not None:
        rc, output = _run((unshare, "--user", "--map-root-user", "true"))
        userns_ok = rc == 0
        userns_detail = "usable" if userns_ok else _single_line(output)

    newuidmap = shutil.which("newuidmap")
    newgidmap = shutil.which("newgidmap")
    uidmap_helpers = newuidmap is not None and newgidmap is not None
    subordinate_ids = _subordinate_id_entry(Path("/etc/subuid"), username) and (
        _subordinate_id_entry(Path("/etc/subgid"), username)
    )

    fuse_device = _fuse_device_available(fuse_device_path)
    prefix_bin = None if launcher is None else launcher.parent
    fusermount = _executable_path(
        fusermount_value,
        shutil.which("fusermount3"),
        None if prefix_bin is None else str(prefix_bin / "fusermount3"),
    )
    squashfuse = _executable_path(
        squashfuse_value,
        shutil.which("squashfuse"),
        None if prefix_bin is None else str(prefix_bin / "squashfuse"),
    )
    cgroup_v2 = cgroup_controllers_path.is_file()

    storage = {
        "cache": _path_observation(cache_dir, label="APPTAINER_CACHEDIR"),
        "temporary": _path_observation(tmp_dir, label="APPTAINER_TMPDIR"),
        "artifacts": _path_observation(
            artifact_root, label="MAGENTABENCH_ARTIFACT_ROOT"
        ),
    }
    storage_ready = all(
        item["configured"] and item["exists"] and item["writable"]
        for item in storage.values()
    )
    image_observation: dict[str, Any]
    if image is None:
        image_observation = {
            "configured": False,
            "exists": False,
            "path": None,
            "kind": None,
            "identity_verified": False,
        }
    else:
        image_path = Path(_safe_text(image, label="Apptainer image")).expanduser()
        resolved_image = image_path.resolve(strict=False)
        image_observation = {
            "configured": True,
            "exists": resolved_image.exists(),
            "path": str(resolved_image),
            "kind": (
                "sandbox"
                if resolved_image.is_dir()
                else "sif-or-file" if resolved_image.is_file() else None
            ),
            # Hashing a multi-hundred-GB sandbox or SIF is intentionally not a
            # readiness side effect. The concrete backend must bind its digest.
            "identity_verified": False,
        }

    gpu = _gpu_observation()
    required_checks = {
        "installed": installed,
        "rootless_principal": rootless_principal,
        "user_namespace": userns_ok,
        "fuse_device": fuse_device,
        "squashfuse": squashfuse is not None,
        "persistent_storage": storage_ready,
    }
    if require_fakeroot:
        required_checks["uidmap_helpers"] = uidmap_helpers
        required_checks["subordinate_ids"] = subordinate_ids
    if require_cgroup_v2:
        required_checks["cgroup_v2"] = cgroup_v2
    if require_gpu:
        required_checks["gpu_visible"] = bool(gpu["visible"])
    if image_observation["configured"]:
        required_checks["image_available"] = bool(image_observation["exists"])
    host_ready = all(required_checks.values())
    return {
        "format": "magentabench-apptainer-readiness-v1",
        "mode": "apptainer",
        "evidence_ceiling": "exploratory",
        "host_ready": host_ready,
        "required_checks": required_checks,
        "launcher": {
            "installed": installed,
            "identity": launcher_identity,
            "version": version,
            "buildcfg": buildcfg,
        },
        "rootless": {
            "uid": uid,
            "username": username,
            "principal_is_non_root": rootless_principal,
            "user_namespace": userns_ok,
            "user_namespace_detail": userns_detail,
            "newuidmap": newuidmap,
            "newgidmap": newgidmap,
            "subordinate_ids": subordinate_ids,
            "fuse_device": fuse_device,
            "fusermount": fusermount,
            "squashfuse": squashfuse,
            "cgroup_v2": cgroup_v2,
        },
        "requirements": {
            "fakeroot": require_fakeroot,
            "cgroup_v2": require_cgroup_v2,
            "gpu": require_gpu,
        },
        "storage": storage,
        "image": image_observation,
        "gpu": gpu,
        "limitations": [
            "This probe does not pull, build, inspect, or execute an image.",
            "GPU visibility does not prove Apptainer --nv passthrough.",
            "Fakeroot and cgroup v2 become gates only when explicitly required.",
            "Image identity and runtime behavior require a concrete backend receipt.",
            (
                "Host readiness never upgrades a result beyond the profile evidence "
                "ceiling."
            ),
        ],
    }


def _render_text(report: Mapping[str, Any]) -> str:
    checks = report["required_checks"]
    lines = [
        f"Apptainer host readiness: {'READY' if report['host_ready'] else 'NOT READY'}",
        f"Evidence ceiling: {report['evidence_ceiling']}",
    ]
    lines.extend(
        f"- {'ok' if value else 'missing'}: {name}"
        for name, value in checks.items()
    )
    launcher = report["launcher"]
    if launcher["identity"]:
        lines.append(
            f"Launcher: {launcher['identity']['path']} ({launcher['version']}, "
            f"sha256={launcher['identity']['sha256']})"
        )
    for limitation in report["limitations"]:
        lines.append(f"NOTE: {limitation}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("apptainer",))
    parser.add_argument("--launcher", default=os.environ.get("APPTAINER_BIN"))
    parser.add_argument("--cache-dir", default=os.environ.get("APPTAINER_CACHEDIR"))
    parser.add_argument("--tmp-dir", default=os.environ.get("APPTAINER_TMPDIR"))
    parser.add_argument(
        "--artifact-root", default=os.environ.get("MAGENTABENCH_ARTIFACT_ROOT")
    )
    parser.add_argument(
        "--image", default=os.environ.get("MAGENTABENCH_APPTAINER_IMAGE")
    )
    parser.add_argument("--require-fakeroot", action="store_true")
    parser.add_argument("--require-cgroup-v2", action="store_true")
    parser.add_argument("--require-gpu", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        report = probe_apptainer(
            launcher_value=args.launcher,
            cache_dir=args.cache_dir,
            tmp_dir=args.tmp_dir,
            artifact_root=args.artifact_root,
            image=args.image,
            require_fakeroot=args.require_fakeroot,
            require_cgroup_v2=args.require_cgroup_v2,
            require_gpu=args.require_gpu,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.format == "json":
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_text(report))
    return 0 if report["host_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
