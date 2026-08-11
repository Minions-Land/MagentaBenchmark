from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.lab import (
    LabArtifactRef,
    LabBlocker,
    LabBlockerCategory,
    LabCheckpoint,
    LabConflictError,
    LabCriterion,
    LabEvent,
    LabEventKind,
    LabDriftError,
    LabIssue,
    LabPriority,
    LabReview,
    LabReviewVerdict,
    LabRunLink,
    LabRunState,
    LabStatus,
    LabStore,
    LabWriteScope,
)
from MagentaBench.lab.cli import main as lab_main
from MagentaBench.lab.store import artifact_ref_from_path, canonical_json_bytes


T0 = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
HEAD = "a" * 40


def _issue(
    issue_id: str,
    *,
    created_at: datetime = T0,
    owner: str | None = "alice",
    dependencies: tuple[str, ...] = (),
    write_paths: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
) -> LabIssue:
    return LabIssue(
        issue_id=issue_id,
        title=f"Issue {issue_id}",
        objective="Make this work recoverably.",
        priority=LabPriority.p1,
        created_by="alice",
        created_at=created_at,
        owner=owner,
        dependencies=dependencies,
        write_scope=LabWriteScope(paths=write_paths, resources=resources),
        acceptance_criteria=(
            LabCriterion(criterion_id="verified", description="Independent checks pass."),
        ),
    )


def _concurrent_note(project_root: str, lab_root: str, event_id: str) -> None:
    LabStore(project_root, lab_root).append_event(
        "parallel",
        event_id,
        LabEventKind.note,
        "worker",
        created_at=T0 + timedelta(seconds=1),
        note=f"completed {event_id}",
    )


def test_issue_creation_and_event_retry_are_idempotent(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    first = store.open_issue(_issue("idempotent"))
    assert first.changed is True
    retry = store.open_issue(_issue("idempotent", created_at=T0 + timedelta(days=1)))
    assert retry.changed is False
    assert retry.state.revision == first.state.revision

    event = store.append_event(
        "idempotent",
        "plan-1",
        LabEventKind.status,
        "alice",
        created_at=T0 + timedelta(seconds=1),
        status=LabStatus.planned,
        note="plan accepted",
    )
    assert event.changed is True
    repeated = store.append_event(
        "idempotent",
        "plan-1",
        LabEventKind.status,
        "alice",
        created_at=T0 + timedelta(hours=1),
        status=LabStatus.planned,
        note="plan accepted",
    )
    assert repeated.changed is False
    assert repeated.state.event_count == 1
    with pytest.raises(LabConflictError, match="another operation"):
        store.append_event(
            "idempotent",
            "plan-1",
            LabEventKind.status,
            "alice",
            status=LabStatus.ready,
            note="different intent",
        )
    assert len(tuple((tmp_path / "lab/issues/idempotent/events").glob("*.json"))) == 1


def test_complete_workflow_requires_lease_checkpoint_review_and_release(
    tmp_path: Path,
) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("workflow"))
    store.append_event(
        "workflow", "ready", "status", "alice", created_at=T0 + timedelta(seconds=1), status="ready"
    )
    store.append_event(
        "workflow",
        "claim",
        "claim",
        "alice",
        created_at=T0 + timedelta(seconds=2),
        owner="alice",
        lease_id="lease-1",
        lease_ttl_seconds=3600,
        lease_base_commit=HEAD,
        lease_branch="work/workflow",
    )
    store.append_event(
        "workflow", "running", "status", "alice", created_at=T0 + timedelta(seconds=3), status="running"
    )
    checkpoint = LabCheckpoint(
        git_head=HEAD,
        git_branch="work/workflow",
        worktree_clean=True,
        resume_argv=("uv", "run", "bmp-run", "experiment.toml"),
        required_env=("OPENAI_API_KEY",),
        next_action="Resume the verified single-case pilot.",
    )
    store.append_event(
        "workflow",
        "checkpoint-1",
        "checkpoint",
        "alice",
        created_at=T0 + timedelta(seconds=4),
        checkpoint=checkpoint,
    )
    store.append_event(
        "workflow",
        "verifying",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=5),
        status="verifying",
    )
    with pytest.raises(LabConflictError, match="approved review"):
        store.append_event(
            "workflow",
            "done-too-early",
            "status",
            "alice",
            created_at=T0 + timedelta(seconds=6),
            status="done",
        )
    assert not (tmp_path / "lab/issues/workflow/events/done-too-early.json").exists()
    store.append_event(
        "workflow",
        "release",
        "release",
        "alice",
        created_at=T0 + timedelta(seconds=7),
        lease_id="lease-1",
    )
    store.append_event(
        "workflow",
        "review",
        "review",
        "alice",
        created_at=T0 + timedelta(seconds=8),
        review=LabReview(
            verdict=LabReviewVerdict.approved,
            summary="All acceptance evidence was reviewed.",
            accepted_criteria=("verified",),
        ),
    )
    result = store.append_event(
        "workflow",
        "done",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=9),
        status="done",
    )
    assert result.state.status == LabStatus.done
    assert result.state.latest_review is not None
    assert store.doctor(at=T0 + timedelta(seconds=10))["ok"] is True


