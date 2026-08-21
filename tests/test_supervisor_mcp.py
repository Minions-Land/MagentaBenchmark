from __future__ import annotations

import hashlib
import os
from pathlib import Path
from dataclasses import replace
from typing import Any, Mapping, cast

import pytest

from MagentaBench.collab.supervisor_mcp import (
    ExperimentExecutionRequest,
    MagentaExperimentServiceError,
    MagentaExperimentTransportError,
    MagentaExperimentWireClient,
    MagentaExperimentWireError,
    SupervisorExperimentRequest,
    SupervisorMcpClient,
    SupervisorMcpError,
    SUPERVISOR_RECEIPT_FORMAT,
    serialize_supervisor_receipt,
    validate_terminal_receipt,
    validate_supervisor_receipt,
)


SHA = "a" * 64


def _request() -> SupervisorExperimentRequest:
    return SupervisorExperimentRequest(
        experiment_id="exp-001",
        bmp_spec_sha256=SHA,
        manifest_digest="b" * 64,
        dataset_sha256="c" * 64,
        evaluator_sha256="d" * 64,
        config_sha256="e" * 64,
        record_root="records/exp-001/run-001",
        magenta_code_commit="f" * 40,
        magenta_interface_version="experiment-mcp.v1",
        profile_sha256="1" * 64,
        deployment_sha256="2" * 64,
    )


