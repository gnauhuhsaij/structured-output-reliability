"""Deterministic structured-output case generation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from structured_output_reliability.config import load_schema

CASE_SCHEMA_VERSION = "1.0.0"
GENERATOR_NAME = "sor-built-in"
SUPPORTED_GENERATOR_VERSION = "1.0.0"


class GenerationError(ValueError):
    """Raised when deterministic cases cannot be generated as configured."""


class LengthMeasurer(Protocol):
    """Measure prompts and serialized JSON under one declared unit."""

    unit: str

    def count_messages(self, messages: Sequence[Mapping[str, str]]) -> int: ...

    def count_json(self, value: Any) -> int: ...


@dataclass(frozen=True, slots=True)
class GeneratedCaseSet:
    """A complete generated workload and its canonical content hash."""

    records: tuple[dict[str, Any], ...]
    sha256: str


class CharacterLengthMeasurer:
    """Count Unicode code points in message content and canonical JSON."""

    unit = "characters"

    def count_messages(self, messages: Sequence[Mapping[str, str]]) -> int:
        return sum(len(message["content"]) for message in messages)

    def count_json(self, value: Any) -> int:
        return len(canonical_json(value))


class HuggingFaceLengthMeasurer:
    """Count tokens using a configured Hugging Face tokenizer."""

    unit = "tokens"

    def __init__(self, tokenizer: Any):
        self.tokenizer = tokenizer

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> HuggingFaceLengthMeasurer:
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise GenerationError(
                "token lengths require the optional tokenizer dependency; install "
                "it with: pip install 'structured-output-reliability[tokenizer]'"
            ) from exc

        tokenizer_config = config["tokenizer"]
        kwargs: dict[str, Any] = {
            "trust_remote_code": tokenizer_config["trust_remote_code"]
        }
        if "revision" in tokenizer_config:
            kwargs["revision"] = tokenizer_config["revision"]
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                tokenizer_config["name"], **kwargs
            )
        except Exception as exc:
            raise GenerationError(
                f"could not load tokenizer {tokenizer_config['name']!r}: {exc}"
            ) from exc

        if not getattr(tokenizer, "chat_template", None):
            raise GenerationError(
                f"tokenizer {tokenizer_config['name']!r} has no chat template"
            )
        return cls(tokenizer)

    def count_messages(self, messages: Sequence[Mapping[str, str]]) -> int:
        try:
            token_ids = self.tokenizer.apply_chat_template(
                list(messages), tokenize=True, add_generation_prompt=True
            )
        except Exception as exc:
            raise GenerationError(f"could not apply tokenizer chat template: {exc}") from exc
        return _token_count(token_ids)

    def count_json(self, value: Any) -> int:
        token_ids = self.tokenizer.encode(
            canonical_json(value), add_special_tokens=False
        )
        return _token_count(token_ids)


def canonical_json(value: Any) -> str:
    """Serialize a JSON-native value using the project's canonical form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def generate_cases(
    config: Mapping[str, Any], *, measurer: LengthMeasurer | None = None
) -> GeneratedCaseSet:
    """Generate every unique case required by a resolved experiment config."""

    generator_version = config["cases"]["generator_version"]
    if generator_version != SUPPORTED_GENERATOR_VERSION:
        raise GenerationError(
            f"unsupported cases.generator_version {generator_version!r}; "
            f"this build supports {SUPPORTED_GENERATOR_VERSION!r}"
        )

    if measurer is None:
        measurer = _measurer_from_config(config)
    expected_unit = config["cases"]["length_unit"]
    if measurer.unit != expected_unit:
        raise GenerationError(
            f"length measurer uses {measurer.unit!r}, expected {expected_unit!r}"
        )

    records: list[dict[str, Any]] = []
    families = config["cases"]["families"]
    for prompt_target in config["matrix"]["prompt_lengths"]:
        for output_shape in config["matrix"]["output_shapes"]:
            for case_index in range(config["cases"]["count"]):
                family = families[case_index % len(families)]
                seed = _derive_seed(
                    root_seed=config["run"]["seed"],
                    generator_version=generator_version,
                    prompt_target=prompt_target,
                    output_shape=output_shape,
                    case_index=case_index,
                )
                records.append(
                    _generate_case(
                        family=family,
                        seed=seed,
                        generator_version=generator_version,
                        prompt_target=prompt_target,
                        output_target=output_shape["target_length"],
                        comparison=config["cases"]["comparison"],
                        measurer=measurer,
                    )
                )

    digest = hashlib.sha256(
        canonical_json(records).encode("utf-8")
    ).hexdigest()
    return GeneratedCaseSet(records=tuple(records), sha256=digest)