def test_blocker_and_recovery_plan_preserve_next_action(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("blocked"))
    blocker = LabBlocker(
        blocker_id="docker-images",
        category=LabBlockerCategory.infrastructure,
        summary="Pinned images are unavailable.",
        recovery_action="Import the exact image archive and record its digest.",
    )
    state = store.append_event(
        "blocked",
        "block-images",
        "block",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        blocker=blocker,
    ).state
    assert state.status == LabStatus.blocked
    recovery = store.recovery_view("blocked", at=T0 + timedelta(seconds=2))
    assert recovery["blockers"][0]["blocker_id"] == "docker-images"
    assert any("Import the exact image" in action for action in recovery["actions"])

    store.append_event(
        "blocked",
        "resolve-images",
        "resolve_blocker",
        "alice",
        created_at=T0 + timedelta(seconds=3),
        blocker_id="docker-images",
    )
    resumed = store.append_event(
        "blocked",
        "planned",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=4),
        status="planned",
    ).state
    assert resumed.blockers == ()
    assert resumed.status == LabStatus.planned


def test_scope_aware_leases_reject_overlapping_work(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("scope-a", write_paths=("plugins/terminal_bench",)))
    store.open_issue(_issue("scope-b", write_paths=("plugins",)))
    store.append_event(
        "scope-a",
        "claim-a",
        "claim",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        owner="alice",
        lease_id="lease-a",
        lease_ttl_seconds=3600,
        lease_base_commit=HEAD,
        lease_branch="work/a",
    )
    with pytest.raises(LabConflictError, match="write scope conflicts"):
        store.append_event(
            "scope-b",
            "claim-b",
            "claim",
            "alice",
            created_at=T0 + timedelta(seconds=2),
            owner="alice",
            lease_id="lease-b",
            lease_ttl_seconds=3600,
            lease_base_commit=HEAD,
            lease_branch="work/b",
        )


def test_expired_lease_can_be_reclaimed_but_active_work_is_reported(
    tmp_path: Path,
) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("reclaim"))
    store.append_event(
        "reclaim",
        "lease-old",
        "claim",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        owner="alice",
        lease_id="old",
        lease_ttl_seconds=60,
        lease_base_commit=HEAD,
        lease_branch="work/old",
    )
    result = store.append_event(
        "reclaim",
        "lease-new",
        "claim",
        "alice",
        created_at=T0 + timedelta(seconds=62),
        owner="bob",
        lease_id="new",
        lease_ttl_seconds=60,
        lease_base_commit=HEAD,
        lease_branch="work/new",
    )
    assert result.state.lease is not None
    assert result.state.lease.owner == "bob"


def test_expired_lease_survives_notes_for_recovery_audit(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("expired-audit"))
    store.append_event(
        "expired-audit",
        "lease-old",
        "claim",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        owner="alice",
        lease_id="old",
        lease_ttl_seconds=60,
        lease_base_commit=HEAD,
        lease_branch="work/old",
    )
    state = store.append_event(
        "expired-audit",
        "post-expiry-note",
        "note",
        "observer",
        created_at=T0 + timedelta(seconds=62),
        note="The interrupted holder remains part of the recovery trail.",
    ).state

    assert state.active_lease(T0 + timedelta(seconds=62)) is None
    assert state.lease is not None
    assert state.lease.lease_id == "old"
    recovery = store.recovery_view(
        "expired-audit", at=T0 + timedelta(seconds=62)
    )
    assert recovery["active_lease"] is None
    assert recovery["last_lease"]["lease_id"] == "old"


