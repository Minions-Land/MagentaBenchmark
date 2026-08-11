from __future__ import annotations

from itertools import combinations
from math import comb, isfinite
from pathlib import Path

import pytest
from pydantic import ValidationError

from MagentaBench.runner.compiler import CompilationError, Compiler
from MagentaBench.runner.pipeline import Pipeline
from MagentaBench.schemas import (
    MetricSpec,
    load_metric_spec,
    verify_run_report,
)
from MagentaBench.schemas.metrics import (
    estimate_pass_at_k,
    estimate_pass_power_k,
    expected_max_at_k,
    quantile_linear,
)


ROOT = Path(__file__).parents[1]
REPEATED_EXPERIMENT = (
    ROOT / "MagentaBench/conformance/experiments/repeated-sampling-smoke.toml"
)


def test_pass_at_k_and_pass_power_k_have_distinct_stable_semantics() -> None:
    for n in range(1, 20):
        for successes in range(n + 1):
            pass_values = []
            reliability_values = []
            for k in range(1, n + 1):
                expected_any = 1.0 - comb(n - successes, k) / comb(n, k)
                expected_all = (
                    0.0
                    if successes < k
                    else comb(successes, k) / comb(n, k)
                )
                observed_any = estimate_pass_at_k(n, successes, k)
                observed_all = estimate_pass_power_k(n, successes, k)
                assert observed_any == pytest.approx(expected_any)
                assert observed_all == pytest.approx(expected_all)
                pass_values.append(observed_any)
                reliability_values.append(observed_all)
            assert pass_values == sorted(pass_values)
            assert reliability_values == sorted(reliability_values, reverse=True)
            assert pass_values[-1] == float(successes > 0)
            assert reliability_values[-1] == float(successes == n)
            assert pass_values[0] == pytest.approx(successes / n)
            assert reliability_values[0] == pytest.approx(successes / n)

    assert isfinite(estimate_pass_at_k(1_000_000, 123_456, 100_000))
    assert isfinite(estimate_pass_power_k(1_000_000, 900_000, 100_000))


def test_expected_max_at_k_and_linear_quantile_are_replayable() -> None:
    values = [0.1, 0.4, 0.7, 1.0]
    for k in range(1, len(values) + 1):
        brute_force = sum(max(items) for items in combinations(values, k)) / comb(
            len(values), k
        )
        assert expected_max_at_k(values, k) == pytest.approx(brute_force)
    assert quantile_linear(values, 0.0) == 0.1
    assert quantile_linear(values, 0.5) == pytest.approx(0.55)
    assert quantile_linear(values, 0.95) == pytest.approx(0.955)


def test_repeated_metric_registry_closes_sampling_assumptions() -> None:
    pass_at_k = load_metric_spec(
        ROOT / "registries/metrics/pass-at-2-unbiased-v1.toml"
    )
    assert pass_at_k.parameters is not None and pass_at_k.parameters.k == 2
    assert pass_at_k.sampling is not None
    assert pass_at_k.sampling.subset_policy == "uniform_without_replacement"
    assert "task" in pass_at_k.sampling.exchangeability_keys
    assert pass_at_k.uncertainty is not None
    assert pass_at_k.uncertainty.rng_algorithm == "sha256_counter_v1"

    payload = pass_at_k.model_dump(mode="json")
    payload["sampling"] = None
    with pytest.raises(ValidationError, match="exchangeable sampling design"):
        MetricSpec.model_validate(payload)

    prefix = load_metric_spec(
        ROOT / "registries/metrics/empirical-any-at-2-v1.toml"
    )
    assert prefix.sampling is not None
    assert prefix.sampling.subset_policy == "first_k"


def test_pass_at_1_cannot_shrink_the_planned_rollout_denominator() -> None:
    metric = load_metric_spec(
        ROOT / "registries/metrics/pass-at-1-infra-zero-v1.toml"
    )
    for field, value in (
        ("missing_observation", "exclude"),
        ("status_policy.infra_error", "exclude"),
        ("status_policy.timeout", "invalidate"),
    ):
        payload = metric.model_dump(mode="json")
        if "." in field:
            table, key = field.split(".", 1)
            payload[table][key] = value
        else:
            payload[field] = value
        with pytest.raises(
            ValidationError,
            match="requires a value for every planned rollout slot",
        ):
            MetricSpec.model_validate(payload)


def test_repeated_sampling_pipeline_replays_groups_and_uncertainty(
    tmp_path: Path, bind_host_subprocess_backend
) -> None:
    pipeline = Pipeline(ROOT, tmp_path)
    bind_host_subprocess_backend(pipeline.compiler)
    completed = pipeline.run(REPEATED_EXPERIMENT)
    results = [
        item
        for item in completed.report.metric_results
        if item.metric_id == "pass-at-2.unbiased.v1"
    ]
    assert sorted(item.value for item in results) == [0.0, 1.0]
    assert all(
        len(item.groups) == 1
        and item.groups[0].population_count == 4
        and item.groups[0].subset_size == 2
        and item.groups[0].numerator is None
        and item.groups[0].denominator is None
        and item.denominator == 1.0
        and item.uncertainty is not None
        and item.uncertainty.rng_algorithm == "sha256_counter_v1"
        and item.uncertainty.replicate_distribution_digest is not None
        and item.sample_ledger_digest == item.canonical_sample_ledger_digest()
        for item in results
    )
    pass_at_1 = [
        item
        for item in completed.report.metric_results
        if item.metric_id == "pass-at-1.infra-zero.task-bootstrap.v1"
    ]
    assert len(pass_at_1) == 2
    assert all(
        len(item.groups) == 1
        and item.uncertainty is not None
        and item.uncertainty.resampling_unit == "task"
        and item.uncertainty.rng_algorithm == "sha256_counter_v1"
        for item in pass_at_1
    )
    verify_run_report(completed.report_path)


def test_rollout_wilson_is_rejected_for_repeated_samples(tmp_path: Path) -> None:
    source = REPEATED_EXPERIMENT.read_text(encoding="utf-8")
    source = source.replace(
        '"pass-at-1.infra-zero.task-bootstrap.v1"',
        '"pass-at-1.infra-zero.v1"',
    )
    source = source.replace('regime = "repeated-sampling.fake.v1"\n', "")
    source = source.replace('stage = "sample"\n', "")
    experiment = tmp_path / "repeated-wilson.toml"
    experiment.write_text(source, encoding="utf-8")
    with pytest.raises(
        CompilationError,
        match="Wilson uncertainty requires exactly one rollout per task",
    ):
        Compiler(ROOT).compile(experiment)
