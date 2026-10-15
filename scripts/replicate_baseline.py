#!/usr/bin/env python3
"""Prepare and execute repeated runs with distinct deterministic condition orders."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from structured_output_reliability.config import load_experiment
from structured_output_reliability.generator import generate_cases
from structured_output_reliability.runner import RunProgress, build_conditions, run_experiment


def prepare(config_path: Path, count: int) -> Path:
    """Save self-contained YAMLs and an auditable plan without contacting the API."""

    config = load_experiment(config_path)
    generated = generate_cases(config)
    root = Path(config["output"]["root"]).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    config["output"]["root"] = str(root)

    batch_id = "replication-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    batch_directory = root / batch_id
    batch_directory.mkdir(parents=True, exist_ok=False)
    chosen_orders: set[tuple[str, ...]] = set()
    entries: list[dict[str, Any]] = []
    for order_seed in range(10_000):
        candidate = copy.deepcopy(config)
        candidate["run"]["condition_order_seed"] = order_seed
        conditions = build_conditions(candidate, generated)
        condition_ids = tuple(item["condition_id"] for item in conditions)
        if condition_ids in chosen_orders:
            continue
        chosen_orders.add(condition_ids)
        index = len(entries) + 1
        saved_config = batch_directory / f"config-{index:02d}.yaml"
        saved_config.write_text(
            yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        entries.append(
            {
                "index": index,
                "config": str(saved_config),
                "condition_order_seed": order_seed,
                "condition_ids": list(condition_ids),
                "run_directory": None,
            }
        )
        if len(entries) == count:
            break
    if len(entries) != count:
        raise ValueError(f"only {len(entries)} distinct condition orders exist")

    plan_path = batch_directory / "plan.json"
    _save_plan(
        plan_path,
        {
            "source_config": str(config_path.resolve()),
            "generated_cases_sha256": generated.sha256,
            "measured_attempts_per_run": (
                len(entries[0]["condition_ids"])
                * len(generated.records)
                * config["run"]["repetitions"]
            ),
            "runs": entries,
        },
    )
    return plan_path


def execute(plan_path: Path, cooldown_seconds: float) -> None:
    """Run each prepared config sequentially, saving its directory after success."""

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    entries = plan["runs"]
    for entry in entries:
        if entry["run_directory"] is not None:
            continue
        config = load_experiment(entry["config"])
        key_name = config["endpoint"]["api_key_env"]
        if key_name is not None and not os.environ.get(key_name):
            raise ValueError(
                f"{key_name} is not set in this terminal; no API requests were sent"
            )

        index = entry["index"]
        print(f"\nRun {index}/{len(entries)}; order seed={entry['condition_order_seed']}", flush=True)
        result = run_experiment(entry["config"], on_progress=_progress)
        entry["run_directory"] = str(result.run_directory.resolve())
        entry["request_failures"] = result.request_failures
        _save_plan(plan_path, plan)
        print(
            f"Run {index} complete: {result.run_directory} "
            f"({result.request_failures} request failures)",
            flush=True,
        )
        if index < len(entries) and cooldown_seconds:
            print(f"Cooling down for {cooldown_seconds:g} seconds", flush=True)
            time.sleep(cooldown_seconds)

    print(f"\nAll runs recorded in {plan_path}", flush=True)


def _progress(event: RunProgress) -> None:
    if event.kind == "condition":
        print(
            f"  Condition {event.condition_index}/{event.condition_count}: "
            f"concurrency={event.concurrency}, mode={event.structured_mode}",
            flush=True,
        )
    elif event.kind == "attempt" and (
        event.completed_attempts % 24 == 0
        or event.completed_attempts == event.planned_attempts
    ):
        print(
            f"  Progress {event.completed_attempts}/{event.planned_attempts} "
            f"attempts",
            flush=True,
        )
    elif event.kind == "waiting":
        print("  Still waiting for the current condition", flush=True)


def _save_plan(path: Path, plan: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("examples/openai-baseline.yaml"))
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--plan", type=Path, help="execute or resume an existing plan")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--cooldown-seconds", type=float, default=15)
    args = parser.parse_args()
    if args.count < 1 or args.cooldown_seconds < 0:
        parser.error("count must be positive and cooldown must be nonnegative")
    if args.plan is not None and args.prepare_only:
        parser.error("--plan and --prepare-only cannot be combined")

    try:
        plan_path = args.plan or prepare(args.config, args.count)
        print(f"Plan: {plan_path}", flush=True)
        if not args.prepare_only:
            execute(plan_path, args.cooldown_seconds)
    except (OSError, ValueError) as exc:
        print(f"Replication stopped: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
