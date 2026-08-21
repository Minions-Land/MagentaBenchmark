"""A narrow, receipt-only boundary for the Magenta Experiment MCP service.

This module deliberately does not discover, start, stop, or supervise a
service.  A caller injects a transport implementation and receives bounded
responses.  The returned receipt is a handoff document; BMP report
verification and the lab/ledger flow remain the only result authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping, NoReturn, Protocol, cast


SUPERVISOR_RECEIPT_FORMAT = "magentabench-magenta-supervisor-receipt-v1"
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_REPORT_BYTES = 256 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "blocked", "lost-worker"})
_STANDALONE = frozenset({"not-run", "failed", "verified"})
_REPORT_NAMES = frozenset({"claim_report.json", "observation_report.json"})
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
        "api_key",
        "api_token",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "secret_value",
    }
)


class SupervisorMcpError(ValueError):
    """Stable, non-secret adapter failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SupervisorMcpTransport(Protocol):
    """Injected RPC transport; implementations own socket/HTTP details."""

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _fail(code: str) -> NoReturn:
    raise SupervisorMcpError(code)


def _exact(value: Any, fields: frozenset[str], code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _fail(code)
    return value


def _string(value: Any, code: str, *, maximum: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        _fail(code)
    return value


def _identifier(value: Any, code: str) -> str:
    value = _string(value, code, maximum=128)
    if _IDENTIFIER.fullmatch(value) is None:
        _fail(code)
    return value


def _digest(value: Any, code: str = "invalid-digest") -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(code)
    return value


def _reject_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = (
                key.lower().replace("-", "_") if isinstance(key, str) else ""
            )
            if isinstance(key, str) and (
                normalized_key in _SECRET_KEYS
                or normalized_key.endswith(
                    ("_token", "_secret", "_password", "_credential", "_api_key")
                )
            ):
                _fail("secret-bearing-response")
            _reject_secrets(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secrets(child)


def _bounded_mapping(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("response-not-object")
    try:
        _reject_secrets(value)
    except RecursionError:
        _fail("response-invalid-json")
    try:
        encoded = json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError, RecursionError):
        _fail("response-invalid-json")
    if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
        _fail("response-too-large")
    return value


def _relative_path(value: Any, code: str) -> PurePosixPath:
    normalized = _string(value, code, maximum=4096)
    if "\\" in normalized or "://" in normalized or normalized.startswith("/"):
        _fail(code)
    path = PurePosixPath(normalized)
    if (
        not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != normalized
    ):
        _fail(code)
    return path


def _verify_report_bytes(
    report_path: Path, *, expected_size: int, expected_digest: str
) -> None:
    """Verify one regular report without opening a special file or link.

    ``O_NONBLOCK`` keeps a raced FIFO from hanging the validator.  The
    descriptor is checked after opening as well as before hashing, so a
    replacement cannot silently turn a report into a device or hardlink.
    Shared-adversarial TOCTOU races remain outside this trusted-tree boundary.
    """

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(report_path, flags)
    except OSError:
        _fail("report-path-invalid")
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode) or descriptor_stat.st_nlink != 1:
            _fail("report-path-invalid")
        if descriptor_stat.st_size > MAX_REPORT_BYTES:
            _fail("report-size-invalid")
        if descriptor_stat.st_size != expected_size:
            _fail("report-size-mismatch")

        digest = hashlib.sha256()
        observed_size = 0
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                observed_size += len(chunk)
                if observed_size > MAX_REPORT_BYTES:
                    _fail("report-size-invalid")
                digest.update(chunk)
        if observed_size != expected_size:
            _fail("report-size-mismatch")
        if digest.hexdigest() != expected_digest:
            _fail("report-digest-mismatch")
    except SupervisorMcpError:
        raise
    except OSError:
        _fail("report-path-invalid")
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass(frozen=True)
class SupervisorExperimentRequest:
    """Frozen identity submitted to an external Supervisor."""

    experiment_id: str
    bmp_spec_sha256: str
    manifest_digest: str
    dataset_sha256: str
    evaluator_sha256: str
    config_sha256: str
    record_root: str
    magenta_code_commit: str
    magenta_interface_version: str
    profile_sha256: str
    deployment_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "bmp": {
                "config_sha256": self.config_sha256,
                "dataset_sha256": self.dataset_sha256,
                "evaluator_sha256": self.evaluator_sha256,
                "manifest_digest": self.manifest_digest,
                "spec_sha256": self.bmp_spec_sha256,
            },
            "magenta": {
                "code_commit": self.magenta_code_commit,
                "interface_version": self.magenta_interface_version,
            },
            "record_root": self.record_root,
            "supervisor": {
                "deployment_sha256": self.deployment_sha256,
                "profile_sha256": self.profile_sha256,
            },
        }


@dataclass(frozen=True)
class SupervisorReceipt:
    """Validated terminal handoff, intentionally never a claim."""

    experiment_id: str
    run_id: str
    record_root: str
    terminal_state: str
    report_locator: str
    report_sha256: str
    report_size_bytes: int
    standalone_verification: str
    profile_sha256: str
    deployment_sha256: str
    magenta_code_commit: str
    magenta_interface_version: str
    bmp_spec_sha256: str
    manifest_digest: str
    dataset_sha256: str
    evaluator_sha256: str
    config_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bmp": {
                "config_sha256": self.config_sha256,
                "dataset_sha256": self.dataset_sha256,
                "evaluator_sha256": self.evaluator_sha256,
                "manifest_digest": self.manifest_digest,
                "spec_sha256": self.bmp_spec_sha256,
            },
            "claim_eligible": False,
            "format": SUPERVISOR_RECEIPT_FORMAT,
            "magenta": {
                "code_commit": self.magenta_code_commit,
                "interface_version": self.magenta_interface_version,
            },
            "record_root": self.record_root,
            "record_root_fresh": True,
            "report": {
                "locator": self.report_locator,
                "sha256": self.report_sha256,
                "size_bytes": self.report_size_bytes,
            },
            "run_id": self.run_id,
            "standalone_verification": self.standalone_verification,
            "supervisor": {
                "deployment_sha256": self.deployment_sha256,
                "experiment_id": self.experiment_id,
                "profile_sha256": self.profile_sha256,
            },
            "terminal_state": self.terminal_state,
        }


