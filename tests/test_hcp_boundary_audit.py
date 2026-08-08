from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "audit_hcp_boundary.sh"


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


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(("rule", "content"), CONTENT_CASES)
def test_each_content_rule_is_enforced(
    tmp_path: Path, rule: str, content: str
) -> None:
    script, source = _audit_fixture(tmp_path)
    (source / "violation.py").write_text(content + "\n", encoding="utf-8")

    result = _run(script)

    assert result.returncode != 0
    assert rule in result.stdout
    assert "violation.py" in result.stdout


@pytest.mark.parametrize("rule", ("BMP-HCP-B12", "BMP-HCP-B13"))
def test_each_path_rule_is_enforced(tmp_path: Path, rule: str) -> None:
    script, source = _audit_fixture(tmp_path)
    if rule.endswith("12"):
        path = source / _joined("HarnessComponent", "Protocol") / "modules" / "item.py"
    else:
        path = source / _joined("Hcp", "Server.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 1\n", encoding="utf-8")

    result = _run(script)

    assert result.returncode != 0
    assert rule in result.stdout
    assert str(path) in result.stdout


def test_clean_fixture_passes(tmp_path: Path) -> None:
    script, _ = _audit_fixture(tmp_path)

    result = _run(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 violation(s), 0 scan error(s)" in result.stdout


def test_repository_passes_boundary_audit() -> None:
    result = _run(AUDIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 violation(s), 0 scan error(s)" in result.stdout
