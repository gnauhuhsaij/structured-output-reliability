from __future__ import annotations

import copy
from typing import Any

import pytest

from structured_output_reliability.report import (
    evaluate_gates,
    render_markdown_report,
)


def _metric(
    value: float | None,
    *,
    lower: float | None = None,
    upper: float | None = None,
) -> dict[str, Any]:
    interval = None
    if lower is not None and upper is not None:
        interval = {"lower": lower, "upper": upper}
    return {
        "value": value,
        "numerator": value,
        "denominator": 1 if value is not None else None,
        "eligible_observations": 1 if value is not None else 0,
        "interval": interval,
        "low_confidence": False,
    }


def _condition(
    *, condition_id: str, comparison_key: str, exact: float
) -> dict[str, Any]:
    metrics = {
        "request_success_rate": _metric(1, lower=0.8, upper=1),
        "json_parse_rate": _metric(1, lower=0.8, upper=1),
        "schema_valid_rate": _metric(exact, lower=max(0, exact - 0.05), upper=1),
        "micro_field_accuracy": _metric(exact),
        "macro_field_accuracy": _metric(exact),
        "exact_correct_rate": _metric(exact, lower=max(0, exact - 0.05), upper=1),
        "repeat_consistency_rate": _metric(None),
        "median_e2e_latency_ms": _metric(100),
        "p95_e2e_latency_ms": _metric(None),
        "median_ttft_ms": _metric(None),
        "p95_ttft_ms": _metric(None),
        "median_tpot_ms": _metric(None),
        "p95_tpot_ms": _metric(None),
        "attempt_throughput_rps": _metric(10),
        "request_throughput_rps": _metric(10),
        "output_token_throughput_tps": _metric(100),
        "quality_goodput_rps": _metric(10 * exact),
    }
    return {
        "condition_id": condition_id,
        "comparison_key": comparison_key,
        "dimensions": {
            "model": "mock",
            "quantization": None,
            "concurrency": 1,
            "prompt_length": 128,
            "prompt_length_unit": "characters",
            "output_length": 32,
            "output_length_unit": "characters",
            "max_completion_tokens": 48,
            "temperature": 0,
            "request_seed": 42,
            "structured_mode": "json_schema",
            "stream": False,
            "repetitions": 1,
        },
        "planned_attempts": 1,
        "started_attempts": 1,
        "terminal_attempts": 1,
        "run_complete": True,
        "outcome_counts": {"exact_correct": 1},
        "metrics": metrics,
    }


def _summary(run_id: str, condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_schema_version": "1.0.0",
        "metric_contract_version": "1.0.0",
        "run_id": run_id,
        "run_complete": True,
        "generated_at": "2026-10-10T00:00:00Z",
        "condition_count": 1,
        "conditions": [condition],
    }


def test_absolute_gate_uses_requested_confidence_bound() -> None:
    candidate = _summary(
        "candidate", _condition(condition_id="cond_candidate", comparison_key="cmp_1", exact=0.99)
    )
    gates = [
        {
            "metric": "exact_correct_rate",
            "kind": "minimum",
            "value": 0.95,
            "estimate": "lower_ci",
        }
    ]

    results, passed = evaluate_gates(candidate, gates)

    assert passed is False
    assert results[0]["candidate_value"] == 0.94
    assert results[0]["status"] == "fail"
    assert results[0]["reason"] == "threshold violated"


def test_relative_gate_pairs_only_exact_comparison_key() -> None:
    candidate = _summary(
        "candidate", _condition(condition_id="cond_candidate", comparison_key="cmp_same", exact=0.9)
    )
    baseline = _summary(
        "baseline", _condition(condition_id="cond_baseline", comparison_key="cmp_same", exact=1)
    )
    gates = [
        {
            "metric": "exact_correct_rate",
            "kind": "max_drop",
            "value": 0.05,
            "estimate": "point",
        }
    ]

    results, passed = evaluate_gates(candidate, gates, baseline)

    assert passed is False
    assert results[0]["baseline_condition_id"] == "cond_baseline"
    assert results[0]["observed"] == pytest.approx(0.1)


