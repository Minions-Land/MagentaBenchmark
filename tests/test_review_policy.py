from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_poor_otter_bob_is_the_only_repository_codeowner() -> None:
    entries = [
        line.strip()
        for line in (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert entries == ["* @PoorOtterBob"]


def test_repository_review_gate_has_one_reviewer_and_exact_head_self_review() -> None:
    workflow = (ROOT / ".github/workflows/repository-review.yml").read_text(encoding="utf-8")
    assert "name: PoorOtterBob review required gate" in workflow
    assert "REQUIRED_REVIEWER: PoorOtterBob" in workflow
    assert "I completed PoorOtterBob final review/self-review" in workflow
    assert "Final review HEAD:" in workflow
    assert "PROTOCOL_CODEOWNERS" not in workflow


def test_contributor_guides_describe_single_final_reviewer() -> None:
    for relative in ("AGENTS.md", "TOAGENT.md", "TOHUMAN.md", "docs/GITHUB_DEVELOPMENT.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "PoorOtterBob" in text, relative
    owners = (ROOT / ".github/OWNERS.md").read_text(encoding="utf-8")
    assert "single accountable GitHub reviewer" in owners
