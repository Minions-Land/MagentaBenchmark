"""Exact-string fake verifier."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .task import FakeTask


class FakeVerifierError(RuntimeError):
    """The verifier itself failed, distinct from a verified wrong answer."""


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    score: float
    expected: str
    actual: str


@dataclass(frozen=True)
class FakeVerifier:
    verifier_id: str = "fake.exact.v1"

    def verify(
        self, task: FakeTask, output_path: Path, *, inject_error: bool = False
    ) -> VerificationResult:
        if inject_error:
            raise FakeVerifierError("injected fake verifier fault")
        try:
            actual = output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"output violates UTF-8 text contract: {exc}") from exc
        passed = actual == task.expected
        return VerificationResult(
            passed=passed,
            score=1.0 if passed else 0.0,
            expected=task.expected,
            actual=actual,
        )


__all__ = ["FakeVerifier", "FakeVerifierError", "VerificationResult"]
