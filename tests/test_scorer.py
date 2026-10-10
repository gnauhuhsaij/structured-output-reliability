from __future__ import annotations

import copy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from structured_output_reliability.config import load_experiment, load_schema
from structured_output_reliability.generator import canonical_json, generate_cases
from structured_output_reliability.scorer import (
    REQUEST_FAILURES,
    ScoringError,
    StrictJSONError,
    score_response,
    strict_json_loads,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHARACTER_EXAMPLE = REPOSITORY_ROOT / "examples" / "character-experiment.yaml"


@pytest.fixture(scope="module")
def cases() -> tuple[dict, ...]:
    config = load_experiment(CHARACTER_EXAMPLE)
    return generate_cases(config).records


def assert_valid_score(score: dict) -> None:
    attempt_schema = load_schema("attempt")
    score_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "#/$defs/score",
        "$defs": attempt_schema["$defs"],
    }
    Draft202012Validator(score_schema).validate(score)


def test_exact_response_passes_every_stage(cases) -> None:
    case = cases[0]

    score = score_response(case, canonical_json(case["expected"]))

    assert_valid_score(score)
    assert score == {
        "request_success": True,
        "json_parsed": True,
        "parsed_json": case["expected"],
        "schema_valid": True,
        "exact_correct": True,
        "correct_fields": case["expected_field_count"],
        "total_fields": case["expected_field_count"],
        "parse_error": None,
        "schema_errors": [],
        "comparison_errors": [],
        "primary_outcome": "exact_correct",
    }


