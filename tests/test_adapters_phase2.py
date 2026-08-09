from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from MagentaBench.adapters.benchmarks.aosebench import (
    AoseBenchConfigurationError,
    build_docker_command,
    check_outputs,
    load_task,
)
from MagentaBench.adapters.subjects.cli_agent import (
    CliInvocationResult,
    MagentaJsonlError,
    MagentaLaunchConfiguration,
    build_cli_command,
    extract_answer,
    parse_magenta_jsonl,
    resolve_magenta_configuration,
    run_cli_agent,
    scrubbed_environment,
    write_magenta_settings,
    write_cli_outputs,
)
from MagentaBench.schemas import (
    CredentialRef,
    EvidenceBundle,
    ProviderBinding,
    ProvenanceRecord,
    RunStatus,
)


AOSE = Path("/mnt/aliyunsb/BioAgent/AOSEBench")


def _magenta_manifest(sequence: int) -> dict[str, object]:
    return {
        "type": "runtime_manifest",
        "protocolVersion": 1,
        "runId": "run-1",
        "sequence": sequence,
        "mode": "json",
        "product": {
            "name": "Magenta",
            "version": "0.1.21",
            "infrastructureVersion": "0.80.2",
        },
        "execution": {
            "anthropicCacheAffinity": "auto",
            "capabilityPolicy": {
                "capabilities": {"workflows": False, "teammates": False},
                "capabilityDecisions": {
                    "workflows": {
                        "enabled": False,
                        "source": "profile",
                        "locked": False,
                    },
                    "teammates": {
                        "enabled": False,
                        "source": "profile",
                        "locked": False,
                    },
                },
                "tools": {
                    "allow": ["read"],
                    "deny": ["bash"],
                    "builtinTools": True,
                    "runtimeMutable": False,
                },
            },
        },
        "tools": {"active": ["read"], "available": [{"name": "read"}]},
        "resources": {
            "extensions": [],
            "skills": [],
            "prompts": [],
            "contextFiles": [],
            "harnessPackages": [],
            "packageTools": [],
            "userMcpTools": [],
        },
        "assembly": {
            "type": "hcp_assembly",
            "schemaVersion": 1,
            "canonicalAssemblyDigest": None,
            "dependencyFileClosure": None,
            "components": [],
            "resolvedAddresses": ["tool:read"],
            "activeTools": ["read"],
            "activeCapabilities": [],
            "systemPromptDigest": {
                "algorithm": "sha256",
                "value": "a" * 64,
            },
            "packageProvenance": [],
            "diagnostics": [],
            "versions": {"magenta": "0.1.21", "runtime": "v24.0.0"},
            "activationReceipt": {
                "status": "observed",
                "addresses": ["tool:read"],
            },
            "namespaces": {
                "state": "/tmp/state",
                "cache": "/tmp/cache",
                "workspace": "/tmp/workspace",
            },
        },
    }


def _magenta_run_end(
    *, status: str = "success", exit_code: int = 0
) -> dict[str, object]:
    return {
        "type": "run_end",
        "protocolVersion": 1,
        "runId": "run-1",
        "status": status,
        "exitCode": exit_code,
        "startedAt": "2026-08-08T00:00:00.000Z",
        "endedAt": "2026-08-08T00:00:01.000Z",
        "durationMs": 1000,
        "stats": {},
        "background": {"policy": "cancel", "settled": True, "events": []},
        "nonInteractiveUi": {"policy": "deny", "requestCount": 0},
    }


def _magenta_stream(*, terminal: dict[str, object] | None = None) -> str:
    return "\n".join(
        json.dumps(item)
        for item in (
            _magenta_manifest(1),
            _magenta_manifest(2),
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "magenta answer"}],
                },
            },
            terminal or _magenta_run_end(),
        )
    )


def test_aosebench_task_contract_and_output_check(tmp_path: Path) -> None:
    task = load_task(AOSE, "da-1-3")
    assert task.instruction_path.name == "instruction.md"
    assert task.rubric_path.name == "rubric.txt"
    assert task.agent_timeout_seconds == 3600.0
    assert task.verifier_timeout_seconds == 900.0
    assert check_outputs(tmp_path).status == RunStatus.no_output
    (tmp_path / "trace.md").write_text("analysis", encoding="utf-8")
    (tmp_path / "answer.txt").write_text("answer", encoding="utf-8")
    checked = check_outputs(tmp_path)
    assert checked.status == RunStatus.pass_
    assert len(checked.output_refs) == 2


