from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from MagentaBench.collab.external_evidence import (
    EvidenceFile,
    ExternalEvidenceError,
    ExternalEvidenceSpec,
    ExternalLocator,
    RelocationMap,
    apply_relocation,
    load_external_evidence_spec,
    materialize_external_evidence,
    relocation_path_map,
    verify_materialized_evidence,
)
from MagentaBench.collab.cli import main as collab_main


ROOT = Path(__file__).parents[1]


def _file(
    payload: bytes,
    *,
    destination: str,
    role: str = "evidence",
    locator: str = "https://artifacts.example.invalid/sample-v1",
) -> dict[str, object]:
    return {
        "destination": destination,
        "locator": {
            "credential_names": [],
            "locator": locator,
            "provider": "https",
            "revision": "v1",
        },
        "role": role,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _spec(*files: dict[str, object]) -> dict[str, object]:
    return {
        "candidate": "public contract test fixture",
        "files": list(files),
        "format": "magentabench-external-evidence-manifest-v1",
        "license_id": "Apache-2.0",
        "non_claim": True,
        "relocation_maps": [],
        "source_id": "external-evidence-test-v1",
    }


def _bytes_fetcher(payloads: dict[str, bytes]):
    def fetcher(locator, _target):
        return payloads[locator.locator]

    return fetcher


def test_materializes_and_reverifies_every_declared_closure_role(
    tmp_path: Path,
) -> None:
    payloads = {
        "https://artifacts.example.invalid/report": b"report",
        "https://artifacts.example.invalid/index": b"index",
        "https://artifacts.example.invalid/manifest": b"manifest",
        "https://artifacts.example.invalid/evidence": b"evidence",
        "https://artifacts.example.invalid/artifact": b"artifact",
    }
    files = [
        _file(payload, destination=f"closure/{role}.bin", role=role, locator=locator)
        for role, locator, payload in (
            (
                "report",
                "https://artifacts.example.invalid/report",
                payloads["https://artifacts.example.invalid/report"],
            ),
            (
                "record-index",
                "https://artifacts.example.invalid/index",
                payloads["https://artifacts.example.invalid/index"],
            ),
            (
                "manifest",
                "https://artifacts.example.invalid/manifest",
                payloads["https://artifacts.example.invalid/manifest"],
            ),
            (
                "evidence",
                "https://artifacts.example.invalid/evidence",
                payloads["https://artifacts.example.invalid/evidence"],
            ),
            (
                "artifact",
                "https://artifacts.example.invalid/artifact",
                payloads["https://artifacts.example.invalid/artifact"],
            ),
        )
    ]
    spec = ExternalEvidenceSpec.from_mapping(_spec(*files))

    receipt = materialize_external_evidence(
        spec,
        tmp_path,
        _bytes_fetcher(payloads),
        root_name="closure",
    )

    assert receipt.root == tmp_path / "closure"
    assert receipt.as_dict()["non_claim"] is True
    assert receipt.as_dict()["standalone_verification"] == "not-run"
    assert (receipt.root / "closure/report.bin").read_bytes() == b"report"
    assert (
        verify_materialized_evidence(receipt.root, spec).receipt_sha256
        == receipt.receipt_sha256
    )


def test_receipt_redacts_explicit_relocation_prefixes(tmp_path: Path) -> None:
    payload = b"safe bytes"
    data = _spec(_file(payload, destination="evidence/one.bin"))
    data["relocation_maps"] = [
        {
            "old_prefix": "/mnt/private/operator-run",
            "new_prefix": "/tmp/restored/operator-run",
        }
    ]
    spec = ExternalEvidenceSpec.from_mapping(data)
    receipt = materialize_external_evidence(
        spec,
        tmp_path,
        _bytes_fetcher({"https://artifacts.example.invalid/sample-v1": payload}),
        root_name="redacted",
    )

    rendered = receipt.receipt_path.read_text(encoding="utf-8")
    assert "/mnt/private" not in rendered
    assert "/tmp/restored" not in rendered
    assert "<redacted-absolute-prefix>" in rendered
    assert apply_relocation(
        "/mnt/private/operator-run/report.json", spec.relocation_maps
    ) == Path("/tmp/restored/operator-run/report.json")
    assert relocation_path_map(spec.relocation_maps) == {
        "/mnt/private/operator-run": "/tmp/restored/operator-run"
    }


def test_relocation_uses_longest_prefix_and_rejects_non_normalized_paths() -> None:
    maps = (
        RelocationMap.from_mapping({"old_prefix": "/old", "new_prefix": "/restored"}),
        RelocationMap.from_mapping(
            {"old_prefix": "/old/nested", "new_prefix": "/restored/nested"}
        ),
    )
    assert apply_relocation("/old/nested/report.json", maps) == Path(
        "/restored/nested/report.json"
    )
    with pytest.raises(ExternalEvidenceError, match="normalized"):
        apply_relocation("/old/../escape", maps)


def test_missing_or_partial_provider_output_removes_the_fresh_root(
    tmp_path: Path,
) -> None:
    payload = b"complete artifact"
    spec = ExternalEvidenceSpec.from_mapping(
        _spec(_file(payload, destination="evidence/a.bin"))
    )

    with pytest.raises(ExternalEvidenceError, match="missing"):
        materialize_external_evidence(
            spec, tmp_path, lambda _locator, _target: None, root_name="missing"
        )
    assert not (tmp_path / "missing").exists()

    def partial_fetcher(_locator, target: Path):
        target.write_bytes(b"partial")
        return None

    with pytest.raises(ExternalEvidenceError, match="size"):
        materialize_external_evidence(
            spec, tmp_path, partial_fetcher, root_name="partial"
        )
    assert not (tmp_path / "partial").exists()


def test_digest_and_size_drift_fail_closed_and_do_not_leave_output(
    tmp_path: Path,
) -> None:
    payload = b"expected"
    spec = ExternalEvidenceSpec.from_mapping(
        _spec(_file(payload, destination="evidence/a.bin"))
    )

    with pytest.raises(ExternalEvidenceError, match="size"):
        materialize_external_evidence(
            spec,
            tmp_path,
            _bytes_fetcher(
                {"https://artifacts.example.invalid/sample-v1": b"wrong-size"}
            ),
            root_name="size-drift",
        )
    assert not (tmp_path / "size-drift").exists()

    same_size_drift = b"changed!"
    assert len(same_size_drift) == len(payload)
    with pytest.raises(ExternalEvidenceError, match="digest"):
        materialize_external_evidence(
            spec,
            tmp_path,
            _bytes_fetcher(
                {"https://artifacts.example.invalid/sample-v1": same_size_drift}
            ),
            root_name="digest-drift",
        )
    assert not (tmp_path / "digest-drift").exists()


def test_existing_root_is_never_overwritten(tmp_path: Path) -> None:
    payload = b"immutable"
    spec = ExternalEvidenceSpec.from_mapping(
        _spec(_file(payload, destination="evidence/a.bin"))
    )
    first = materialize_external_evidence(
        spec,
        tmp_path,
        _bytes_fetcher({"https://artifacts.example.invalid/sample-v1": payload}),
        root_name="already-there",
    )

    with pytest.raises(ExternalEvidenceError, match="already exists"):
        materialize_external_evidence(
            spec,
            tmp_path,
            _bytes_fetcher({"https://artifacts.example.invalid/sample-v1": payload}),
            root_name="already-there",
        )
    assert (
        verify_materialized_evidence(first.root, spec).receipt_sha256
        == first.receipt_sha256
    )


@pytest.mark.parametrize("destination", ("../escape", "/absolute", "nested/../escape"))
def test_traversal_destinations_are_rejected(destination: str) -> None:
    with pytest.raises(ExternalEvidenceError, match="destination"):
        ExternalEvidenceSpec.from_mapping(_spec(_file(b"x", destination=destination)))


def test_duplicate_destinations_are_rejected_case_insensitively() -> None:
    with pytest.raises(ExternalEvidenceError, match="unique"):
        ExternalEvidenceSpec.from_mapping(
            _spec(
                _file(b"one", destination="evidence/result.json"),
                _file(b"two", destination="EVIDENCE/result.json"),
            )
        )


def test_direct_dataclass_construction_cannot_bypass_locator_validation(
    tmp_path: Path,
) -> None:
    payload = b"x"
    locator = ExternalLocator(
        provider="fixture",
        locator="file:///private/result.json",
        credential_names=("not-a-name",),
    )
    spec = ExternalEvidenceSpec(
        source_id="test",
        candidate="candidate",
        license_id="CC0-1.0",
        files=(
            EvidenceFile(
                destination="result.json",
                locator=locator,
                size_bytes=1,
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )
    with pytest.raises(ExternalEvidenceError):
        materialize_external_evidence(spec, tmp_path, lambda _locator, _target: payload)
    assert not tuple(tmp_path.iterdir())


def test_manifest_loader_rejects_duplicate_keys_without_values_in_error(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"source_id":"one","source_id":"two"}', encoding="utf-8")
    with pytest.raises(ExternalEvidenceError, match="duplicate") as raised:
        load_external_evidence_spec(manifest)
    assert "one" not in str(raised.value)
    assert "two" not in str(raised.value)


def test_manifest_loader_rejects_non_finite_json(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"size_bytes":NaN}', encoding="utf-8")
    with pytest.raises(ExternalEvidenceError, match="non-finite"):
        load_external_evidence_spec(manifest)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda data: data["files"][0]["locator"].update(
            {"locator": "https://example.invalid/item?X-Amz-Signature=secret"}
        ),
        lambda data: data["files"][0]["locator"].update(
            {"locator": "file:///private/report.json"}
        ),
        lambda data: data["files"][0]["locator"].update(
            {"credential_names": ["invalid-credential-value"]}
        ),
        lambda data: data["files"][0]["locator"].update({"api_key": "not-allowed"}),
        lambda data: data.update({"auth_token": "not-allowed"}),
    ),
)
def test_secret_or_unauthorized_locator_forms_fail_without_echoing_values(
    mutate,
) -> None:
    data = deepcopy(_spec(_file(b"x", destination="evidence/a.bin")))
    mutate(data)

    with pytest.raises(ExternalEvidenceError) as raised:
        ExternalEvidenceSpec.from_mapping(data)
    assert "secret" not in str(raised.value).casefold()
    assert "not-allowed" not in str(raised.value)


def test_materialized_byte_drift_and_receipt_drift_fail_closed(tmp_path: Path) -> None:
    payload = b"verified"
    spec = ExternalEvidenceSpec.from_mapping(
        _spec(_file(payload, destination="evidence/a.bin"))
    )
    receipt = materialize_external_evidence(
        spec,
        tmp_path,
        _bytes_fetcher({"https://artifacts.example.invalid/sample-v1": payload}),
        root_name="verify-again",
    )
    (receipt.root / "evidence/a.bin").write_bytes(b"drifted!")
    with pytest.raises(ExternalEvidenceError, match="digest"):
        verify_materialized_evidence(receipt.root, spec)

    (receipt.root / "evidence/a.bin").write_bytes(payload)
    receipt.receipt_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ExternalEvidenceError, match="receipt"):
        verify_materialized_evidence(receipt.root, spec)


