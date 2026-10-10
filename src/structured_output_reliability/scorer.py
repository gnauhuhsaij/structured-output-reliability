"""Strict, offline scoring for structured-output responses."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker

from structured_output_reliability.config import load_schema

RequestFailure = Literal[
    "cancelled",
    "timeout",
    "transport_error",
    "http_error",
    "protocol_error",
]

REQUEST_FAILURES: tuple[RequestFailure, ...] = (
    "cancelled",
    "timeout",
    "transport_error",
    "http_error",
    "protocol_error",
)

_MISSING = object()


class ScoringError(ValueError):
    """Raised when a case is invalid or scoring cannot be configured safely."""


class StrictJSONError(ValueError):
    """Raised when assistant content is not exactly one strict JSON value."""


def strict_json_loads(content: str) -> Any:
    """Parse one complete JSON value without repair or non-standard constants."""

    if not isinstance(content, str):
        raise TypeError("JSON content must be a string")

    try:
        return json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise StrictJSONError(str(exc)) from exc


def load_case_from_jsonl(path: str | Path, case_id: str) -> dict[str, Any]:
    """Load exactly one case by ID from a strict JSONL case artifact."""

    case_path = Path(path)
    try:
        text = case_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ScoringError(f"cannot read case file {case_path}: {exc}") from exc

    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ScoringError(
                f"invalid case file {case_path}: blank line at {line_number}"
            )
        try:
            record = strict_json_loads(line)
        except StrictJSONError as exc:
            raise ScoringError(
                f"invalid case file {case_path} at line {line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ScoringError(
                f"invalid case file {case_path} at line {line_number}: "
                "record must be an object"
            )
        if record.get("case_id") == case_id:
            matches.append(record)

    if not matches:
        raise ScoringError(f"case_id {case_id!r} was not found in {case_path}")
    if len(matches) > 1:
        raise ScoringError(f"case_id {case_id!r} appears more than once in {case_path}")
    _validate_case_for_scoring(matches[0])
    return matches[0]


def score_response(
    case: Mapping[str, Any],
    content: str | None,
    *,
    request_failure: RequestFailure | None = None,
) -> dict[str, Any]:
    """Score one assistant content string against one generated case."""

    _validate_case_for_scoring(case)
    total_fields = case["expected_field_count"]

    if request_failure is not None:
        if request_failure not in REQUEST_FAILURES:
            raise ScoringError(f"unsupported request failure: {request_failure!r}")
        return _empty_score(
            request_success=False,
            total_fields=total_fields,
            primary_outcome=request_failure,
        )

    if content is None or content == "":
        return _empty_score(
            request_success=True,
            total_fields=total_fields,
            primary_outcome="empty_content",
        )

    try:
        parsed = strict_json_loads(content)
    except StrictJSONError as exc:
        score = _empty_score(
            request_success=True,
            total_fields=total_fields,
            primary_outcome="invalid_json",
        )
        score["parse_error"] = str(exc)
        return score

    schema_errors = _schema_errors(case["json_schema"], parsed)
    comparison_errors, correct_fields = _compare_expected_fields(case, parsed)
    values_match = _values_equal(
        case["expected"], parsed, case["comparison"]
    )
    schema_valid = not schema_errors
    exact_correct = schema_valid and values_match

    if not schema_valid:
        primary_outcome = "schema_invalid"
    elif not exact_correct:
        primary_outcome = "field_incorrect"
    else:
        primary_outcome = "exact_correct"

    return {
        "request_success": True,
        "json_parsed": True,
        "parsed_json": parsed,
        "schema_valid": schema_valid,
        "exact_correct": exact_correct,
        "correct_fields": correct_fields,
        "total_fields": total_fields,
        "parse_error": None,
        "schema_errors": schema_errors,
        "comparison_errors": comparison_errors,
        "primary_outcome": primary_outcome,
    }


def semantically_equal(
    left: Any,
    right: Any,
    comparison: Mapping[str, Any],
) -> bool:
    """Compare two parsed JSON values using one case's comparison policy."""

    return _values_equal(left, right, comparison) and _values_equal(
        right, left, comparison
    )


def _empty_score(
    *, request_success: bool, total_fields: int, primary_outcome: str
) -> dict[str, Any]:
    return {
        "request_success": request_success,
        "json_parsed": False,
        "parsed_json": None,
        "schema_valid": False,
        "exact_correct": False,
        "correct_fields": 0,
        "total_fields": total_fields,
        "parse_error": None,
        "schema_errors": [],
        "comparison_errors": [],
        "primary_outcome": primary_outcome,
    }


