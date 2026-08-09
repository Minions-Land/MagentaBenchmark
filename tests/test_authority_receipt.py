from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from MagentaBench.schemas import ReportVerificationError, verify_external_protocol_authority


ROOT = Path(__file__).parents[1]
RECEIPT = ROOT / "docs" / "authority" / "magenta-hcp-authority.json"
BOUNDARY_LAW = ROOT / "docs" / "governance" / "bmp-boundary-law.md"


def test_tracked_magenta_hcp_authority_receipt_is_standalone_verifiable() -> None:
    verified = verify_external_protocol_authority(RECEIPT)

    assert verified.receipt.protocol_id == "magenta-hcp"
    assert verified.receipt.source_commit == (
        "78e2998f5bb78aa029c5cfe6f9508777f307679d"
    )
    assert verified.receipt.authority_documents


def test_authority_receipt_fails_closed_when_tracked_bytes_drift(tmp_path: Path) -> None:
    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    payload["authority_documents"][0]["artifact_ref"]["sha256"] = "0" * 64
    mutated = tmp_path / "authority.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReportVerificationError, match="sha256 mismatch"):
        verify_external_protocol_authority(mutated)


def test_boundary_law_quotes_exist_at_pinned_authority_lines() -> None:
    """Keep human-readable HCP citations bound to the pinned source bytes."""

    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    authority_root = Path(payload["source_root"])
    law = BOUNDARY_LAW.read_text(encoding="utf-8")
    citations = {
        int(line_number): quote
        for line_number, quote in re.findall(
            r"\[`hcp-architecture\.md:(\d+)`\]\([^)]*\): \"([^\"]+)\"",
            law,
        )
    }
    expected_lines = {144, 168, 170, 171, 172, 173, 177, 178, 179}
    assert expected_lines <= citations.keys()
    architecture = (
        authority_root
        / "HarnessComponentProtocol"
        / "docs"
        / "governance"
        / "hcp-architecture.md"
    ).read_text(encoding="utf-8").splitlines()
    for line_number in expected_lines:
        quote = citations[line_number]
        assert quote in architecture[line_number - 1]


def test_authority_receipt_supports_explicit_checkout_relocation(tmp_path: Path) -> None:
    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    old_root = Path(payload["source_root"])
    new_root = tmp_path / "Magenta"
    # The verifier's relocation map is intentionally explicit; all source
    # artifacts remain at their recorded absolute paths in this test, while
    # the git authority checkout is relocated to an equivalent temporary
    # clone when one is available.
    if not (old_root / ".git").exists():
        pytest.skip("Magenta checkout is not available")
    import subprocess

    subprocess.run(
        ["git", "clone", "--no-local", str(old_root), str(new_root)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    payload["source_root"] = str(new_root)
    relocated = tmp_path / "authority.json"
    relocated.write_text(json.dumps(payload), encoding="utf-8")

    verified = verify_external_protocol_authority(
        relocated,
        path_map={str(old_root): str(new_root)},
    )
    assert verified.receipt.protocol_id == "magenta-hcp"