def validate_supervisor_receipt(
    value: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
    artifact_base: Path | None = None,
) -> SupervisorReceipt:
    """Validate a receipt and optionally verify its report bytes.

    Freshness is a submit-time assertion and cannot be proved after the fact;
    callers must run their pre-submit ``check_run_root --require-new`` gate.
    When supplied, ``artifact_root`` must already be the resolved durable root
    named by ``record_root``.  Alternatively, ``artifact_base`` explicitly
    names the parent under which the relative ``record_root`` is resolved;
    callers must not provide both forms.
    """

    root = _exact(
        _bounded_mapping(value),
        frozenset(
            {
                "bmp",
                "claim_eligible",
                "format",
                "magenta",
                "record_root",
                "record_root_fresh",
                "report",
                "run_id",
                "standalone_verification",
                "supervisor",
                "terminal_state",
            }
        ),
        "receipt-fields-invalid",
    )
    if root["format"] != SUPERVISOR_RECEIPT_FORMAT:
        _fail("unsupported-format")
    supervisor = _exact(
        root["supervisor"],
        frozenset({"deployment_sha256", "experiment_id", "profile_sha256"}),
        "supervisor-identity-invalid",
    )
    experiment_id = _identifier(
        supervisor["experiment_id"], "supervisor-identity-invalid"
    )
    profile_sha256 = _digest(supervisor["profile_sha256"])
    deployment_sha256 = _digest(supervisor["deployment_sha256"])
    magenta = _exact(
        root["magenta"],
        frozenset({"code_commit", "interface_version"}),
        "magenta-identity-invalid",
    )
    if (
        type(magenta["code_commit"]) is not str
        or _COMMIT.fullmatch(magenta["code_commit"]) is None
    ):
        _fail("magenta-identity-invalid")
    interface_version = _identifier(
        magenta["interface_version"], "magenta-identity-invalid"
    )
    bmp = _exact(
        root["bmp"],
        frozenset(
            {
                "config_sha256",
                "dataset_sha256",
                "evaluator_sha256",
                "manifest_digest",
                "spec_sha256",
            }
        ),
        "bmp-identity-invalid",
    )
    bmp_spec_sha256 = _digest(bmp["spec_sha256"])
    manifest_digest = _digest(bmp["manifest_digest"])
    dataset_sha256 = _digest(bmp["dataset_sha256"])
    evaluator_sha256 = _digest(bmp["evaluator_sha256"])
    config_sha256 = _digest(bmp["config_sha256"])
    run_id = _identifier(root["run_id"], "run-identity-invalid")
    if root["record_root_fresh"] is not True:
        _fail("record-root-not-fresh")
    record_root = _relative_path(root["record_root"], "record-root-invalid")
    if ".runs" in record_root.parts:
        _fail("scratch-record-root")
    if type(root["claim_eligible"]) is not bool:
        _fail("claim-state-invalid")
    if root["claim_eligible"]:
        _fail("claim-eligibility-derived")
    terminal_state = root["terminal_state"]
    if type(terminal_state) is not str or terminal_state not in _TERMINAL:
        _fail("nonterminal-state")
    standalone = root["standalone_verification"]
    if type(standalone) is not str or standalone not in _STANDALONE:
        _fail("standalone-state-invalid")
    report = _exact(
        root["report"],
        frozenset({"locator", "sha256", "size_bytes"}),
        "report-binding-invalid",
    )
    report_locator = _relative_path(report["locator"], "report-path-invalid")
    if report_locator.name not in _REPORT_NAMES:
        _fail("report-path-invalid")
    report_sha256 = _digest(report["sha256"])
    size = report["size_bytes"]
    if type(size) is not int or size < 0 or size > MAX_REPORT_BYTES:
        _fail("report-size-invalid")
    if artifact_root is not None and artifact_base is not None:
        _fail("artifact-root-ambiguous")
    if artifact_root is not None or artifact_base is not None:
        try:
            if artifact_base is not None:
                base_path = artifact_base.resolve(strict=True)
                if not base_path.is_dir():
                    _fail("record-root-invalid")
                root_path = base_path.joinpath(*record_root.parts)
                root_path.resolve(strict=True).relative_to(base_path)
            else:
                if artifact_root is None:
                    _fail("record-root-invalid")
                root_path = artifact_root.resolve(strict=True)
        except (OSError, ValueError):
            _fail("record-root-invalid")
        if not root_path.is_dir():
            _fail("record-root-invalid")
        report_path = root_path.joinpath(*report_locator.parts)
        try:
            report_path.resolve(strict=True).relative_to(root_path)
        except (OSError, ValueError):
            _fail("report-path-invalid")
        if report_path.is_symlink() or not report_path.is_file():
            _fail("report-path-invalid")
        _verify_report_bytes(
            report_path, expected_size=size, expected_digest=report_sha256
        )
    return SupervisorReceipt(
        experiment_id=experiment_id,
        run_id=run_id,
        record_root=record_root.as_posix(),
        terminal_state=terminal_state,
        report_locator=report_locator.as_posix(),
        report_sha256=report_sha256,
        report_size_bytes=size,
        standalone_verification=standalone,
        profile_sha256=profile_sha256,
        deployment_sha256=deployment_sha256,
        magenta_code_commit=magenta["code_commit"],
        magenta_interface_version=interface_version,
        bmp_spec_sha256=bmp_spec_sha256,
        manifest_digest=manifest_digest,
        dataset_sha256=dataset_sha256,
        evaluator_sha256=evaluator_sha256,
        config_sha256=config_sha256,
    )


