"""Command-line entry point for mirror diagnostics and OCI acquisition."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from typing import Any, Never

from .mirror import (
    DEFAULT_OCI_MIRROR,
    AcquisitionError,
    DockerClient,
    PinnedExecutable,
    SubprocessRunner,
    acquire_image,
    acquisition_plan,
    configure_git_mirror,
    mirror_doctor,
    resolve_public_executable,
    verify_cached_image,
)
from .models import ImageSpecError, load_image_spec


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise AcquisitionError(
            "INVALID_ARGUMENTS", "command arguments are invalid", exit_code=2
        )


def _json(value: Any) -> str:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="bmp-mirror",
        description="Secret-safe mirror diagnostics and digest-bound OCI acquisition",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="inspect mirror readiness without network acquisition"
    )
    doctor.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="repository containing the local Git mirror configuration",
    )

    configure = subparsers.add_parser(
        "git-configure", help="idempotently configure the fetch-only Git mirror"
    )
    configure.add_argument(
        "--repository", type=Path, default=Path.cwd(), help="Git repository root"
    )

    for name, help_text in (
        ("plan", "render canonical and mirror references without side effects"),
        ("verify", "verify a cached image without registry or pull activity"),
        ("acquire", "verify, pull if needed, retag, and write a receipt"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("spec", type=Path)
        command.add_argument(
            "--mirror-registry", default=DEFAULT_OCI_MIRROR, metavar="HOST[:PORT]"
        )
        if name == "acquire":
            command.add_argument(
                "--receipt",
                type=Path,
                required=True,
                help="new or matching durable JSON receipt path",
            )
    return parser


@contextmanager
def _docker_context() -> Iterator[DockerClient]:
    temporary = tempfile.TemporaryDirectory(prefix="magentabench-docker-config-")
    pinned: PinnedExecutable | None = None
    try:
        executable = resolve_public_executable("docker")
        pinned = PinnedExecutable.open(executable)
        runner = SubprocessRunner(
            docker_config=Path(temporary.name), pass_fds=(pinned.descriptor,)
        )
        yield DockerClient(
            runner,
            pinned.invocation_path,
            pinned_executable=pinned,
        )
    finally:
        if pinned is not None:
            pinned.close()
        temporary.cleanup()


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "format": "magentabench-mirror-error-v1",
        "ok": False,
        "error": {"code": code, "message": message},
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "doctor":
            with tempfile.TemporaryDirectory(
                prefix="magentabench-docker-config-"
            ) as docker_config:
                report = mirror_doctor(
                    args.repository,
                    environment=os.environ,
                    git_runner=SubprocessRunner(inherit_git_config=True),
                    docker_runner=SubprocessRunner(docker_config=Path(docker_config)),
                    git_executable=shutil.which("git"),
                    docker_executable=shutil.which("docker"),
                )
            sys.stdout.write(_json(report))
            return 0 if report["ok"] else 1

        if args.command == "git-configure":
            report = configure_git_mirror(args.repository)
            sys.stdout.write(_json(report))
            return 0

        loaded = load_image_spec(args.spec)
        if args.command == "plan":
            sys.stdout.write(_json(acquisition_plan(loaded, args.mirror_registry)))
            return 0

        with _docker_context() as docker:
            if args.command == "verify":
                report = verify_cached_image(loaded, args.mirror_registry, docker)
            else:
                report = acquire_image(
                    loaded,
                    args.mirror_registry,
                    args.receipt,
                    docker,
                )
        sys.stdout.write(_json(report))
        return 0
    except ImageSpecError:
        sys.stdout.write(
            _json(_error_payload("INVALID_IMAGE_SPEC", "image spec is invalid"))
        )
        return 2
    except AcquisitionError as exc:
        sys.stdout.write(_json(_error_payload(exc.code, str(exc))))
        return exc.exit_code
    except Exception:
        sys.stdout.write(
            _json(_error_payload("INTERNAL_ERROR", "mirror operation failed safely"))
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