def test_blocked_state_requires_structured_and_resolved_blockers(
    tmp_path: Path,
) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("structured-blocker"))
    with pytest.raises(LabConflictError, match="structured block event"):
        store.append_event(
            "structured-blocker",
            "bare-blocked-status",
            "status",
            "alice",
            created_at=T0 + timedelta(seconds=1),
            status="blocked",
        )
    store.append_event(
        "structured-blocker",
        "real-blocker",
        "block",
        "alice",
        created_at=T0 + timedelta(seconds=2),
        blocker=LabBlocker(
            blocker_id="missing-input",
            category=LabBlockerCategory.dependency,
            summary="A required input is unavailable.",
            recovery_action="Restore and verify the required input.",
        ),
    )
    with pytest.raises(LabConflictError, match="all blockers must be resolved"):
        store.append_event(
            "structured-blocker",
            "premature-resume",
            "status",
            "alice",
            created_at=T0 + timedelta(seconds=3),
            status="planned",
        )


def test_ready_state_requires_completed_dependencies(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("dependency"))
    store.open_issue(_issue("dependent", dependencies=("dependency",)))

    with pytest.raises(LabConflictError, match="requires completed dependencies"):
        store.append_event(
            "dependent",
            "ready-too-soon",
            "status",
            "alice",
            created_at=T0 + timedelta(seconds=1),
            status="ready",
        )
    assert store.load("dependent").event_count == 0


def test_event_time_cannot_move_backwards(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("clock"))
    store.append_event(
        "clock", "note-later", "note", "worker", created_at=T0 + timedelta(seconds=2), note="later"
    )
    with pytest.raises(LabConflictError, match="moves backwards"):
        store.append_event(
            "clock",
            "note-earlier",
            "note",
            "worker",
            created_at=T0 + timedelta(seconds=1),
            note="earlier",
        )
    assert not (tmp_path / "lab/issues/clock/events/note-earlier.json").exists()


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        (LabEventKind.status, {"status": "planned"}),
        (LabEventKind.assign, {"owner": "mallory"}),
        (
            LabEventKind.claim,
            {
                "owner": "mallory",
                "lease_id": "lease-intruder",
                "lease_ttl_seconds": 3600,
                "lease_base_commit": HEAD,
                "lease_branch": "work/intruder",
            },
        ),
        (
            LabEventKind.block,
            {
                "blocker": LabBlocker(
                    blocker_id="intruder-blocker",
                    category=LabBlockerCategory.process,
                    summary="An unauthorized blocker.",
                    recovery_action="Do not persist this event.",
                )
            },
        ),
        (
            LabEventKind.resolve_blocker,
            {"blocker_id": "missing-blocker"},
        ),
        (
            LabEventKind.checkpoint,
            {
                "checkpoint": LabCheckpoint(
                    git_head=HEAD,
                    git_branch="work/owned",
                    worktree_clean=True,
                    resume_argv=("uv", "run", "bmp-lab", "doctor"),
                    next_action="Continue only as the owner.",
                )
            },
        ),
        (
            LabEventKind.link_run,
            {
                "run": LabRunLink(
                    run_id="intruder-run",
                    state=LabRunState.planned,
                    record_root="records/intruder-run",
                )
            },
        ),
        (
            LabEventKind.review,
            {
                "review": LabReview(
                    verdict=LabReviewVerdict.changes_requested,
                    summary="An unauthorized review.",
                )
            },
        ),
    ],
)
def test_weight_bearing_events_reject_non_owner(
    tmp_path: Path,
    kind: LabEventKind,
    fields: dict[str, object],
) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("owned", owner="bob"))

    with pytest.raises(LabConflictError, match="lease holder or issue owner"):
        store.append_event(
            "owned",
            f"unauthorized-{kind.value}",
            kind,
            "alice",
            created_at=T0 + timedelta(seconds=1),
            **fields,
        )

    assert not (tmp_path / f"lab/issues/owned/events/unauthorized-{kind.value}.json").exists()
    assert store.load("owned").event_count == 0