@dataclass(frozen=True)
class SupervisorStatus:
    experiment_id: str
    state: str
    sequence: int
    terminal_state: str | None = None
    receipt: SupervisorReceipt | None = None


class SupervisorMcpClient:
    """Transport-neutral submit/status/watch facade.

    The client never writes the lab ledger and never treats event logs as
    metrics.  A service implementation may expose a Unix socket, HTTP, or a
    native MCP transport behind :class:`SupervisorMcpTransport`.
    """

    def __init__(self, transport: SupervisorMcpTransport) -> None:
        self._transport = transport

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        response = _bounded_mapping(self._transport.request(method, params))
        if set(response) != {"result"}:
            _fail("rpc-response-invalid")
        return _bounded_mapping(response["result"])

    def submit(self, request: SupervisorExperimentRequest) -> SupervisorReceipt:
        _validate_submit_request(request)
        result = self._request("experiment_submit", request.as_dict())
        receipt = validate_supervisor_receipt(result)
        _validate_receipt_binding(request, receipt)
        return receipt

    def status(
        self,
        experiment_id: str,
        *,
        expected_request: SupervisorExperimentRequest | None = None,
    ) -> SupervisorStatus:
        requested_experiment_id = _identifier(experiment_id, "experiment-id-invalid")
        if expected_request is not None:
            _validate_submit_request(expected_request)
            if expected_request.experiment_id != requested_experiment_id:
                _fail("status-experiment-mismatch")
        result = self._request(
            "experiment_status",
            {"experiment_id": requested_experiment_id},
        )
        status = _status(result)
        if status.experiment_id != requested_experiment_id:
            _fail("status-experiment-mismatch")
        if status.receipt is not None and expected_request is not None:
            _validate_receipt_binding(expected_request, status.receipt)
        return status

    def watch(
        self,
        experiment_id: str,
        *,
        after_sequence: int = 0,
        timeout_ms: int = 25_000,
        expected_request: SupervisorExperimentRequest | None = None,
    ) -> tuple[SupervisorStatus, ...]:
        if type(after_sequence) is not int or after_sequence < 0:
            _fail("watch-sequence-invalid")
        if type(timeout_ms) is not int or timeout_ms < 1 or timeout_ms > 25_000:
            _fail("watch-timeout-invalid")
        requested_experiment_id = _identifier(experiment_id, "experiment-id-invalid")
        if expected_request is not None:
            _validate_submit_request(expected_request)
            if expected_request.experiment_id != requested_experiment_id:
                _fail("watch-experiment-mismatch")
        result = self._request(
            "experiment_watch",
            {
                "after_sequence": after_sequence,
                "experiment_id": requested_experiment_id,
                "timeout_ms": timeout_ms,
            },
        )
        events = result.get("events")
        if type(events) is not list or len(events) > 100:
            _fail("watch-events-invalid")
        statuses: list[SupervisorStatus] = []
        previous_sequence = after_sequence
        for event in cast(list[Any], events):
            if type(event) is not dict:
                _fail("watch-events-invalid")
            status = _status(event)
            if status.experiment_id != requested_experiment_id:
                _fail("watch-experiment-mismatch")
            if status.sequence <= previous_sequence:
                _fail("watch-sequence-order")
            if status.receipt is not None and expected_request is not None:
                _validate_receipt_binding(expected_request, status.receipt)
            previous_sequence = status.sequence
            statuses.append(status)
        return tuple(statuses)