def test_aosebench_docker_command_uses_readonly_contract(tmp_path: Path, monkeypatch) -> None:
    task_dir = tmp_path / "da-1-3"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("do task", encoding="utf-8")
    (task_dir / "tests" / "rubric.txt").write_text("rubric", encoding="utf-8")
    (task_dir / "task.toml").write_text(
        "[environment]\ncpus=2\nmemory_mb=100\nstorage_mb=200\nallow_internet=false\n",
        encoding="utf-8",
    )
    data = tmp_path / "data" / "da-1-3" / "environment" / "data"
    data.mkdir(parents=True)
    from MagentaBench.adapters.benchmarks.aosebench import AoseTask
    import tomllib

    task = AoseTask(
        task_id="da-1-3",
        task_dir=task_dir,
        instruction_path=task_dir / "instruction.md",
        rubric_path=task_dir / "tests" / "rubric.txt",
        task_config=tomllib.loads((task_dir / "task.toml").read_text()),
        instruction_digest=hashlib.sha256(
            (task_dir / "instruction.md").read_bytes()
        ).hexdigest(),
        data_path=data,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    command = build_docker_command(
        task,
        image="biomnibench-da-magenta-0.0.22",
        output_root=tmp_path / "out",
        runner_command="/opt/agent/run_task",
        pass_env=("OPENAI_API_KEY",),
    )
    joined = " ".join(command)
    assert "/app/data:ro" in joined
    assert "/app/instruction.md:ro" in joined
    assert "biomnibench-da-magenta-0.0.22" in command
    assert "OPENAI_API_KEY" in command
    assert "secret-value" not in joined


def test_cli_command_variants_and_provider_extraction() -> None:
    assert build_cli_command("claude", prompt="p")[:4] == (
        "claude",
        "-p",
        "--output-format",
        "stream-json",
    )
    assert build_cli_command("codex", prompt="p")[:2] == ("codex", "exec")
    magenta_command = build_cli_command("magenta", prompt="p", model="m")
    assert "--mode" in magenta_command
    assert "--anthropic-cache-affinity" in magenta_command
    assert "--no-harness-workflows" in magenta_command
    assert "--no-harness-teammates" in magenta_command
    assert "--lock-tools" in magenta_command
    claude = "\n".join(
        [
            json.dumps({"type": "assistant", "message": {"content": [{"text": "draft"}]}}),
            json.dumps({"type": "result", "result": "final answer"}),
        ]
    )
    assert extract_answer(claude, agent="claude") == "final answer"
    stream = _magenta_stream()
    parsed = parse_magenta_jsonl(stream)
    assert len(parsed.runtime_manifests) == 2
    assert parsed.effective_runtime_manifest["sequence"] == 2
    assert parsed.effective_assembly_sidecar is not None
    assert parsed.answer == "magenta answer"
    assert extract_answer(stream, agent="magenta") == "magenta answer"
    with pytest.raises(MagentaJsonlError, match="exactly one run_end"):
        parse_magenta_jsonl(stream + "\n" + json.dumps({"type": "run_end"}))
    gap_stream = stream.replace(
        json.dumps(_magenta_manifest(2)), json.dumps(_magenta_manifest(3))
    )
    with pytest.raises(MagentaJsonlError, match="contiguous"):
        parse_magenta_jsonl(gap_stream)
    missing_sidecar_field = _magenta_manifest(1)
    del missing_sidecar_field["assembly"]["versions"]
    malformed_sidecar_stream = stream.replace(
        json.dumps(_magenta_manifest(1)), json.dumps(missing_sidecar_field)
    )
    with pytest.raises(MagentaJsonlError, match="lacks required fields"):
        parse_magenta_jsonl(malformed_sidecar_stream)
    nullable_versions = _magenta_manifest(1)
    nullable_versions["assembly"]["versions"] = {"magenta": None, "runtime": None}
    nullable_stream = stream.replace(
        json.dumps(_magenta_manifest(1)), json.dumps(nullable_versions)
    )
    assert parse_magenta_jsonl(nullable_stream).effective_assembly_sidecar is not None
    assert extract_answer("codex answer", agent="codex") == "codex answer"


def test_magenta_v022_configuration_maps_to_stable_native_argv() -> None:
    configuration = {
        "agent": {
            "provider": "openai",
            "model": "gpt-5.6",
            "transport": "websocket-cached",
            "cacheRetention": "long",
            "openai_prompt_cache_mode": "explicit",
            "cacheTelemetry": True,
            "cacheDiagnostics": False,
            "harness": {"toolSearch": True},
            "retry": {
                "enabled": True,
                "provider": {"timeoutMs": 12_000, "maxRetries": 2},
            },
        }
    }
    command = build_cli_command("magenta", prompt="p", configuration=configuration)
    assert command == (
        "magenta",
        "--print",
        "--no-session",
        "--mode",
        "json",
        "--model",
        "gpt-5.6",
        "--provider",
        "openai",
        "--transport",
        "websocket-cached",
        "--cache-retention",
        "long",
        "--openai-prompt-cache-mode",
        "explicit",
        "--cache-telemetry",
        "--no-cache-diagnostics",
        "--anthropic-cache-affinity",
        "auto",
        "--no-harness-workflows",
        "--no-harness-teammates",
        "--harness-tool-search",
        "--lock-tools",
        "p",
    )
    resolved = resolve_magenta_configuration(configuration)
    assert isinstance(resolved, MagentaLaunchConfiguration)
    assert resolved.provider_timeout_seconds == 12.0
    assert resolved.settings_document()["retry"]["provider"]["timeoutMs"] == 12_000


def test_magenta_configuration_alias_conflicts_and_settings_are_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="conflicting Magenta configuration"):
        resolve_magenta_configuration(
            {"transport": "sse", "agent": {"transport": "websocket"}}
        )
    with pytest.raises(ValueError, match="secret-like key"):
        resolve_magenta_configuration({"api_key": "must-not-be-read"})
    settings = write_magenta_settings(
        tmp_path / "agent",
        {"retry": {"provider": {"timeoutMs": 1234, "maxRetries": 1}}},
    )
    assert json.loads(settings.read_text()) == {
        "retry": {"provider": {"maxRetries": 1, "timeoutMs": 1234}}
    }
    with pytest.raises(ValueError, match="differs"):
        write_magenta_settings(
            tmp_path / "agent",
            {"retry": {"provider": {"timeoutMs": 999}}},
        )