def _receipt(content: bytes = b"report\n") -> dict[str, Any]:
    return {
        "bmp": {
            "config_sha256": "e" * 64,
            "dataset_sha256": "c" * 64,
            "evaluator_sha256": "d" * 64,
            "manifest_digest": "b" * 64,
            "spec_sha256": SHA,
        },
        "claim_eligible": False,
        "format": SUPERVISOR_RECEIPT_FORMAT,
        "magenta": {
            "code_commit": "f" * 40,
            "interface_version": "experiment-mcp.v1",
        },
        "record_root": "records/exp-001/run-001",
        "record_root_fresh": True,
        "report": {
            "locator": "observation_report.json",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
        "run_id": "run-001",
        "standalone_verification": "verified",
        "supervisor": {
            "deployment_sha256": "2" * 64,
            "experiment_id": "exp-001",
            "profile_sha256": "1" * 64,
        },
        "terminal_state": "succeeded",
    }


class FakeTransport:
    def __init__(self, responses: Mapping[str, Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((method, params))
        return {"result": self.responses[method]}


class FakeMagentaTransport:
    def __init__(
        self, responses: Mapping[str, Mapping[str, Any] | BaseException]
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, BaseException):
            raise response
        return response


def _execution_request() -> ExperimentExecutionRequest:
    return ExperimentExecutionRequest(
        experiment_id="exp-wire-001",
        command="python train.py",
        cwd="/workspace",
        gpu_count=2,
        name="training",
        timeout_seconds=3600,
    )


def test_magenta_wire_client_maps_the_pinned_lifecycle_without_receipt_projection() -> (
    None
):
    transport = FakeMagentaTransport(
        {
            "service_status": {"state": "running", "profile_name": "h20"},
            "experiment_submit": {"accepted": True, "experiment_id": "exp-wire-001"},
            "experiment_status": {"experiments": [], "deployment_id": "opaque"},
            "experiment_watch": {"events": [], "timed_out": True},
            "experiment_cancel": {"accepted": True, "state": "stopping"},
            "experiment_retry": {"accepted": True, "state": "pending"},
        }
    )
    client = MagentaExperimentWireClient(transport)

    service = client.service_status()
    accepted = client.submit(_execution_request())
    status = client.status("exp-wire-001", limit=10, offset=2)
    watched = client.watch("exp-wire-001", after_sequence=7, timeout_ms=25000)
    cancelled = client.cancel("exp-wire-001")
    retried = client.retry("exp-wire-001", "transient node failure")

    assert service.result["state"] == "running"
    assert accepted.experiment_id == "exp-wire-001"
    assert accepted.result["accepted"] is True
    assert "claim_eligible" not in accepted.result
    assert status.result["deployment_id"] == "opaque"
    assert watched.result["timed_out"] is True
    assert cancelled.result["state"] == "stopping"
    assert retried.result["state"] == "pending"
    assert [method for method, _ in transport.calls] == [
        "service_status",
        "experiment_submit",
        "experiment_status",
        "experiment_watch",
        "experiment_cancel",
        "experiment_retry",
    ]
    assert transport.calls[1][1] == {
        "command": "python train.py",
        "cwd": "/workspace",
        "experiment_id": "exp-wire-001",
        "gpu_count": 2,
        "name": "training",
        "timeout_seconds": 3600,
    }
    assert transport.calls[2][1] == {
        "experiment_id": "exp-wire-001",
        "limit": 10,
        "offset": 2,
    }
    assert transport.calls[3][1] == {
        "experiment_id": "exp-wire-001",
        "after_sequence": 7,
        "timeout_seconds": 25.0,
    }
    assert transport.calls[4][1] == {"experiment_id": "exp-wire-001"}
    assert transport.calls[5][1] == {
        "experiment_id": "exp-wire-001",
        "reason": "transient node failure",
    }


def test_magenta_wire_preserves_multiline_commands() -> None:
    transport = FakeMagentaTransport({"experiment_submit": {"accepted": True}})
    request = ExperimentExecutionRequest(
        experiment_id="exp-wire-001",
        command="printf 'first\\n';\nsleep 1",
        cwd="/workspace",
        gpu_count=1,
    )

    MagentaExperimentWireClient(transport).submit(request)

    assert transport.calls[0][1]["command"] == request.command


def test_magenta_wire_submit_keeps_identity_local_and_rejects_response_drift() -> None:
    request = _execution_request()
    transport = FakeMagentaTransport(
        {"experiment_submit": {"accepted": True, "experiment_id": "other"}}
    )

    with pytest.raises(MagentaExperimentWireError, match="identity-mismatch"):
        MagentaExperimentWireClient(transport).submit(request)

    assert set(transport.calls[0][1]) == {
        "experiment_id",
        "command",
        "cwd",
        "gpu_count",
        "name",
        "timeout_seconds",
    }
    assert not {"bmp", "magenta", "supervisor", "record_root"}.intersection(
        transport.calls[0][1]
    )


def test_magenta_wire_binds_optional_immutable_identity_context_without_sending_it() -> (
    None
):
    identity = replace(_request(), experiment_id="exp-wire-001")
    transport = FakeMagentaTransport({"experiment_submit": {"accepted": True}})

    accepted = MagentaExperimentWireClient(transport).submit(
        _execution_request(), identity_context=identity
    )

    assert accepted.identity_context == identity
    assert "bmp" not in transport.calls[0][1]

    with pytest.raises(SupervisorMcpError, match="identity-context-mismatch"):
        MagentaExperimentWireClient(
            FakeMagentaTransport({"experiment_submit": {"accepted": True}})
        ).submit(_execution_request(), identity_context=_request())


def test_terminal_receipt_requires_the_immutable_bmp_identity_context() -> None:
    identity = _request()
    receipt = _receipt()

    validated = validate_terminal_receipt(receipt, identity_context=identity)
    assert validated.experiment_id == identity.experiment_id

    drifted = {**receipt, "record_root": "records/other/run-001"}
    with pytest.raises(SupervisorMcpError, match="receipt-identity-mismatch"):
        validate_terminal_receipt(drifted, identity_context=identity)


def test_magenta_wire_service_rejection_is_nonretryable() -> None:
    transport = FakeMagentaTransport(
        {
            "experiment_cancel": MagentaExperimentServiceError("not owner"),
        }
    )

    with pytest.raises(MagentaExperimentWireError) as raised:
        MagentaExperimentWireClient(transport).cancel("exp-wire-001")

    assert raised.value.code == "service"
    assert raised.value.retryable is False
    assert raised.value.outcome_unknown is False


@pytest.mark.parametrize(
    "method", ["experiment_status", "experiment_watch", "experiment_cancel"]
)
def test_magenta_wire_rejects_optional_response_identity_drift(method: str) -> None:
    transport = FakeMagentaTransport({method: {"experiment_id": "other"}})
    client = MagentaExperimentWireClient(transport)

    with pytest.raises(MagentaExperimentWireError, match="identity-mismatch"):
        if method == "experiment_status":
            client.status("exp-wire-001")
        elif method == "experiment_watch":
            client.watch("exp-wire-001", after_sequence=0)
        else:
            client.cancel("exp-wire-001")


def test_magenta_wire_read_response_must_be_a_bounded_object() -> None:
    transport = FakeMagentaTransport(
        {"experiment_status": cast(Any, ["not-an-object"])}
    )

    with pytest.raises(MagentaExperimentWireError) as raised:
        MagentaExperimentWireClient(transport).status("exp-wire-001")

    assert raised.value.code == "invalid_response"
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("method", "call"),
    [
        (
            "experiment_submit",
            lambda client: client.submit(_execution_request()),
        ),
        ("experiment_cancel", lambda client: client.cancel("exp-wire-001")),
        (
            "experiment_retry",
            lambda client: client.retry("exp-wire-001", "retry"),
        ),
    ],
)
def test_magenta_wire_mutation_dispatch_failure_is_outcome_unknown(
    method: str, call: Any
) -> None:
    transport = FakeMagentaTransport(
        {
            method: MagentaExperimentTransportError(
                "connection lost", request_dispatched=True
            )
        }
    )

    with pytest.raises(MagentaExperimentWireError) as raised:
        call(MagentaExperimentWireClient(transport))

    assert raised.value.code == "transport"
    assert raised.value.retryable is False
    assert raised.value.outcome_unknown is True


def test_magenta_wire_read_failure_is_retryable_only_before_dispatch() -> None:
    transport = FakeMagentaTransport(
        {
            "experiment_status": MagentaExperimentTransportError(
                "socket unavailable", request_dispatched=False
            )
        }
    )

    with pytest.raises(MagentaExperimentWireError) as raised:
        MagentaExperimentWireClient(transport).status("exp-wire-001")

    assert raised.value.code == "unavailable"
    assert raised.value.retryable is True
    assert raised.value.outcome_unknown is False


def test_magenta_wire_rejects_unbounded_execution_inputs() -> None:
    client = MagentaExperimentWireClient(
        FakeMagentaTransport({"experiment_status": {}})
    )

    with pytest.raises(SupervisorMcpError, match="gpu-count-invalid"):
        client.submit(
            ExperimentExecutionRequest(
                experiment_id="exp-wire-001",
                command="echo ok",
                cwd="/workspace",
                gpu_count=9,
            )
        )
    with pytest.raises(SupervisorMcpError, match="watch-timeout-invalid"):
        client.watch("exp-wire-001", after_sequence=0, timeout_ms=25_001)
    with pytest.raises(SupervisorMcpError, match="retry-reason-invalid"):
        client.retry("exp-wire-001", " ")


def test_submit_status_watch_are_bounded_and_transport_neutral() -> None:
    receipt = _receipt()
    transport = FakeTransport(
        {
            "experiment_submit": receipt,
            "experiment_status": {
                "experiment_id": "exp-001",
                "sequence": 3,
                "state": "finished",
                "terminal_state": "succeeded",
                "receipt": receipt,
            },
            "experiment_watch": {
                "events": [
                    {
                        "experiment_id": "exp-001",
                        "sequence": 4,
                        "state": "finished",
                        "terminal_state": "succeeded",
                    }
                ]
            },
        }
    )
    client = SupervisorMcpClient(transport)

    submitted = client.submit(_request())
    status = client.status("exp-001")
    events = client.watch("exp-001", after_sequence=status.sequence)

    assert submitted.as_dict()["claim_eligible"] is False
    assert status.receipt == submitted
    assert events[0].sequence == 4
    assert [call[0] for call in transport.calls] == [
        "experiment_submit",
        "experiment_status",
        "experiment_watch",
    ]
    assert transport.calls[-1][1]["after_sequence"] == 3


def test_submit_rejects_response_identity_drift() -> None:
    receipt = _receipt()
    receipt["bmp"] = {**receipt["bmp"], "dataset_sha256": "9" * 64}
    transport = FakeTransport({"experiment_submit": receipt})

    with pytest.raises(SupervisorMcpError, match="receipt-identity-mismatch"):
        SupervisorMcpClient(transport).submit(_request())


def test_status_rejects_response_for_another_experiment() -> None:
    transport = FakeTransport(
        {
            "experiment_status": {
                "experiment_id": "other-exp",
                "sequence": 4,
                "state": "finished",
            }
        }
    )

    with pytest.raises(SupervisorMcpError, match="status-experiment-mismatch"):
        SupervisorMcpClient(transport).status("exp-001")


def test_status_can_bind_a_returned_receipt_to_the_submit_request() -> None:
    receipt = _receipt()
    receipt["bmp"] = {**receipt["bmp"], "config_sha256": "9" * 64}
    transport = FakeTransport(
        {
            "experiment_status": {
                "experiment_id": "exp-001",
                "sequence": 4,
                "state": "finished",
                "receipt": receipt,
            }
        }
    )

    with pytest.raises(SupervisorMcpError, match="receipt-identity-mismatch"):
        SupervisorMcpClient(transport).status("exp-001", expected_request=_request())


def test_receipt_serialization_is_canonical() -> None:
    serialized = serialize_supervisor_receipt(validate_supervisor_receipt(_receipt()))

    assert serialized.endswith("\n")
    assert serialized == serialize_supervisor_receipt(
        validate_supervisor_receipt(_receipt())
    )
    assert serialized.index('"bmp"') < serialized.index('"format"')


def test_recursive_or_excessively_deep_response_fails_as_invalid_json() -> None:
    payload = _receipt()
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(1500):
        child: dict[str, Any] = {}
        cursor["nested"] = child
        cursor = child
    payload["nested"] = nested
    with pytest.raises(SupervisorMcpError, match="response-invalid-json"):
        validate_supervisor_receipt(payload)

    cyclic = _receipt()
    cyclic["cycle"] = cyclic
    with pytest.raises(SupervisorMcpError, match="response-invalid-json"):
        validate_supervisor_receipt(cyclic)


def test_receipt_report_bytes_are_checked_when_artifact_root_is_given(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records/exp-001/run-001"
    root.mkdir(parents=True)
    content = b"report\n"
    (root / "observation_report.json").write_bytes(content)
    value = _receipt(content)

    validated = validate_supervisor_receipt(value, artifact_root=root)

    assert validated.report_size_bytes == len(content)
    assert validated.report_sha256 == hashlib.sha256(content).hexdigest()

    (root / "observation_report.json").write_bytes(b"drift!\n")
    with pytest.raises(SupervisorMcpError, match="report-digest-mismatch"):
        validate_supervisor_receipt(value, artifact_root=root)


def test_artifact_base_resolves_receipt_record_root(tmp_path: Path) -> None:
    root = tmp_path / "records/exp-001/run-001"
    root.mkdir(parents=True)
    content = b"report\n"
    (root / "observation_report.json").write_bytes(content)
    value = _receipt(content)

    assert validate_supervisor_receipt(value, artifact_base=tmp_path).record_root == (
        "records/exp-001/run-001"
    )
    with pytest.raises(SupervisorMcpError, match="artifact-root-ambiguous"):
        validate_supervisor_receipt(value, artifact_root=root, artifact_base=tmp_path)


@pytest.mark.parametrize(
    "locator", ["/observation_report.json", "../observation_report.json"]
)
def test_report_locator_cannot_escape_or_be_absolute(locator: str) -> None:
    payload = _receipt()
    payload["report"] = {**payload["report"], "locator": locator}

    with pytest.raises(SupervisorMcpError, match="report-path-invalid"):
        validate_supervisor_receipt(payload)


def test_report_symlink_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "records/exp-001/run-001"
    root.mkdir(parents=True)
    target = root / "real-report.json"
    target.write_bytes(b"report\n")
    report = root / "observation_report.json"
    report.symlink_to(target.name)

    with pytest.raises(SupervisorMcpError, match="report-path-invalid"):
        validate_supervisor_receipt(_receipt(), artifact_root=root)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("claim_eligible", True, "claim-eligibility-derived"),
        ("terminal_state", "running", "nonterminal-state"),
        ("record_root", ".runs/run-001", "scratch-record-root"),
        ("record_root_fresh", False, "record-root-not-fresh"),
    ],
)
def test_receipt_boundary_fails_closed(field: str, value: Any, error: str) -> None:
    payload = _receipt()
    payload[field] = value

    with pytest.raises(SupervisorMcpError, match=error):
        validate_supervisor_receipt(payload)


def test_secret_key_and_invalid_watch_limits_fail_closed() -> None:
    payload = _receipt()
    payload["secret"] = "do-not-copy"
    with pytest.raises(SupervisorMcpError, match="secret-bearing-response"):
        validate_supervisor_receipt(payload)

    payload = _receipt()
    payload["api_key"] = "do-not-copy"
    with pytest.raises(SupervisorMcpError, match="secret-bearing-response"):
        validate_supervisor_receipt(payload)

    transport = FakeTransport({"experiment_watch": {"events": []}})
    client = SupervisorMcpClient(transport)
    with pytest.raises(SupervisorMcpError, match="watch-timeout-invalid"):
        client.watch("exp-001", timeout_ms=25_001)


@pytest.mark.parametrize(
    ("events", "error"),
    [
        (
            [
                {
                    "experiment_id": "other-exp",
                    "sequence": 4,
                    "state": "finished",
                }
            ],
            "watch-experiment-mismatch",
        ),
        (
            [
                {"experiment_id": "exp-001", "sequence": 3, "state": "queued"},
            ],
            "watch-sequence-order",
        ),
        (
            [
                {"experiment_id": "exp-001", "sequence": 4, "state": "queued"},
                {"experiment_id": "exp-001", "sequence": 4, "state": "running"},
            ],
            "watch-sequence-order",
        ),
    ],
)
def test_watch_binds_experiment_and_monotonic_cursor(
    events: list[dict[str, Any]], error: str
) -> None:
    client = SupervisorMcpClient(
        FakeTransport({"experiment_watch": {"events": events}})
    )

    with pytest.raises(SupervisorMcpError, match=error):
        client.watch("exp-001", after_sequence=3)


def test_report_hardlink_and_oversize_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "records/exp-001/run-001"
    root.mkdir(parents=True)
    content = b"report\n"
    report = root / "subdir/observation_report.json"
    report.parent.mkdir()
    report.write_bytes(content)
    hardlink = root / "subdir/hardlink-target.json"
    os.link(report, hardlink)
    value = _receipt(content)

    with pytest.raises(SupervisorMcpError, match="report-path-invalid"):
        validate_supervisor_receipt(
            {
                **value,
                "report": {
                    **value["report"],
                    "locator": "subdir/observation_report.json",
                },
            },
            artifact_root=root,
        )
    with pytest.raises(SupervisorMcpError, match="report-size-invalid"):
        validate_supervisor_receipt(
            {
                **value,
                "report": {**value["report"], "size_bytes": 256 * 1024 * 1024 + 1},
            }
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not supported")
def test_report_fifo_fails_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "records/exp-001/run-001"
    root.mkdir(parents=True)
    os.mkfifo(root / "observation_report.json")

    with pytest.raises(SupervisorMcpError, match="report-path-invalid"):
        validate_supervisor_receipt(_receipt(), artifact_root=root)
