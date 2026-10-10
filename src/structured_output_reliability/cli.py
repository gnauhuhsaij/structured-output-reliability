"""Command-line interface for structured-output reliability experiments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from structured_output_reliability import __version__
from structured_output_reliability.config import (
    ConfigurationError,
    load_experiment,
    summarize_experiment,
)
from structured_output_reliability.generator import (
    GenerationError,
    generate_cases,
    write_cases_jsonl,
)
from structured_output_reliability.metrics import AggregationError, summarize_run
from structured_output_reliability.scorer import (
    REQUEST_FAILURES,
    ScoringError,
    load_case_from_jsonl,
    score_response,
)
from structured_output_reliability.runner import RunnerError, run_experiment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sor",
        description="Structured-output reliability testing under load.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate and summarize an experiment YAML file",
    )
    validate_parser.add_argument("config", type=Path)
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable validation result",
    )

    generate_parser = subparsers.add_parser(
        "generate",
        help="generate deterministic cases from an experiment YAML file",
    )
    generate_parser.add_argument("config", type=Path)
    generate_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="path for the generated cases.jsonl file",
    )
    generate_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )
    generate_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable generation result",
    )

    score_parser = subparsers.add_parser(
        "score",
        help="score one saved response against one generated case",
    )
    score_parser.add_argument("cases", type=Path)
    score_parser.add_argument("--case-id", required=True)
    response_source = score_parser.add_mutually_exclusive_group(required=True)
    response_source.add_argument(
        "--response",
        type=Path,
        help="UTF-8 file containing the exact assistant content",
    )
    response_source.add_argument(
        "--request-failure",
        choices=REQUEST_FAILURES,
        help="score an attempt that failed before content evaluation",
    )
    score_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete machine-readable score",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="run an experiment against its configured endpoint",
    )
    run_parser.add_argument("config", type=Path)
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable run result",
    )

    summarize_parser = subparsers.add_parser(
        "summarize",
        help="regenerate JSON and CSV metrics from a saved run",
    )
    summarize_parser.add_argument("run_directory", type=Path)
    summarize_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable summary result",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate_command(args.config, json_output=args.json)
    if args.command == "generate":
        return _generate_command(
            args.config,
            output_path=args.output,
            overwrite=args.force,
            json_output=args.json,
        )
    if args.command == "score":
        return _score_command(
            args.cases,
            case_id=args.case_id,
            response_path=args.response,
            request_failure=args.request_failure,
            json_output=args.json,
        )
    if args.command == "run":
        return _run_command(args.config, json_output=args.json)
    if args.command == "summarize":
        return _summarize_command(args.run_directory, json_output=args.json)
    raise AssertionError(f"unhandled command: {args.command}")


def _validate_command(config_path: Path, *, json_output: bool) -> int:
    try:
        config = load_experiment(config_path)
    except ConfigurationError as exc:
        if json_output:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "path": str(config_path),
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"Configuration invalid: {config_path}", file=sys.stderr)
            for issue in exc.issues:
                print(f"- {issue.path}: {issue.message}", file=sys.stderr)
        return 2

    summary = summarize_experiment(config)
    if json_output:
        print(
            json.dumps(
                {
                    "valid": True,
                    "path": str(config_path),
                    **summary.to_dict(),
                },
                sort_keys=True,
            )
        )
    else:
        print(f"Configuration valid: {config_path}")
        print(f"{summary.conditions} conditions")
        print(f"{summary.generated_cases} generated cases")
        print(f"{summary.measured_attempts} measured attempts")
        print(f"{summary.warmup_attempts} warmup attempts")
    return 0


def _generate_command(
    config_path: Path,
    *,
    output_path: Path,
    overwrite: bool,
    json_output: bool,
) -> int:
    try:
        config = load_experiment(config_path)
        generated = generate_cases(config)
        write_cases_jsonl(generated, output_path, overwrite=overwrite)
    except (ConfigurationError, GenerationError) as exc:
        if json_output:
            if isinstance(exc, ConfigurationError):
                issues = [
                    {"path": issue.path, "message": issue.message}
                    for issue in exc.issues
                ]
            else:
                issues = [{"path": "$", "message": str(exc)}]
            print(
                json.dumps(
                    {
                        "generated": False,
                        "config": str(config_path),
                        "output": str(output_path),
                        "issues": issues,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"Generation failed: {exc}", file=sys.stderr)
        return 2

    result = {
        "generated": True,
        "config": str(config_path),
        "output": str(output_path),
        "case_count": len(generated.records),
        "case_set_sha256": generated.sha256,
    }
    if json_output:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Generated {len(generated.records)} cases: {output_path}")
        print(f"Case-set SHA-256: {generated.sha256}")
    return 0


def _score_command(
    cases_path: Path,
    *,
    case_id: str,
    response_path: Path | None,
    request_failure: str | None,
    json_output: bool,
) -> int:
    try:
        case = load_case_from_jsonl(cases_path, case_id)
        if response_path is None:
            content = None
        else:
            try:
                content = response_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ScoringError(
                    f"cannot read response file {response_path}: {exc}"
                ) from exc
        score = score_response(
            case,
            content,
            request_failure=request_failure,
        )
    except ScoringError as exc:
        if json_output:
            print(
                json.dumps(
                    {
                        "scored": False,
                        "case_id": case_id,
                        "issue": str(exc),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"Scoring failed: {exc}", file=sys.stderr)
        return 2

    if json_output:
        print(json.dumps(score, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Case: {case_id}")
        print(f"Primary outcome: {score['primary_outcome']}")
        print(f"Request success: {score['request_success']}")
        print(f"JSON parsed: {score['json_parsed']}")
        print(f"Schema valid: {score['schema_valid']}")
        print(f"Exact correct: {score['exact_correct']}")
        print(f"Correct fields: {score['correct_fields']}/{score['total_fields']}")
    return 0


def _run_command(config_path: Path, *, json_output: bool) -> int:
    try:
        result = run_experiment(config_path)
    except (ConfigurationError, GenerationError, RunnerError) as exc:
        if json_output:
            print(
                json.dumps(
                    {
                        "completed": False,
                        "config": str(config_path),
                        "issue": str(exc),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"Run failed: {exc}", file=sys.stderr)
        return 2

    output = {
        "completed": True,
        "run_id": result.run_id,
        "run_directory": str(result.run_directory),
        "condition_count": result.condition_count,
        "measured_attempts": result.measured_attempts,
        "warmup_attempts": result.warmup_attempts,
        "request_failures": result.request_failures,
    }
    if json_output:
        print(json.dumps(output, sort_keys=True))
    else:
        print(f"Run complete: {result.run_id}")
        print(f"Artifacts: {result.run_directory}")
        print(f"{result.condition_count} conditions")
        print(f"{result.measured_attempts} measured attempts")
        print(f"{result.warmup_attempts} warmup attempts")
        print(f"{result.request_failures} request failures")
    return 0


def _summarize_command(run_directory: Path, *, json_output: bool) -> int:
    try:
        result = summarize_run(run_directory)
    except AggregationError as exc:
        if json_output:
            print(
                json.dumps(
                    {
                        "summarized": False,
                        "run_directory": str(run_directory),
                        "issue": str(exc),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"Summarization failed: {exc}", file=sys.stderr)
        return 2

    output = {
        "summarized": True,
        "run_id": result.summary["run_id"],
        "run_complete": result.summary["run_complete"],
        "condition_count": result.summary["condition_count"],
        "summary_json": str(result.json_path),
        "summary_csv": str(result.csv_path),
    }
    if json_output:
        print(json.dumps(output, sort_keys=True))
    else:
        print(f"Summary complete: {result.summary['run_id']}")
        print(f"JSON: {result.json_path}")
        print(f"CSV: {result.csv_path}")
        print(f"{result.summary['condition_count']} conditions")
    return 0