def test_magenta_configuration_receipt_binds_effective_settings_and_activation(
    tmp_path: Path,
) -> None:
    manifests = []
    for sequence in (1, 2):
        manifest = _magenta_manifest(sequence)
        manifest["model"] = {"provider": "openai", "id": "gpt-5.6", "api": "responses"}
        manifest["execution"].update(
            {
                "transport": "websocket",
                "cacheRetention": "long",
                "openaiPromptCacheMode": "explicit",
                "cacheTelemetry": True,
                "cacheDiagnostics": False,
                "harnessCapabilities": {
                    "workflows": False,
                    "teammates": False,
                    "toolSearch": True,
                },
            }
        )
        manifest["policies"] = {
            "retry": {
                "enabled": True,
                "provider": {"timeoutMs": 1234, "maxRetries": 1},
            }
        }
        manifests.append(manifest)
    stream = "\n".join(
        json.dumps(item)
        for item in (*manifests, {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}, _magenta_run_end())
    )
    result = CliInvocationResult(
        command=("/opt/bin/magenta",),
        returncode=0,
        stdout=stream,
        stderr="",
        duration_seconds=1.0,
        agent="magenta",
    )
    artifacts = write_cli_outputs(
        result,
        tmp_path / "receipt",
        configuration={
            "transport": "websocket",
            "cacheRetention": "long",
            "openaiPromptCacheMode": "explicit",
            "cacheTelemetry": True,
            "cacheDiagnostics": False,
            "toolSearch": True,
            "retry": {"provider": {"timeoutMs": 1234, "maxRetries": 1}},
            "model": "gpt-5.6",
            "provider": "openai",
        },
        provider_binding=ProviderBinding(
            provider_id="openai",
            base_url="https://api.openai.com/v1",
            wire_api="responses",
            model_id="gpt-5.6",
            credential_ref=CredentialRef(
                name="openai-primary",
                value_sha256="a" * 64,
                secret=True,
                source_file="credentials/providers.toml",
            ),
        ),
        requested_model="gpt-5.6",
    )
    assert artifacts.configuration_receipt is not None
    receipt = artifacts.configuration_receipt
    assert receipt.status == "matched"
    assert receipt.requested["toolSearch"] is True
    assert receipt.effective["cacheRetention"] == "long"
    assert receipt.effective["lockTools"] is True
    assert receipt.activation_receipt is not None
    assert receipt.path is not None and receipt.path.is_file()
    neutral = receipt.to_bmp_activation_receipt(
        configuration_digest="a" * 64,
    )
    assert neutral.configuration_digest == "a" * 64
    assert neutral.status == "matched"
    assert neutral.requested_paths
    assert neutral.requested_paths == neutral.activated_paths
    provenance = ProvenanceRecord(
        manifest_digest="0" * 64,
        runner_digest="1" * 64,
        benchmark_digest="2" * 64,
        subject_digest="3" * 64,
        backend_digest="4" * 64,
    )
    bound = artifacts.bind_provenance(
        provenance,
        configuration_digest="a" * 64,
    )
    assert bound.configuration_activation == neutral
    assert bound.model_activation is not None
    assert bound.model_activation.status == "unobserved"
    assert bound.model_activation.activated_provider_id is None
    assert bound.model_activation.activated_model_id is None
    assert "no provider-call binding evidence" in bound.model_activation.reason[0]
    status = json.loads(artifacts.status_path.read_text())
    assert status["configuration_activation_status"] == "matched"
    assert status["activation_receipt"]["status"] == "observed"
    assert status["model_activation"]["status"] == "unobserved"