def test_active_lease_holder_exclusively_authorizes_weight_bearing_events(
    tmp_path: Path,
) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("leased", owner="alice"))
    store.append_event(
        "leased",
        "claim-holder",
        LabEventKind.claim,
        "alice",
        created_at=T0 + timedelta(seconds=1),
        owner="bob",
        lease_id="lease-holder",
        lease_ttl_seconds=3600,
        lease_base_commit=HEAD,
        lease_branch="work/leased",
    )

    blocker = LabBlocker(
        blocker_id="holder-only",
        category=LabBlockerCategory.process,
        summary="Only the active worker may record this blocker.",
        recovery_action="Coordinate with the lease holder.",
    )
    with pytest.raises(LabConflictError, match="lease holder or issue owner"):
        store.append_event(
            "leased",
            "owner-during-lease",
            LabEventKind.block,
            "alice",
            created_at=T0 + timedelta(seconds=2),
            blocker=blocker,
        )
    state = store.append_event(
        "leased",
        "holder-block",
        LabEventKind.block,
        "bob",
        created_at=T0 + timedelta(seconds=2),
        blocker=blocker,
    ).state
    assert state.status == LabStatus.blocked


def test_unowned_issue_can_be_self_claimed_but_not_delegated_by_a_stranger(
    tmp_path: Path,
) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("unowned", owner=None))

    with pytest.raises(LabConflictError, match="lease holder or issue owner"):
        store.append_event(
            "unowned",
            "stranger-delegation",
            "claim",
            "bob",
            created_at=T0 + timedelta(seconds=1),
            owner="mallory",
            lease_id="delegated",
            lease_ttl_seconds=3600,
            lease_base_commit=HEAD,
            lease_branch="work/delegated",
        )
    state = store.append_event(
        "unowned",
        "self-claim",
        "claim",
        "bob",
        created_at=T0 + timedelta(seconds=2),
        owner="bob",
        lease_id="bob-lease",
        lease_ttl_seconds=3600,
        lease_base_commit=HEAD,
        lease_branch="work/unowned",
    ).state
    assert state.lease is not None
    assert state.lease.owner == "bob"


def test_material_change_invalidates_an_approved_review(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("review-boundary"))
    store.append_event(
        "review-boundary",
        "ready",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        status="ready",
    )
    store.append_event(
        "review-boundary",
        "claim",
        "claim",
        "alice",
        created_at=T0 + timedelta(seconds=2),
        owner="alice",
        lease_id="lease-review",
        lease_ttl_seconds=3600,
        lease_base_commit=HEAD,
        lease_branch="work/review-boundary",
    )
    store.append_event(
        "review-boundary",
        "running",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=3),
        status="running",
    )
    store.append_event(
        "review-boundary",
        "checkpoint",
        "checkpoint",
        "alice",
        created_at=T0 + timedelta(seconds=4),
        checkpoint=LabCheckpoint(
            git_head=HEAD,
            git_branch="work/review-boundary",
            worktree_clean=True,
            resume_argv=("uv", "run", "bmp-lab", "recover", "review-boundary"),
            next_action="Review all acceptance evidence.",
        ),
    )
    store.append_event(
        "review-boundary",
        "verifying",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=5),
        status="verifying",
    )
    store.append_event(
        "review-boundary",
        "release",
        "release",
        "alice",
        created_at=T0 + timedelta(seconds=6),
        lease_id="lease-review",
    )
    approved = LabReview(
        verdict=LabReviewVerdict.approved,
        summary="The current checkpoint satisfies the criterion.",
        accepted_criteria=("verified",),
    )
    store.append_event(
        "review-boundary",
        "approved",
        "review",
        "alice",
        created_at=T0 + timedelta(seconds=7),
        review=approved,
    )
    store.append_event(
        "review-boundary",
        "new-run-plan",
        "link_run",
        "alice",
        created_at=T0 + timedelta(seconds=8),
        run=LabRunLink(
            run_id="new-run",
            state=LabRunState.planned,
            record_root="records/new-run",
        ),
    )

    with pytest.raises(LabConflictError, match="approved review"):
        store.append_event(
            "review-boundary",
            "stale-review-done",
            "status",
            "alice",
            created_at=T0 + timedelta(seconds=9),
            status="done",
        )
    assert store.load("review-boundary").latest_review is None

    store.append_event(
        "review-boundary",
        "approved-again",
        "review",
        "alice",
        created_at=T0 + timedelta(seconds=10),
        review=approved,
    )
    assert store.append_event(
        "review-boundary",
        "done-after-review",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=11),
        status="done",
    ).state.status == LabStatus.done