def _validate_case_for_scoring(case: Mapping[str, Any]) -> None:
    case_validator = Draft202012Validator(load_schema("case"))
    case_errors = sorted(
        case_validator.iter_errors(case),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if case_errors:
        error = case_errors[0]
        raise ScoringError(
            f"invalid case at {_pointer(error.absolute_path)!r}: {error.message}"
        )

    schema = case["json_schema"]
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ScoringError(f"case contains an invalid JSON Schema: {exc}") from exc

    remote_reference = _find_remote_reference(schema)
    if remote_reference is not None:
        keyword, reference = remote_reference
        raise ScoringError(
            f"case schema must be self-contained; {keyword} uses remote "
            f"reference {reference!r}"
        )

    try:
        expected_schema_errors = _schema_errors(schema, case["expected"])
    except Exception as exc:
        raise ScoringError(f"case schema could not be resolved: {exc}") from exc
    if expected_schema_errors:
        raise ScoringError("case expected value does not satisfy its JSON Schema")

    computed_paths = _field_paths(case["expected"])
    configured_paths = case["expected_field_paths"]
    if len(computed_paths) != case["expected_field_count"]:
        raise ScoringError("case expected_field_count does not match expected value")
    if set(computed_paths) != set(configured_paths):
        raise ScoringError("case expected_field_paths do not match expected value")


def _schema_errors(schema: Mapping[str, Any] | bool, value: Any) -> list[dict[str, str]]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            str(error.validator),
            error.message,
        ),
    )
    return [
        {
            "path": _pointer(error.absolute_path),
            "message": _truncate(error.message),
        }
        for error in errors
    ]


def _compare_expected_fields(
    case: Mapping[str, Any], actual: Any
) -> tuple[list[dict[str, str]], int]:
    expected = case["expected"]
    policy = case["comparison"]
    errors: list[dict[str, str]] = []
    correct = 0

    for path in case["expected_field_paths"]:
        expected_value = _resolve_pointer(expected, path)
        actual_value = _resolve_pointer(actual, path)
        if actual_value is _MISSING:
            errors.append(
                {
                    "path": path,
                    "message": "expected field is missing or has an incompatible ancestor",
                }
            )
        elif _values_equal(expected_value, actual_value, policy):
            correct += 1
        else:
            errors.append(
                {
                    "path": path,
                    "message": (
                        f"expected {_describe(expected_value)}, "
                        f"got {_describe(actual_value)}"
                    ),
                }
            )

    errors.extend(_unexpected_structure_errors(expected, actual))
    return errors, correct


def _values_equal(expected: Any, actual: Any, policy: Mapping[str, Any]) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, int):
        return type(actual) is int and actual == expected
    if isinstance(expected, float):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        if isinstance(actual, float) and not math.isfinite(actual):
            return False
        return _numbers_equal(expected, actual, policy)
    if isinstance(expected, str):
        return isinstance(actual, str) and actual == expected
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _values_equal(expected_item, actual_item, policy)
                for expected_item, actual_item in zip(expected, actual, strict=True)
            )
        )
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and set(expected) == set(actual)
            and all(
                _values_equal(expected[key], actual[key], policy)
                for key in expected
            )
        )
    return type(actual) is type(expected) and actual == expected


def _numbers_equal(
    expected: int | float,
    actual: int | float,
    policy: Mapping[str, Any],
) -> bool:
    mode = policy["numeric"]
    if mode == "exact":
        return actual == expected

    expected_decimal = Decimal(str(expected))
    actual_decimal = Decimal(str(actual))
    difference = abs(actual_decimal - expected_decimal)
    if mode == "absolute_tolerance":
        return difference <= Decimal(str(policy["absolute_tolerance"]))
    if mode == "relative_tolerance":
        tolerance = Decimal(str(policy["relative_tolerance"]))
        return difference <= tolerance * abs(expected_decimal)
    raise ScoringError(f"unsupported numeric comparison policy: {mode!r}")


def _unexpected_structure_errors(
    expected: Any, actual: Any, path: str = ""
) -> list[dict[str, str]]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        errors = [
            {
                "path": _join_pointer(path, key),
                "message": "unexpected object field",
            }
            for key in sorted(set(actual) - set(expected))
        ]
        for key in sorted(set(expected) & set(actual)):
            errors.extend(
                _unexpected_structure_errors(
                    expected[key], actual[key], _join_pointer(path, key)
                )
            )
        return errors

    if isinstance(expected, list) and isinstance(actual, list):
        errors = [
            {
                "path": _join_pointer(path, str(index)),
                "message": "unexpected array item",
            }
            for index in range(len(expected), len(actual))
        ]
        for index, (expected_item, actual_item) in enumerate(
            zip(expected, actual)
        ):
            errors.extend(
                _unexpected_structure_errors(
                    expected_item,
                    actual_item,
                    _join_pointer(path, str(index)),
                )
            )
        return errors

    return []


def _resolve_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                return _MISSING
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _field_paths(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key in sorted(value):
            paths.extend(_field_paths(value[key], _join_pointer(path, key)))
        return paths or [path]
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_field_paths(child, _join_pointer(path, str(index))))
        return paths or [path]
    return [path]


def _pointer(parts: Sequence[Any]) -> str:
    pointer = ""
    for part in parts:
        pointer = _join_pointer(pointer, str(part))
    return pointer


def _join_pointer(pointer: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{escaped}"


def _find_remote_reference(value: Any) -> tuple[str, str] | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(child, str):
                if not child.startswith("#"):
                    return key, child
            found = _find_remote_reference(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_remote_reference(child)
            if found is not None:
                return found
    return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _describe(value: Any) -> str:
    if value is _MISSING:
        return "a missing value"
    type_name = (
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "integer"
        if isinstance(value, int)
        else "number"
        if isinstance(value, float)
        else "string"
        if isinstance(value, str)
        else "array"
        if isinstance(value, list)
        else "object"
        if isinstance(value, dict)
        else type(value).__name__
    )
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        serialized = repr(value)
    return f"{type_name} {_truncate(serialized, limit=120)}"


def _truncate(value: str, *, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