def test_unindexed_provider_output_fails_closed_and_removes_root(
    tmp_path: Path,
) -> None:
    payload = b"declared"
    spec = ExternalEvidenceSpec.from_mapping(
        _spec(_file(payload, destination="evidence/a.bin"))
    )

    def noisy_fetcher(_locator, target: Path):
        target.write_bytes(payload)
        (target.parent / "unindexed.bin").write_bytes(b"unexpected")
        return None

    with pytest.raises(ExternalEvidenceError, match="unindexed"):
        materialize_external_evidence(
            spec, tmp_path, noisy_fetcher, root_name="unindexed"
        )
    assert not (tmp_path / "unindexed").exists()


def test_checked_in_public_pilot_materializes_end_to_end_and_stays_out_of_ledger(
    tmp_path: Path,
) -> None:
    fixture_root = ROOT / "imports/materialized/public-pilot"
    spec = ExternalEvidenceSpec.from_mapping(
        json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    )
    payload = (fixture_root / "payload.json").read_bytes()
    receipt = materialize_external_evidence(
        spec,
        tmp_path,
        lambda _locator, _target: payload,
        root_name="public-pilot",
    )

    assert (receipt.root / "fixture/public-pilot.json").read_bytes() == payload
    assert receipt.as_dict()["status"] == "materialized-bytes-verified"
    assert "private" in (
        ROOT / "imports/materialized/naturebench-4b51202/BLOCKER.md"
    ).read_text(encoding="utf-8")


def test_materialize_cli_uses_a_local_provider_mirror_without_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = b"local mirror bytes"
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "report.json").write_bytes(payload)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            _spec(
                _file(
                    payload,
                    destination="report.json",
                    role="report",
                    locator="fixture://local/report.json",
                )
            )
        ),
        encoding="utf-8",
    )

    assert (
        collab_main(
            (
                "--project-root",
                str(ROOT),
                "materialize",
                "--manifest",
                str(manifest),
                "--source-root",
                str(source_root),
                "--destination-parent",
                str(tmp_path / "out"),
                "--root-name",
                "cli-pilot",
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["receipt"]["non_claim"] is True
    assert output["root"] == "<materialization-root>"
    assert output["root_name"] == "cli-pilot"
    assert (tmp_path / "out/cli-pilot/report.json").read_bytes() == payload
