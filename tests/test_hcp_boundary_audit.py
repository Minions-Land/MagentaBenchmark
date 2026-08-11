from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "audit_hcp_boundary.sh"
BASH = Path(shutil.which("bash", path=os.defpath) or "/bin/bash").resolve()
SH = Path(shutil.which("sh", path=os.defpath) or "/bin/sh").resolve()
MANAGED_RG_SPEC = "ripgrep-bin==15.1.0"


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


def _supports_pcre2(executable: Path) -> bool:
    try:
        result = subprocess.run(
            [str(executable), "--pcre2", "-q", "(?<=p)cre"],
            check=False,
            capture_output=True,
            input="pcre\n",
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def real_rg() -> Path:
    ambient = shutil.which("rg")
    if ambient is not None:
        candidate = Path(ambient).resolve()
        if _supports_pcre2(candidate):
            return candidate

    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("tests require ambient PCRE2 rg or uv for managed real rg")
    result = subprocess.run(
        [
            uv,
            "--no-config",
            "tool",
            "run",
            "--isolated",
            "--no-build",
            "--from",
            MANAGED_RG_SPEC,
            str(SH),
            "-c",
            "command -v rg",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.fail(f"cannot resolve managed real rg: {result.stdout}{result.stderr}")
    candidate = Path(result.stdout.splitlines()[0]).resolve()
    version = subprocess.run(
        [str(candidate), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if (
        version.returncode != 0
        or not version.stdout.startswith("ripgrep 15.1.0 ")
        or not _supports_pcre2(candidate)
    ):
        pytest.fail(f"{MANAGED_RG_SPEC} did not provide PCRE2 ripgrep 15.1.0")
    return candidate


def _ambient_rg_env(tmp_path: Path, real_rg: Path) -> dict[str, str]:
    binary_directory = tmp_path / "ambient-rg-bin"
    binary_directory.mkdir()
    (binary_directory / "rg").symlink_to(real_rg)
    unavailable_uv = binary_directory / "uv"
    unavailable_uv.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    unavailable_uv.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        (str(binary_directory), environment.get("PATH", os.defpath))
    )
    return environment


def _managed_rg_env(tmp_path: Path) -> dict[str, str]:
    if shutil.which("uv") is None:
        pytest.fail("managed-rg tests require uv")
    binary_directory = tmp_path / "managed-rg-bin"
    binary_directory.mkdir()
    unsupported_rg = binary_directory / "rg"
    unsupported_rg.write_text("#!/bin/sh\nexit 98\n", encoding="utf-8")
    unsupported_rg.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        (str(binary_directory), environment.get("PATH", os.defpath))
    )
    return environment


@pytest.fixture(params=("ambient-rg", "managed-rg"))
def scan_environment(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    real_rg: Path,
) -> dict[str, str]:
    if request.param == "ambient-rg":
        return _ambient_rg_env(tmp_path, real_rg)
    return _managed_rg_env(tmp_path)


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
        timeout=180,
    )


@pytest.mark.parametrize(("rule", "content"), CONTENT_CASES)
def test_each_content_rule_is_enforced(
    tmp_path: Path,
    scan_environment: dict[str, str],
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
    tmp_path: Path, scan_environment: dict[str, str], rule: str
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


def test_clean_fixture_passes(tmp_path: Path, scan_environment: dict[str, str]) -> None:
    script, _ = _audit_fixture(tmp_path)

    result = _run(script, env=scan_environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 violation(s), 0 scan error(s)" in result.stdout


def test_repository_passes_boundary_audit(
    scan_environment: dict[str, str],
) -> None:
    result = _run(AUDIT, env=scan_environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 violation(s), 0 scan error(s)" in result.stdout


def test_missing_rg_and_uv_fails_closed(tmp_path: Path) -> None:
    script, _ = _audit_fixture(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = ""

    result = _run(script, env=environment)

    assert result.returncode == 2
    assert "rg with PCRE2 is required; managed bootstrap requires uv" in result.stderr


def test_invalid_utf8_fails_closed(
    tmp_path: Path, scan_environment: dict[str, str]
) -> None:
    script, source = _audit_fixture(tmp_path)
    invalid_source = source / "invalid.py"
    invalid_source.write_bytes(b"\xff\n")

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "cannot read UTF-8 source" in result.stderr
    assert str(invalid_source) in result.stderr
    assert "0 violation(s), 1 scan error(s)" in result.stdout


def test_root_excluded_directories_are_preserved(
    tmp_path: Path, scan_environment: dict[str, str]
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
    tmp_path: Path, scan_environment: dict[str, str]
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
    tmp_path: Path, scan_environment: dict[str, str]
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
    tmp_path: Path, scan_environment: dict[str, str]
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
    tmp_path: Path, scan_environment: dict[str, str]
) -> None:
    script, source = _audit_fixture(tmp_path)
    violation = source / "violation.py"
    violation.write_text(_joined("class\f", "HcpOwned:\n    pass\n"), encoding="utf-8")

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "BMP-HCP-B02" in result.stdout
    assert str(violation) in result.stdout


def test_nul_byte_does_not_hide_declared_source_text(
    tmp_path: Path, scan_environment: dict[str, str]
) -> None:
    script, source = _audit_fixture(tmp_path)
    violation = source / "violation.py"
    violation.write_bytes(_joined("VALUE = 1\0role = Hcp", "Client\n").encode())

    result = _run(script, env=scan_environment)

    assert result.returncode == 1
    assert "BMP-HCP-B03" in result.stdout
    assert str(violation) in result.stdout


def test_utf8_preflight_ignores_cwd_module_shadowing(
    tmp_path: Path, scan_environment: dict[str, str]
) -> None:
    script, source = _audit_fixture(tmp_path)
    (tmp_path / "pathlib.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    invalid_source = source / "invalid.py"
    invalid_source.write_bytes(b"\xff\n")

    result = _run(
        script,
        env=scan_environment,
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "cannot read UTF-8 source" in result.stderr
    assert str(invalid_source) in result.stderr


def test_ripgrep_config_cannot_silence_violations(
    tmp_path: Path, scan_environment: dict[str, str]
) -> None:
    script, source = _audit_fixture(tmp_path)
    violation = source / "violation.py"
    violation.write_text(_joined("role = Hcp", "Client\n"), encoding="utf-8")
    config = tmp_path / "ripgrep.conf"
    config.write_text("--quiet\n", encoding="utf-8")
    environment = scan_environment.copy()
    environment["RIPGREP_CONFIG_PATH"] = str(config)

    result = _run(script, env=environment)

    assert result.returncode == 1
    assert "BMP-HCP-B03" in result.stdout
    assert str(violation) in result.stdout
