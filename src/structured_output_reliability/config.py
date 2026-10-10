"""Experiment loading, validation, defaulting, and matrix expansion."""

from __future__ import annotations

import copy
import itertools
import json
import math
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

CORE_REQUEST_FIELDS = {
    "max_completion_tokens",
    "messages",
    "model",
    "response_format",
    "seed",
    "stream",
    "temperature",
}
RELATIVE_GATE_KINDS = {"max_drop", "max_increase_ratio"}
SENSITIVE_EXTRA_BODY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "bearer_token",
    "client_secret",
    "password",
    "secret",
}
SENSITIVE_VALUE_KEYS = SENSITIVE_EXTRA_BODY_KEYS | {"api_key_env"}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One actionable configuration problem."""

    path: str
    message: str


class ConfigurationError(ValueError):
    """Raised when an experiment cannot be loaded or validated."""

    def __init__(self, issues: Sequence[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues))


@dataclass(frozen=True, slots=True)
class ExperimentSummary:
    """Static work counts derived from a resolved experiment."""

    conditions: int
    generated_cases: int
    measured_attempts: int
    warmup_attempts: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )

    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    """Load a canonical JSON Schema from a wheel or source checkout."""

    if name not in {"attempt", "case", "condition", "experiment", "summary"}:
        raise ValueError(f"unknown schema: {name}")

    packaged = resources.files("structured_output_reliability").joinpath(
        "schemas", f"{name}.schema.json"
    )
    try:
        text = packaged.read_text(encoding="utf-8")
    except FileNotFoundError:
        source_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / f"{name}.schema.json"
        )
        text = source_path.read_text(encoding="utf-8")

    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return schema


def load_experiment_schema() -> dict[str, Any]:
    """Load the canonical experiment schema."""

    return load_schema("experiment")


def load_experiment(path: str | Path) -> dict[str, Any]:
    """Read, default, and validate one YAML experiment file."""

    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            [ValidationIssue("$", f"cannot read {config_path}: {exc.strerror or exc}")]
        ) from exc

    try:
        document = yaml.load(raw_text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ConfigurationError([ValidationIssue("$", f"invalid YAML: {problem}")]) from exc

    if not isinstance(document, dict):
        raise ConfigurationError(
            [ValidationIssue("$", "the YAML document must be a mapping")]
        )
    return validate_experiment(document)


def validate_experiment(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a resolved copy of an experiment or raise ConfigurationError."""

    resolved = copy.deepcopy(dict(document))
    schema = load_experiment_schema()
    _apply_defaults(resolved, schema, schema)

    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    secrets = tuple(_collect_sensitive_values(resolved))
    schema_issues = [
        ValidationIssue(_format_path(error.absolute_path), _redact(error.message, secrets))
        for error in sorted(
            validator.iter_errors(resolved),
            key=lambda error: (_format_path(error.absolute_path), error.message),
        )
    ]
    if schema_issues:
        raise ConfigurationError(schema_issues)

    semantic_issues = _semantic_issues(resolved)
    if semantic_issues:
        raise ConfigurationError(semantic_issues)
    return resolved


