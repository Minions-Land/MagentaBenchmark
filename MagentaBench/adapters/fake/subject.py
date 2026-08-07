"""Deterministic fake subject and typed fault injection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .task import FakeTaskInput


class FakeFault(str, Enum):
    none = "none"
    no_output = "no_output"
    invalid_output = "invalid_output"
    timeout = "timeout"
    agent_error = "agent_error"
    harness_fault = "harness_fault"
    verifier_error = "verifier_error"
    infra_error = "infra_error"
    unsupported = "unsupported"


class FakeSubjectError(RuntimeError):
    """Typed fake error whose ``fault`` maps directly to BMP taxonomy."""

    def __init__(self, fault: FakeFault, message: str | None = None) -> None:
        self.fault = fault
        super().__init__(message or f"injected fake fault: {fault.value}")


@dataclass(frozen=True)
class SubjectReceipt:
    subject_id: str
    activated: bool
    output_path: str | None
    response: str | None
    fault: FakeFault


@dataclass(frozen=True)
class FakeSubject:
    """A subject that writes a fixed response or raises a typed fault."""

    subject_id: str
    response: str = "BMP_OK"
    fault: FakeFault = FakeFault.none

    def run(self, task: FakeTaskInput, workspace: Path) -> SubjectReceipt:
        if self.fault in {
            FakeFault.timeout,
            FakeFault.agent_error,
            FakeFault.harness_fault,
            FakeFault.infra_error,
            FakeFault.unsupported,
        }:
            raise FakeSubjectError(self.fault)

        output_path = workspace / task.output_filename
        if self.fault == FakeFault.no_output:
            return SubjectReceipt(
                subject_id=self.subject_id,
                activated=True,
                output_path=None,
                response=None,
                fault=self.fault,
            )
        if self.fault == FakeFault.invalid_output:
            output_path.write_bytes(b"\xff\xfe\xfa")
            return SubjectReceipt(
                subject_id=self.subject_id,
                activated=True,
                output_path=str(output_path),
                response=None,
                fault=self.fault,
            )

        output_path.write_text(self.response, encoding="utf-8")
        return SubjectReceipt(
            subject_id=self.subject_id,
            activated=True,
            output_path=str(output_path),
            response=self.response,
            fault=self.fault,
        )


__all__ = ["FakeFault", "FakeSubject", "FakeSubjectError", "SubjectReceipt"]