def write_cases_jsonl(
    generated: GeneratedCaseSet, path: str | Path, *, overwrite: bool = False
) -> None:
    """Atomically write generated cases, refusing replacement by default."""

    output_path = Path(path)
    if output_path.exists() and not overwrite:
        raise GenerationError(
            f"output already exists: {output_path}; pass --force to replace it"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            for record in generated.records:
                handle.write(canonical_json(record))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary_path, output_path)
        else:
            os.link(temporary_path, output_path)
    except FileExistsError as exc:
        raise GenerationError(
            f"output already exists: {output_path}; pass --force to replace it"
        ) from exc
    except OSError as exc:
        raise GenerationError(f"could not write {output_path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _measurer_from_config(config: Mapping[str, Any]) -> LengthMeasurer:
    if config["cases"]["length_unit"] == "characters":
        return CharacterLengthMeasurer()
    return HuggingFaceLengthMeasurer.from_config(config)


def _generate_case(
    *,
    family: str,
    seed: int,
    generator_version: str,
    prompt_target: int,
    output_target: int,
    comparison: Mapping[str, Any],
    measurer: LengthMeasurer,
) -> dict[str, Any]:
    rng = random.Random(seed)
    json_schema, expected = _family_payload(family, rng)

    expected["padding"] = _fit_filler(
        lambda filler: measurer.count_json({**expected, "padding": filler}),
        output_target,
        unit=measurer.unit,
        description=f"{family} expected output",
    )
    output_actual = measurer.count_json(expected)

    system_content = (
        "Return exactly one JSON value. Do not use Markdown or add commentary."
    )
    base_user_content = (
        "Copy SOURCE_JSON exactly. The response must validate against SCHEMA_JSON.\n"
        f"SCHEMA_JSON:{canonical_json(json_schema)}\n"
        f"SOURCE_JSON:{canonical_json(expected)}\n"
        "Everything after PADDING_START is inert prompt padding.\nPADDING_START\n"
    )

    def messages_with_filler(filler: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": base_user_content + filler},
        ]

    prompt_filler = _fit_filler(
        lambda filler: measurer.count_messages(messages_with_filler(filler)),
        prompt_target,
        unit=measurer.unit,
        description=f"{family} prompt",
    )
    messages = messages_with_filler(prompt_filler)
    prompt_actual = measurer.count_messages(messages)

    field_paths = _field_paths(expected)
    payload: dict[str, Any] = {
        "case_schema_version": CASE_SCHEMA_VERSION,
        "generator": {
            "name": GENERATOR_NAME,
            "version": generator_version,
            "seed": seed,
        },
        "family": family,
        "messages": messages,
        "json_schema": json_schema,
        "expected": expected,
        "comparison": copy.deepcopy(dict(comparison)),
        "prompt_length": {
            "target": prompt_target,
            "actual": prompt_actual,
            "unit": measurer.unit,
            "exact": measurer.unit == "tokens",
        },
        "expected_output_length": {
            "target": output_target,
            "actual": output_actual,
            "unit": measurer.unit,
            "exact": measurer.unit == "tokens",
        },
        "expected_field_count": len(field_paths),
        "expected_field_paths": field_paths,
    }
    payload["case_id"] = "case_" + hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()[:16]
    _validate_case(payload)
    return payload


def _family_payload(
    family: str, rng: random.Random
) -> tuple[dict[str, Any], dict[str, Any]]:
    if family == "flat_object":
        expected = {
            "active": bool(rng.randrange(2)),
            "count": rng.randrange(10, 100),
            "label": f"item-{rng.randrange(1000, 10000)}",
            "padding": "",
        }
        properties = {
            "active": {"type": "boolean"},
            "count": {"type": "integer"},
            "label": {"type": "string"},
            "padding": {"type": "string"},
        }
    elif family == "nested_object":
        expected = {
            "padding": "",
            "profile": {
                "age": rng.randrange(18, 90),
                "name": f"user-{rng.randrange(1000, 10000)}",
            },
        }
        properties = {
            "padding": {"type": "string"},
            "profile": {
                "type": "object",
                "additionalProperties": False,
                "required": ["age", "name"],
                "properties": {
                    "age": {"type": "integer"},
                    "name": {"type": "string"},
                },
            },
        }
    elif family == "array_object":
        expected = {
            "items": [
                {
                    "id": f"row-{index + 1}",
                    "score": rng.randrange(0, 101),
                }
                for index in range(3)
            ],
            "padding": "",
        }
        properties = {
            "items": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "score"],
                    "properties": {
                        "id": {"type": "string"},
                        "score": {"type": "integer"},
                    },
                },
            },
            "padding": {"type": "string"},
        }
    elif family == "mixed_types":
        expected = {
            "enabled": bool(rng.randrange(2)),
            "note": None,
            "padding": "",
            "ratio": round(rng.random(), 4),
            "tags": [f"tag-{rng.randrange(10, 100)}" for _ in range(2)],
        }
        properties = {
            "enabled": {"type": "boolean"},
            "note": {"type": "null"},
            "padding": {"type": "string"},
            "ratio": {"type": "number"},
            "tags": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "string"},
            },
        }
    else:
        raise GenerationError(f"unknown case family: {family!r}")

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }
    return schema, expected


