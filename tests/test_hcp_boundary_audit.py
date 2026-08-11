from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "audit_hcp_boundary.sh"
BASH = Path(shutil.which("bash", path=os.defpath) or "/bin/bash").resolve()


def _joined(*parts: str) -> str:
    return "".join(parts)


CONTENT_CASES = (
    ("BMP-HCP-B01", _joined("HcpBenchmark", "Server")),
    ("BMP-HCP-B02", _joined("class ", "HcpOwned:\n    pass")),
    ("BMP-HCP-B03", _joined("role = Hcp", "Client")),
    ("BMP-HCP-B04", _joined("value = to", "Tool()")),
    ("BMP-HCP-B05", _joined("path = 'sources.", "generated.ts'")),
    ("BMP-HCP-B06", _joined("hcp_module_", "registry = {}")),
    ("BMP-HCP-B07", _joined("hcp_product_", "factory = {}")),
    ("BMP-HCP-B08", _joined("value = Universal", "Magnet")),
    ("BMP-HCP-B09", _joined("hcp_tool_call_", "middleware = object()")),
    (
        "BMP-HCP-B10",
        _joined(
            'import value from "x/HarnessComponent',
            'Protocol/_magenta/value"',
        ),
    ),
    (
        "BMP-HCP-B11",
        _joined("from HarnessComponent", "Protocol.value import item"),
    ),
)


