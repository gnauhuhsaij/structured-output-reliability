from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_DIRECTORY = Path(__file__).resolve().parents[1] / "schemas"
SCHEMA_FILES = sorted(SCHEMA_DIRECTORY.glob("*.schema.json"))


@pytest.mark.parametrize("schema_path", SCHEMA_FILES, ids=lambda path: path.name)
def test_schema_is_valid_draft_2020_12(schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)

