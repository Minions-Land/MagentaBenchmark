from __future__ import annotations

from pathlib import Path

import pytest

from MagentaBench.runner.compiler import CompilationError, Compiler

ROOT = Path(__file__).parents[1]
# Retained for when whole_harness is reactivated: the pinned image and local task
# data that the full container-contract assertions require.
IMAGE = "sha256:8b54d62b3ff7fb4521b4291d5c0622d54c25333786996e8d807c66d2d748c222"
AOSE = Path("/mnt/aliyunsb/BioAgent/AOSEBench")
DATA = Path("/mnt/aliyunsb/BioAgent/BiomniBench-DA/Data")


def test_aose_coding_agent_path_is_inactive_without_capabilities() -> None:
    """AOSE stays fail-closed until its production adapters are registered.

    Deactivation is evidenced, not inferred: the recorded bundle under
    records/aose-zero-cost-run-a/f541c9dd.../cases/da-1-3/ has status=no_output,
    verifier_evidence=null and output_refs=[], because the subject entrypoint is
    /usr/bin/true. Its claim_report already records execution_valid=false. There is
    no registered loader, backend factory, or execution capability to activate
    the coding-agent path.

    Container mechanics previously asserted here (image digest pin, instruction
    sha256, read-only data probe, network_mode=none, judge_invocations=0, agent
    executable digest) remain in those bundle records. Restore the full path only
    once AOSE has production capability declarations and trajectory closure.
    """
    experiments = ROOT / "MagentaBench/conformance/experiments"
    for name in ("aose-zero-cost-run-a.toml", "aose-zero-cost-run-b.toml"):
        with pytest.raises(CompilationError, match="missing required adapter capabilities"):
            Compiler(ROOT).compile(experiments / name)
