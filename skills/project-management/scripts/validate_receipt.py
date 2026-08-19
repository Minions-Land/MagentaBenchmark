#!/usr/bin/env python3
"""Validate a Markdown receipt's structure, digest, and claim boundary."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit


REQUIRED = (
    "conclusion",
    "frozen protocol",
    "results and denominator",
    "sentinel",
    "fidelity",
    "cost",
    "evidence and next action",
)
HEADING = re.compile(r"(?im)^#{1,6}\s+(.+?)\s*$")
SECRET_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|"
    r"API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*\s*=\s*(?![<$])\S+"
)
SECRET_VALUE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN .*PRIVATE KEY-----)",
    re.IGNORECASE,
)
URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>()]+")
SIDECAR = re.compile(r"^([0-9a-f]{64})(?:[ \t]+\*?(.+))?\n?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_PAIRS = {
    ("complete", "reproduced"),
    ("complete", "external-declaration"),
    ("incomplete", "incomplete"),
    ("not-run", "incomplete"),
    ("invalid", "invalid"),
    ("infrastructure-failed", "infrastructure-failure"),
}


def _field_values(text: str, label: str) -> list[str]:
    pattern = re.compile(
        rf"(?im)^\s*-\s*{re.escape(label)}\s*:\s*(.*?)\s*$"
    )
    return [match.group(1) for match in pattern.finditer(text)]


def _exact_field(text: str, label: str, errors: list[str]) -> str | None:
    values = _field_values(text, label)
    if len(values) != 1 or not values[0]:
        errors.append(f"receipt requires exactly one non-empty {label} field")
        return None
    return values[0]


def _contains_unsafe_uri(text: str) -> bool:
    for match in URI.finditer(text):
        try:
            parsed = urlsplit(match.group(0).rstrip(".,;"))
        except ValueError:
            return True
        has_userinfo = parsed.username is not None or parsed.password is not None
        unsafe_http_suffix = parsed.scheme.casefold() in {"http", "https"} and (
            parsed.query or parsed.fragment
        )
        if has_userinfo or unsafe_http_suffix:
            return True
    return False


def _verify_sidecar(path: Path, errors: list[str]) -> None:
    sidecar = path.with_suffix(".sha256")
    try:
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ValueError("receipt requires a regular .sha256 sidecar")
        content = sidecar.read_text(encoding="ascii")
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(str(exc))
        return
    match = SIDECAR.fullmatch(content)
    if match is None:
        errors.append("receipt sidecar must contain one lowercase SHA-256")
        return
    named_path = match.group(2)
    if named_path is not None and Path(named_path).name != path.name:
        errors.append("receipt sidecar names a different file")
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    if match.group(1) != observed:
        errors.append("receipt sidecar digest does not match")


def _validate_reproduced_claim(
    text: str,
    expected_review_head: str | None,
    errors: list[str],
) -> None:
    expected = _exact_field(text, "Expected cells", errors)
    unique = _exact_field(text, "Unique cells", errors)
    source_commit = _exact_field(text, "Source commit", errors)
    artifact_sha = _exact_field(text, "Artifact SHA256", errors)
    owner = _exact_field(text, "Owner", errors)
    reviewer = _exact_field(text, "Final reviewer", errors)
    review_state = _exact_field(text, "Review state", errors)
    review_head = _exact_field(text, "Final review HEAD", errors)
    if expected is not None and (not expected.isdigit() or int(expected) <= 0):
        errors.append("Expected cells must be a positive integer")
    if unique is not None and (not unique.isdigit() or int(unique) <= 0):
        errors.append("Unique cells must be a positive integer")
    if expected is not None and unique is not None and expected != unique:
        errors.append("Expected cells and Unique cells must match")
    if source_commit is not None and COMMIT.fullmatch(source_commit) is None:
        errors.append("Source commit must be an exact lowercase 40-hex commit")
    if artifact_sha is not None and SHA256.fullmatch(artifact_sha) is None:
        errors.append("Artifact SHA256 must be an exact lowercase digest")
    if owner is not None and owner != "PoorOtterBob":
        errors.append("Owner must be PoorOtterBob")
    if reviewer is not None and reviewer != "PoorOtterBob":
        errors.append("Final reviewer must be PoorOtterBob")
    if review_state is not None and review_state != "approved":
        errors.append("Review state must be approved")
    if review_head is not None and COMMIT.fullmatch(review_head) is None:
        errors.append("Final review HEAD must be an exact lowercase 40-hex commit")
    if expected_review_head is None:
        errors.append("reproduced claims require --expected-review-head")
    elif COMMIT.fullmatch(expected_review_head) is None:
        errors.append("expected review head must be a lowercase 40-hex commit")
    elif review_head is not None and review_head != expected_review_head:
        errors.append("Final review HEAD does not match the expected head")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument(
        "--expected-review-head",
        help="trusted exact Git/GitHub head for a reproduced claim",
    )
    args = parser.parse_args()
    configured = args.receipt.expanduser()
    try:
        if configured.is_symlink():
            raise ValueError("receipt cannot be a symlink")
        path = configured.resolve(strict=True)
        if not path.is_file():
            raise ValueError("receipt must be a regular file")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    headings = {match.group(1).strip().casefold() for match in HEADING.finditer(text)}
    errors = [
        f"missing section: {name}"
        for name in REQUIRED
        if not any(name in heading for heading in headings)
    ]
    if SECRET_ASSIGNMENT.search(text) or SECRET_VALUE.search(text):
        errors.append("possible credential value")
    if _contains_unsafe_uri(text):
        errors.append(
            "credential-bearing URI or query-bearing/fragmented HTTP(S) URL"
        )

    state = _exact_field(text, "State", errors)
    evidence_class = _exact_field(text, "Evidence class", errors)
    claim_eligible = _exact_field(text, "Claim eligible", errors)
    if (
        state is not None
        and evidence_class is not None
        and (state, evidence_class) not in ALLOWED_PAIRS
    ):
        errors.append("State and Evidence class are inconsistent")
    if claim_eligible not in {None, "true", "false"}:
        errors.append("Claim eligible must be true or false")
    if evidence_class != "reproduced" and claim_eligible not in {None, "false"}:
        errors.append("only reproduced evidence may be claim eligible")
    if state == "complete" and evidence_class == "reproduced":
        _validate_reproduced_claim(text, args.expected_review_head, errors)
    _verify_sidecar(path, errors)

    if errors:
        for error in errors:
            print("ERROR:", error)
        return 1
    print("PASS: receipt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