def test_run_identity_cannot_drift_or_reuse_a_record_root(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("run-identity"))
    store.append_event(
        "run-identity",
        "run-planned",
        "link_run",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        run=LabRunLink(
            run_id="run-one",
            state=LabRunState.planned,
            record_root="records/run-one",
            manifest_digest="d" * 64,
        ),
    )
    with pytest.raises(LabConflictError, match="cannot change record root"):
        store.append_event(
            "run-identity",
            "run-root-drift",
            "link_run",
            "alice",
            created_at=T0 + timedelta(seconds=2),
            run=LabRunLink(
                run_id="run-one",
                state=LabRunState.running,
                record_root="records/other-root",
                manifest_digest="d" * 64,
            ),
        )
    with pytest.raises(LabConflictError, match="remove or change manifest"):
        store.append_event(
            "run-identity",
            "run-manifest-drift",
            "link_run",
            "alice",
            created_at=T0 + timedelta(seconds=2),
            run=LabRunLink(
                run_id="run-one",
                state=LabRunState.running,
                record_root="records/run-one",
            ),
        )
    with pytest.raises(LabConflictError, match="already bound to lab run"):
        store.append_event(
            "run-identity",
            "second-run-same-root",
            "link_run",
            "alice",
            created_at=T0 + timedelta(seconds=2),
            run=LabRunLink(
                run_id="run-two",
                state=LabRunState.planned,
                record_root="records/run-one",
            ),
        )


def test_record_root_binding_is_global_across_issues(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("run-link-a"))
    store.open_issue(_issue("run-link-b"))
    store.append_event(
        "run-link-a",
        "first-run",
        "link_run",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        run=LabRunLink(
            run_id="run-a",
            state=LabRunState.planned,
            record_root="records/shared-root",
        ),
    )
    with pytest.raises(LabConflictError, match="already bound to lab run run-a"):
        store.append_event(
            "run-link-b",
            "second-run",
            "link_run",
            "alice",
            created_at=T0 + timedelta(seconds=1),
            run=LabRunLink(
                run_id="run-b",
                state=LabRunState.planned,
                record_root="records/shared-root",
            ),
        )


def test_verifying_requires_a_recovery_checkpoint(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("verification-boundary"))
    store.append_event(
        "verification-boundary",
        "ready",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        status="ready",
    )
    store.append_event(
        "verification-boundary",
        "claim",
        "claim",
        "alice",
        created_at=T0 + timedelta(seconds=2),
        owner="alice",
        lease_id="verify-lease",
        lease_ttl_seconds=3600,
        lease_base_commit=HEAD,
        lease_branch="work/verification-boundary",
    )
    store.append_event(
        "verification-boundary",
        "running",
        "status",
        "alice",
        created_at=T0 + timedelta(seconds=3),
        status="running",
    )
    with pytest.raises(LabConflictError, match="requires a recovery checkpoint"):
        store.append_event(
            "verification-boundary",
            "verifying-without-checkpoint",
            "status",
            "alice",
            created_at=T0 + timedelta(seconds=4),
            status="verifying",
        )


def test_dirty_checkpoint_requires_a_content_addressed_patch() -> None:
    with pytest.raises(ValidationError, match="dirty checkpoint requires"):
        LabCheckpoint(
            git_head=HEAD,
            git_branch="work/dirty",
            worktree_clean=False,
            dirty_paths=("MagentaBench/lab/store.py",),
            resume_argv=("uv", "run", "pytest"),
            next_action="Continue the interrupted test run.",
        )


@pytest.mark.parametrize(
    "argv",
    [
        ("runner", "--api-key", "not-a-real-credential"),
        ("runner", "--github-token", "not-a-real-credential"),
        ("runner", "--access-token=value-never-store"),
        ("OPENAI_API_KEY=value-never-store", "runner"),
        ("curl", "--header", "Authorization: Bearer value-never-store"),
    ],
)
def test_checkpoint_rejects_secret_bearing_argv(argv: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="credential|authorization"):
        LabCheckpoint(
            git_head=HEAD,
            git_branch="main",
            worktree_clean=True,
            resume_argv=argv,
            next_action="Never persist command-line credentials.",
        )


def test_checkpoint_environment_contains_names_not_values() -> None:
    checkpoint = LabCheckpoint(
        git_head=HEAD,
        git_branch="main",
        worktree_clean=True,
        resume_argv=("uv", "run", "bmp-run", "experiment.toml"),
        required_env=("OPENAI_API_KEY",),
        next_action="Provide the credential out of band.",
    )
    assert checkpoint.required_env == ("OPENAI_API_KEY",)

    with pytest.raises(ValidationError, match="environment variable names only"):
        LabCheckpoint(
            git_head=HEAD,
            git_branch="main",
            worktree_clean=True,
            resume_argv=("uv", "run", "bmp-run", "experiment.toml"),
            required_env=("OPENAI_API_KEY=value-never-store",),
            next_action="Reject inline environment values.",
        )


