"""Markdown reporting and regression-gate evaluation for saved runs."""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from structured_output_reliability.config import ConfigurationError, load_experiment
from structured_output_reliability.metrics import (
    AggregationError,
    build_summary,
)


class ReportingError(ValueError):
    """Raised when a report cannot be rebuilt safely from saved artifacts."""


@dataclass(frozen=True, slots=True)
class ReportResult:
    """A rendered report and its regression-gate outcome."""

    report_path: Path
    gates_configured: int
    gates_passed: bool
    gate_results: tuple[dict[str, Any], ...]
    baseline_run_id: str | None
    matched_conditions: int
    unmatched_candidate_conditions: int
    unmatched_baseline_conditions: int


def generate_report(
    run_directory: str | Path,
    *,
    candidate_summary: Mapping[str, Any] | None = None,
) -> ReportResult:
    """Rebuild report.md and evaluate every configured gate offline."""

    directory = Path(run_directory)
    try:
        config = load_experiment(directory / "config.resolved.yaml")
        candidate = (
            dict(candidate_summary)
            if candidate_summary is not None
            else build_summary(directory)
        )
    except (ConfigurationError, AggregationError) as exc:
        raise ReportingError(f"cannot load candidate run {directory}: {exc}") from exc

    baseline_directory: Path | None = None
    baseline: dict[str, Any] | None = None
    if "baseline_run" in config:
        baseline_directory = _resolve_baseline(directory, config["baseline_run"])
        try:
            baseline = build_summary(baseline_directory)
        except AggregationError as exc:
            raise ReportingError(
                f"cannot load baseline run {baseline_directory}: {exc}"
            ) from exc

    gates = config["gates"]
    gate_results, gates_passed = evaluate_gates(candidate, gates, baseline)
    comparison = _comparison_inventory(candidate, baseline)
    markdown = render_markdown_report(
        config=config,
        candidate=candidate,
        baseline=baseline,
        baseline_directory=baseline_directory,
        gate_results=gate_results,
        gates_passed=gates_passed,
        comparison=comparison,
    )
    report_path = directory / "report.md"
    _atomic_write_text(report_path, markdown)
    return ReportResult(
        report_path=report_path,
        gates_configured=len(gates),
        gates_passed=gates_passed,
        gate_results=tuple(gate_results),
        baseline_run_id=baseline["run_id"] if baseline is not None else None,
        matched_conditions=comparison["matched"],
        unmatched_candidate_conditions=comparison["unmatched_candidate"],
        unmatched_baseline_conditions=comparison["unmatched_baseline"],
    )


