from __future__ import annotations

from pathlib import Path

import pytest

from MagentaBench.runner.compiler import CompilationError, Compiler

ROOT = Path(__file__).parents[1]
# Retained for when whole_harness is reactivated: the pinned image and local task
# data that the full container-contract assertions require.
IMAGE = "sha256:8b54d62b3ff7fb4521b4291d5c0622d54c25333786996e8d807c66d2d748c222"


def test_aose_coding_agent_path_is_inactive_without_capabilities(
    aosebench_source: Path, bind_registry_source
) -> None:
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
        compiler = Compiler(ROOT)
        bind_registry_source(
            compiler,
            "dataset",
            "dataset.aosebench.biomnibench-da.v1",
            aosebench_source,
        )
        bind_registry_source(
            compiler,
            "subject",
            "aose.dryrun.true" if name.endswith("a.toml") else "aose.dryrun.echo",
            aosebench_source,
        )
        assert Path(
            compiler._dataset_artifact(
                "dataset.aosebench.biomnibench-da.v1"
            ).source
        ) == aosebench_source.resolve()
        with pytest.raises(CompilationError, match="missing required adapter capabilities"):
            compiler.compile(experiments / name)
