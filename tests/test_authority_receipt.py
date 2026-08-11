from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from MagentaBench.schemas import ReportVerificationError, verify_external_protocol_authority
from MagentaBench.runner.evidence import artifact_ref


ROOT = Path(__file__).parents[1]
RECEIPT = ROOT / "docs" / "authority" / "magenta-hcp-authority.json"
BOUNDARY_LAW = ROOT / "docs" / "governance" / "bmp-boundary-law.md"
AUDIT = ROOT / "scripts" / "audit_hcp_boundary.sh"


def _tracked_path_map() -> dict[str, str]:
    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    recorded_source = str(payload["source_root"])
    source = os.environ.get("MAGENTABENCH_HCP_AUTHORITY_ROOT", recorded_source)
    recorded_repository = str(Path(payload["audit_rules_ref"]["path"]).parents[1])
    return {recorded_source: source, recorded_repository: str(ROOT)}


def _write_hermetic_receipt(tmp_path: Path) -> Path:
    source = tmp_path / "authority-source"
    protocol_directory = "HarnessComponent" + "Protocol"
    client_filename = "Hcp" + "Client.ts"
    architecture_relative = (
        f"{protocol_directory}/docs/governance/hcp-architecture.md"
    )
    architecture = (
        source
        / protocol_directory
        / "docs"
        / "governance"
        / "hcp-architecture.md"
    )
    architecture.parent.mkdir(parents=True)
    architecture.write_text("# Fixture HCP architecture\n", encoding="utf-8")
    contract = source / protocol_directory / client_filename
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        "export class " + "Hcp" + "Client {}\n", encoding="utf-8"
    )
    audit = tmp_path / "audit-rules.sh"
    audit.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=MagentaBench tests",
            "-c",
            "user.email=magentabench-tests@example.invalid",
            "commit",
            "-q",
            "--no-gpg-sign",
            "-m",
            "fixture authority",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    receipt = tmp_path / "authority.json"
    receipt.write_text(
        json.dumps(
            {
                "format": "bmp-external-protocol-authority-v1",
                "protocol_id": "fixture-hcp",
                "source_commit": commit,
                "source_root": str(source.resolve()),
                "authority_documents": [
                    {
                        "id": "hcp-architecture",
                        "relative_path": architecture_relative,
                        "artifact_ref": artifact_ref(architecture).model_dump(
                            mode="json"
                        ),
                    }
                ],
                "contract_version": "fixture-v1",
                "contract_relative_path": f"{protocol_directory}/{client_filename}",
                "contract_ref": artifact_ref(contract).model_dump(mode="json"),
                "audit_rules_ref": artifact_ref(audit).model_dump(mode="json"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return receipt


def test_tracked_audit_rules_ref_matches_repository_bytes() -> None:
    """Keep the required CI gate bound to its tracked audit script bytes."""

    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    recorded = payload["audit_rules_ref"]
    recorded_path = Path(recorded["path"])
    recorded_root = recorded_path.parents[1]
    assert recorded_path.relative_to(recorded_root) == Path(
        "scripts/audit_hcp_boundary.sh"
    )
    observed = artifact_ref(AUDIT)
    assert recorded["sha256"] == observed.sha256
    assert recorded["size_bytes"] == observed.size_bytes


@pytest.mark.external_checkout
def test_tracked_magenta_hcp_authority_receipt_is_standalone_verifiable() -> None:
    verified = verify_external_protocol_authority(
        RECEIPT, path_map=_tracked_path_map()
    )

    assert verified.receipt.protocol_id == "magenta-hcp"
    assert verified.receipt.source_commit == (
        "78e2998f5bb78aa029c5cfe6f9508777f307679d"
    )
    assert verified.receipt.authority_documents


def test_authority_receipt_fails_closed_when_authority_bytes_drift(
    tmp_path: Path,
) -> None:
    receipt = _write_hermetic_receipt(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["authority_documents"][0]["artifact_ref"]["sha256"] = "0" * 64
    mutated = tmp_path / "authority.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReportVerificationError, match="sha256 mismatch"):
        verify_external_protocol_authority(mutated)


def test_authority_receipt_is_hermetic_and_standalone_verifiable(
    tmp_path: Path,
) -> None:
    verified = verify_external_protocol_authority(
        _write_hermetic_receipt(tmp_path)
    )
    assert verified.receipt.protocol_id == "fixture-hcp"


@pytest.mark.external_checkout
def test_boundary_law_quotes_exist_at_pinned_authority_lines() -> None:
    """Keep human-readable HCP citations bound to the pinned source bytes."""

    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    authority_root = Path(_tracked_path_map()[str(payload["source_root"])])
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


@pytest.mark.external_checkout
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
        path_map={
            str(old_root): str(new_root),
            str(Path(payload["audit_rules_ref"]["path"]).parents[1]): str(ROOT),
        },
    )
    assert verified.receipt.protocol_id == "magenta-hcp"