def test_whitespace_and_object_key_order_do_not_affect_exactness(cases) -> None:
    expected = cases[0]["expected"]
    content = (
        " \n{"
        f'"padding":{canonical_json(expected["padding"])},'
        f'"label":{canonical_json(expected["label"])},'
        f'"count":{expected["count"]},'
        f'"active":{str(expected["active"]).lower()}'
        "}\t"
    )

    score = score_response(cases[0], content)

    assert score["exact_correct"] is True


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"value": 1}\n```',
        'answer: {"value": 1}',
        '{"value": 1} trailing',
        '{"value":',
        '{"value": NaN}',
        '{"value": Infinity}',
        '{"value": -Infinity}',
        '1e400',
    ],
)
def test_strict_parser_rejects_repairable_or_non_json_content(content) -> None:
    with pytest.raises(StrictJSONError):
        strict_json_loads(content)


def test_strict_parser_rejects_duplicate_keys() -> None:
    with pytest.raises(StrictJSONError, match="duplicate object key"):
        strict_json_loads('{"value":1,"value":2}')


@pytest.mark.parametrize("content", [None, ""])
def test_empty_content_preserves_field_denominator(cases, content) -> None:
    score = score_response(cases[0], content)

    assert_valid_score(score)
    assert score["request_success"] is True
    assert score["json_parsed"] is False
    assert score["correct_fields"] == 0
    assert score["total_fields"] == cases[0]["expected_field_count"]
    assert score["primary_outcome"] == "empty_content"


def test_whitespace_only_content_is_invalid_json_not_empty(cases) -> None:
    score = score_response(cases[0], " \n\t")

    assert score["primary_outcome"] == "invalid_json"
    assert score["parse_error"] is not None


def test_parsed_json_null_is_distinct_from_parse_failure(cases) -> None:
    score = score_response(cases[0], "null")

    assert score["json_parsed"] is True
    assert score["parsed_json"] is None
    assert score["schema_valid"] is False
    assert score["correct_fields"] == 0
    assert score["primary_outcome"] == "schema_invalid"


def test_schema_failure_still_computes_field_accuracy(cases) -> None:
    case = cases[0]
    actual = copy.deepcopy(case["expected"])
    actual["count"] = "wrong type"

    score = score_response(case, canonical_json(actual))

    assert score["json_parsed"] is True
    assert score["schema_valid"] is False
    assert score["exact_correct"] is False
    assert score["correct_fields"] == case["expected_field_count"] - 1
    assert score["schema_errors"][0]["path"] == "/count"
    assert score["comparison_errors"][0]["path"] == "/count"
    assert score["primary_outcome"] == "schema_invalid"


def test_schema_valid_wrong_value_is_field_incorrect(cases) -> None:
    case = cases[0]
    actual = copy.deepcopy(case["expected"])
    actual["count"] += 1

    score = score_response(case, canonical_json(actual))

    assert score["schema_valid"] is True
    assert score["exact_correct"] is False
    assert score["correct_fields"] == case["expected_field_count"] - 1
    assert score["primary_outcome"] == "field_incorrect"


def test_boolean_is_not_equal_to_integer(cases) -> None:
    case = cases[0]
    actual = copy.deepcopy(case["expected"])
    actual["active"] = 1

    score = score_response(case, canonical_json(actual))

    assert score["schema_valid"] is False
    assert score["correct_fields"] == case["expected_field_count"] - 1
    assert score["comparison_errors"][0]["path"] == "/active"


def test_wrong_ancestor_marks_every_affected_leaf_incorrect(cases) -> None:
    case = cases[1]
    actual = copy.deepcopy(case["expected"])
    actual["profile"] = "wrong type"

    score = score_response(case, canonical_json(actual))

    assert score["correct_fields"] == 1
    assert [error["path"] for error in score["comparison_errors"]] == [
        "/profile/age",
        "/profile/name",
    ]


def test_unexpected_field_does_not_change_denominator(cases) -> None:
    case = cases[0]
    actual = copy.deepcopy(case["expected"])
    actual["extra"] = "unexpected"

    score = score_response(case, canonical_json(actual))

    assert score["correct_fields"] == case["expected_field_count"]
    assert score["total_fields"] == case["expected_field_count"]
    assert score["schema_valid"] is False
    assert score["exact_correct"] is False
    assert {error["path"] for error in score["comparison_errors"]} == {"/extra"}


def test_schema_allowed_extra_field_still_fails_exact_comparison(cases) -> None:
    case = copy.deepcopy(cases[0])
    case["json_schema"]["additionalProperties"] = True
    actual = copy.deepcopy(case["expected"])
    actual["extra"] = "schema permits this"

    score = score_response(case, canonical_json(actual))

    assert score["schema_valid"] is True
    assert score["correct_fields"] == case["expected_field_count"]
    assert score["exact_correct"] is False
    assert score["primary_outcome"] == "field_incorrect"
    assert score["comparison_errors"] == [
        {"path": "/extra", "message": "unexpected object field"}
    ]


def test_arrays_are_order_sensitive(cases) -> None:
    case = cases[2]
    actual = copy.deepcopy(case["expected"])
    actual["items"].reverse()

    score = score_response(case, canonical_json(actual))

    assert score["schema_valid"] is True
    assert score["exact_correct"] is False
    assert score["correct_fields"] < case["expected_field_count"]


@pytest.mark.parametrize(
    ("mode", "tolerance", "delta"),
    [
        ("absolute_tolerance", 0.01, 0.005),
        ("relative_tolerance", 0.1, 0.005),
    ],
)
def test_declared_float_tolerance_applies_to_fields_and_exactness(
    cases, mode, tolerance, delta
) -> None:
    case = copy.deepcopy(cases[3])
    case["comparison"]["numeric"] = mode
    case["comparison"][mode] = tolerance
    actual = copy.deepcopy(case["expected"])
    actual["ratio"] += delta

    score = score_response(case, canonical_json(actual))

    assert score["schema_valid"] is True
    assert score["correct_fields"] == case["expected_field_count"]
    assert score["exact_correct"] is True


def test_integer_fields_remain_exact_under_numeric_tolerance(cases) -> None:
    case = copy.deepcopy(cases[0])
    case["comparison"]["numeric"] = "absolute_tolerance"
    case["comparison"]["absolute_tolerance"] = 100
    actual = copy.deepcopy(case["expected"])
    actual["count"] += 1

    score = score_response(case, canonical_json(actual))

    assert score["correct_fields"] == case["expected_field_count"] - 1
    assert score["exact_correct"] is False


def test_expected_empty_container_is_one_scorable_field(cases) -> None:
    case = copy.deepcopy(cases[0])
    case["expected"] = {"empty": {}}
    case["json_schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["empty"],
        "properties": {
            "empty": {
                "type": "object",
                "additionalProperties": False,
            }
        },
    }
    case["expected_field_paths"] = ["/empty"]
    case["expected_field_count"] = 1

    correct = score_response(case, '{"empty":{}}')
    wrong = score_response(case, '{"empty":{"extra":1}}')

    assert correct["correct_fields"] == 1
    assert correct["exact_correct"] is True
    assert wrong["correct_fields"] == 0
    assert wrong["total_fields"] == 1


@pytest.mark.parametrize("failure", REQUEST_FAILURES)
def test_request_failure_stops_later_stages_and_preserves_denominator(
    cases, failure
) -> None:
    case = cases[0]

    score = score_response(
        case,
        canonical_json(case["expected"]),
        request_failure=failure,
    )

    assert_valid_score(score)
    assert score["request_success"] is False
    assert score["json_parsed"] is False
    assert score["schema_valid"] is False
    assert score["exact_correct"] is False
    assert score["correct_fields"] == 0
    assert score["total_fields"] == case["expected_field_count"]
    assert score["primary_outcome"] == failure


def test_remote_schema_reference_is_a_setup_error(cases) -> None:
    case = copy.deepcopy(cases[0])
    case["json_schema"] = {"$ref": "https://example.com/remote-schema.json"}

    with pytest.raises(ScoringError, match="self-contained"):
        score_response(case, canonical_json(case["expected"]))


def test_unresolved_local_schema_reference_is_a_setup_error(cases) -> None:
    case = copy.deepcopy(cases[0])
    case["json_schema"] = {"$ref": "#/$defs/missing"}

    with pytest.raises(ScoringError, match="could not be resolved"):
        score_response(case, canonical_json(case["expected"]))


def test_field_metadata_mismatch_is_a_setup_error(cases) -> None:
    case = copy.deepcopy(cases[0])
    case["expected_field_paths"] = ["/active"]
    case["expected_field_count"] = 1

    with pytest.raises(ScoringError, match="expected_field"):
        score_response(case, canonical_json(case["expected"]))
