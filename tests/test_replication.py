from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from structured_output_reliability.config import load_experiment

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "replicate_baseline.py"
SPEC = importlib.util.spec_from_file_location("replicate_baseline", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
replicate_baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replicate_baseline)
COMPARE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compare_replications.py"
COMPARE_SPEC = importlib.util.spec_from_file_location("compare_replications", COMPARE_PATH)
assert COMPARE_SPEC is not None and COMPARE_SPEC.loader is not None
compare_replications = importlib.util.module_from_spec(COMPARE_SPEC)
COMPARE_SPEC.loader.exec_module(compare_replications)


def test_prepared_replications_keep_cases_and_use_distinct_orders(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "examples" / "openai-baseline.yaml"
    config = load_experiment(source)
    config["output"]["root"] = str(tmp_path / "runs")
    saved = tmp_path / "source.yaml"
    saved.write_text(yaml.safe_dump(config), encoding="utf-8")

    plan_path = replicate_baseline.prepare(saved, 5)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert plan["measured_attempts_per_run"] == 192
    assert len(plan["runs"]) == 5
    assert len({tuple(entry["condition_ids"]) for entry in plan["runs"]}) == 5
    assert all(entry["run_directory"] is None for entry in plan["runs"])
    assert all(
        load_experiment(entry["config"])["run"]["seed"] == config["run"]["seed"]
        for entry in plan["runs"]
    )


def test_execute_records_runs_and_can_resume_without_repeating_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_experiment(
        Path(__file__).resolve().parents[1] / "examples" / "openai-baseline.yaml"
    )
    config["output"]["root"] = str(tmp_path / "runs")
    config["endpoint"]["api_key_env"] = "SOR_REPLICATION_TEST_KEY"
    saved = tmp_path / "source.yaml"
    saved.write_text(yaml.safe_dump(config), encoding="utf-8")
    plan_path = replicate_baseline.prepare(saved, 2)
    called: list[Path] = []

    def fake_run(path: str, *, on_progress) -> SimpleNamespace:
        called.append(Path(path))
        return SimpleNamespace(
            run_directory=tmp_path / f"finished-{len(called)}",
            request_failures=0,
        )

    monkeypatch.setattr(replicate_baseline, "run_experiment", fake_run)
    with pytest.raises(ValueError, match="not set in this terminal"):
        replicate_baseline.execute(plan_path, 0)
    assert called == []

    monkeypatch.setenv("SOR_REPLICATION_TEST_KEY", "test-only")
    replicate_baseline.execute(plan_path, 0)
    replicate_baseline.execute(plan_path, 0)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(called) == 2
    assert all(entry["run_directory"] is not None for entry in plan["runs"])


def test_five_run_interval_uses_runs_as_observations() -> None:
    summary = compare_replications._run_level_summary([5.0] * 5)

    assert summary == {
        "by_run": [5.0] * 5,
        "mean": 5.0,
        "range": [5.0, 5.0],
        "t_interval": [5.0, 5.0],
    }
    unavailable = compare_replications._run_level_summary(
        [1.0, None, 3.0, 4.0, 5.0]
    )
    assert unavailable["t_interval"] is None