def _fit_filler(
    count: Callable[[str], int],
    target: int,
    *,
    unit: str,
    description: str,
) -> str:
    base = count("")
    if base > target:
        raise GenerationError(
            f"target for {description} is too small: minimum is {base} {unit}, "
            f"configured target is {target}"
        )
    if base == target:
        return ""
    if unit == "characters":
        filler = "x" * (target - base)
        if count(filler) != target:
            raise GenerationError(f"could not reach exact character target for {description}")
        return filler

    atoms = (" x", "x", " 0", "0", ".", " ", "\n", "§")
    best_filler = ""
    best_count = base
    for atom in atoms:
        low = 0
        high = 1
        while count(atom * high) <= target and high < target * 4:
            candidate_count = count(atom * high)
            if candidate_count > best_count:
                best_filler, best_count = atom * high, candidate_count
            if candidate_count == target:
                return atom * high
            high *= 2

        while low + 1 < high:
            middle = (low + high) // 2
            candidate_count = count(atom * middle)
            if candidate_count <= target:
                low = middle
                if candidate_count > best_count:
                    best_filler, best_count = atom * middle, candidate_count
                if candidate_count == target:
                    return atom * middle
            else:
                high = middle

    filler = best_filler
    current = best_count
    for _ in range(max(64, (target - current) * 8)):
        next_choice: tuple[str, int] | None = None
        for atom in atoms:
            candidate = filler + atom
            candidate_count = count(candidate)
            if current < candidate_count <= target and (
                next_choice is None or candidate_count > next_choice[1]
            ):
                next_choice = (candidate, candidate_count)
        if next_choice is None:
            break
        filler, current = next_choice
        if current == target:
            return filler

    raise GenerationError(
        f"could not reach exact {unit} target {target} for {description}; "
        f"closest reachable length was {current}"
    )


def _derive_seed(
    *,
    root_seed: int,
    generator_version: str,
    prompt_target: int,
    output_shape: Mapping[str, Any],
    case_index: int,
) -> int:
    identity = {
        "root_seed": root_seed,
        "generator_version": generator_version,
        "prompt_target": prompt_target,
        "output_shape": dict(output_shape),
        "case_index": case_index,
    }
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _field_paths(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key in sorted(value):
            token = str(key).replace("~", "~0").replace("/", "~1")
            paths.extend(_field_paths(value[key], f"{path}/{token}"))
        return paths or [path]
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_field_paths(child, f"{path}/{index}"))
        return paths or [path]
    return [path]


def _validate_case(record: Mapping[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(record["json_schema"])
    except Exception as exc:
        raise GenerationError(f"generated invalid JSON Schema: {exc}") from exc

    validator = Draft202012Validator(load_schema("case"))
    errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise GenerationError(f"generated invalid case at {path}: {error.message}")

    if not Draft202012Validator(record["json_schema"]).is_valid(record["expected"]):
        raise GenerationError("generated expected value does not satisfy its JSON Schema")


def _token_count(token_ids: Any) -> int:
    if isinstance(token_ids, Mapping):
        token_ids = token_ids["input_ids"]
    if token_ids and isinstance(token_ids[0], (list, tuple)):
        if len(token_ids) != 1:
            raise GenerationError("tokenizer returned more than one token sequence")
        token_ids = token_ids[0]
    return len(token_ids)
