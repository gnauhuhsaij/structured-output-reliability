from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from structured_output_reliability.metrics import REQUIRED_METRICS, aggregate_condition

BASE_TIME = datetime(2026, 10, 10, tzinfo=UTC)


def _condition(*, repetitions: int, stream: bool = False) -> dict[str, Any]:
    return {
        "condition_id": "cond_0000000000000001",
        "comparison_key": "cmp_0000000000000001",
        "endpoint": {"model": "mock-model"},
        "concurrency": 2,
        "prompt_length": {"target": 128, "unit": "characters"},
        "output_shape": {
            "target_length": 32,
            "target_unit": "characters",
            "max_completion_tokens": 48,
        },
        "temperature": 0,
        "request_seed": 42,
        "structured_mode": {"id": "prompt_only"},
        "stream": stream,
        "repetitions": repetitions,
    }


def _case(index: int) -> dict[str, Any]:
    return {
        "case_id": f"case_{index:016x}",
        "comparison": {
            "numeric": "exact",
            "absolute_tolerance": 0,
            "relative_tolerance": 0,
            "arrays": "ordered",
        },
    }


def _attempt(
    case: dict[str, Any],
    repeat_id: int,
    *,
    start_seconds: float,
    observed_ms: float,
    request_success: bool,
    json_parsed: bool,
    schema_valid: bool,
    exact_correct: bool,
    correct_fields: int,
    total_fields: int,
    parsed_json: Any,
    primary_outcome: str,
    output_tokens: int | None,
    ttft_ms: float | None = None,
    tpot_ms: float | None = None,
) -> dict[str, Any]:
    started_at = BASE_TIME + timedelta(seconds=start_seconds)
    return {
        "case_id": case["case_id"],
        "repeat_id": repeat_id,
        "phase": "measure",
        "terminal": True,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "score": {
            "request_success": request_success,
            "json_parsed": json_parsed,
            "parsed_json": parsed_json,
            "schema_valid": schema_valid,
            "exact_correct": exact_correct,
            "correct_fields": correct_fields,
            "total_fields": total_fields,
            "primary_outcome": primary_outcome,
        },
        "timing": {
            "observed_duration_ns": int(observed_ms * 1_000_000),
            "e2e_latency_ns": (
                int(observed_ms * 1_000_000) if request_success else None
            ),
            "ttft_ns": int(ttft_ms * 1_000_000) if ttft_ms is not None else None,
            "tpot_ns": int(tpot_ms * 1_000_000) if tpot_ms is not None else None,
        },
        "tokens": {
            "output": output_tokens,
            "token_count_source": "server" if output_tokens is not None else "unavailable",
            "tokenizer": None,
        },
    }


def test_correctness_field_consistency_latency_and_throughput_denominators() -> None:
    condition = _condition(repetitions=2)
    first, second = _case(1), _case(2)
    attempts = [
        _attempt(
            first,
            0,
            start_seconds=0,
            observed_ms=100,
            request_success=True,
            json_parsed=True,
            schema_valid=True,
            exact_correct=True,
            correct_fields=2,
            total_fields=2,
            parsed_json={"value": 1},
            primary_outcome="exact_correct",
            output_tokens=10,
        ),
        _attempt(
            second,
            0,
            start_seconds=0,
            observed_ms=200,
            request_success=False,
            json_parsed=False,
            schema_valid=False,
            exact_correct=False,
            correct_fields=0,
            total_fields=3,
            parsed_json=None,
            primary_outcome="timeout",
            output_tokens=None,
        ),
        _attempt(
            first,
            1,
            start_seconds=1,
            observed_ms=300,
            request_success=True,
            json_parsed=True,
            schema_valid=False,
            exact_correct=False,
            correct_fields=1,
            total_fields=2,
            parsed_json={"value": 2},
            primary_outcome="schema_invalid",
            output_tokens=10,
        ),
        _attempt(
            second,
            1,
            start_seconds=1,
            observed_ms=100,
            request_success=True,
            json_parsed=False,
            schema_valid=False,
            exact_correct=False,
            correct_fields=0,
            total_fields=3,
            parsed_json=None,
            primary_outcome="invalid_json",
            output_tokens=5,
        ),
    ]

    summary = aggregate_condition(
        condition,
        [first, second],
        attempts,
        experiment_seed=7,
        manifest_complete=True,
    )
    metrics = summary["metrics"]

    assert summary["planned_attempts"] == 4
    assert summary["started_attempts"] == 4
    assert summary["terminal_attempts"] == 4
    assert summary["run_complete"] is True
    assert set(REQUIRED_METRICS).issubset(metrics)
    assert metrics["request_success_rate"]["value"] == 3 / 4
    assert metrics["json_parse_rate"]["value"] == 2 / 4
    assert metrics["schema_valid_rate"]["value"] == 1 / 4
    assert metrics["exact_correct_rate"]["value"] == 1 / 4
    assert metrics["schema_valid_given_parse"]["value"] == 1 / 2
    assert metrics["exact_correct_given_schema"]["value"] == 1
    assert metrics["micro_field_accuracy"]["value"] == 3 / 10
    assert metrics["non_padding_field_accuracy"]["value"] == 3 / 10
    assert metrics["macro_field_accuracy"]["value"] == 3 / 8
    assert metrics["repeat_consistency_rate"]["value"] == 0
    assert metrics["all_repeats_correct_case_rate"]["value"] == 0
    assert metrics["median_e2e_latency_ms"]["value"] == 100
    assert metrics["p95_e2e_latency_ms"]["value"] is None
    assert metrics["median_ttft_ms"]["value"] is None
    assert metrics["attempt_throughput_rps"]["value"] == pytest.approx(8)
    assert metrics["request_throughput_rps"]["value"] == pytest.approx(6)
    assert metrics["output_token_throughput_tps"]["value"] == pytest.approx(50)
    assert metrics["quality_goodput_rps"]["value"] == pytest.approx(2)
    assert metrics["request_success_rate"]["interval"]["method"] == "wilson"
    assert metrics["micro_field_accuracy"]["interval"]["resamples"] == 10_000

    repeated = aggregate_condition(
        condition,
        [first, second],
        attempts,
        experiment_seed=7,
        manifest_complete=True,
    )
    assert repeated["metrics"] == metrics


