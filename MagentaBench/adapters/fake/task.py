"""Fake task definition."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FakeTaskInput:
    """The subject-visible projection; verifier gold is deliberately absent."""

    task_id: str
    instruction: str
    output_filename: str


@dataclass(frozen=True)
class FakeTask:
    """A deterministic exact-output task with verifier-private gold."""

    task_id: str = "case-001"
    instruction: str = "Emit the BMP protocol sentinel."
    expected: str = "BMP_OK"
    output_filename: str = "answer.txt"

    def public_input(self) -> FakeTaskInput:
        return FakeTaskInput(
            task_id=self.task_id,
            instruction=self.instruction,
            output_filename=self.output_filename,
        )


__all__ = ["FakeTask", "FakeTaskInput"]
