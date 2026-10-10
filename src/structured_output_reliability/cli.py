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