def test_consistency_is_independent_of_correctness() -> None:
    condition = _condition(repetitions=2)
    case = _case(1)
    attempts = [
        _attempt(
            case,
            repeat_id,
            start_seconds=repeat_id,
            observed_ms=10,
            request_success=True,
            json_parsed=True,
            schema_valid=True,
            exact_correct=False,
            correct_fields=0,
            total_fields=1,
            parsed_json={"wrong": 9},
            primary_outcome="field_incorrect",
            output_tokens=1,
        )
        for repeat_id in range(2)
    ]

    metrics = aggregate_condition(
        condition,
        [case],
        attempts,
        experiment_seed=1,
        manifest_complete=True,
    )["metrics"]

    assert metrics["repeat_consistency_rate"]["value"] == 1
    assert metrics["exact_correct_rate"]["value"] == 0


def test_p95_uses_linear_interpolation_and_is_emitted_at_twenty_samples() -> None:
    condition = _condition(repetitions=1)
    cases = [_case(index) for index in range(1, 21)]
    attempts = [
        _attempt(
            case,
            0,
            start_seconds=0,
            observed_ms=index,
            request_success=True,
            json_parsed=True,
            schema_valid=True,
            exact_correct=True,
            correct_fields=1,
            total_fields=1,
            parsed_json={"value": index},
            primary_outcome="exact_correct",
            output_tokens=1,
        )
        for index, case in enumerate(cases, 1)
    ]

    metric = aggregate_condition(
        condition,
        cases,
        attempts,
        experiment_seed=2,
        manifest_complete=True,
    )["metrics"]["p95_e2e_latency_ms"]

    assert metric["value"] == pytest.approx(19.05)
    assert metric["eligible_observations"] == 20
    assert metric["low_confidence"] is True
    assert metric["interval"]["method"] == "percentile_cluster_bootstrap"


def test_throughput_interval_bootstraps_complete_repetitions() -> None:
    condition = _condition(repetitions=3)
    case = _case(1)
    attempts = [
        _attempt(
            case,
            repeat_id,
            start_seconds=repeat_id,
            observed_ms=duration,
            request_success=True,
            json_parsed=True,
            schema_valid=True,
            exact_correct=True,
            correct_fields=1,
            total_fields=1,
            parsed_json={"value": 1},
            primary_outcome="exact_correct",
            output_tokens=2,
        )
        for repeat_id, duration in enumerate((100, 200, 400))
    ]

    metric = aggregate_condition(
        condition,
        [case],
        attempts,
        experiment_seed=3,
        manifest_complete=True,
    )["metrics"]["attempt_throughput_rps"]

    assert metric["value"] == pytest.approx(3 / 0.7)
    assert metric["eligible_observations"] == 3
    assert metric["interval"]["method"] == "percentile_repetition_bootstrap"
    assert metric["interval"]["sample_count"] == 3
    assert metric["low_confidence"] is True


