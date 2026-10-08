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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate_command(args.config, json_output=args.json)
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