def evaluate_gates(
    candidate: Mapping[str, Any],
    gates: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Evaluate gates for every candidate condition using exact comparison keys."""

    if not gates:
        return [], True

    baseline_by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    if baseline is not None:
        for condition in baseline.get("conditions", []):
            baseline_by_key[condition["comparison_key"]].append(condition)

    contract_compatible = baseline is None or _major_version(
        candidate.get("metric_contract_version")
    ) == _major_version(baseline.get("metric_contract_version"))
    results: list[dict[str, Any]] = []
    for gate_index, gate in enumerate(gates):
        for condition in candidate.get("conditions", []):
            result = {
                "gate_index": gate_index,
                "metric": gate["metric"],
                "kind": gate["kind"],
                "threshold": gate["value"],
                "estimate": gate["estimate"],
                "condition_id": condition["condition_id"],
                "comparison_key": condition["comparison_key"],
                "candidate_value": None,
                "baseline_condition_id": None,
                "baseline_value": None,
                "observed": None,
                "status": "unavailable",
                "passed": False,
                "reason": None,
            }
            if candidate.get("run_complete") is not True or condition.get(
                "run_complete"
            ) is not True:
                result["reason"] = "candidate run or condition is incomplete"
                results.append(result)
                continue

            candidate_value, reason = _select_estimate(
                condition.get("metrics", {}).get(gate["metric"]), gate["estimate"]
            )
            result["candidate_value"] = candidate_value
            if reason is not None:
                result["reason"] = f"candidate {reason}"
                results.append(result)
                continue

            if gate["kind"] in {"minimum", "maximum"}:
                result["observed"] = candidate_value
                passed = (
                    candidate_value >= gate["value"]
                    if gate["kind"] == "minimum"
                    else candidate_value <= gate["value"]
                )
                _finish_gate_result(result, passed)
                results.append(result)
                continue

            if baseline is None:
                result["reason"] = "relative gate has no baseline run"
                results.append(result)
                continue
            if baseline.get("run_complete") is not True:
                result["reason"] = "baseline run is incomplete"
                results.append(result)
                continue
            if not contract_compatible:
                result["reason"] = "metric-contract major versions differ"
                results.append(result)
                continue

            matches = baseline_by_key.get(condition["comparison_key"], [])
            if len(matches) != 1:
                result["reason"] = (
                    "no baseline condition matches comparison_key"
                    if not matches
                    else "multiple baseline conditions match comparison_key"
                )
                results.append(result)
                continue
            baseline_condition = matches[0]
            result["baseline_condition_id"] = baseline_condition["condition_id"]
            if baseline_condition.get("run_complete") is not True:
                result["reason"] = "baseline condition is incomplete"
                results.append(result)
                continue
            baseline_value, reason = _select_estimate(
                baseline_condition.get("metrics", {}).get(gate["metric"]),
                gate["estimate"],
            )
            result["baseline_value"] = baseline_value
            if reason is not None:
                result["reason"] = f"baseline {reason}"
                results.append(result)
                continue
            if baseline_value == 0:
                result["reason"] = "relative gate baseline estimate is zero"
                results.append(result)
                continue

            if gate["kind"] == "max_drop":
                observed = (baseline_value - candidate_value) / baseline_value
            else:
                observed = candidate_value / baseline_value
            result["observed"] = observed
            _finish_gate_result(result, observed <= gate["value"])
            results.append(result)

    return results, bool(results) and all(item["passed"] for item in results)


def render_markdown_report(
    *,
    config: Mapping[str, Any],
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    baseline_directory: Path | None,
    gate_results: Sequence[Mapping[str, Any]],
    gates_passed: bool,
    comparison: Mapping[str, int],
) -> str:
    """Render a compact, deterministic Markdown view of one summary."""

    gates = config["gates"]
    if not gates:
        gate_status = "NOT CONFIGURED"
    else:
        gate_status = "PASS" if gates_passed else "FAIL"
    lines = [
        f"# Structured Output Reliability Report: {_escape(config['name'])}",
        "",
        f"Generated: `{_utc_now()}`  ",
        f"Run ID: `{candidate['run_id']}`  ",
        f"Run complete: **{'yes' if candidate['run_complete'] else 'no'}**  ",
        f"Regression gates: **{gate_status}**",
        "",
        "## Run overview",
        "",
        "| Item | Value |",
        "|---|---:|",
        f"| Conditions | {candidate['condition_count']} |",
        f"| Planned attempts | {sum(c['planned_attempts'] for c in candidate['conditions'])} |",
        f"| Started attempts | {sum(c['started_attempts'] for c in candidate['conditions'])} |",
        f"| Terminal attempts | {sum(c['terminal_attempts'] for c in candidate['conditions'])} |",
        f"| Configured gates | {len(gates)} |",
    ]
    if baseline is not None:
        lines.extend(
            [
                f"| Baseline run | `{baseline['run_id']}` |",
                f"| Matched baseline conditions | {comparison['matched']} |",
                f"| Unmatched candidate conditions | {comparison['unmatched_candidate']} |",
                f"| Unmatched baseline conditions | {comparison['unmatched_baseline']} |",
            ]
        )

    lines.extend(["", "## Correctness and reliability", ""])
    lines.extend(_condition_table(candidate["conditions"]))
    lines.extend(["", "## Timing and throughput", ""])
    lines.extend(_performance_table(candidate["conditions"]))
    lines.extend(["", "## Outcome counts", ""])
    lines.extend(_outcome_table(candidate["conditions"]))
    lines.extend(["", "## Regression gates", ""])
    if not gates:
        lines.append("No regression gates were configured for this run.")
    else:
        lines.extend(_gate_table(gate_results))

    if baseline is not None:
        lines.extend(["", "## Baseline comparison", ""])
        if baseline_directory is not None:
            lines.append(f"Baseline directory: `{_escape(str(baseline_directory))}`")
            lines.append("")
        lines.extend(_baseline_table(candidate, baseline, gates))

    lines.extend(
        [
            "",
            "## Interpretation notes",
            "",
            "- Primary correctness rates include failed measured requests in their denominators.",
            "- Warmup attempts are excluded from every reported metric.",
            "- `—` means unavailable, never a measured zero.",
            "- p95 is suppressed below 20 eligible observations; confidence bounds may also be unavailable for small samples.",
            "- Baseline gates pair conditions only when their exact `comparison_key` values match.",
            "",
        ]
    )
    return "\n".join(lines)


def _condition_table(conditions: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Condition | Dimensions | Request success | JSON parse | Schema valid | Exact correct | Field accuracy (micro) | Field accuracy (macro) | Consistency |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in conditions:
        dimensions = condition["dimensions"]
        quantization = (
            f"; quant={dimensions['quantization']}"
            if dimensions["quantization"] is not None
            else ""
        )
        label = (
            f"model={dimensions['model']}; c={dimensions['concurrency']}; "
            f"prompt={dimensions['prompt_length']} {dimensions['prompt_length_unit']}; "
            f"output={dimensions['output_length']} {dimensions['output_length_unit']}; "
            f"max={dimensions['max_completion_tokens']}; temp={dimensions['temperature']}; "
            f"seed={dimensions['request_seed']}; mode={dimensions['structured_mode']}; "
            f"stream={dimensions['stream']}; reps={dimensions['repetitions']}"
            f"{quantization}"
        )
        metrics = condition["metrics"]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{condition['condition_id']}`",
                    _escape(label),
                    _format_metric(metrics["request_success_rate"], percent=True),
                    _format_metric(metrics["json_parse_rate"], percent=True),
                    _format_metric(metrics["schema_valid_rate"], percent=True),
                    _format_metric(metrics["exact_correct_rate"], percent=True),
                    _format_metric(metrics["micro_field_accuracy"], percent=True),
                    _format_metric(metrics["macro_field_accuracy"], percent=True),
                    _format_metric(metrics["repeat_consistency_rate"], percent=True),
                ]
            )
            + " |"
        )
    return lines


def _performance_table(conditions: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Condition | Median E2E | p95 E2E | Median TTFT | p95 TTFT | Median TPOT | p95 TPOT | Attempt throughput | Request throughput | Output-token throughput | Quality goodput |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in conditions:
        metrics = condition["metrics"]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{condition['condition_id']}`",
                    _format_metric(metrics["median_e2e_latency_ms"], suffix=" ms"),
                    _format_metric(metrics["p95_e2e_latency_ms"], suffix=" ms"),
                    _format_metric(metrics["median_ttft_ms"], suffix=" ms"),
                    _format_metric(metrics["p95_ttft_ms"], suffix=" ms"),
                    _format_metric(metrics["median_tpot_ms"], suffix=" ms"),
                    _format_metric(metrics["p95_tpot_ms"], suffix=" ms"),
                    _format_metric(metrics["attempt_throughput_rps"], suffix=" req/s"),
                    _format_metric(metrics["request_throughput_rps"], suffix=" req/s"),
                    _format_metric(
                        metrics["output_token_throughput_tps"], suffix=" tok/s"
                    ),
                    _format_metric(metrics["quality_goodput_rps"], suffix=" req/s"),
                ]
            )
            + " |"
        )
    return lines


def _outcome_table(conditions: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["| Condition | Outcomes |", "|---|---|"]
    for condition in conditions:
        outcomes = ", ".join(
            f"{name}={count}" for name, count in condition["outcome_counts"].items()
        )
        lines.append(f"| `{condition['condition_id']}` | {_escape(outcomes or 'none')} |")
    return lines


def _gate_table(results: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Gate | Condition | Metric | Rule | Estimate | Candidate | Baseline | Observed | Result |",
        "|---:|---|---|---|---|---:|---:|---:|---|",
    ]
    for result in results:
        rule = f"{result['kind']} {result['threshold']:g}"
        outcome = result["status"].upper()
        if result["reason"]:
            outcome += f": {result['reason']}"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(result["gate_index"] + 1),
                    f"`{result['condition_id']}`",
                    f"`{result['metric']}`",
                    rule,
                    str(result["estimate"]),
                    _format_number(result["candidate_value"]),
                    _format_number(result["baseline_value"]),
                    _format_number(result["observed"]),
                    _escape(outcome),
                ]
            )
            + " |"
        )
    return lines


def _baseline_table(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    gates: Sequence[Mapping[str, Any]],
) -> list[str]:
    baseline_by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for condition in baseline["conditions"]:
        baseline_by_key[condition["comparison_key"]].append(condition)
    metrics = list(dict.fromkeys(gate["metric"] for gate in gates))
    if not metrics:
        metrics = [
            "request_success_rate",
            "json_parse_rate",
            "schema_valid_rate",
            "exact_correct_rate",
        ]
    lines = [
        "| Comparison key | Metric | Baseline | Candidate | Change |",
        "|---|---|---:|---:|---:|",
    ]
    emitted = False
    for condition in candidate["conditions"]:
        matches = baseline_by_key.get(condition["comparison_key"], [])
        if len(matches) != 1:
            continue
        baseline_condition = matches[0]
        for metric_name in metrics:
            current_value = condition["metrics"][metric_name]["value"]
            baseline_value = baseline_condition["metrics"][metric_name]["value"]
            change = (
                current_value - baseline_value
                if current_value is not None and baseline_value is not None
                else None
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{condition['comparison_key']}`",
                        f"`{metric_name}`",
                        _format_number(baseline_value),
                        _format_number(current_value),
                        _format_signed(change),
                    ]
                )
                + " |"
            )
            emitted = True
    if not emitted:
        lines.append("| — | — | — | — | — |")
    return lines


def _comparison_inventory(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None
) -> dict[str, int]:
    if baseline is None:
        return {"matched": 0, "unmatched_candidate": 0, "unmatched_baseline": 0}
    candidate_keys = [condition["comparison_key"] for condition in candidate["conditions"]]
    baseline_counts: dict[str, int] = defaultdict(int)
    for condition in baseline["conditions"]:
        baseline_counts[condition["comparison_key"]] += 1
    matched_keys = {key for key in candidate_keys if baseline_counts[key] == 1}
    matched = sum(key in matched_keys for key in candidate_keys)
    unmatched_candidate = len(candidate_keys) - matched
    unmatched_baseline = sum(
        count for key, count in baseline_counts.items() if key not in matched_keys
    )
    return {
        "matched": matched,
        "unmatched_candidate": unmatched_candidate,
        "unmatched_baseline": unmatched_baseline,
    }


def _select_estimate(
    metric: Mapping[str, Any] | None, estimate: str
) -> tuple[float | None, str | None]:
    if metric is None:
        return None, "metric is missing"
    if estimate == "point":
        value = metric.get("value")
        return (value, None) if value is not None else (None, "point estimate is unavailable")
    interval = metric.get("interval")
    if interval is None:
        return None, "confidence interval is unavailable"
    key = "lower" if estimate == "lower_ci" else "upper"
    value = interval.get(key)
    return (value, None) if value is not None else (None, f"{estimate} is unavailable")


def _finish_gate_result(result: dict[str, Any], passed: bool) -> None:
    result["passed"] = passed
    result["status"] = "pass" if passed else "fail"
    if not passed:
        result["reason"] = "threshold violated"


def _resolve_baseline(candidate_directory: Path, configured: str) -> Path:
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    return (candidate_directory / path).resolve()


def _major_version(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.split(".", 1)[0]


def _format_metric(
    metric: Mapping[str, Any], *, percent: bool = False, suffix: str = ""
) -> str:
    value = metric["value"]
    if value is None:
        return "—"
    rendered = f"{value * 100:.2f}%" if percent else f"{value:.3f}{suffix}"
    numerator = metric.get("numerator")
    denominator = metric.get("denominator")
    if numerator is not None and denominator is not None:
        rendered += f" ({_format_number(numerator)}/{_format_number(denominator)})"
    interval = metric.get("interval")
    if interval is not None:
        lower = interval["lower"] * 100 if percent else interval["lower"]
        upper = interval["upper"] * 100 if percent else interval["upper"]
        interval_suffix = "%" if percent else suffix
        rendered += f"; 95% CI [{lower:.3f}, {upper:.3f}]{interval_suffix}"
    return rendered


def _format_number(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    return f"{float(value):.6g}"


def _format_signed(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.6g}"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


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
        raise ReportingError(f"could not write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