def _status(value: Mapping[str, Any]) -> SupervisorStatus:
    item = _bounded_mapping(value)
    allowed = {"experiment_id", "receipt", "sequence", "state", "terminal_state"}
    if set(item) - allowed or not {"experiment_id", "sequence", "state"} <= set(item):
        _fail("status-response-invalid")
    experiment_id = _identifier(item["experiment_id"], "status-response-invalid")
    state = _identifier(item["state"], "status-response-invalid")
    sequence = item["sequence"]
    if type(sequence) is not int or sequence < 0:
        _fail("status-sequence-invalid")
    terminal = item.get("terminal_state")
    if terminal is not None and (
        type(terminal) is not str or terminal not in _TERMINAL
    ):
        _fail("status-terminal-state-invalid")
    receipt_value = item.get("receipt")
    receipt = (
        None if receipt_value is None else validate_supervisor_receipt(receipt_value)
    )
    if receipt is not None and receipt.experiment_id != experiment_id:
        _fail("status-experiment-mismatch")
    return SupervisorStatus(experiment_id, state, sequence, terminal, receipt)


def _validate_submit_request(request: SupervisorExperimentRequest) -> None:
    """Validate the frozen request before it crosses an external boundary."""

    _identifier(request.experiment_id, "experiment-id-invalid")
    _digest(request.bmp_spec_sha256)
    _digest(request.manifest_digest)
    _digest(request.dataset_sha256)
    _digest(request.evaluator_sha256)
    _digest(request.config_sha256)
    _digest(request.profile_sha256)
    _digest(request.deployment_sha256)
    if (
        type(request.magenta_code_commit) is not str
        or _COMMIT.fullmatch(request.magenta_code_commit) is None
    ):
        _fail("magenta-identity-invalid")
    _identifier(request.magenta_interface_version, "magenta-identity-invalid")
    root = _relative_path(request.record_root, "record-root-invalid")
    if ".runs" in root.parts:
        _fail("scratch-record-root")


def _validate_receipt_binding(
    request: SupervisorExperimentRequest, receipt: SupervisorReceipt
) -> None:
    """Keep a Supervisor response on the exact identity submitted by a caller."""

    fields = (
        ("experiment_id", request.experiment_id, receipt.experiment_id),
        ("record_root", request.record_root, receipt.record_root),
        (
            "magenta_code_commit",
            request.magenta_code_commit,
            receipt.magenta_code_commit,
        ),
        (
            "magenta_interface_version",
            request.magenta_interface_version,
            receipt.magenta_interface_version,
        ),
        ("profile_sha256", request.profile_sha256, receipt.profile_sha256),
        ("deployment_sha256", request.deployment_sha256, receipt.deployment_sha256),
        ("bmp_spec_sha256", request.bmp_spec_sha256, receipt.bmp_spec_sha256),
        ("manifest_digest", request.manifest_digest, receipt.manifest_digest),
        ("dataset_sha256", request.dataset_sha256, receipt.dataset_sha256),
        ("evaluator_sha256", request.evaluator_sha256, receipt.evaluator_sha256),
        ("config_sha256", request.config_sha256, receipt.config_sha256),
    )
    if any(expected != actual for _, expected, actual in fields):
        _fail("receipt-identity-mismatch")


def serialize_supervisor_receipt(receipt: SupervisorReceipt) -> str:
    """Return one deterministic JSON representation for durable handoff."""

    return (
        json.dumps(
            receipt.as_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


__all__ = [
    "MAX_REPORT_BYTES",
    "MAX_RESPONSE_BYTES",
    "SUPERVISOR_RECEIPT_FORMAT",
    "SupervisorExperimentRequest",
    "SupervisorMcpClient",
    "SupervisorMcpError",
    "SupervisorMcpTransport",
    "SupervisorReceipt",
    "SupervisorStatus",
    "serialize_supervisor_receipt",
    "validate_supervisor_receipt",
]