def expand_matrix(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expand a validated matrix in the normative deterministic order."""

    modes_by_id = {
        mode["id"]: _resolve_structured_mode(mode)
        for mode in config["structured_modes"]
    }
    matrix = config["matrix"]
    unit = config["cases"]["length_unit"]
    points: list[dict[str, Any]] = []

    for concurrency, prompt_length, output_shape, temperature, seed, mode_id in (
        itertools.product(
            matrix["concurrency"],
            matrix["prompt_lengths"],
            matrix["output_shapes"],
            matrix["temperatures"],
            matrix["request_seeds"],
            matrix["structured_modes"],
        )
    ):
        points.append(
            {
                "concurrency": concurrency,
                "prompt_length": {
                    "target": prompt_length,
                    "unit": unit,
                    "exact": unit == "tokens",
                },
                "output_shape": {
                    "target_length": output_shape["target_length"],
                    "target_unit": unit,
                    "max_completion_tokens": output_shape["max_completion_tokens"],
                },
                "temperature": temperature,
                "request_seed": seed,
                "structured_mode": copy.deepcopy(modes_by_id[mode_id]),
            }
        )
    return points


def summarize_experiment(config: Mapping[str, Any]) -> ExperimentSummary:
    """Calculate planned static work without generating cases."""

    matrix = config["matrix"]
    condition_count = math.prod(
        len(matrix[axis])
        for axis in (
            "concurrency",
            "prompt_lengths",
            "output_shapes",
            "temperatures",
            "request_seeds",
            "structured_modes",
        )
    )
    per_condition_cases = config["cases"]["count"]
    generated_cases = (
        per_condition_cases
        * len(matrix["prompt_lengths"])
        * len(matrix["output_shapes"])
    )
    measured_attempts = (
        condition_count * per_condition_cases * config["run"]["repetitions"]
    )
    warmup_attempts = (
        condition_count * config["run"]["warmup_requests_per_condition"]
    )
    return ExperimentSummary(
        conditions=condition_count,
        generated_cases=generated_cases,
        measured_attempts=measured_attempts,
        warmup_attempts=warmup_attempts,
    )


def _semantic_issues(config: Mapping[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    modes = config["structured_modes"]
    ids = [mode["id"] for mode in modes]
    duplicate_ids = sorted(mode_id for mode_id, count in Counter(ids).items() if count > 1)
    for mode_id in duplicate_ids:
        issues.append(
            ValidationIssue(
                "$.structured_modes",
                f"structured mode id {mode_id!r} is defined more than once",
            )
        )

    defined_ids = set(ids)
    for index, mode_id in enumerate(config["matrix"]["structured_modes"]):
        if mode_id not in defined_ids:
            issues.append(
                ValidationIssue(
                    f"$.matrix.structured_modes[{index}]",
                    f"references undefined structured mode {mode_id!r}",
                )
            )

    if config["cases"]["length_unit"] == "tokens" and "tokenizer" not in config:
        issues.append(
            ValidationIssue(
                "$.tokenizer",
                "is required when cases.length_unit is 'tokens'",
            )
        )

    slos = config.get("slos", {})
    if not config["run"]["stream"]:
        for name in ("max_ttft_ms", "max_tpot_ms"):
            if name in slos:
                issues.append(
                    ValidationIssue(
                        f"$.slos.{name}",
                        "requires run.stream to be true",
                    )
                )

    relative_gate_indexes = [
        index
        for index, gate in enumerate(config["gates"])
        if gate["kind"] in RELATIVE_GATE_KINDS
    ]
    if relative_gate_indexes and "baseline_run" not in config:
        for index in relative_gate_indexes:
            issues.append(
                ValidationIssue(
                    f"$.gates[{index}].kind",
                    "requires baseline_run",
                )
            )

    for index, mode in enumerate(modes):
        if mode["type"] != "extra_body":
            continue
        body = mode["body"]
        collisions = sorted(CORE_REQUEST_FIELDS.intersection(body))
        if collisions:
            issues.append(
                ValidationIssue(
                    f"$.structured_modes[{index}].body",
                    "may not overwrite core request fields: " + ", ".join(collisions),
                )
            )
        issues.extend(_extra_body_secret_issues(body, index))
        pointer_issue = _validate_schema_pointer(body, mode["schema_json_pointer"])
        if pointer_issue:
            issues.append(
                ValidationIssue(
                    f"$.structured_modes[{index}].schema_json_pointer",
                    pointer_issue,
                )
            )

    return issues


def _extra_body_secret_issues(
    value: Any, mode_index: int, path: str = "body"
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            normalized = str(key).lower().replace("-", "_")
            if normalized in SENSITIVE_EXTRA_BODY_KEYS and child not in (None, ""):
                issues.append(
                    ValidationIssue(
                        f"$.structured_modes[{mode_index}].{child_path}",
                        "must not contain a credential value",
                    )
                )
            issues.extend(_extra_body_secret_issues(child, mode_index, child_path))
    elif isinstance(value, list):
        for child_index, child in enumerate(value):
            issues.extend(
                _extra_body_secret_issues(
                    child, mode_index, f"{path}[{child_index}]"
                )
            )
    return issues


def _validate_schema_pointer(body: Mapping[str, Any], pointer: str) -> str | None:
    tokens = _decode_json_pointer(pointer)
    current: Any = body
    for token in tokens[:-1]:
        if not isinstance(current, dict) or token not in current:
            return f"parent path segment {token!r} does not exist in body"
        current = current[token]
        if not isinstance(current, dict):
            return f"parent path segment {token!r} is not an object"

    final = tokens[-1]
    if final in current and current[final] not in (None, {}):
        return "target must be absent, null, or an empty object placeholder"
    return None


def _decode_json_pointer(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _resolve_structured_mode(mode: Mapping[str, Any]) -> dict[str, Any]:
    resolved = copy.deepcopy(dict(mode))
    resolved["portable"] = mode["type"] != "extra_body"
    return resolved


def _apply_defaults(instance: Any, schema: Mapping[str, Any], root: Mapping[str, Any]) -> None:
    schema = _dereference(schema, root)

    for branch in schema.get("oneOf", []):
        if _branch_discriminator_matches(instance, branch, root):
            _apply_defaults(instance, branch, root)
            break

    if isinstance(instance, dict):
        for key, property_schema in schema.get("properties", {}).items():
            resolved_property = _dereference(property_schema, root)
            if key not in instance and "default" in resolved_property:
                instance[key] = copy.deepcopy(resolved_property["default"])
            if key in instance:
                _apply_defaults(instance[key], property_schema, root)
    elif isinstance(instance, list) and "items" in schema:
        for item in instance:
            _apply_defaults(item, schema["items"], root)


def _branch_discriminator_matches(
    instance: Any, branch: Mapping[str, Any], root: Mapping[str, Any]
) -> bool:
    if not isinstance(instance, dict):
        return False
    branch = _dereference(branch, root)
    constants = {
        key: value_schema["const"]
        for key, value_schema in branch.get("properties", {}).items()
        if isinstance(value_schema, dict) and "const" in value_schema
    }
    return bool(constants) and all(instance.get(key) == value for key, value in constants.items())


def _dereference(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    if "$ref" not in schema:
        return schema
    ref = schema["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    target: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        target = target[part]
    if len(schema) == 1:
        return target
    merged = dict(target)
    merged.update({key: value for key, value in schema.items() if key != "$ref"})
    return merged


def _format_path(parts: Sequence[Any]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _collect_sensitive_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in SENSITIVE_VALUE_KEYS and isinstance(child, str) and child:
                yield child
            yield from _collect_sensitive_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _collect_sensitive_values(child)


def _redact(message: str, secrets: Sequence[str]) -> str:
    redacted = message
    for secret in secrets:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