@pytest.mark.parametrize(
    "record",
    [
        lambda: LabArtifactRef(
            locator="https://artifacts.example/object?X-Amz-Signature=value",
            sha256="b" * 64,
            size_bytes=1,
        ),
        lambda: LabBlocker(
            blocker_id="unsafe-ref",
            category=LabBlockerCategory.external,
            summary="The locator must not carry credentials.",
            recovery_action="Use a secret-free stable locator.",
            external_ref="https://user:" + "password@example.invalid/issue",
        ),
        lambda: LabCheckpoint(
            git_head=HEAD,
            git_branch="main",
            worktree_clean=True,
            record_root="https://records.example/run?access_token=value",
            resume_argv=("uv", "run", "bmp-lab", "doctor"),
            next_action="Use a credential-free record root.",
        ),
        lambda: LabRunLink(
            run_id="unsafe-run",
            state=LabRunState.running,
            record_root="https://records.example/run?sig=value",
        ),
    ],
)
def test_all_locator_fields_reject_embedded_credentials(record) -> None:
    with pytest.raises(ValidationError, match="userinfo|credential"):
        record()


def test_lab_locking_fails_closed_without_fcntl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import MagentaBench.lab.store as store_module

    store = LabStore(tmp_path)
    monkeypatch.setattr(store_module, "fcntl", None)
    with pytest.raises(LabDriftError, match="require POSIX fcntl"):
        store.list()


def test_same_parent_fork_fails_closed_instead_of_last_write_wins(
    tmp_path: Path,
) -> None:
    store = LabStore(tmp_path)
    initial = store.open_issue(_issue("forked")).state
    events = tmp_path / "lab/issues/forked/events"
    for event_id in ("branch-a", "branch-b"):
        event = LabEvent(
            issue_id="forked",
            event_id=event_id,
            sequence=1,
            previous_revision=initial.revision,
            kind=LabEventKind.note,
            actor="worker",
            created_at=T0 + timedelta(seconds=1),
            note=event_id,
        )
        (events / f"{event_id}.json").write_bytes(canonical_json_bytes(event))
    with pytest.raises(LabConflictError, match="forked or has a gap"):
        store.load("forked")