def test_magenta_jsonl_never_falls_back_to_event_stream_as_answer() -> None:
    assert extract_answer(json.dumps({"result": "legacy"}), agent="magenta") == ""
    assert extract_answer("not json", agent="magenta") == ""


def test_magenta_invocation_status_uses_terminal_contract(tmp_path: Path) -> None:
    valid = CliInvocationResult(
        command=("/opt/bin/magenta",),
        returncode=0,
        stdout=_magenta_stream(),
        stderr="",
        duration_seconds=1.0,
        agent="magenta",
    )
    assert valid.status == RunStatus.pass_
    assert valid.answer_text() == "magenta answer"

    artifacts = write_cli_outputs(valid, tmp_path / "valid")
    _, _, valid_status_path = artifacts
    assert artifacts.runtime_manifest_receipt is not None
    assert artifacts.runtime_manifest_receipt.effective_sequence == 2
    assert (
        artifacts.runtime_manifest_receipt.trace_ref.sha256
        == hashlib.sha256(valid.stdout.encode("utf-8")).hexdigest()
    )
    assert (
        artifacts.runtime_manifest_receipt.effective_assembly_sidecar_ref
        == artifacts.runtime_manifest_receipt.assembly_sidecar_refs[-1]
    )
    provenance = ProvenanceRecord(
        manifest_digest="0" * 64,
        runner_digest="1" * 64,
        benchmark_digest="2" * 64,
        subject_digest="3" * 64,
        backend_digest="4" * 64,
    )
    assert (
        artifacts.bind_provenance(provenance).runtime_manifest_receipt
        == artifacts.runtime_manifest_receipt
    )
    bundle = EvidenceBundle(
        run_id="run-1",
        status=RunStatus.no_output,
        provenance=artifacts.bind_provenance(provenance),
    )
    assert bundle.effective_assembly_sidecar_ref is not None
    assert (
        bundle.effective_assembly_sidecar_ref.sha256
        == artifacts.runtime_manifest_receipt.assembly_sidecar_refs[-1].sha256
    )
    valid_status = json.loads(valid_status_path.read_text())
    effective_ref = valid_status["effective_assembly_sidecar_ref"]
    assert effective_ref["sequence"] == 2
    persisted_sidecar = Path(effective_ref["path"])
    assert persisted_sidecar.name == f'{effective_ref["sha256"]}.json'
    assert (
        hashlib.sha256(persisted_sidecar.read_bytes()).hexdigest()
        == effective_ref["sha256"]
    )
    assert persisted_sidecar.stat().st_size == effective_ref["size_bytes"]
    assert [
        item["sequence"] for item in valid_status["assembly_sidecar_refs"]
    ] == [1, 2]
    persisted_sidecar.write_text("{}\n", encoding="utf-8")
    with pytest.raises(MagentaJsonlError, match="sidecar content drift"):
        write_cli_outputs(valid, tmp_path / "valid")

    malformed = CliInvocationResult(
        command=("/opt/bin/magenta",),
        returncode=0,
        stdout=json.dumps({"result": "legacy"}),
        stderr="",
        duration_seconds=1.0,
        agent="magenta",
    )
    assert malformed.status == RunStatus.invalid_output
    malformed_artifacts = write_cli_outputs(malformed, tmp_path / "malformed")
    with pytest.raises(MagentaJsonlError, match="lack a valid runtime manifest"):
        malformed_artifacts.bind_provenance(provenance)

    contradictory = CliInvocationResult(
        command=("/opt/bin/magenta",),
        returncode=1,
        stdout=_magenta_stream(),
        stderr="",
        duration_seconds=1.0,
        agent="magenta",
    )
    assert contradictory.status == RunStatus.invalid_output

    failed = CliInvocationResult(
        command=("/opt/bin/magenta",),
        returncode=1,
        stdout=_magenta_stream(
            terminal=_magenta_run_end(status="error", exit_code=1)
        ),
        stderr="agent failed",
        duration_seconds=1.0,
        agent="magenta",
    )
    assert failed.status == RunStatus.agent_error

    unsettled_end = _magenta_run_end()
    unsettled_end["background"] = {
        "policy": "cancel",
        "settled": False,
        "events": [],
    }
    unsettled = CliInvocationResult(
        command=("/opt/bin/magenta",),
        returncode=0,
        stdout=_magenta_stream(terminal=unsettled_end),
        stderr="",
        duration_seconds=1.0,
        agent="magenta",
    )
    assert unsettled.status == RunStatus.invalid_output

    _, empty_answer, status_path = write_cli_outputs(malformed, tmp_path)
    assert empty_answer.read_bytes() == b""
    status_payload = json.loads(status_path.read_text())
    assert status_payload["status"] == "invalid_output"
    assert status_payload["headless_protocol_valid"] is False


