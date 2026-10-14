from __future__ import annotations

import copy
from pathlib import Path

import pytest

from structured_output_reliability.config import (
    ConfigurationError,
    expand_matrix,
    load_experiment,
    summarize_experiment,
    validate_experiment,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def minimal_config() -> dict:
    return {
        "schema_version": "1.0.0",
        "name": "minimal",
        "endpoint": {
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "test-model",
            "api_key_env": "TEST_API_KEY",
        },
        "cases": {
            "count": 2,
            "length_unit": "characters",
            "families": ["flat_object"],
        },
        "run": {
            "seed": 7,
            "repetitions": 2,
        },
        "structured_modes": [
            {
                "id": "prompt_only",
                "type": "prompt_only",
            }
        ],
        "matrix": {
            "concurrency": [1],
            "prompt_lengths": [128],
            "output_shapes": [
                {
                    "target_length": 32,
                    "max_completion_tokens": 48,
                }
            ],
            "temperatures": [0],
            "request_seeds": [None],
            "structured_modes": ["prompt_only"],
        },
        "output": {
            "root": "./runs",
        },
    }


def test_example_configuration_and_counts() -> None:
    config = load_experiment(REPOSITORY_ROOT / "examples" / "experiment.yaml")

    assert summarize_experiment(config).to_dict() == {
        "conditions": 16,
        "generated_cases": 40,
        "measured_attempts": 960,
        "warmup_attempts": 32,
    }


def test_defaults_are_applied_without_mutating_input() -> None:
    document = minimal_config()
    original = copy.deepcopy(document)

    resolved = validate_experiment(document)

    assert document == original
    assert resolved["endpoint"]["path"] == "/chat/completions"
    assert resolved["endpoint"]["timeout_seconds"] == 60
    assert resolved["cases"]["generator_version"] == "1.0.0"
    assert resolved["cases"]["comparison"] == {
        "numeric": "exact",
        "absolute_tolerance": 0,
        "relative_tolerance": 0,
        "arrays": "ordered",
    }
    assert resolved["run"]["stream"] is True
    assert resolved["run"]["warmup_requests_per_condition"] == 0
    assert resolved["gates"] == []
    assert resolved["output"]["flush_every"] == 1
    assert resolved["output"]["include_raw_response"] is True
    assert resolved["output"]["include_stream_events"] is True


def test_null_api_key_allows_unauthenticated_local_service() -> None:
    document = minimal_config()
    document["endpoint"]["api_key_env"] = None

    resolved = validate_experiment(document)

    assert resolved["endpoint"]["api_key_env"] is None


def test_stream_usage_requires_streaming() -> None:
    document = minimal_config()
    document["run"].update({"stream": False, "stream_include_usage": True})

    with pytest.raises(ConfigurationError, match="stream_include_usage"):
        validate_experiment(document)


def test_matrix_expansion_uses_normative_order() -> None:
    config = load_experiment(REPOSITORY_ROOT / "examples" / "experiment.yaml")

    points = expand_matrix(config)

    assert len(points) == 16
    assert points[0]["concurrency"] == 1
    assert points[0]["prompt_length"]["target"] == 256
    assert points[0]["temperature"] == 0
    assert points[0]["structured_mode"]["id"] == "prompt_only"
    assert points[1]["structured_mode"]["id"] == "json_schema"
    assert points[2]["temperature"] == 0.7
    assert points[-1]["concurrency"] == 8
    assert points[-1]["prompt_length"]["target"] == 2048
    assert points[-1]["temperature"] == 0.7
    assert points[-1]["structured_mode"]["id"] == "json_schema"
    assert points[-1]["structured_mode"]["portable"] is True


def test_response_format_strict_default_is_applied() -> None:
    document = minimal_config()
    document["structured_modes"] = [
        {
            "id": "json_schema",
            "type": "response_format",
        }
    ]
    document["matrix"]["structured_modes"] = ["json_schema"]

    resolved = validate_experiment(document)

    assert resolved["structured_modes"][0]["strict"] is True
    assert expand_matrix(resolved)[0]["structured_mode"]["portable"] is True


def test_duplicate_yaml_key_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "duplicate.yaml"
    config_path.write_text("name: first\nname: second\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="duplicate key"):
        load_experiment(config_path)


def test_unknown_property_is_rejected() -> None:
    document = minimal_config()
    document["endpoint"]["unexpected"] = True

    with pytest.raises(ConfigurationError) as caught:
        validate_experiment(document)

    assert caught.value.issues[0].path == "$.endpoint"
    assert "Additional properties" in caught.value.issues[0].message


def test_token_lengths_require_tokenizer() -> None:
    document = minimal_config()
    document["cases"]["length_unit"] = "tokens"

    with pytest.raises(ConfigurationError, match="tokenizer"):
        validate_experiment(document)


def test_matrix_mode_reference_must_exist() -> None:
    document = minimal_config()
    document["matrix"]["structured_modes"] = ["missing"]

    with pytest.raises(ConfigurationError, match="undefined structured mode"):
        validate_experiment(document)


def test_structured_mode_ids_must_be_unique() -> None:
    document = minimal_config()
    document["structured_modes"].append(
        {
            "id": "prompt_only",
            "type": "prompt_only",
        }
    )

    with pytest.raises(ConfigurationError, match="defined more than once"):
        validate_experiment(document)


def test_matrix_axis_values_must_be_unique() -> None:
    document = minimal_config()
    document["matrix"]["concurrency"] = [1, 1]

    with pytest.raises(ConfigurationError, match="non-unique elements"):
        validate_experiment(document)


def test_stream_timing_slo_requires_streaming() -> None:
    document = minimal_config()
    document["run"]["stream"] = False
    document["slos"] = {"max_ttft_ms": 100}

    with pytest.raises(ConfigurationError, match="requires run.stream"):
        validate_experiment(document)


def test_relative_gate_requires_baseline() -> None:
    document = minimal_config()
    document["gates"] = [
        {
            "metric": "schema_valid_rate",
            "kind": "max_drop",
            "value": 0.05,
        }
    ]

    with pytest.raises(ConfigurationError, match="requires baseline_run"):
        validate_experiment(document)


def test_extra_body_is_resolved_and_marked_backend_specific() -> None:
    document = minimal_config()
    document["structured_modes"] = [
        {
            "id": "vllm_guided",
            "type": "extra_body",
            "body": {"structured_outputs": {"json": None}},
            "schema_json_pointer": "/structured_outputs/json",
        }
    ]
    document["matrix"]["structured_modes"] = ["vllm_guided"]

    config = validate_experiment(document)
    point = expand_matrix(config)[0]

    assert point["structured_mode"]["portable"] is False
    assert point["structured_mode"]["body"] == {
        "structured_outputs": {"json": None}
    }


def test_extra_body_cannot_override_core_fields_or_store_secrets() -> None:
    document = minimal_config()
    document["structured_modes"] = [
        {
            "id": "unsafe",
            "type": "extra_body",
            "body": {
                "model": "replacement",
                "authorization": "Bearer do-not-log-this",
                "structured_outputs": {"json": None},
            },
            "schema_json_pointer": "/structured_outputs/json",
        }
    ]
    document["matrix"]["structured_modes"] = ["unsafe"]

    with pytest.raises(ConfigurationError) as caught:
        validate_experiment(document)

    messages = " ".join(issue.message for issue in caught.value.issues)
    assert "core request fields" in messages
    assert "credential value" in messages
    assert "do-not-log-this" not in str(caught.value)


def test_extra_body_schema_pointer_parent_must_exist() -> None:
    document = minimal_config()
    document["structured_modes"] = [
        {
            "id": "invalid_pointer",
            "type": "extra_body",
            "body": {},
            "schema_json_pointer": "/structured_outputs/json",
        }
    ]
    document["matrix"]["structured_modes"] = ["invalid_pointer"]

    with pytest.raises(ConfigurationError, match="does not exist in body"):
        validate_experiment(document)


def test_schema_error_redacts_api_key_env_value() -> None:
    document = minimal_config()
    document["endpoint"]["api_key_env"] = "sk-do-not-log-this"

    with pytest.raises(ConfigurationError) as caught:
        validate_experiment(document)

    assert "sk-do-not-log-this" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