def test_cross_process_events_are_serialized_without_loss(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lab_root = project / "lab"
    LabStore(project, lab_root).open_issue(_issue("parallel"))
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(
            target=_concurrent_note,
            args=(str(project), str(lab_root), f"note-{index}"),
        )
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    state = LabStore(project, lab_root).load("parallel")
    assert state.event_count == 8
    assert len(tuple((lab_root / "issues/parallel/events").glob("*.json"))) == 8


def test_secret_material_is_rejected_before_event_persistence(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("secrets"))
    with pytest.raises(Exception, match="secret material"):
        store.append_event(
            "secrets",
            "unsafe",
            "note",
            "worker",
            note="OPENAI_API_KEY=sk-" + "x" * 30,
        )
    assert not (tmp_path / "lab/issues/secrets/events/unsafe.json").exists()


def test_doctor_detects_content_addressed_artifact_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    artifact = project / "artifacts/check.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("first\n", encoding="utf-8")
    store = LabStore(project)
    store.open_issue(_issue("artifact"))
    ref = artifact_ref_from_path(artifact, project_root=project)
    assert isinstance(ref, LabArtifactRef)
    checkpoint = LabCheckpoint(
        git_head=HEAD,
        git_branch="main",
        worktree_clean=True,
        resume_argv=("uv", "run", "bmp-lab", "doctor"),
        next_action="Verify the retained artifact.",
        artifact_refs=(ref,),
    )
    store.append_event(
        "artifact",
        "checkpoint",
        "checkpoint",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        checkpoint=checkpoint,
    )
    assert store.doctor()["ok"] is True
    artifact.write_text("drift\n", encoding="utf-8")
    result = store.doctor()
    assert result["ok"] is False
    assert any("artifact digest drift" in message for message in result["errors"])


def test_doctor_rejects_missing_absolute_checkpoint_artifact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = LabStore(project)
    store.open_issue(_issue("missing-absolute-artifact"))
    missing = project / "external-artifacts/missing.txt"
    store.append_event(
        "missing-absolute-artifact",
        "checkpoint",
        "checkpoint",
        "alice",
        created_at=T0 + timedelta(seconds=1),
        checkpoint=LabCheckpoint(
            git_head=HEAD,
            git_branch="main",
            worktree_clean=True,
            resume_argv=("uv", "run", "bmp-lab", "doctor"),
            next_action="Restore the exact referenced bytes.",
            artifact_refs=(
                LabArtifactRef(
                    locator=missing.as_posix(),
                    sha256="c" * 64,
                    size_bytes=1,
                ),
            ),
        ),
    )

    result = store.doctor()
    assert result["ok"] is False
    assert any("artifact is unavailable" in item for item in result["errors"])


def test_doctor_invokes_standalone_verifier_for_finished_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    report = project / "records/report.json"
    report.parent.mkdir()
    report.write_text("{}\n", encoding="utf-8")
    store = LabStore(project)
    store.open_issue(_issue("finished-report"))
    report_ref = artifact_ref_from_path(report, project_root=project)
    for index, state in enumerate(
        (LabRunState.planned, LabRunState.running, LabRunState.finished), start=1
    ):
        store.append_event(
            "finished-report",
            f"run-{state.value}",
            LabEventKind.link_run,
            "alice",
            created_at=T0 + timedelta(seconds=index),
            run=LabRunLink(
                run_id="verified-run",
                state=state,
                record_root="records/verified-run",
                report_ref=report_ref if state == LabRunState.finished else None,
            ),
        )

    import MagentaBench.schemas as schemas

    verified_paths: list[Path] = []
    monkeypatch.setattr(
        schemas,
        "verify_run_report",
        lambda path: verified_paths.append(Path(path)),
    )
    assert store.doctor()["ok"] is True
    assert verified_paths == [report]

    def reject_report(path: Path) -> None:
        raise ValueError(f"invalid report at {path}")

    monkeypatch.setattr(schemas, "verify_run_report", reject_report)
    result = store.doctor()
    assert result["ok"] is False
    assert any("linked finished report does not verify" in item for item in result["errors"])


def test_finished_external_report_is_not_treated_as_verified(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("external-report"))
    for index, state in enumerate(
        (LabRunState.planned, LabRunState.running, LabRunState.finished), start=1
    ):
        store.append_event(
            "external-report",
            f"run-{state.value}",
            LabEventKind.link_run,
            "alice",
            created_at=T0 + timedelta(seconds=index),
            run=LabRunLink(
                run_id="external-run",
                state=state,
                record_root="records/external-run",
                report_ref=(
                    LabArtifactRef(
                        locator="https://artifacts.example/report.json",
                        sha256="b" * 64,
                        size_bytes=100,
                    )
                    if state == LabRunState.finished
                    else None
                ),
            ),
        )

    result = store.doctor()
    assert result["ok"] is False
    assert any("not verified locally" in item for item in result["errors"])


def test_symlink_roots_and_artifacts_are_rejected(tmp_path: Path) -> None:
    real_lab = tmp_path / "real-lab"
    real_lab.mkdir()
    linked_lab = tmp_path / "linked-lab"
    linked_lab.symlink_to(real_lab, target_is_directory=True)
    with pytest.raises(LabDriftError, match="contains a symlink"):
        LabStore(tmp_path, linked_lab)

    artifact = tmp_path / "artifact.txt"
    artifact.write_text("retained bytes\n", encoding="utf-8")
    linked_artifact = tmp_path / "linked-artifact.txt"
    linked_artifact.symlink_to(artifact)
    with pytest.raises(LabDriftError, match="contains a symlink"):
        artifact_ref_from_path(linked_artifact, project_root=tmp_path)


def test_doctor_rejects_symlink_substitution_for_bound_artifact(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    artifact = project / "artifacts/check.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("bound bytes\n", encoding="utf-8")
    store = LabStore(project)
    store.open_issue(_issue("artifact-symlink"))
    store.append_event(
        "artifact-symlink",
        "checkpoint",
        LabEventKind.checkpoint,
        "alice",
        created_at=T0 + timedelta(seconds=1),
        checkpoint=LabCheckpoint(
            git_head=HEAD,
            git_branch="main",
            worktree_clean=True,
            resume_argv=("uv", "run", "bmp-lab", "doctor"),
            next_action="Verify the non-symlink artifact.",
            artifact_refs=(
                artifact_ref_from_path(artifact, project_root=project),
            ),
        ),
    )

    displaced = project / "artifacts/displaced.txt"
    artifact.replace(displaced)
    artifact.symlink_to(displaced)
    result = store.doctor()
    assert result["ok"] is False
    assert any("contains a symlink" in item for item in result["errors"])


def test_historical_event_byte_drift_breaks_the_hash_chain(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("history"))
    store.append_event(
        "history",
        "first-note",
        LabEventKind.note,
        "worker",
        created_at=T0 + timedelta(seconds=1),
        note="original first note",
    )
    store.append_event(
        "history",
        "second-note",
        LabEventKind.note,
        "worker",
        created_at=T0 + timedelta(seconds=2),
        note="second note anchors the first revision",
    )

    first_path = tmp_path / "lab/issues/history/events/first-note.json"
    first = json.loads(first_path.read_text(encoding="utf-8"))
    first["note"] = "canonically rewritten history"
    first_path.write_bytes(canonical_json_bytes(first))

    with pytest.raises(LabConflictError, match="previous revision mismatch"):
        store.load("history")


def test_doctor_detects_dependency_cycles(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.open_issue(_issue("cycle-a", dependencies=("cycle-b",)))
    store.open_issue(_issue("cycle-b", dependencies=("cycle-a",)))
    result = store.doctor()
    assert result["ok"] is False
    assert any("dependency cycle" in message for message in result["errors"])


def test_cli_open_retry_board_and_doctor(tmp_path: Path, capsys) -> None:
    arguments = [
        "--project-root",
        str(tmp_path),
        "open",
        "cli-item",
        "--title",
        "CLI item",
        "--objective",
        "Exercise the repository control plane.",
        "--actor",
        "alice",
        "--acceptance",
        "checked=Doctor passes",
    ]
    assert lab_main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True
    assert lab_main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False
    assert lab_main(
        ["--project-root", str(tmp_path), "status", "--format", "json"]
    ) == 0
    board = json.loads(capsys.readouterr().out)
    assert board["issues"][0]["issue_id"] == "cli-item"
    assert lab_main(["--project-root", str(tmp_path), "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_block_and_recover_emit_structured_handoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    LabStore(tmp_path).open_issue(_issue("cli-blocked"))
    common = ["--project-root", str(tmp_path)]
    assert lab_main(
        [
            *common,
            "block",
            "cli-blocked",
            "--event-id",
            "missing-image",
            "--actor",
            "alice",
            "--blocker-id",
            "docker-image",
            "--category",
            "infrastructure",
            "--summary",
            "The pinned image is unavailable.",
            "--recovery-action",
            "Import the pinned archive and verify its digest.",
            "--expected",
            "The exact pinned image is locally inspectable.",
            "--observed",
            "Docker returned No such image.",
            "--reproduce-arg",
            "docker",
            "--reproduce-arg",
            "image",
            "--reproduce-arg",
            "inspect",
            "--reproduce-arg",
            "alexgshaw/regex-log:20251031",
            "--exit-code",
            "1",
            "--unblock-condition",
            "Docker inspection succeeds and an immutable image ID is retained.",
        ]
    ) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked"

    assert lab_main(
        [
            *common,
            "recover",
            "cli-blocked",
            "--as-of",
            "2026-08-12T00:00:00Z",
        ]
    ) == 0
    recovery = json.loads(capsys.readouterr().out)
    assert recovery["format"] == "magentabench-lab-recovery-v1"
    assert recovery["blockers"][0]["blocker_id"] == "docker-image"
    assert recovery["blockers"][0]["exit_code"] == 1
    assert recovery["blockers"][0]["expected"].startswith("The exact pinned")
    assert recovery["blockers"][0]["observed"] == "Docker returned No such image."
    assert recovery["blockers"][0]["reproduce_argv"] == [
        "docker",
        "image",
        "inspect",
        "alexgshaw/regex-log:20251031",
    ]
    assert recovery["actions"] == [
        "resolve docker-image: Import the pinned archive and verify its digest.",
        "reproduce docker-image: docker image inspect alexgshaw/regex-log:20251031",
        "unblock condition docker-image: Docker inspection succeeds and an immutable image ID is retained.",
        "record a work checkpoint before the next interruptible step",
    ]
