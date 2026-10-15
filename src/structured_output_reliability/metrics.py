"""Offline aggregation for saved structured-output reliability runs."""

from __future__ import annotations

import csv
import hashlib
import math
import os
import random
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from structured_output_reliability.config import load_schema
from structured_output_reliability.generator import canonical_json
from structured_output_reliability.scorer import (
    StrictJSONError,
    semantically_equal,
    strict_json_loads,
)

SUMMARY_SCHEMA_VERSION = "1.0.0"
METRIC_CONTRACT_VERSION = "1.0.0"
CONFIDENCE_LEVEL = 0.95
WILSON_Z = 1.959963984540054
BOOTSTRAP_RESAMPLES = 10_000

REQUIRED_METRICS = (
    "request_success_rate",
    "json_parse_rate",
    "schema_valid_rate",
    "micro_field_accuracy",
    "non_padding_field_accuracy",
    "macro_field_accuracy",
    "exact_correct_rate",
    "repeat_consistency_rate",
    "median_e2e_latency_ms",
    "p95_e2e_latency_ms",
    "median_ttft_ms",
    "p95_ttft_ms",
    "median_tpot_ms",
    "p95_tpot_ms",
    "attempt_throughput_rps",
    "request_throughput_rps",
    "output_token_throughput_tps",
    "quality_goodput_rps",
)


class AggregationError(ValueError):
    """Raised when saved run artifacts are missing, corrupt, or inconsistent."""


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """One regenerated run summary and its machine-readable artifact paths."""

    summary: dict[str, Any]
    json_path: Path
    csv_path: Path


