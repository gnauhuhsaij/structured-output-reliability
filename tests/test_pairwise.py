from __future__ import annotations

from copy import deepcopy

from structured_output_reliability.pairwise import build_pairwise_contrasts


def _condition(condition_id: str, concurrency: int, mode: str, throughput: float) -> dict:
    return {
        "condition_id": condition_id,
        "dimensions": {
            "model": "mock",
            "quantization": None,
            "concurrency": concurrency,
            "prompt_length": 2048,
            "prompt_length_unit": "characters",
            "output_length": 256,
            "output_length_unit": "characters",
            "max_completion_tokens": 512,
            "temperature": 0,
            "request_seed": 42,
            "structured_mode": mode,
            "stream": True,
            "repetitions": 2,
        },
        "metrics": {"request_throughput_rps": {"value": throughput}},
    }


def _attempt(condition_id: str, case_id: str, repeat_id: int, latency_ms: int) -> dict:
    return {
        "condition_id": condition_id,
        "case_id": case_id,
        "repeat_id": repeat_id,
        "phase": "measure",
        "timing": {
            "e2e_latency_ns": latency_ms * 1_000_000,
            "ttft_ns": latency_ms * 500_000,
            "tpot_ns": None,
        },
    }


def test_pairwise_contrasts_match_cases_and_change_one_axis() -> None:
    conditions = [
        _condition("c1_prompt", 1, "prompt_only", 1),
        _condition("c1_schema", 1, "json_schema", 1.1),
        _condition("c4_prompt", 4, "prompt_only", 3),
        _condition("c4_schema", 4, "json_schema", 3.3),
    ]
    offsets = {"c1_prompt": 0, "c1_schema": -10, "c4_prompt": 20, "c4_schema": 10}
    attempts = [
        _attempt(condition["condition_id"], f"case_{case_index}", repeat_id,
                 100 + case_index * 10 + repeat_id * 4 + offsets[condition["condition_id"]])
        for condition in conditions
        for case_index in range(3)
        for repeat_id in range(2)
    ]
    attempts.append({**_attempt("c1_prompt", "case_0", 0, 999), "phase": "warmup"})

    first = build_pairwise_contrasts(conditions, attempts, experiment_seed=7)
    second = build_pairwise_contrasts(conditions, attempts, experiment_seed=7)
    reordered = build_pairwise_contrasts(
        list(reversed(conditions)), attempts, experiment_seed=7
    )

    assert first == second
    assert first == reordered
    assert len(first) == 4
    by_pair = {(item["first_condition_id"], item["second_condition_id"]): item
               for item in first}
    concurrency = by_pair[("c1_prompt", "c4_prompt")]
    assert concurrency["latency"]["mean_e2e_ms"] == {
        "value_ms": 20,
        "interval_ms": [20, 20],
        "matched_cases": 3,
    }
    assert concurrency["latency"]["mean_ttft_ms"]["value_ms"] == 10
    assert concurrency["latency"]["mean_tpot_ms"] == {
        "value_ms": None,
        "interval_ms": None,
        "matched_cases": 0,
    }
    assert concurrency["request_throughput_ratio"] == 3
    assert by_pair[("c1_prompt", "c1_schema")]["latency"]["mean_e2e_ms"]["value_ms"] == -10


def test_pairwise_contrasts_require_same_other_dimensions_and_complete_pairs() -> None:
    first = _condition("first", 1, "prompt_only", 1)
    second = _condition("second", 4, "prompt_only", 2)
    different_prompt = deepcopy(second)
    different_prompt["dimensions"]["prompt_length"] = 4096
    attempts = [
        _attempt("first", "case_a", 0, 100),
        _attempt("first", "case_a", 1, 100),
        _attempt("second", "case_a", 0, 110),
        _attempt("second", "case_a", 1, 110),
    ]

    assert build_pairwise_contrasts([first, different_prompt], attempts,
                                    experiment_seed=1) == []
    attempts[-1]["timing"]["e2e_latency_ns"] = None
    contrasts = build_pairwise_contrasts([first, second], attempts, experiment_seed=1)
    assert contrasts[0]["latency"]["mean_e2e_ms"]["matched_cases"] == 0
    assert contrasts[0]["latency"]["mean_e2e_ms"]["interval_ms"] is None
