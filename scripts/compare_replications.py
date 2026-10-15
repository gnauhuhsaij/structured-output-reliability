#!/usr/bin/env python3
"""Compare five complete replications using the whole run as the sampling unit."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from structured_output_reliability.metrics import build_summary
from structured_output_reliability.pairwise import build_pairwise_contrasts

T_CRITICAL_DF4_95 = 2.7764451051977987
LATENCY_KEYS = (
    ("mean_e2e_ms", "Mean E2E change"),
    ("mean_ttft_ms", "Mean TTFT change"),
    ("mean_tpot_ms", "Mean TPOT change"),
)
RELIABILITY_KEYS = (
    ("request_success_rate", "Request success"),
    ("json_parse_rate", "JSON parse"),
    ("schema_valid_rate", "Schema valid"),
    ("exact_correct_rate", "Exact correct"),
    ("micro_field_accuracy", "Field accuracy"),
    ("repeat_consistency_rate", "Consistency"),
)


def analyze(plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    entries = plan["runs"]
    if len(entries) != 5 or any(entry["run_directory"] is None for entry in entries):
        raise ValueError("this comparison requires five completed runs")

    run_contrasts = []
    condition_ids: set[str] | None = None
    total_measured = 0
    total_exact = 0
    total_failures = 0
    output_tokens_by_condition: dict[tuple[int, str], list[int]] = defaultdict(list)
    reliability_by_condition: dict[str, dict[str, Any]] = {}
    for entry in entries:
        directory = Path(entry["run_directory"])
        summary = build_summary(directory)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if summary["run_complete"] is not True:
            raise ValueError(f"run {entry['index']} is incomplete")
        if manifest["generated_cases_sha256"] != plan["generated_cases_sha256"]:
            raise ValueError(f"run {entry['index']} used a different case set")
        current_ids = {item["condition_id"] for item in summary["conditions"]}
        if condition_ids is not None and current_ids != condition_ids:
            raise ValueError(f"run {entry['index']} used different conditions")
        condition_ids = current_ids
        total_measured += manifest["terminal_measured_attempts"]
        total_failures += manifest["request_failures"]
        for condition in summary["conditions"]:
            condition_id = condition["condition_id"]
            row = reliability_by_condition.setdefault(
                condition_id,
                {
                    "condition_id": condition_id,
                    "concurrency": condition["dimensions"]["concurrency"],
                    "structured_mode": condition["dimensions"]["structured_mode"],
                    "metrics": {
                        key: {"numerator": 0, "denominator": 0}
                        for key, _ in RELIABILITY_KEYS
                    },
                },
            )
            for key, _ in RELIABILITY_KEYS:
                metric = condition["metrics"][key]
                if metric["numerator"] is not None and metric["denominator"] is not None:
                    row["metrics"][key]["numerator"] += metric["numerator"]
                    row["metrics"][key]["denominator"] += metric["denominator"]

        attempts = [
            json.loads(line)
            for line in (directory / "responses.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        conditions_by_id = {
            item["condition_id"]: item for item in summary["conditions"]
        }
        for attempt in attempts:
            if attempt["phase"] != "measure":
                continue
            total_exact += attempt["score"]["exact_correct"]
            condition = conditions_by_id[attempt["condition_id"]]
            token_count = attempt["tokens"]["output"]
            if token_count is not None:
                output_tokens_by_condition[
                    (
                        condition["dimensions"]["concurrency"],
                        condition["dimensions"]["structured_mode"],
                    )
                ].append(token_count)
        config = yaml.safe_load((directory / "config.resolved.yaml").read_text(encoding="utf-8"))
        contrasts = build_pairwise_contrasts(
            summary["conditions"], attempts, experiment_seed=config["run"]["seed"]
        )
        run_contrasts.append(
            {
                (item["first_condition_id"], item["second_condition_id"]): item
                for item in contrasts
            }
        )

    pair_keys = set(run_contrasts[0])
    if any(set(items) != pair_keys for items in run_contrasts[1:]):
        raise ValueError("the runs do not contain the same pairwise comparisons")
    comparisons = []
    for pair_key in sorted(pair_keys):
        members = [items[pair_key] for items in run_contrasts]
        latency = {}
        for metric_key, _ in LATENCY_KEYS:
            values = [item["latency"][metric_key]["value_ms"] for item in members]
            latency[metric_key] = _run_level_summary(values)
        ratios = [item["request_throughput_ratio"] for item in members]
        throughput = _run_level_summary(
            [math.log(value) if value is not None and value > 0 else None for value in ratios]
        )
        if throughput["mean"] is not None:
            throughput["geometric_mean_ratio"] = math.exp(throughput["mean"])
            interval = throughput["t_interval"]
            throughput["ratio_t_interval"] = (
                [math.exp(interval[0]), math.exp(interval[1])]
                if interval is not None
                else None
            )
        comparisons.append(
            {
                "label": members[0]["label"],
                "first_condition_id": pair_key[0],
                "second_condition_id": pair_key[1],
                "latency": latency,
                "throughput": throughput,
                "throughput_ratios_by_run": ratios,
            }
        )
    comparisons.sort(key=lambda item: (not item["label"].startswith("concurrency"), item["label"]))
    return {
        "plan": str(plan_path.resolve()),
        "run_count": len(entries),
        "measured_attempts": total_measured,
        "exact_correct_attempts": total_exact,
        "request_failures": total_failures,
        "generated_cases_sha256": plan["generated_cases_sha256"],
        "reliability_by_condition": sorted(
            reliability_by_condition.values(),
            key=lambda item: (item["concurrency"], item["structured_mode"]),
        ),
        "output_tokens_by_condition": [
            {
                "concurrency": concurrency,
                "structured_mode": mode,
                "observations": len(values),
                "mean_output_tokens": statistics.fmean(values),
            }
            for (concurrency, mode), values in sorted(output_tokens_by_condition.items())
        ],
        "comparisons": comparisons,
    }


def _run_level_summary(values: list[float | None]) -> dict[str, Any]:
    if any(value is None for value in values):
        return {"by_run": values, "mean": None, "range": None, "t_interval": None}
    observed = [float(value) for value in values if value is not None]
    mean = statistics.fmean(observed)
    interval = None
    if len(observed) == 5:
        margin = T_CRITICAL_DF4_95 * statistics.stdev(observed) / math.sqrt(5)
        interval = [mean - margin, mean + margin]
    return {
        "by_run": observed,
        "mean": mean,
        "range": [min(observed), max(observed)],
        "t_interval": interval,
    }


def render_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# Five-run replication comparison",
        "",
        f"Measured attempts: {analysis['measured_attempts']}; exactly correct: "
        f"{analysis['exact_correct_attempts']}; request failures: {analysis['request_failures']}.",
        "",
        "Each value is a matched condition difference within one complete run. "
        "Positive latency means B is slower than A. The across-run mean and "
        "95% Student-t interval use the five run-level differences (df=4), not "
        "individual requests. The interval assumes roughly independent runs "
        "and approximately normal run-level differences; with only five runs "
        "it is exploratory. Intervals are not adjusted for the many comparisons. "
        "Sequential runs and service drift remain possible.",
        "",
        "## Correctness and reliability",
        "",
        "| Concurrency | Mode | Request success | JSON parse | Schema valid | Exact correct | Field accuracy | Consistency |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in analysis["reliability_by_condition"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["concurrency"]),
                    row["structured_mode"],
                    *[
                        _format_reliability(row["metrics"][key])
                        for key, _ in RELIABILITY_KEYS
                    ],
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "These are observed aggregate counts from five runs of the same case "
            "set. Request failures remain in the correctness denominators. "
            "With no observed failures, a paired bootstrap would yield a "
            "misleading zero-width interval; the table does not establish "
            "equal underlying reliability. Individual run reports retain "
            "their condition-level Wilson intervals.",
            "",
        ]
    )
    for metric_key, heading in LATENCY_KEYS:
        lines.extend(
            [
                f"## {heading}",
                "",
                "| Comparison (A → B) | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | Mean | 95% run-level t interval |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in analysis["comparisons"]:
            metric = item["latency"][metric_key]
            values = metric["by_run"]
            interval = metric["t_interval"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        item["label"],
                        *[_format_latency(value) for value in values],
                        _format_latency(metric["mean"]),
                        (
                            f"[{interval[0]:+.1f}, {interval[1]:+.1f}] ms"
                            if interval is not None
                            else "—"
                        ),
                    ]
                )
                + " |"
            )
        lines.append("")

    lines.extend(
        [
            "## Request throughput ratio",
            "",
            "For each run this is B / A. The across-run estimate is the geometric "
            "mean; its exploratory t interval is calculated on log ratios.",
            "",
            "| Comparison (A → B) | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | Geometric mean | 95% run-level t interval |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in analysis["comparisons"]:
        metric = item["throughput"]
        interval = metric.get("ratio_t_interval")
        lines.append(
            "| "
            + " | ".join(
                [
                    item["label"],
                    *[
                        f"{value:.2f}×" if value is not None else "—"
                        for value in item["throughput_ratios_by_run"]
                    ],
                    (
                        f"{metric['geometric_mean_ratio']:.2f}×"
                        if metric.get("geometric_mean_ratio") is not None
                        else "—"
                    ),
                    (
                        f"[{interval[0]:.2f}, {interval[1]:.2f}]×"
                        if interval is not None
                        else "—"
                    ),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "All-success outcomes are reported as observed counts, not as proof "
            "that the underlying error rate is zero. These repeated runs vary "
            "condition order but share one case set and one testing session.",
            "",
            "## Output length check",
            "",
            "| Concurrency | Mode | Measured attempts with token usage | Mean output tokens |",
            "|---:|---|---:|---:|",
        ]
    )
    for item in analysis["output_tokens_by_condition"]:
        lines.append(
            f"| {item['concurrency']} | {item['structured_mode']} | "
            f"{item['observations']} | {item['mean_output_tokens']:.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def _format_latency(value: float | None) -> str:
    return f"{value:+.1f} ms" if value is not None else "—"


def _format_reliability(metric: dict[str, float | int]) -> str:
    denominator = metric["denominator"]
    if denominator == 0:
        return "—"
    numerator = metric["numerator"]
    return f"{numerator:g}/{denominator:g} ({numerator / denominator:.1%})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    try:
        result = analyze(args.plan)
        args.plan.with_name("comparison.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        report = args.plan.with_name("comparison.md")
        report.write_text(render_markdown(result), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"Comparison failed: {exc}", file=sys.stderr)
        return 2
    print(f"Comparison: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
