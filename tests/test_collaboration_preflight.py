from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import subprocess

import pytest

from MagentaBench.collab import ExperimentRepository
from MagentaBench.collab.repository import (
    BundleSummary,
    _authorize_preflight,
)
from MagentaBench.lab import LabLease, LabStatus, LabWriteScope


ROOT = Path(__file__).parents[1]
ACTOR = "codex/preflight-test"


def _authorized_state(repository: ExperimentRepository):
    bundle = repository.load_bundle(
        "experiments/terminal-bench-magenta-smoke/bundle.json"
    )
    summary = repository.validate().bundles[0]
    from MagentaBench.lab import LabStore

    stored = LabStore(ROOT).load(bundle.lab_issue)
    now = datetime.now(timezone.utc)
    lease = LabLease(
        lease_id="preflight-test-lease",
        owner=ACTOR,
        acquired_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
        event_id="preflight-test-claim",
        base_commit="0" * 40,
        branch="main",
        write_scope=LabWriteScope(),
    )
    return bundle, summary, stored.model_copy(
        update={"status": LabStatus.running, "lease": lease, "blockers": ()}
    ), now


def _summary() -> BundleSummary:
    return BundleSummary(
        id="terminal-bench-magenta-smoke",
        purpose="exploratory",
        bmp_spec="MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml",
        protocol_id="terminal-bench.probe.v1",
        lab_issue="magenta-single-case-pilot",
        lab_status="running",
        lease_holder=ACTOR,
        blocker_count=0,
        dependencies_complete=True,
        available=False,
    )


def test_preflight_requires_the_current_lease_holder() -> None:
    repository = ExperimentRepository(ROOT)
    bundle, _, state, now = _authorized_state(repository)

    with pytest.raises(ValueError, match="does not hold"):
        _authorize_preflight(
            bundle,
            _summary(),
            state,
            actor="codex/other-agent",
            environment={name: "configured" for name in bundle.execution.required_env},
            at=now,
        )


def test_preflight_requires_declared_environment_names_without_logging_values() -> None:
    repository = ExperimentRepository(ROOT)
    bundle, _, state, now = _authorized_state(repository)
    environment = {bundle.execution.required_env[0]: "configured"}

    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        _authorize_preflight(
            bundle,
            _summary(),
            state,
            actor=ACTOR,
            environment=environment,
            at=now,
        )


def test_preflight_authorization_accepts_running_lease_and_required_names() -> None:
    repository = ExperimentRepository(ROOT)
    bundle, _, state, now = _authorized_state(repository)

    _authorize_preflight(
        bundle,
        _summary(),
        state,
        actor=ACTOR,
        environment={name: "configured" for name in bundle.execution.required_env},
        at=now,
    )


def test_preflight_cli_rejects_currently_blocked_bundle_before_execution() -> None:
    repository = ExperimentRepository(ROOT)

    with pytest.raises(ValueError, match="requires the primary lab issue to be running"):
        repository.authorize_preflight(
            "terminal-bench-magenta-smoke",
            actor=ACTOR,
            environment={name: "configured" for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL")},
        )


def test_direct_shell_preflight_fails_closed_without_explicit_binding() -> None:
    environment = os.environ.copy()
    environment.pop("BMP_LAB_ACTOR", None)
    environment.pop("BMP_LAB_ISSUE_ID", None)
    result = subprocess.run(
        (
            "bash",
            "scripts/preflight_experiment.sh",
            "MagentaBench/conformance/experiments/terminal-bench-magenta-smoke.toml",
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "BMP_LAB_ACTOR and BMP_LAB_ISSUE_ID are required" in result.stderr


def test_preflight_script_uses_only_frozen_uv_commands() -> None:
    script = (ROOT / "scripts/preflight_experiment.sh").read_text(encoding="utf-8")

    assert "uv run --frozen" in script
    assert not re.search(r"uv run(?! --frozen)", script)
    subprocess.run(("bash", "-n", str(ROOT / "scripts/preflight_experiment.sh")), check=True)