def test_incomplete_condition_preserves_planned_counts_and_disables_diagnostic() -> None:
    condition = _condition(repetitions=2)
    case = _case(1)
    attempt = _attempt(
        case,
        0,
        start_seconds=0,
        observed_ms=10,
        request_success=True,
        json_parsed=True,
        schema_valid=True,
        exact_correct=True,
        correct_fields=1,
        total_fields=1,
        parsed_json={"value": 1},
        primary_outcome="exact_correct",
        output_tokens=1,
    )

    summary = aggregate_condition(
        condition,
        [case],
        [attempt],
        experiment_seed=4,
        manifest_complete=False,
    )

    assert summary["planned_attempts"] == 2
    assert summary["started_attempts"] == 1
    assert summary["run_complete"] is False
    diagnostic = summary["metrics"]["all_repeats_correct_case_rate"]
    assert diagnostic["value"] is None
    assert diagnostic["denominator"] == 1


def test_quality_goodput_is_unavailable_when_a_configured_slo_is_unmeasurable() -> None:
    condition = _condition(repetitions=1, stream=True)
    condition["slos"] = {"max_ttft_ms": 20}
    case = _case(1)
    attempt = _attempt(
        case,
        0,
        start_seconds=0,
        observed_ms=10,
        request_success=True,
        json_parsed=True,
        schema_valid=True,
        exact_correct=True,
        correct_fields=1,
        total_fields=1,
        parsed_json={"value": 1},
        primary_outcome="exact_correct",
        output_tokens=2,
        ttft_ms=None,
    )

    metric = aggregate_condition(
        condition,
        [case],
        [attempt],
        experiment_seed=5,
        manifest_complete=True,
    )["metrics"]["quality_goodput_rps"]

    assert metric["value"] is None
    assert metric["numerator"] is None
    assert metric["denominator"] == pytest.approx(0.01)


def test_output_token_throughput_does_not_pool_incompatible_sources() -> None:
    condition = _condition(repetitions=2)
    case = _case(1)
    attempts = [
        _attempt(
            case,
            repeat_id,
            start_seconds=repeat_id,
            observed_ms=10,
            request_success=True,
            json_parsed=True,
            schema_valid=True,
            exact_correct=True,
            correct_fields=1,
            total_fields=1,
            parsed_json={"value": 1},
            primary_outcome="exact_correct",
            output_tokens=2,
        )
        for repeat_id in range(2)
    ]
    attempts[1]["tokens"]["token_count_source"] = "local_tokenizer"
    attempts[1]["tokens"]["tokenizer"] = "mock/tokenizer"

    metric = aggregate_condition(
        condition,
        [case],
        attempts,
        experiment_seed=6,
        manifest_complete=True,
    )["metrics"]["output_token_throughput_tps"]

    assert metric["value"] is None
    assert metric["numerator"] is None
    assert metric["denominator"] == pytest.approx(0.02)


def test_no_metric_contains_non_finite_numbers() -> None:
    condition = _condition(repetitions=1)
    case = _case(1)
    summary = aggregate_condition(
        condition,
        [case],
        [],
        experiment_seed=7,
        manifest_complete=False,
    )

    for metric in summary["metrics"].values():
        for key in ("value", "numerator", "denominator"):
            value = metric[key]
            assert value is None or math.isfinite(value)


def test_non_padding_accuracy_separates_filler_from_task_fields() -> None:
    condition = _condition(repetitions=3)
    case = _case(1)
    case["generator"] = {"name": "sor-built-in"}
    case["expected_field_paths"] = ["/active", "/count", "/label", "/padding"]
    attempts = [
        _attempt(
            case,
            repeat_id,
            start_seconds=repeat_id,
            observed_ms=10,
            request_success=repeat_id != 2,
            json_parsed=repeat_id != 2,
            schema_valid=repeat_id != 2,
            exact_correct=False,
            correct_fields=3 if repeat_id != 2 else 0,
            total_fields=4,
            parsed_json={} if repeat_id != 2 else None,
            primary_outcome="field_incorrect" if repeat_id != 2 else "timeout",
            output_tokens=5 if repeat_id != 2 else None,
        )
        for repeat_id in range(3)
    ]
    attempts[0]["score"]["comparison_errors"] = [{"path": "/padding"}]
    attempts[1]["score"]["comparison_errors"] = [{"path": "/count"}]
    attempts[2]["score"]["comparison_errors"] = []

    metrics = aggregate_condition(
        condition,
        [case],
        attempts,
        experiment_seed=8,
        manifest_complete=True,
    )["metrics"]

    assert metrics["micro_field_accuracy"]["value"] == 6 / 12
    assert metrics["non_padding_field_accuracy"]["value"] == 5 / 9
    assert metrics["non_padding_field_accuracy"]["numerator"] == 5
    assert metrics["non_padding_field_accuracy"]["denominator"] == 9