def test_cli_echo_smoke_allowlists_environment_and_writes_outputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLI_SECRET_TOKEN", "do-not-leak")
    result = run_cli_agent(
        ("/bin/echo", "BMP_OK"),
        cwd=tmp_path,
        timeout_seconds=5,
        env_var_names=("CLI_SECRET_TOKEN",),
    )
    # Names are accepted as configuration, but arbitrary parent variables are
    # not inherited by the child process.
    assert result.status == RunStatus.pass_
    assert result.stdout == "BMP_OK\n"
    trace, answer, status = write_cli_outputs(result, tmp_path, agent="codex")
    assert trace.read_text() == "BMP_OK\n"
    assert answer.read_text() == "BMP_OK\n"
    assert json.loads(status.read_text())["answer_exists"] is True
    assert scrubbed_environment(("CLI_SECRET_TOKEN",)).get("CLI_SECRET_TOKEN") == "do-not-leak"


def test_aose_data_mount_identity_is_content_addressed(tmp_path: Path) -> None:
    from MagentaBench.adapters.benchmarks.aosebench import AoseTask

    roots = []
    for name in ("checkout-a", "checkout-b"):
        root = tmp_path / name / "data"
        root.mkdir(parents=True)
        (root / "payload.bin").write_bytes(b"same data")
        roots.append(root)

    def task(root: Path) -> AoseTask:
        return AoseTask(
            task_id="da-1-3",
            task_dir=root,
            instruction_path=root / "instruction.md",
            rubric_path=root / "rubric.txt",
            task_config={},
            instruction_digest="0" * 64,
            data_path=root,
        )

    first, second = (task(root) for root in roots)
    assert first.data_content_digest == second.data_content_digest
    (roots[1] / "payload.bin").write_bytes(b"changed")
    assert first.data_content_digest != second.data_content_digest
