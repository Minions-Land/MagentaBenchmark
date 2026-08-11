from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from MagentaBench.runner.compiler import Compiler


RegistrySourceBinder = Callable[[Compiler, str, str, Path], None]
SubprocessBackendBinder = Callable[[Compiler], Path]


def _bind_registry_updates(
    compiler: Compiler,
    kind: str,
    entry_id: str,
    updates: dict[str, Any],
) -> None:
    """Install an instance-local override that survives ``compile()``.

    ``Compiler.compile`` deliberately clears its parsed-registry cache before
    every compilation.  Writing a fixture into that cache therefore creates a
    false-positive test seam on workstations where the original source happens
    to exist.  The lookup wrapper keeps production cache behavior intact and
    applies only the explicitly registered test update after each lookup.
    """

    state_name = "_magentabench_test_registry_updates"
    state = getattr(compiler, state_name, None)
    if state is None:
        state = {}
        original_lookup = compiler._lookup

        def lookup(selected_kind: str, selected_id: str):
            spec, declaration_path = original_lookup(selected_kind, selected_id)
            selected_updates = state.get((selected_kind, selected_id))
            if selected_updates is None:
                return spec, declaration_path
            return spec.model_copy(update=selected_updates), declaration_path

        setattr(compiler, state_name, state)
        setattr(compiler, "_lookup", lookup)

    state[(kind, entry_id)] = dict(updates)
    compiler._registry_cache.pop((kind, entry_id), None)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-external-checkouts",
        action="store_true",
        default=False,
        help="run tests that require separately provisioned, pinned source checkouts",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "external_checkout: requires a separately provisioned, pinned source checkout",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-external-checkouts") or os.environ.get(
        "MAGENTABENCH_RUN_EXTERNAL_CHECKOUTS"
    ) == "1":
        return
    skip = pytest.mark.skip(
        reason="requires --run-external-checkouts and the pinned external checkout"
    )
    for item in items:
        if "external_checkout" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def bind_registry_source() -> RegistrySourceBinder:
    """Relocate one registry source to a deterministic test-only fixture.

    The compiler cache is the explicit test seam: production declarations stay
    pinned to their release checkouts, while unit tests exercise the same
    compiler and adapter code without depending on a workstation layout.
    """

    def bind(
        compiler: Compiler,
        kind: str,
        entry_id: str,
        source: Path,
    ) -> None:
        _bind_registry_updates(
            compiler,
            kind,
            entry_id,
            {
                "source": str(source.resolve(strict=True)),
                "commit": f"hermetic-{entry_id}-fixture-v1",
            },
        )

    return bind


@pytest.fixture
def bind_host_subprocess_backend() -> SubprocessBackendBinder:
    """Bind subprocess conformance to the executable bytes on this host."""

    def bind(compiler: Compiler) -> Path:
        selected = shutil.which("echo")
        if selected is None:
            pytest.fail("the subprocess conformance tests require an echo executable")
        executable = Path(selected).resolve(strict=True)
        _bind_registry_updates(
            compiler,
            "backend",
            "subprocess.echo",
            {
                "executable": str(executable),
                "digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
            },
        )
        return executable

    return bind


@pytest.fixture
def aosebench_source(tmp_path: Path) -> Path:
    source = tmp_path / "aosebench"
    task = source / "benchmark" / "tasks" / "da-1-3"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Analyze the fixture data.\n", encoding="utf-8")
    (task / "tests" / "rubric.txt").write_text("fixture rubric\n", encoding="utf-8")
    (task / "task.toml").write_text(
        """schema_version = "1.1"

[task]
name = "fixture/da-1-3"

[agent]
timeout_sec = 3600.0

[verifier]
timeout_sec = 900.0

[environment]
cpus = 2
memory_mb = 1024
storage_mb = 2048
allow_internet = false
""",
        encoding="utf-8",
    )
    return source


@pytest.fixture
def swebench_source(tmp_path: Path) -> Path:
    source = tmp_path / "swebench"
    source.mkdir()
    row = {
        "instance_id": "astropy__astropy-6938",
        "repo": "astropy/astropy",
        "base_commit": "c76af9ed6bb89bfba45b9f5bc1e635188278e2fa",
        "problem_statement": "Possible bug in io.fits related to D exponents",
        "test_patch": "fixture verifier patch",
        "FAIL_TO_PASS": [
            "astropy/io/fits/tests/test_checksum.py::TestChecksumFunctions::test_ascii_table_data",
            "astropy/io/fits/tests/test_table.py::TestTableFunctions::test_ascii_table",
        ],
        "PASS_TO_PASS": [
            "astropy/io/fits/tests/test_table.py::test_regression_scalar_indexing"
        ],
    }
    (source / "swebench_lite_test.json").write_text(
        json.dumps([row], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return source


@pytest.fixture
def terminal_bench_source(tmp_path: Path) -> Path:
    source = tmp_path / "terminal-bench"
    task = source / "tasks" / "regex-log"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "task.toml").write_text(
        """schema_version = "1.1"

[task]
name = "terminal-bench/regex-log"

[environment]
allow_internet = true
""",
        encoding="utf-8",
    )
    (task / "instruction.md").write_text(
        "Write the requested regular expression.\n", encoding="utf-8"
    )
    (task / "environment" / "Dockerfile").write_text(
        "FROM ubuntu:24.04\n", encoding="utf-8"
    )
    (task / "tests" / "test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return source


@pytest.fixture
def terminal_bench_release_source() -> Path:
    compiler = Compiler(Path(__file__).parents[1])
    spec, declaration_path = compiler._lookup(
        "dataset", "dataset.terminal-bench-2.1"
    )
    configured = os.environ.get("MAGENTABENCH_TERMINAL_BENCH_ROOT")
    declared = Path(spec.source).expanduser()
    source = (
        Path(configured).expanduser()
        if configured
        else (
            declared
            if declared.is_absolute()
            else declaration_path.parent / declared
        )
    )
    try:
        return source.resolve(strict=True)
    except OSError as exc:
        pytest.fail(
            "the pinned Terminal-Bench checkout is unavailable; set "
            "MAGENTABENCH_TERMINAL_BENCH_ROOT"
        )
        raise AssertionError("pytest.fail did not stop execution") from exc
