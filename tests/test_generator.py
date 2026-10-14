from __future__ import annotations

import builtins
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from structured_output_reliability.config import load_experiment, load_schema
from structured_output_reliability.generator import (
    GenerationError,
    HuggingFaceLengthMeasurer,
    canonical_json,
    generate_cases,
    write_cases_jsonl,
)
from structured_output_reliability.runner import build_conditions, build_request_body
from structured_output_reliability.scorer import score_response

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHARACTER_EXAMPLE = REPOSITORY_ROOT / "examples" / "character-experiment.yaml"
OPENAI_SMOKE_EXAMPLE = REPOSITORY_ROOT / "examples" / "openai-smoke.yaml"


class CharacterLikeTokenMeasurer:
    unit = "tokens"

    def count_messages(self, messages) -> int:
        return sum(len(message["content"]) for message in messages)

    def count_json(self, value: Any) -> int:
        return len(canonical_json(value))


def test_generation_is_deterministic_valid_and_exactly_sized() -> None:
    config = load_experiment(CHARACTER_EXAMPLE)

    first = generate_cases(config)
    second = generate_cases(config)

    assert first == second
    assert len(first.records) == 4
    assert [record["family"] for record in first.records] == [
        "flat_object",
        "nested_object",
        "array_object",
        "mixed_types",
    ]
    assert len({record["case_id"] for record in first.records}) == 4
    assert first.sha256 == hashlib.sha256(
        canonical_json(list(first.records)).encode("utf-8")
    ).hexdigest()
    assert (
        first.sha256
        == "0800fb47b0c1fb499adb5a63df7e32f8a1090444c37e1c64f73116da8f49360e"
    )

    case_validator = Draft202012Validator(load_schema("case"))
    for record in first.records:
        case_validator.validate(record)
        Draft202012Validator(record["json_schema"]).validate(record["expected"])
        assert record["prompt_length"] == {
            "target": 2048,
            "actual": 2048,
            "unit": "characters",
            "exact": False,
        }
        assert record["expected_output_length"] == {
            "target": 256,
            "actual": 256,
            "unit": "characters",
            "exact": False,
        }
        assert record["expected_field_count"] == len(
            record["expected_field_paths"]
        )
        payload = {key: value for key, value in record.items() if key != "case_id"}
        expected_id = "case_" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()[:16]
        assert record["case_id"] == expected_id


def test_each_length_cell_gets_the_configured_case_count() -> None:
    config = load_experiment(CHARACTER_EXAMPLE)
    config["matrix"]["prompt_lengths"] = [2048, 2300]
    config["matrix"]["output_shapes"].append(
        {"target_length": 300, "max_completion_tokens": 160}
    )

    generated = generate_cases(config)

    assert len(generated.records) == 16
    cells: dict[tuple[int, int], int] = {}
    for record in generated.records:
        cell = (
            record["prompt_length"]["target"],
            record["expected_output_length"]["target"],
        )
        cells[cell] = cells.get(cell, 0) + 1
    assert cells == {
        (2048, 256): 4,
        (2048, 300): 4,
        (2300, 256): 4,
        (2300, 300): 4,
    }


def test_token_measurer_marks_exact_targets() -> None:
    config = load_experiment(CHARACTER_EXAMPLE)
    config["cases"]["length_unit"] = "tokens"

    generated = generate_cases(config, measurer=CharacterLikeTokenMeasurer())

    assert all(record["prompt_length"]["exact"] for record in generated.records)
    assert all(
        record["expected_output_length"]["exact"] for record in generated.records
    )
    assert all(
        record["prompt_length"]["actual"]
        == record["prompt_length"]["target"]
        for record in generated.records
    )


def test_too_small_length_target_is_actionable() -> None:
    config = load_experiment(CHARACTER_EXAMPLE)
    config["matrix"]["output_shapes"][0]["target_length"] = 1

    with pytest.raises(GenerationError, match="minimum is"):
        generate_cases(config)


def test_openai_smoke_cases_are_generatable_and_score_correctly() -> None:
    config = load_experiment(OPENAI_SMOKE_EXAMPLE)
    generated = generate_cases(config)
    conditions = build_conditions(config, generated)

    assert [case["family"] for case in generated.records] == [
        "flat_object",
        "nested_object",
        "array_object",
        "mixed_types",
    ]
    assert len(conditions) == 4

    case_validator = Draft202012Validator(load_schema("case"))
    for case in generated.records:
        case_validator.validate(case)
        Draft202012Validator(case["json_schema"]).validate(case["expected"])
        assert case["prompt_length"]["actual"] == 2048
        assert case["expected_output_length"]["actual"] == 256
        assert case["generator"]["version"] == "1.1.0"
        user_content = case["messages"][-1]["content"]
        assert user_content.index("PADDING_END") < user_content.index("SOURCE_JSON:")
        assert user_content.endswith("END_SOURCE_JSON")
        assert "x" not in user_content.split("PADDING_START\n", 1)[1].split(
            "\nPADDING_END", 1
        )[0]
        score = score_response(case, canonical_json(case["expected"]))
        assert score["exact_correct"] is True
        assert score["correct_fields"] == score["total_fields"]

        for condition in conditions:
            body = build_request_body(condition, case)
            assert body["max_completion_tokens"] == 512
            assert body["stream_options"] == {"include_usage": True}
            if condition["structured_mode"]["type"] == "response_format":
                sent_schema = body["response_format"]["json_schema"]["schema"]
                assert "$schema" not in sent_schema
                assert sent_schema == {
                    key: value for key, value in case["json_schema"].items()
                    if key != "$schema"
                }
            else:
                assert "response_format" not in body


def test_unknown_generator_version_is_rejected() -> None:
    config = load_experiment(CHARACTER_EXAMPLE)
    config["cases"]["generator_version"] = "2.0.0"

    with pytest.raises(GenerationError, match="unsupported"):
        generate_cases(config)


def test_jsonl_writer_is_atomic_and_refuses_overwrite(tmp_path: Path) -> None:
    generated = generate_cases(load_experiment(CHARACTER_EXAMPLE))
    output = tmp_path / "nested" / "cases.jsonl"

    write_cases_jsonl(generated, output)

    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert records == list(generated.records)
    with pytest.raises(GenerationError, match="--force"):
        write_cases_jsonl(generated, output)

    changed = copy.deepcopy(records)
    changed[0]["generator"]["seed"] += 1
    replacement = type(generated)(records=tuple(changed), sha256="replacement")
    write_cases_jsonl(replacement, output, overwrite=True)
    assert json.loads(output.read_text().splitlines()[0]) == changed[0]


def test_missing_tokenizer_extra_has_install_hint(monkeypatch) -> None:
    real_import = builtins.__import__

    def import_without_transformers(name, *args, **kwargs):
        if name == "transformers":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_transformers)
    config = load_experiment(CHARACTER_EXAMPLE)
    config["tokenizer"] = {
        "name": "example/tokenizer",
        "trust_remote_code": False,
    }

    with pytest.raises(GenerationError, match=r"\[tokenizer\]"):
        HuggingFaceLengthMeasurer.from_config(config)