def _audit_fixture(tmp_path: Path) -> tuple[Path, Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / AUDIT.name
    shutil.copy2(AUDIT, script)
    source = tmp_path / "MagentaBench"
    source.mkdir()
    (source / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    return script, source


def _python_only_env(tmp_path: Path) -> dict[str, str]:
    binary_directory = tmp_path / "python-only-bin"
    binary_directory.mkdir()
    (binary_directory / "python3").symlink_to(Path(sys.executable).resolve())
    environment = os.environ.copy()
    environment["PATH"] = str(binary_directory)
    return environment


@pytest.fixture(params=("ambient", "python-fallback"))
def scan_environment(
    request: pytest.FixtureRequest, tmp_path: Path
) -> dict[str, str] | None:
    if request.param == "python-fallback":
        return _python_only_env(tmp_path)
    return None


def _run(
    script: Path,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BASH), str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


@pytest.mark.parametrize(("rule", "content"), CONTENT_CASES)
def test_each_content_rule_is_enforced(
    tmp_path: Path,
    scan_environment: dict[str, str] | None,
    rule: str,
    content: str,
) -> None:
    script, source = _audit_fixture(tmp_path)
    (source / "violation.py").write_text(content + "\n", encoding="utf-8")

    result = _run(script, env=scan_environment)

    assert result.returncode != 0
    assert rule in result.stdout
    assert "violation.py" in result.stdout


@pytest.mark.parametrize("rule", ("BMP-HCP-B12", "BMP-HCP-B13"))
def test_each_path_rule_is_enforced(
    tmp_path: Path, scan_environment: dict[str, str] | None, rule: str
) -> None:
    script, source = _audit_fixture(tmp_path)
    if rule.endswith("12"):
        path = source / _joined("HarnessComponent", "Protocol") / "modules" / "item.py"
    else:
        path = source / _joined("Hcp", "Server.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 1\n", encoding="utf-8")

    result = _run(script, env=scan_environment)

    assert result.returncode != 0
    assert rule in result.stdout
    assert str(path) in result.stdout


def test_clean_fixture_passes(
    tmp_path: Path, scan_environment: dict[str, str] | None
) -> None:
    script, _ = _audit_fixture(tmp_path)

    result = _run(script, env=scan_environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 violation(s), 0 scan error(s)" in result.stdout


def test_repository_passes_boundary_audit(
    scan_environment: dict[str, str] | None,
) -> None:
    result = _run(AUDIT, env=scan_environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 violation(s), 0 scan error(s)" in result.stdout


def test_missing_scan_engines_fails_closed(tmp_path: Path) -> None:
    script, _ = _audit_fixture(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = ""

    result = _run(script, env=environment)

    assert result.returncode == 2
    assert "rg with PCRE2 or Python 3 is required" in result.stderr


def test_python_fallback_fails_closed_on_invalid_utf8(tmp_path: Path) -> None:
    script, source = _audit_fixture(tmp_path)
    invalid_source = source / "invalid.py"
    invalid_source.write_bytes(b"\xff\n")

    result = _run(script, env=_python_only_env(tmp_path))

    assert result.returncode == 1
    assert "cannot read UTF-8 source" in result.stderr
    assert str(invalid_source) in result.stderr
    assert "0 violation(s), 1 scan error(s)" in result.stdout


def test_root_excluded_directories_are_preserved(
    tmp_path: Path, scan_environment: dict[str, str] | None
) -> None:
    script, _ = _audit_fixture(tmp_path)
    excluded = tmp_path / "docs"
    excluded.mkdir()
    (excluded / "example.py").write_text(
        _joined("role = Hcp", "Client\n"), encoding="utf-8"
    )

    result = _run(script, env=scan_environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 violation(s), 0 scan error(s)" in result.stdout


def test_nested_exclusion_name_remains_in_scope(
    tmp_path: Path, scan_environment: dict[str, str] | None
) -> None:
    script, source = _audit_fixture(tmp_path)
    nested = source / "package" / "docs"
    nested.mkdir(parents=True)
    violation = nested / "violation.py"
    violation.write_text(_joined("role = Hcp", "Client\n"), encoding="utf-8")

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "BMP-HCP-B03" in result.stdout
    assert str(violation) in result.stdout


def test_ignore_files_cannot_hide_in_scope_source(
    tmp_path: Path, scan_environment: dict[str, str] | None
) -> None:
    script, _ = _audit_fixture(tmp_path)
    (tmp_path / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    violation = scratch / "violation.py"
    violation.write_text(_joined("role = Hcp", "Client\n"), encoding="utf-8")

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "BMP-HCP-B03" in result.stdout
    assert str(violation) in result.stdout


def test_utf8_bom_does_not_hide_first_line_import(
    tmp_path: Path, scan_environment: dict[str, str] | None
) -> None:
    script, source = _audit_fixture(tmp_path)
    violation = source / "violation.py"
    violation.write_text(
        "\ufeff" + _joined("from HarnessComponent", "Protocol.value import item\n"),
        encoding="utf-8",
    )

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "BMP-HCP-B11" in result.stdout
    assert str(violation) in result.stdout


def test_form_feed_does_not_split_a_scanned_line(
    tmp_path: Path, scan_environment: dict[str, str] | None
) -> None:
    script, source = _audit_fixture(tmp_path)
    violation = source / "violation.py"
    violation.write_text(_joined("class\f", "HcpOwned:\n    pass\n"), encoding="utf-8")

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "BMP-HCP-B02" in result.stdout
    assert str(violation) in result.stdout


def test_nul_byte_does_not_hide_declared_source_text(
    tmp_path: Path, scan_environment: dict[str, str] | None
) -> None:
    script, source = _audit_fixture(tmp_path)
    violation = source / "violation.py"
    violation.write_bytes(_joined("VALUE = 1\0role = Hcp", "Client\n").encode())

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "BMP-HCP-B03" in result.stdout
    assert str(violation) in result.stdout


def test_python_fallback_ignores_cwd_module_shadowing(tmp_path: Path) -> None:
    script, source = _audit_fixture(tmp_path)
    (tmp_path / "re.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    violation = source / "violation.py"
    violation.write_text(_joined("role = Hcp", "Client\n"), encoding="utf-8")

    result = _run(
        script,
        env=_python_only_env(tmp_path),
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "BMP-HCP-B03" in result.stdout
    assert str(violation) in result.stdout


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_ripgrep_config_cannot_silence_violations(tmp_path: Path) -> None:
    script, source = _audit_fixture(tmp_path)
    violation = source / "violation.py"
    violation.write_text(_joined("role = Hcp", "Client\n"), encoding="utf-8")
    config = tmp_path / "ripgrep.conf"
    config.write_text("--quiet\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["RIPGREP_CONFIG_PATH"] = str(config)

    result = _run(script, env=environment)

    assert result.returncode == 1
    assert "BMP-HCP-B03" in result.stdout
    assert str(violation) in result.stdout
