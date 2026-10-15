"""Exploratory within-run contrasts for matched generated cases."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from structured_output_reliability.generator import canonical_json

BOOTSTRAP_RESAMPLES = 10_000
LATENCY_METRICS = (
    ("mean_e2e_ms", "Mean E2E", "e2e_latency_ns"),
    ("mean_ttft_ms", "Mean TTFT", "ttft_ns"),
    ("mean_tpot_ms", "Mean TPOT", "tpot_ns"),
)


def build_pairwise_contrasts(
    conditions: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
    *,
    experiment_seed: int,
) -> list[dict[str, Any]]:
    """Compare matched cases when only concurrency or output mode differs.

    Repetitions are averaged within each case; percentile bootstrap resampling
    then uses case IDs, not individual attempts, as independent clusters.
    Throughput ratios remain descriptive because one run has one throughput
    observation per condition.
    """

    by_condition: dict[str, dict[str, dict[int, Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for attempt in attempts:
        if attempt["phase"] == "measure":
            by_condition[attempt["condition_id"]][attempt["case_id"]][
                attempt["repeat_id"]
            ] = attempt

    contrasts: list[dict[str, Any]] = []
    for index, first in enumerate(conditions):
        for second in conditions[index + 1 :]:
            axis = _single_comparison_axis(first["dimensions"], second["dimensions"])
            if axis is None:
                continue
            left, right = _canonical_pair(first, second, axis)
            first_cases = by_condition[left["condition_id"]]
            second_cases = by_condition[right["condition_id"]]
            if not first_cases or set(first_cases) != set(second_cases):
                continue
            metric_results = {
                key: _paired_latency_metric(
                    first_cases,
                    second_cases,
                    field=field,
                    repetitions=left["dimensions"]["repetitions"],
                    seed=_bootstrap_seed(
                        experiment_seed,
                        left["condition_id"],
                        right["condition_id"],
                        key,
                    ),
                )
                for key, _, field in LATENCY_METRICS
            }
            first_throughput = left["metrics"]["request_throughput_rps"]["value"]
            second_throughput = right["metrics"]["request_throughput_rps"]["value"]
            throughput_ratio = (
                second_throughput / first_throughput
                if first_throughput not in (None, 0) and second_throughput is not None
                else None
            )
            contrasts.append(
                {
                    "axis": axis,
                    "label": _comparison_label(left["dimensions"], right["dimensions"], axis),
                    "first_condition_id": left["condition_id"],
                    "second_condition_id": right["condition_id"],
                    "latency": metric_results,
                    "first_request_throughput_rps": first_throughput,
                    "second_request_throughput_rps": second_throughput,
                    "request_throughput_ratio": throughput_ratio,
                }
            )
    return sorted(
        contrasts,
        key=lambda item: (0 if item["axis"] == "concurrency" else 1, item["label"]),
    )


def _canonical_pair(
    first: Mapping[str, Any], second: Mapping[str, Any], axis: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Keep contrast signs stable when the execution order changes."""

    if axis == "concurrency":
        first_key = first["dimensions"]["concurrency"]
        second_key = second["dimensions"]["concurrency"]
    else:
        first_mode = first["dimensions"]["structured_mode"]
        second_mode = second["dimensions"]["structured_mode"]
        first_key = (first_mode != "prompt_only", first_mode)
        second_key = (second_mode != "prompt_only", second_mode)
    return (first, second) if first_key < second_key else (second, first)


def _single_comparison_axis(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> str | None:
    if set(first) != set(second):
        return None
    differing = [key for key in first if first[key] != second[key]]
    if len(differing) == 1 and differing[0] in {"concurrency", "structured_mode"}:
        return differing[0]
    return None


def _comparison_label(
    first: Mapping[str, Any], second: Mapping[str, Any], axis: str
) -> str:
    if axis == "concurrency":
        return (
            f"concurrency {first['concurrency']} → {second['concurrency']}; "
            f"mode={first['structured_mode']}"
        )
    return (
        f"mode {first['structured_mode']} → {second['structured_mode']}; "
        f"concurrency={first['concurrency']}"
    )


def _paired_latency_metric(
    first_cases: Mapping[str, Mapping[int, Mapping[str, Any]]],
    second_cases: Mapping[str, Mapping[int, Mapping[str, Any]]],
    *,
    field: str,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    case_differences: list[float] = []
    for case_id in sorted(first_cases):
        first_repeats = first_cases[case_id]
        second_repeats = second_cases[case_id]
        differences: list[float] = []
        for repeat_id in range(repetitions):
            first_attempt = first_repeats.get(repeat_id)
            second_attempt = second_repeats.get(repeat_id)
            if first_attempt is None or second_attempt is None:
                break
            first_value = first_attempt["timing"][field]
            second_value = second_attempt["timing"][field]
            if first_value is None or second_value is None:
                break
            differences.append((second_value - first_value) / 1_000_000)
        if len(differences) == repetitions:
            case_differences.append(sum(differences) / repetitions)

    case_count = len(case_differences)
    if case_count == 0:
        return {"value_ms": None, "interval_ms": None, "matched_cases": 0}
    value = sum(case_differences) / case_count
    interval = None
    if case_count >= 2:
        rng = random.Random(seed)
        resampled_means = []
        for _ in range(BOOTSTRAP_RESAMPLES):
            resampled_means.append(
                sum(case_differences[rng.randrange(case_count)] for _ in range(case_count))
                / case_count
            )
        resampled_means.sort()
        interval = [
            _quantile(resampled_means, 0.025),
            _quantile(resampled_means, 0.975),
        ]
    return {"value_ms": value, "interval_ms": interval, "matched_cases": case_count}


def _bootstrap_seed(*parts: Any) -> int:
    digest = hashlib.sha256(canonical_json(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
