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
MAX_EXPERIMENT_GPUS = 8
MAX_EXPERIMENT_TIMEOUT_SECONDS = 604_800
MAX_EXPERIMENT_WATCH_MS = 25_000
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


class MagentaExperimentTransportError(RuntimeError):
    """Transport failure with an explicit dispatch boundary.

    A transport must set ``request_dispatched`` when it cannot determine
    whether the Supervisor received a mutation.  This keeps retry decisions
    out of the bridge and prevents duplicate submit/cancel/retry operations.
    """

    def __init__(self, code: str, *, request_dispatched: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.request_dispatched = request_dispatched


class MagentaExperimentServiceError(RuntimeError):
    """Supervisor rejected a request after parsing it."""


class MagentaExperimentWireError(RuntimeError):
    """Stable, non-secret error for the Magenta Experiment wire boundary."""

    def __init__(
        self,
        code: str,
        *,
        method: str,
        experiment_id: str | None = None,
        retryable: bool,
        outcome_unknown: bool = False,
    ) -> None:
        detail = f"{code}:{method}"
        if experiment_id is not None:
            detail += f":{experiment_id}"
        if outcome_unknown:
            detail += ":outcome-unknown"
        super().__init__(detail)
        self.code = code
        self.method = method
        self.experiment_id = experiment_id
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


class MagentaExperimentTransport(Protocol):
    """Injected transport returning the already-unwrapped JSON result.

    Socket framing, request IDs, envelope validation, and connection secrets
    belong to the transport implementation.  The bridge only sees one bounded
    JSON object per call and never opens a socket itself.
    """

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...


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


@dataclass(frozen=True)
class ExperimentExecutionRequest:
    """The exact execution fields accepted by Magenta's ExperimentService."""

    experiment_id: str
    command: str
    cwd: str
    gpu_count: int
    name: str | None = None
    timeout_seconds: int | None = None

    def as_wire_params(self) -> dict[str, Any]:
        _validate_execution_request(self)
        params: dict[str, Any] = {
            "command": self.command,
            "cwd": self.cwd,
            "experiment_id": self.experiment_id,
            "gpu_count": self.gpu_count,
        }
        if self.name is not None:
            params["name"] = self.name
        if self.timeout_seconds is not None:
            params["timeout_seconds"] = self.timeout_seconds
        return params


@dataclass(frozen=True)
class ExperimentSubmitAcceptance:
    """Opaque submit acknowledgement; it is never a BMP receipt."""

    experiment_id: str
    result: Mapping[str, Any]
    identity_context: SupervisorExperimentRequest | None = None


@dataclass(frozen=True)
class ExperimentOperationResult:
    """Bounded opaque result for service status, mutation, or read calls."""

    method: str
    experiment_id: str | None
    result: Mapping[str, Any]


class MagentaExperimentWireClient:
    """Map the pinned Magenta ExperimentService contract to an injected wire.

    This version intentionally does not parse Supervisor-specific projections.
    The Magenta source declares result objects as opaque records.  A later
    reviewed adapter may parse a separately versioned status fixture and then
    call :func:`validate_supervisor_receipt` for a terminal artifact receipt.
    """

    def __init__(self, transport: MagentaExperimentTransport) -> None:
        self._transport = transport

    def _call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        experiment_id: str | None = None,
        mutation: bool = False,
    ) -> Mapping[str, Any]:
        try:
            result = self._transport.request(method, params)
        except MagentaExperimentTransportError as exc:
            if mutation and exc.request_dispatched:
                raise MagentaExperimentWireError(
                    "transport",
                    method=method,
                    experiment_id=experiment_id,
                    retryable=False,
                    outcome_unknown=True,
                ) from exc
            raise MagentaExperimentWireError(
                "unavailable" if not exc.request_dispatched else "transport",
                method=method,
                experiment_id=experiment_id,
                retryable=not mutation or not exc.request_dispatched,
            ) from exc
        except MagentaExperimentServiceError as exc:
            raise MagentaExperimentWireError(
                "service",
                method=method,
                experiment_id=experiment_id,
                retryable=False,
            ) from exc
        except Exception as exc:
            raise MagentaExperimentWireError(
                "transport",
                method=method,
                experiment_id=experiment_id,
                retryable=not mutation,
                outcome_unknown=mutation,
            ) from exc
        try:
            bounded = _bounded_mapping(result)
        except SupervisorMcpError as exc:
            if mutation:
                raise MagentaExperimentWireError(
                    "transport",
                    method=method,
                    experiment_id=experiment_id,
                    retryable=False,
                    outcome_unknown=True,
                ) from exc
            raise MagentaExperimentWireError(
                "invalid_response",
                method=method,
                experiment_id=experiment_id,
                retryable=False,
            ) from exc
        return bounded

    def service_status(self) -> ExperimentOperationResult:
        result = self._call("service_status", {})
        return ExperimentOperationResult("service_status", None, result)

    def submit(
        self,
        request: ExperimentExecutionRequest,
        *,
        identity_context: SupervisorExperimentRequest | None = None,
    ) -> ExperimentSubmitAcceptance:
        _validate_execution_request(request)
        if identity_context is not None:
            _validate_submit_request(identity_context)
            if identity_context.experiment_id != request.experiment_id:
                _fail("identity-context-mismatch")
        result = self._call(
            "experiment_submit",
            request.as_wire_params(),
            experiment_id=request.experiment_id,
            mutation=True,
        )
        returned_id = result.get("experiment_id")
        if returned_id is not None and returned_id != request.experiment_id:
            raise MagentaExperimentWireError(
                "identity-mismatch",
                method="experiment_submit",
                experiment_id=request.experiment_id,
                retryable=False,
            )
        return ExperimentSubmitAcceptance(
            request.experiment_id, result, identity_context
        )

    def status(
        self,
        experiment_id: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ExperimentOperationResult:
        params: dict[str, Any] = {}
        if experiment_id is not None:
            _identifier(experiment_id, "experiment-id-invalid")
            params["experiment_id"] = experiment_id
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 100:
                _fail("status-limit-invalid")
            params["limit"] = limit
        if offset is not None:
            if type(offset) is not int or offset < 0:
                _fail("status-offset-invalid")
            params["offset"] = offset
        result = self._call("experiment_status", params, experiment_id=experiment_id)
        _validate_optional_result_identity(result, experiment_id, "experiment_status")
        return ExperimentOperationResult("experiment_status", experiment_id, result)

    def watch(
        self,
        experiment_id: str,
        *,
        after_sequence: int,
        timeout_ms: int | None = None,
    ) -> ExperimentOperationResult:
        _identifier(experiment_id, "experiment-id-invalid")
        if type(after_sequence) is not int or after_sequence < 0:
            _fail("watch-sequence-invalid")
        params = {
            "after_sequence": after_sequence,
            "experiment_id": experiment_id,
        }
        if timeout_ms is not None:
            if (
                type(timeout_ms) is not int
                or not 0 <= timeout_ms <= MAX_EXPERIMENT_WATCH_MS
            ):
                _fail("watch-timeout-invalid")
            params["timeout_seconds"] = timeout_ms / 1000
        result = self._call("experiment_watch", params, experiment_id=experiment_id)
        _validate_optional_result_identity(result, experiment_id, "experiment_watch")
        return ExperimentOperationResult("experiment_watch", experiment_id, result)

    def cancel(self, experiment_id: str) -> ExperimentOperationResult:
        _identifier(experiment_id, "experiment-id-invalid")
        result = self._call(
            "experiment_cancel",
            {"experiment_id": experiment_id},
            experiment_id=experiment_id,
            mutation=True,
        )
        _validate_optional_result_identity(result, experiment_id, "experiment_cancel")
        return ExperimentOperationResult("experiment_cancel", experiment_id, result)

    def retry(self, experiment_id: str, reason: str) -> ExperimentOperationResult:
        _identifier(experiment_id, "experiment-id-invalid")
        _string(reason, "retry-reason-invalid", maximum=1024)
        result = self._call(
            "experiment_retry",
            {"experiment_id": experiment_id, "reason": reason},
            experiment_id=experiment_id,
            mutation=True,
        )
        _validate_optional_result_identity(result, experiment_id, "experiment_retry")
        return ExperimentOperationResult("experiment_retry", experiment_id, result)


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


def _validate_execution_request(request: ExperimentExecutionRequest) -> None:
    """Validate only the fields that cross Magenta's ExperimentService wire."""

    _identifier(request.experiment_id, "experiment-id-invalid")
    _wire_text(request.command, "command-invalid", maximum=16_384)
    _wire_text(request.cwd, "cwd-invalid", maximum=4_096)
    if (
        type(request.gpu_count) is not int
        or not 1 <= request.gpu_count <= MAX_EXPERIMENT_GPUS
    ):
        _fail("gpu-count-invalid")
    if request.name is not None:
        _string(request.name, "name-invalid", maximum=256)
    if request.timeout_seconds is not None and (
        type(request.timeout_seconds) is not int
        or not 1 <= request.timeout_seconds <= MAX_EXPERIMENT_TIMEOUT_SECONDS
    ):
        _fail("timeout-seconds-invalid")


def _wire_text(value: Any, code: str, *, maximum: int) -> str:
    """Validate Magenta's non-empty text without rejecting shell newlines."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        _fail(code)
    if "\x00" in value or any(ord(char) == 0x7F for char in value):
        _fail(code)
    return value


def _validate_optional_result_identity(
    result: Mapping[str, Any], experiment_id: str | None, method: str
) -> None:
    """Reject a response that names a different experiment identity."""

    returned_id = result.get("experiment_id")
    if (
        experiment_id is not None
        and returned_id is not None
        and returned_id != experiment_id
    ):
        raise MagentaExperimentWireError(
            "identity-mismatch",
            method=method,
            experiment_id=experiment_id,
            retryable=False,
        )


def validate_terminal_receipt(
    value: Mapping[str, Any],
    *,
    identity_context: SupervisorExperimentRequest,
    artifact_root: Path | None = None,
    artifact_base: Path | None = None,
) -> SupervisorReceipt:
    """Validate a terminal receipt exported after a wire status/watch call.

    The wire service returns opaque records, so callers must select and export
    a terminal receipt using a separately reviewed status projection.  This
    helper only performs the known BMP receipt and immutable identity checks;
    it never treats an ACK, status event, or log as a metric.
    """

    _validate_submit_request(identity_context)
    receipt = validate_supervisor_receipt(
        value,
        artifact_root=artifact_root,
        artifact_base=artifact_base,
    )
    _validate_receipt_binding(identity_context, receipt)
    return receipt


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
    "MAX_EXPERIMENT_GPUS",
    "MAX_EXPERIMENT_TIMEOUT_SECONDS",
    "MAX_EXPERIMENT_WATCH_MS",
    "MAX_REPORT_BYTES",
    "MAX_RESPONSE_BYTES",
    "SUPERVISOR_RECEIPT_FORMAT",
    "ExperimentExecutionRequest",
    "ExperimentOperationResult",
    "ExperimentSubmitAcceptance",
    "MagentaExperimentTransport",
    "MagentaExperimentTransportError",
    "MagentaExperimentServiceError",
    "MagentaExperimentWireClient",
    "MagentaExperimentWireError",
    "SupervisorExperimentRequest",
    "SupervisorMcpClient",
    "SupervisorMcpError",
    "SupervisorMcpTransport",
    "SupervisorReceipt",
    "SupervisorStatus",
    "serialize_supervisor_receipt",
    "validate_terminal_receipt",
    "validate_supervisor_receipt",
]