def test_relative_gate_cannot_pass_with_missing_or_duplicate_baseline_match() -> None:
    candidate = _summary(
        "candidate", _condition(condition_id="cond_candidate", comparison_key="cmp_candidate", exact=1)
    )
    baseline = _summary(
        "baseline", _condition(condition_id="cond_baseline", comparison_key="cmp_other", exact=1)
    )
    gate = {
        "metric": "exact_correct_rate",
        "kind": "max_drop",
        "value": 0,
        "estimate": "point",
    }

    results, passed = evaluate_gates(candidate, [gate], baseline)
    assert passed is False
    assert results[0]["status"] == "unavailable"
    assert "no baseline condition" in results[0]["reason"]

    duplicate = _condition(
        condition_id="cond_duplicate", comparison_key="cmp_candidate", exact=1
    )
    baseline["conditions"] = [
        _condition(condition_id="cond_first", comparison_key="cmp_candidate", exact=1),
        duplicate,
    ]
    baseline["condition_count"] = 2
    results, passed = evaluate_gates(candidate, [gate], baseline)
    assert passed is False
    assert "multiple baseline conditions" in results[0]["reason"]


def test_markdown_report_shows_counts_unavailable_values_and_gate_status() -> None:
    candidate = _summary(
        "candidate", _condition(condition_id="cond_candidate", comparison_key="cmp_1", exact=1)
    )
    config = {"name": "report-demo", "gates": []}

    markdown = render_markdown_report(
        config=config,
        candidate=candidate,
        baseline=None,
        baseline_directory=None,
        gate_results=[],
        gates_passed=True,
        comparison={"matched": 0, "unmatched_candidate": 0, "unmatched_baseline": 0},
    )

    assert "# Structured Output Reliability Report: report-demo" in markdown
    assert "Regression gates: **NOT CONFIGURED**" in markdown
    assert "100.00% (1/1)" in markdown
    assert "| — |" in markdown
    assert "exact_correct=1" in markdown


def test_report_uses_separate_condition_columns_in_metric_tables() -> None:
    first = _condition(condition_id="cond_first", comparison_key="cmp_first", exact=1)
    second = copy.deepcopy(first)
    second["condition_id"] = "cond_second"
    second["comparison_key"] = "cmp_second"
    second["dimensions"]["concurrency"] = 4
    second["dimensions"]["structured_mode"] = "prompt_only"
    candidate = _summary("candidate", first)
    candidate["conditions"].append(second)
    candidate["condition_count"] = 2

    markdown = render_markdown_report(
        config={"name": "report-demo", "gates": []},
        candidate=candidate,
        baseline=None,
        baseline_directory=None,
        gate_results=[],
        gates_passed=True,
        comparison={"matched": 0, "unmatched_candidate": 0, "unmatched_baseline": 0},
    )

    assert "| Model | `mock` |" in markdown
    assert "### Latency" in markdown
    assert "### Throughput" in markdown
    assert "| Dimensions |" not in markdown
    for heading in (
        "## Correctness and reliability",
        "### Latency",
        "### Throughput",
    ):
        section = markdown.split(f"{heading}\n\n", 1)[1].split("\n\n", 1)[0]
        rows = [line.strip("|").split("|") for line in section.splitlines()]
        headers = [cell.strip() for cell in rows[0]]
        assert headers[:8] == [
            "Condition",
            "Concurrency",
            "Prompt length",
            "Output length",
            "Max tokens",
            "Temperature",
            "Seed",
            "Mode",
        ]
        assert len(rows) == 4
        assert all(len(row) == len(headers) for row in rows)
        assert [cell.strip() for cell in rows[2][:8]] == [
            "`cond_first`", "1", "128 characters", "32 characters", "48", "0", "42", "json_schema"
        ]
        assert [cell.strip() for cell in rows[3][:8]] == [
            "`cond_second`", "4", "128 characters", "32 characters", "48", "0", "42", "prompt_only"
        ]
