from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
CHECK_RUN_ROOT = SKILLS / "experiment-infrastructure/scripts/check_run_root.py"
VERIFY_GRID = SKILLS / "experiment-integrity/scripts/verify_grid.py"
VALIDATE_LAYOUT = SKILLS / "project-management/scripts/validate_project_layout.py"
VALIDATE_RECEIPT = SKILLS / "project-management/scripts/validate_receipt.py"
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+\.md)\)")


def run_script(script: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *(str(item) for item in arguments)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_skill_markdown_links_resolve() -> None:
    missing: list[str] = []
    for document in SKILLS.rglob("*.md"):
        for target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            if not (document.parent / target).resolve().is_file():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")
    assert missing == []


def test_direct_operation_skills_inherit_repository_authority() -> None:
    infrastructure = (SKILLS / "experiment-infrastructure/SKILL.md").read_text(
        encoding="utf-8"
    )
    integrity = (SKILLS / "experiment-integrity/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "AGENTS.md" in infrastructure
    assert "bmp-lab" in infrastructure
    assert "registered" in infrastructure
    assert "fresh durable record root" in infrastructure
    assert "PoorOtterBob" in integrity
    assert "Accountable review/claim" in integrity


def test_check_run_root_requires_strict_descendant(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    accepted = run_script(CHECK_RUN_ROOT, project, project / "runs/new")
    equal = run_script(CHECK_RUN_ROOT, project, project)
    outside = run_script(CHECK_RUN_ROOT, project, tmp_path / "elsewhere")

    assert accepted.returncode == 0
    assert "AUTHORIZED_RUN_ROOT: runs/new" in accepted.stdout
    assert equal.returncode == 2
    assert outside.returncode == 2


def test_check_run_root_rejects_existing_or_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    existing = project / "runs/existing"
    existing.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project / "linked"
    dangling = project / "runs/dangling"
    try:
        link.symlink_to(outside, target_is_directory=True)
        dangling.symlink_to("../redirected", target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    existing_result = run_script(
        CHECK_RUN_ROOT, project, existing, "--require-new"
    )
    escaped_result = run_script(CHECK_RUN_ROOT, project, link / "run")
    dangling_result = run_script(
        CHECK_RUN_ROOT, project, dangling, "--require-new"
    )

    assert existing_result.returncode == 2
    assert escaped_result.returncode == 2
    assert dangling_result.returncode == 2


def test_check_run_root_rejects_control_character_output_injection(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = run_script(CHECK_RUN_ROOT, project, project / "runs/a\nEXISTS: False")
    assert result.returncode == 2
    assert result.stdout.count("\n") == 1
    assert result.stdout.startswith("UNAUTHORIZED_RUN_ROOT:")


def _grid_args(expected: Path) -> tuple[object, ...]:
    return (
        "--expected-grid",
        expected,
        "--required-field",
        "reward",
        "--required-field",
        "terminal_state",
    )


def test_verify_grid_binds_frozen_identities_and_required_fields(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps({"task_ids": ["nature-a", "nature-b"], "trial_ids": [0, 1]}),
        encoding="utf-8",
    )
    complete = tmp_path / "complete.json"
    complete.write_text(
        json.dumps(
            [
                {
                    "task_id": task,
                    "trial": trial,
                    "reward": 0.0,
                    "terminal_state": "verified_fail",
                }
                for task in ("nature-a", "nature-b")
                for trial in (0, 1)
            ]
        ),
        encoding="utf-8",
    )
    wrong_ids = tmp_path / "wrong.json"
    wrong_ids.write_text(
        json.dumps(
            [
                {
                    "task_id": task,
                    "trial": trial,
                    "reward": 1.0,
                    "terminal_state": "completed",
                }
                for task in ("wrong-a", "wrong-b")
                for trial in (0, 1)
            ]
        ),
        encoding="utf-8",
    )
    missing_field = tmp_path / "missing.json"
    missing_field.write_text(
        json.dumps(
            [
                {"task_id": task, "trial": trial, "reward": 0.0}
                for task in ("nature-a", "nature-b")
                for trial in (0, 1)
            ]
        ),
        encoding="utf-8",
    )

    assert run_script(VERIFY_GRID, complete, *_grid_args(expected)).returncode == 0
    assert run_script(VERIFY_GRID, wrong_ids, *_grid_args(expected)).returncode == 2
    assert (
        run_script(VERIFY_GRID, missing_field, *_grid_args(expected)).returncode == 2
    )


def test_verify_grid_does_not_collapse_string_and_integer_ids(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps({"task_ids": [1, "1"], "trial_ids": [0]}), encoding="utf-8"
    )
    mixed = tmp_path / "mixed.json"
    mixed.write_text(
        json.dumps(
            [
                {
                    "task_id": task,
                    "trial": 0,
                    "reward": 0.0,
                    "terminal_state": "completed",
                }
                for task in (1, "1")
            ]
        ),
        encoding="utf-8",
    )

    assert run_script(VERIFY_GRID, mixed, *_grid_args(expected)).returncode == 0


def _create_project_layout(root: Path) -> None:
    (root / "AGENTS.md").write_text("authority\n", encoding="utf-8")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    (root / "infra").mkdir()
    (root / "infra/ENVIRONMENT_MANIFEST.json").write_text(
        '{"status":"READY"}\n', encoding="utf-8"
    )
    (root / "infra/READY").write_text("ready\n", encoding="utf-8")
    package = root / "work-packages/wp-1"
    package.mkdir(parents=True)
    for name in ("CONTRACT.md", "HANDOFF.md", "STATUS.md"):
        (package / name).write_text(f"{name}\n", encoding="utf-8")
    (root / "runs").mkdir()


def test_validate_project_layout_rejects_configured_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _create_project_layout(project)

    accepted = run_script(VALIDATE_LAYOUT, project, "--require-ready")
    escaped = run_script(
        VALIDATE_LAYOUT,
        project,
        "--work-packages-dir",
        "../outside",
    )

    assert accepted.returncode == 0
    assert escaped.returncode == 1


def test_validate_project_layout_rejects_wrong_node_types(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").mkdir()
    (project / "README.md").write_text("project\n", encoding="utf-8")
    (project / "infra/ENVIRONMENT_MANIFEST.json").mkdir(parents=True)
    (project / "infra/READY").mkdir()
    package = project / "work-packages/wp-1"
    package.mkdir(parents=True)
    for name in ("CONTRACT.md", "HANDOFF.md", "STATUS.md"):
        (package / name).write_text(f"{name}\n", encoding="utf-8")
    (project / "runs").write_text("not a directory\n", encoding="utf-8")

    assert run_script(VALIDATE_LAYOUT, project, "--require-ready").returncode == 1


def _receipt(
    state: str,
    evidence_class: str,
    extra: str = "",
    *,
    claim_eligible: str = "false",
) -> str:
    return f"""# Receipt

## Conclusion
- State: {state}
- Evidence class: {evidence_class}
- Claim eligible: {claim_eligible}

## Frozen protocol
N/A

## Results and denominator
N/A

## Sentinel checks
N/A

## Fidelity and instrumentation
N/A

## Cost, deviations and limits
N/A

## Evidence and next action
{extra or 'Owner action remains pending.'}
"""


def _write_receipt(path: Path, content: str, *, correct_digest: bool = True) -> None:
    path.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not correct_digest:
        digest = "0" * 64
    path.with_suffix(".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="ascii"
    )


def _valid_claim_fields() -> str:
    return "\n".join(
        (
            "- Expected cells: 1",
            "- Unique cells: 1",
            f"- Source commit: {'a' * 40}",
            f"- Artifact SHA256: {'b' * 64}",
            "- Owner: PoorOtterBob",
            "- Final reviewer: PoorOtterBob",
            "- Review state: approved",
            f"- Final review HEAD: {'c' * 40}",
        )
    )


def test_validate_receipt_enforces_state_claim_and_sidecar(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete.md"
    _write_receipt(incomplete, _receipt("incomplete", "incomplete"))
    unsupported = tmp_path / "unsupported.md"
    _write_receipt(unsupported, _receipt("done", "reproduced"))
    incomplete_claim = tmp_path / "claim.md"
    _write_receipt(incomplete_claim, _receipt("complete", "reproduced"))
    valid_claim = tmp_path / "valid.md"
    _write_receipt(
        valid_claim,
        _receipt(
            "complete",
            "reproduced",
            _valid_claim_fields(),
            claim_eligible="true",
        ),
    )
    drifted = tmp_path / "drifted.md"
    _write_receipt(
        drifted,
        _receipt(
            "complete",
            "reproduced",
            _valid_claim_fields(),
            claim_eligible="true",
        ),
        correct_digest=False,
    )

    assert run_script(VALIDATE_RECEIPT, incomplete).returncode == 0
    assert run_script(VALIDATE_RECEIPT, unsupported).returncode == 1
    assert run_script(VALIDATE_RECEIPT, incomplete_claim).returncode == 1
    expected_head = "c" * 40
    assert (
        run_script(
            VALIDATE_RECEIPT,
            valid_claim,
            "--expected-review-head",
            expected_head,
        ).returncode
        == 0
    )
    assert (
        run_script(
            VALIDATE_RECEIPT,
            valid_claim,
            "--expected-review-head",
            "d" * 40,
        ).returncode
        == 1
    )
    assert (
        run_script(
            VALIDATE_RECEIPT,
            drifted,
            "--expected-review-head",
            expected_head,
        ).returncode
        == 1
    )


def test_validate_receipt_rejects_forged_review_and_credentials(
    tmp_path: Path,
) -> None:
    forged = tmp_path / "forged.md"
    _write_receipt(
        forged,
        _receipt(
            "complete",
            "reproduced",
            "\n".join(
                (
                    "- Expected cells: N/A",
                    "- Unique cells: N/A",
                    "- Source commit: N/A",
                    "- Artifact SHA256: N/A",
                    "- Owner: Mallory",
                    "- Final reviewer: Mallory",
                    "- Review state: pending",
                    "- Final review HEAD: N/A",
                    "GH_TOKEN=fake-token-value",
                    "https://user:password@example.invalid/result",
                )
            ),
            claim_eligible="true",
        ),
    )

    result = run_script(VALIDATE_RECEIPT, forged)
    assert result.returncode == 1
    assert "credential" in result.stdout
    assert "Final reviewer must be PoorOtterBob" in result.stdout


def test_external_declaration_must_be_non_claim(tmp_path: Path) -> None:
    accepted = tmp_path / "external.md"
    _write_receipt(
        accepted,
        _receipt("complete", "external-declaration"),
    )
    rejected = tmp_path / "external-claim.md"
    _write_receipt(
        rejected,
        _receipt(
            "complete",
            "external-declaration",
            claim_eligible="true",
        ),
    )

    assert run_script(VALIDATE_RECEIPT, accepted).returncode == 0
    assert run_script(VALIDATE_RECEIPT, rejected).returncode == 1