def build_summary(run_directory: str | Path) -> dict[str, Any]:
    """Validate saved evidence and aggregate it without writing artifacts."""

    directory = Path(run_directory)
    manifest = _load_json_object(directory / "manifest.json")
    config = _load_resolved_config(directory / "config.resolved.yaml")
    conditions = _load_jsonl(directory / "conditions.jsonl", "condition")
    cases = _load_jsonl(directory / "cases.jsonl", "case")
    attempts = _load_jsonl(directory / "responses.jsonl", "attempt")

    run_id = manifest.get("run_id")
    if not isinstance(run_id, str):
        raise AggregationError("manifest.json has no valid run_id")
    if manifest.get("metric_contract_version") != METRIC_CONTRACT_VERSION:
        raise AggregationError(
            "manifest metric contract is unsupported: "
            f"{manifest.get('metric_contract_version')!r}"
        )
    experiment_seed = config.get("run", {}).get("seed")
    if isinstance(experiment_seed, bool) or not isinstance(experiment_seed, int):
        raise AggregationError("config.resolved.yaml has no valid run.seed")
    config_sha256 = hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()
    if manifest.get("config_sha256") != config_sha256:
        raise AggregationError("resolved configuration hash does not match manifest")
    generated_cases_sha256 = hashlib.sha256(
        canonical_json(cases).encode("utf-8")
    ).hexdigest()
    if manifest.get("generated_cases_sha256") != generated_cases_sha256:
        raise AggregationError("generated case-set hash does not match manifest")

    condition_by_id = _unique_records(conditions, "condition_id", "condition")
    case_by_id = _unique_records(cases, "case_id", "case")
    _unique_records(attempts, "attempt_id", "attempt")
    attempts_by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        if attempt["run_id"] != run_id:
            raise AggregationError(
                f"attempt {attempt['attempt_id']} belongs to a different run_id"
            )
        condition_id = attempt["condition_id"]
        if condition_id not in condition_by_id:
            raise AggregationError(
                f"attempt {attempt['attempt_id']} references unknown condition "
                f"{condition_id}"
            )
        if attempt["case_id"] not in case_by_id:
            raise AggregationError(
                f"attempt {attempt['attempt_id']} references unknown case "
                f"{attempt['case_id']}"
            )
        _validate_attempt_invariants(attempt, condition_by_id[condition_id])
        attempts_by_condition[condition_id].append(attempt)

    manifest_complete = manifest.get("run_complete") is True
    condition_summaries: list[dict[str, Any]] = []
    for condition in conditions:
        condition_cases = _condition_cases(cases, condition)
        if not condition_cases:
            raise AggregationError(
                f"condition {condition['condition_id']} selects no generated cases"
            )
        condition_case_sha256 = hashlib.sha256(
            canonical_json(condition_cases).encode("utf-8")
        ).hexdigest()
        if condition_case_sha256 != condition["case_set_sha256"]:
            raise AggregationError(
                f"condition {condition['condition_id']} case-set hash does not match"
            )
        condition_summaries.append(
            aggregate_condition(
                condition,
                condition_cases,
                attempts_by_condition.get(condition["condition_id"], []),
                experiment_seed=experiment_seed,
                manifest_complete=manifest_complete,
            )
        )

    run_complete = (
        manifest_complete
        and len(condition_summaries) == manifest.get("condition_count")
        and all(item["run_complete"] for item in condition_summaries)
    )
    if not run_complete:
        for condition_summary in condition_summaries:
            diagnostic = condition_summary["metrics"][
                "all_repeats_correct_case_rate"
            ]
            diagnostic.update(
                {
                    "value": None,
                    "numerator": None,
                    "eligible_observations": 0,
                    "interval": None,
                    "low_confidence": False,
                }
            )
    summary = {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "run_id": run_id,
        "run_complete": run_complete,
        "generated_at": _utc_now(),
        "condition_count": len(condition_summaries),
        "conditions": condition_summaries,
    }
    errors = sorted(
        Draft202012Validator(
            load_schema("summary"), format_checker=FormatChecker()
        ).iter_errors(summary),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        raise AggregationError(f"generated invalid summary: {error.message}")

    return summary


def summarize_run(run_directory: str | Path) -> SummaryResult:
    """Validate saved evidence, aggregate it, and replace summary artifacts."""

    directory = Path(run_directory)
    summary = build_summary(directory)
    json_path = directory / "summary.json"
    csv_path = directory / "summary.csv"
    _atomic_write_text(json_path, canonical_json(summary) + "\n")
    _write_summary_csv(csv_path, summary)
    return SummaryResult(summary=summary, json_path=json_path, csv_path=csv_path)


def aggregate_condition(
    condition: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
    *,
    experiment_seed: int,
    manifest_complete: bool,
) -> dict[str, Any]:
    """Aggregate one condition without reading or writing files."""

    case_by_id = {case["case_id"]: case for case in cases}
    case_ids = tuple(sorted(case_by_id))
    repetitions = condition["repetitions"]
    planned_keys = {
        (case_id, repeat_id)
        for case_id in case_ids
        for repeat_id in range(repetitions)
    }
    measured = [attempt for attempt in attempts if attempt["phase"] == "measure"]
    observed_by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
    for attempt in measured:
        key = (attempt["case_id"], attempt["repeat_id"])
        if key not in planned_keys:
            raise AggregationError(
                f"condition {condition['condition_id']} has unexpected measured "
                f"attempt key {key!r}"
            )
        if key in observed_by_key:
            raise AggregationError(
                f"condition {condition['condition_id']} repeats measured attempt "
                f"key {key!r}"
            )
        observed_by_key[key] = attempt

    planned_attempts = len(planned_keys)
    started_attempts = len(measured)
    terminal_attempts = sum(attempt["terminal"] is True for attempt in measured)
    condition_complete = (
        set(observed_by_key) == planned_keys
        and terminal_attempts == planned_attempts
    )
    outcome_counts = dict(
        sorted(Counter(attempt["score"]["primary_outcome"] for attempt in measured).items())
    )

    metrics: dict[str, dict[str, Any]] = {}
    for metric_name, score_key in (
        ("request_success_rate", "request_success"),
        ("json_parse_rate", "json_parsed"),
        ("schema_valid_rate", "schema_valid"),
        ("exact_correct_rate", "exact_correct"),
    ):
        numerator = sum(bool(attempt["score"][score_key]) for attempt in measured)
        metrics[metric_name] = _binary_metric(numerator, started_attempts)

    parsed_count = sum(attempt["score"]["json_parsed"] for attempt in measured)
    schema_count = sum(attempt["score"]["schema_valid"] for attempt in measured)
    exact_count = sum(attempt["score"]["exact_correct"] for attempt in measured)
    metrics["schema_valid_given_parse"] = _ratio_metric(
        schema_count, parsed_count, eligible_observations=parsed_count
    )
    metrics["exact_correct_given_schema"] = _ratio_metric(
        exact_count, schema_count, eligible_observations=schema_count
    )

    attempts_by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for attempt in measured:
        attempts_by_case[attempt["case_id"]].append(attempt)

    field_totals_by_case = {
        case_id: (
            sum(item["score"]["correct_fields"] for item in case_attempts),
            sum(item["score"]["total_fields"] for item in case_attempts),
        )
        for case_id, case_attempts in attempts_by_case.items()
    }
    total_correct_fields = sum(value[0] for value in field_totals_by_case.values())
    total_expected_fields = sum(value[1] for value in field_totals_by_case.values())
    field_cluster_ids = tuple(sorted(field_totals_by_case))

    def micro_statistic(sampled: Sequence[str]) -> float | None:
        numerator = sum(field_totals_by_case[case_id][0] for case_id in sampled)
        denominator = sum(field_totals_by_case[case_id][1] for case_id in sampled)
        return _safe_ratio(numerator, denominator)

    micro_interval = _cluster_interval(
        condition,
        experiment_seed,
        "micro_field_accuracy",
        field_cluster_ids,
        micro_statistic,
    )
    metrics["micro_field_accuracy"] = _metric(
        value=_safe_ratio(total_correct_fields, total_expected_fields),
        numerator=total_correct_fields,
        denominator=total_expected_fields,
        eligible_observations=started_attempts,
        interval=micro_interval,
        low_confidence=_interval_low_confidence(micro_interval),
    )

    task_field_totals_by_case: dict[str, tuple[int, int]] = {}
    for case_id, case_attempts in attempts_by_case.items():
        counts = [
            _non_padding_field_counts(case_by_id[case_id], item["score"])
            for item in case_attempts
        ]
        task_field_totals_by_case[case_id] = (
            sum(correct for correct, _ in counts),
            sum(total for _, total in counts),
        )
    task_cluster_ids = tuple(sorted(task_field_totals_by_case))

    def task_field_statistic(sampled: Sequence[str]) -> float | None:
        correct = sum(task_field_totals_by_case[case_id][0] for case_id in sampled)
        total = sum(task_field_totals_by_case[case_id][1] for case_id in sampled)
        return _safe_ratio(correct, total)

    task_interval = _cluster_interval(
        condition,
        experiment_seed,
        "non_padding_field_accuracy",
        task_cluster_ids,
        task_field_statistic,
    )
    metrics["non_padding_field_accuracy"] = _metric(
        value=task_field_statistic(task_cluster_ids),
        numerator=sum(value[0] for value in task_field_totals_by_case.values()),
        denominator=sum(value[1] for value in task_field_totals_by_case.values()),
        eligible_observations=started_attempts,
        interval=task_interval,
        low_confidence=_interval_low_confidence(task_interval),
    )

    case_field_accuracy = {
        case_id: _safe_ratio(correct, total)
        for case_id, (correct, total) in field_totals_by_case.items()
    }
    case_field_accuracy = {
        case_id: value
        for case_id, value in case_field_accuracy.items()
        if value is not None
    }
    macro_cluster_ids = tuple(sorted(case_field_accuracy))

    def macro_statistic(sampled: Sequence[str]) -> float | None:
        if not sampled:
            return None
        return sum(case_field_accuracy[case_id] for case_id in sampled) / len(sampled)

    macro_value = macro_statistic(macro_cluster_ids)
    macro_interval = _cluster_interval(
        condition,
        experiment_seed,
        "macro_field_accuracy",
        macro_cluster_ids,
        macro_statistic,
    )
    metrics["macro_field_accuracy"] = _metric(
        value=macro_value,
        numerator=(
            sum(case_field_accuracy.values()) if case_field_accuracy else None
        ),
        denominator=len(case_field_accuracy) if case_field_accuracy else None,
        eligible_observations=len(case_field_accuracy),
        interval=macro_interval,
        low_confidence=_interval_low_confidence(macro_interval),
    )

    consistency_values: dict[str, float] = {}
    consistent_pairs = 0
    total_pairs = 0
    if repetitions >= 2:
        planned_pairs_per_case = repetitions * (repetitions - 1) // 2
        for case_id in case_ids:
            case_consistent = 0
            for left_repeat in range(repetitions):
                for right_repeat in range(left_repeat + 1, repetitions):
                    left = observed_by_key.get((case_id, left_repeat))
                    right = observed_by_key.get((case_id, right_repeat))
                    if (
                        left is not None
                        and right is not None
                        and left["score"]["json_parsed"]
                        and right["score"]["json_parsed"]
                        and semantically_equal(
                            left["score"]["parsed_json"],
                            right["score"]["parsed_json"],
                            case_by_id[case_id]["comparison"],
                        )
                    ):
                        case_consistent += 1
            consistency_values[case_id] = case_consistent / planned_pairs_per_case
            consistent_pairs += case_consistent
            total_pairs += planned_pairs_per_case

    consistency_cluster_ids = tuple(sorted(consistency_values))

    def consistency_statistic(sampled: Sequence[str]) -> float | None:
        if not sampled:
            return None
        return sum(consistency_values[case_id] for case_id in sampled) / len(sampled)

    consistency_value = consistency_statistic(consistency_cluster_ids)
    consistency_interval = _cluster_interval(
        condition,
        experiment_seed,
        "repeat_consistency_rate",
        consistency_cluster_ids,
        consistency_statistic,
    )
    metrics["repeat_consistency_rate"] = _metric(
        value=consistency_value,
        numerator=consistent_pairs if total_pairs else None,
        denominator=total_pairs if total_pairs else None,
        eligible_observations=len(consistency_values),
        interval=consistency_interval,
        low_confidence=_interval_low_confidence(consistency_interval),
    )

    if manifest_complete and condition_complete:
        all_correct_cases = sum(
            all(
                observed_by_key[(case_id, repeat_id)]["score"]["exact_correct"]
                for repeat_id in range(repetitions)
            )
            for case_id in case_ids
        )
        metrics["all_repeats_correct_case_rate"] = _ratio_metric(
            all_correct_cases,
            len(case_ids),
            eligible_observations=len(case_ids),
        )
    else:
        metrics["all_repeats_correct_case_rate"] = _metric(
            value=None,
            numerator=None,
            denominator=len(case_ids),
            eligible_observations=0,
        )

    metrics.update(
        _latency_metrics(
            condition,
            measured,
            experiment_seed=experiment_seed,
        )
    )
    metrics.update(
        _throughput_metrics(
            condition,
            measured,
            case_ids=case_ids,
            experiment_seed=experiment_seed,
        )
    )

    missing = set(REQUIRED_METRICS).difference(metrics)
    if missing:
        raise AggregationError(f"internal error: missing metrics {sorted(missing)}")
    return {
        "condition_id": condition["condition_id"],
        "comparison_key": condition["comparison_key"],
        "dimensions": {
            "model": condition["endpoint"]["model"],
            "quantization": condition["endpoint"].get("quantization"),
            "concurrency": condition["concurrency"],
            "prompt_length": condition["prompt_length"]["target"],
            "prompt_length_unit": condition["prompt_length"]["unit"],
            "output_length": condition["output_shape"]["target_length"],
            "output_length_unit": condition["output_shape"]["target_unit"],
            "max_completion_tokens": condition["output_shape"]["max_completion_tokens"],
            "temperature": condition["temperature"],
            "request_seed": condition["request_seed"],
            "structured_mode": condition["structured_mode"]["id"],
            "stream": condition["stream"],
            "repetitions": repetitions,
        },
        "planned_attempts": planned_attempts,
        "started_attempts": started_attempts,
        "terminal_attempts": terminal_attempts,
        "run_complete": condition_complete,
        "outcome_counts": outcome_counts,
        "metrics": metrics,
    }


def _latency_metrics(
    condition: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    *,
    experiment_seed: int,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for timing_key, median_name, p95_name in (
        ("e2e_latency_ns", "median_e2e_latency_ms", "p95_e2e_latency_ms"),
        ("ttft_ns", "median_ttft_ms", "p95_ttft_ms"),
        ("tpot_ns", "median_tpot_ms", "p95_tpot_ms"),
    ):
        values_by_case: dict[str, list[float]] = defaultdict(list)
        for attempt in attempts:
            value = attempt["timing"][timing_key]
            if attempt["score"]["request_success"] and value is not None:
                values_by_case[attempt["case_id"]].append(value / 1_000_000)
        cluster_ids = tuple(sorted(values_by_case))
        all_values = [value for values in values_by_case.values() for value in values]

        def statistic(quantile: float) -> Callable[[Sequence[str]], float | None]:
            def calculate(sampled: Sequence[str]) -> float | None:
                values = [
                    value
                    for case_id in sampled
                    for value in values_by_case[case_id]
                ]
                return _quantile(values, quantile) if values else None

            return calculate

        median_value = _quantile(all_values, 0.5) if all_values else None
        median_interval = _cluster_interval(
            condition,
            experiment_seed,
            median_name,
            cluster_ids,
            statistic(0.5),
        )
        result[median_name] = _metric(
            value=median_value,
            eligible_observations=len(all_values),
            interval=median_interval,
            low_confidence=_interval_low_confidence(median_interval),
        )

        p95_value = _quantile(all_values, 0.95) if len(all_values) >= 20 else None
        p95_interval = None
        if p95_value is not None:
            p95_interval = _cluster_interval(
                condition,
                experiment_seed,
                p95_name,
                cluster_ids,
                statistic(0.95),
            )
        result[p95_name] = _metric(
            value=p95_value,
            eligible_observations=len(all_values),
            interval=p95_interval,
            low_confidence=(
                20 <= len(all_values) < 100
                or _interval_low_confidence(p95_interval)
            ),
        )
    return result


def _throughput_metrics(
    condition: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    *,
    case_ids: Sequence[str],
    experiment_seed: int,
) -> dict[str, dict[str, Any]]:
    attempts_by_repeat: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_repeat[attempt["repeat_id"]].append(attempt)

    complete_repetitions: list[dict[str, Any]] = []
    expected_case_ids = set(case_ids)
    for repeat_id in range(condition["repetitions"]):
        repetition = attempts_by_repeat.get(repeat_id, [])
        if (
            len(repetition) != len(case_ids)
            or {attempt["case_id"] for attempt in repetition} != expected_case_ids
        ):
            continue
        starts = [_parse_datetime(attempt["started_at"]).timestamp() for attempt in repetition]
        terminals = [
            start + attempt["timing"]["observed_duration_ns"] / 1_000_000_000
            for start, attempt in zip(starts, repetition, strict=True)
        ]
        duration_seconds = max(terminals) - min(starts)
        if duration_seconds <= 0:
            continue

        successful = [
            attempt for attempt in repetition if attempt["score"]["request_success"]
        ]
        token_identities = {
            (
                attempt["tokens"]["token_count_source"],
                attempt["tokens"]["tokenizer"],
            )
            for attempt in successful
            if attempt["tokens"]["output"] is not None
        }
        output_tokens_available = all(
            attempt["tokens"]["output"] is not None for attempt in successful
        ) and len(token_identities) <= 1
        output_tokens = (
            float(sum(attempt["tokens"]["output"] for attempt in successful))
            if output_tokens_available
            else None
        )
        token_identity = (
            next(iter(token_identities))
            if output_tokens_available and token_identities
            else None
        )

        slos = condition.get("slos", {})
        quality_available = all(
            _slo_values_available(attempt, slos) for attempt in repetition
        )
        quality_count = (
            float(
                sum(
                    attempt["score"]["exact_correct"]
                    and _slo_values_satisfied(attempt, slos)
                    for attempt in repetition
                )
            )
            if quality_available
            else None
        )
        complete_repetitions.append(
            {
                "duration": duration_seconds,
                "attempts": float(len(repetition)),
                "successful": float(len(successful)),
                "output_tokens": output_tokens,
                "token_identity": token_identity,
                "quality": quality_count,
            }
        )

    pooled_token_identities = {
        item["token_identity"]
        for item in complete_repetitions
        if item["token_identity"] is not None
    }
    if len(pooled_token_identities) > 1:
        for item in complete_repetitions:
            item["output_tokens"] = None

    result = {}
    for metric_name, numerator_key in (
        ("attempt_throughput_rps", "attempts"),
        ("request_throughput_rps", "successful"),
        ("output_token_throughput_tps", "output_tokens"),
        ("quality_goodput_rps", "quality"),
    ):
        result[metric_name] = _throughput_metric(
            condition,
            experiment_seed,
            metric_name,
            complete_repetitions,
            numerator_key,
        )
    return result


def _throughput_metric(
    condition: Mapping[str, Any],
    experiment_seed: int,
    metric_name: str,
    repetitions: Sequence[Mapping[str, Any]],
    numerator_key: str,
) -> dict[str, Any]:
    denominator = sum(item["duration"] or 0 for item in repetitions)
    available = bool(repetitions) and all(
        item[numerator_key] is not None for item in repetitions
    )
    numerator = (
        sum(item[numerator_key] or 0 for item in repetitions) if available else None
    )
    value = _safe_ratio(numerator, denominator) if numerator is not None else None
    interval = None
    if value is not None and len(repetitions) >= 3:
        rng = random.Random(
            _bootstrap_seed(experiment_seed, condition["condition_id"], metric_name)
        )
        bootstrap_values: list[float] = []
        for _ in range(BOOTSTRAP_RESAMPLES):
            sampled = [repetitions[rng.randrange(len(repetitions))] for _ in repetitions]
            sampled_numerator = sum(item[numerator_key] or 0 for item in sampled)
            sampled_denominator = sum(item["duration"] or 0 for item in sampled)
            sampled_value = _safe_ratio(sampled_numerator, sampled_denominator)
            if sampled_value is not None:
                bootstrap_values.append(sampled_value)
        interval = _bootstrap_interval_record(
            bootstrap_values,
            method="percentile_repetition_bootstrap",
            sample_count=len(repetitions),
            low_confidence=len(repetitions) < 5,
        )
    return _metric(
        value=value,
        numerator=numerator,
        denominator=denominator if repetitions else None,
        eligible_observations=len(repetitions),
        interval=interval,
        low_confidence=_interval_low_confidence(interval),
    )


def _cluster_interval(
    condition: Mapping[str, Any],
    experiment_seed: int,
    metric_name: str,
    cluster_ids: Sequence[str],
    statistic: Callable[[Sequence[str]], float | None],
) -> dict[str, Any] | None:
    if len(cluster_ids) < 2:
        return None
    rng = random.Random(
        _bootstrap_seed(experiment_seed, condition["condition_id"], metric_name)
    )
    bootstrap_values: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sampled = [cluster_ids[rng.randrange(len(cluster_ids))] for _ in cluster_ids]
        value = statistic(sampled)
        if value is not None:
            bootstrap_values.append(value)
    return _bootstrap_interval_record(
        bootstrap_values,
        method="percentile_cluster_bootstrap",
        sample_count=len(cluster_ids),
        low_confidence=len(cluster_ids) < 20,
    )


def _bootstrap_interval_record(
    values: Sequence[float],
    *,
    method: str,
    sample_count: int,
    low_confidence: bool,
) -> dict[str, Any] | None:
    if not values:
        return None
    return {
        "method": method,
        "confidence_level": CONFIDENCE_LEVEL,
        "sample_count": sample_count,
        "resamples": BOOTSTRAP_RESAMPLES,
        "valid_resamples": len(values),
        "lower": _quantile(values, 0.025),
        "upper": _quantile(values, 0.975),
        "low_confidence": low_confidence,
    }


def _binary_metric(numerator: int, denominator: int) -> dict[str, Any]:
    interval = _wilson_interval(numerator, denominator)
    return _metric(
        value=_safe_ratio(numerator, denominator),
        numerator=numerator,
        denominator=denominator,
        eligible_observations=denominator,
        interval=interval,
    )


def _wilson_interval(numerator: int, denominator: int) -> dict[str, Any] | None:
    if denominator == 0:
        return None
    proportion = numerator / denominator
    z_squared = WILSON_Z**2
    scale = 1 + z_squared / denominator
    center = (proportion + z_squared / (2 * denominator)) / scale
    margin = (
        WILSON_Z
        * math.sqrt(
            proportion * (1 - proportion) / denominator
            + z_squared / (4 * denominator**2)
        )
        / scale
    )
    return {
        "method": "wilson",
        "confidence_level": CONFIDENCE_LEVEL,
        "sample_count": denominator,
        "resamples": None,
        "valid_resamples": None,
        "lower": max(0.0, center - margin),
        "upper": min(1.0, center + margin),
        "low_confidence": False,
    }


def _ratio_metric(
    numerator: float,
    denominator: float,
    *,
    eligible_observations: int,
) -> dict[str, Any]:
    return _metric(
        value=_safe_ratio(numerator, denominator),
        numerator=numerator,
        denominator=denominator,
        eligible_observations=eligible_observations,
    )


def _metric(
    *,
    value: float | None,
    numerator: float | None = None,
    denominator: float | None = None,
    eligible_observations: int = 0,
    interval: dict[str, Any] | None = None,
    low_confidence: bool = False,
) -> dict[str, Any]:
    return {
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "eligible_observations": eligible_observations,
        "interval": interval,
        "low_confidence": low_confidence,
    }


def _safe_ratio(numerator: float | None, denominator: float) -> float | None:
    if numerator is None or denominator == 0:
        return None
    return numerator / denominator


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )


def _bootstrap_seed(experiment_seed: int, condition_id: str, metric_name: str) -> int:
    payload = [
        experiment_seed,
        condition_id,
        metric_name,
        METRIC_CONTRACT_VERSION,
    ]
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _interval_low_confidence(interval: Mapping[str, Any] | None) -> bool:
    return bool(interval is not None and interval["low_confidence"])


def _slo_values_available(
    attempt: Mapping[str, Any], slos: Mapping[str, Any]
) -> bool:
    mapping = {
        "max_e2e_latency_ms": "e2e_latency_ns",
        "max_ttft_ms": "ttft_ns",
        "max_tpot_ms": "tpot_ns",
    }
    return all(attempt["timing"][mapping[name]] is not None for name in slos)


def _slo_values_satisfied(
    attempt: Mapping[str, Any], slos: Mapping[str, Any]
) -> bool:
    mapping = {
        "max_e2e_latency_ms": "e2e_latency_ns",
        "max_ttft_ms": "ttft_ns",
        "max_tpot_ms": "tpot_ns",
    }
    return all(
        attempt["timing"][mapping[name]] / 1_000_000 <= limit
        for name, limit in slos.items()
    )


def _non_padding_field_counts(
    case: Mapping[str, Any], score: Mapping[str, Any]
) -> tuple[int, int]:
    """Exclude only the built-in generator's synthetic root padding leaf."""

    paths = case.get("expected_field_paths", ())
    generator = case.get("generator", {})
    if generator.get("name") != "sor-built-in" or "/padding" not in paths:
        return score["correct_fields"], score["total_fields"]

    total = score["total_fields"] - 1
    padding_incorrect = any(
        error["path"] == "/padding"
        for error in score.get("comparison_errors", ())
    )
    correct = score["correct_fields"] - int(
        score["json_parsed"] and not padding_incorrect
    )
    if not 0 <= correct <= total:
        raise AggregationError("saved field counts are inconsistent with padding score")
    return correct, total


def _validate_attempt_invariants(
    attempt: Mapping[str, Any], condition: Mapping[str, Any]
) -> None:
    score = attempt["score"]
    if score["correct_fields"] > score["total_fields"]:
        raise AggregationError(
            f"attempt {attempt['attempt_id']} has more correct than total fields"
        )
    if score["schema_valid"] and not score["json_parsed"]:
        raise AggregationError(
            f"attempt {attempt['attempt_id']} is schema-valid without parsed JSON"
        )
    if score["exact_correct"] and not score["schema_valid"]:
        raise AggregationError(
            f"attempt {attempt['attempt_id']} is exact without schema validity"
        )
    tokens = attempt["tokens"]
    if (
        tokens["input"] is not None
        and tokens["output"] is not None
        and tokens["total"] is not None
        and tokens["total"] != tokens["input"] + tokens["output"]
    ):
        raise AggregationError(
            f"attempt {attempt['attempt_id']} has inconsistent token totals"
        )
    if not condition["stream"] and (
        attempt["timing"]["ttft_ns"] is not None
        or attempt["timing"]["tpot_ns"] is not None
    ):
        raise AggregationError(
            f"non-streaming attempt {attempt['attempt_id']} has TTFT or TPOT"
        )


def _condition_cases(
    cases: Sequence[dict[str, Any]], condition: Mapping[str, Any]
) -> list[dict[str, Any]]:
    return [
        case
        for case in cases
        if case["prompt_length"]["target"] == condition["prompt_length"]["target"]
        and case["expected_output_length"]["target"]
        == condition["output_shape"]["target_length"]
    ]


def _load_jsonl(path: Path, schema_name: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AggregationError(f"cannot read {path}: {exc}") from exc
    validator = Draft202012Validator(
        load_schema(schema_name), format_checker=FormatChecker()
    )
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line:
            raise AggregationError(f"{path}:{line_number}: blank JSONL line")
        try:
            record = strict_json_loads(line)
        except StrictJSONError as exc:
            raise AggregationError(
                f"{path}:{line_number}: invalid JSON: {exc}"
            ) from exc
        errors = sorted(
            validator.iter_errors(record), key=lambda error: list(error.absolute_path)
        )
        if errors:
            raise AggregationError(
                f"{path}:{line_number}: schema error: {errors[0].message}"
            )
        records.append(record)
    return records


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, StrictJSONError) as exc:
        raise AggregationError(f"cannot read valid JSON object from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AggregationError(f"{path} must contain a JSON object")
    return value


def _load_resolved_config(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise AggregationError(f"cannot read resolved configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AggregationError(f"{path} must contain a YAML mapping")
    return value


def _unique_records(
    records: Sequence[dict[str, Any]], key: str, label: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        identity = record[key]
        if identity in result:
            raise AggregationError(f"duplicate {label} identifier {identity}")
        result[identity] = record
    return result


def _write_summary_csv(path: Path, summary: Mapping[str, Any]) -> None:
    metric_names = tuple(summary["conditions"][0]["metrics"]) if summary["conditions"] else ()
    base_fields = (
        "run_id",
        "condition_id",
        "comparison_key",
        "run_complete",
        "planned_attempts",
        "started_attempts",
        "terminal_attempts",
        "model",
        "quantization",
        "concurrency",
        "prompt_length",
        "prompt_length_unit",
        "output_length",
        "output_length_unit",
        "max_completion_tokens",
        "temperature",
        "request_seed",
        "structured_mode",
        "stream",
        "repetitions",
        "outcome_counts_json",
    )
    metric_suffixes = (
        "value",
        "numerator",
        "denominator",
        "eligible_observations",
        "ci_lower",
        "ci_upper",
        "low_confidence",
    )
    fieldnames = list(base_fields) + [
        f"{metric_name}_{suffix}"
        for metric_name in metric_names
        for suffix in metric_suffixes
    ]
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for condition in summary["conditions"]:
                row: dict[str, Any] = {
                    "run_id": summary["run_id"],
                    "condition_id": condition["condition_id"],
                    "comparison_key": condition["comparison_key"],
                    "run_complete": condition["run_complete"],
                    "planned_attempts": condition["planned_attempts"],
                    "started_attempts": condition["started_attempts"],
                    "terminal_attempts": condition["terminal_attempts"],
                    **condition["dimensions"],
                    "outcome_counts_json": canonical_json(condition["outcome_counts"]),
                }
                for metric_name, metric in condition["metrics"].items():
                    interval = metric["interval"]
                    row[f"{metric_name}_value"] = metric["value"]
                    row[f"{metric_name}_numerator"] = metric["numerator"]
                    row[f"{metric_name}_denominator"] = metric["denominator"]
                    row[f"{metric_name}_eligible_observations"] = metric[
                        "eligible_observations"
                    ]
                    row[f"{metric_name}_ci_lower"] = (
                        interval["lower"] if interval is not None else None
                    )
                    row[f"{metric_name}_ci_upper"] = (
                        interval["upper"] if interval is not None else None
                    )
                    row[f"{metric_name}_low_confidence"] = metric["low_confidence"]
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise AggregationError(f"could not write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise AggregationError(f"could not write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AggregationError(f"invalid attempt timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise AggregationError(f"attempt timestamp has no timezone: {value!r}")
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
