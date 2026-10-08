from __future__ import annotations

import json
from pathlib import Path

from structured_output_reliability.cli import main

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPOSITORY_ROOT / "examples" / "experiment.yaml"


def test_validate_command(capsys) -> None:
    result = main(["validate", str(EXAMPLE)])

    captured = capsys.readouterr()
    assert result == 0
    assert "16 conditions" in captured.out
    assert "40 generated cases" in captured.out
    assert "960 measured attempts" in captured.out
    assert "32 warmup attempts" in captured.out
    assert captured.err == ""


def test_validate_command_json(capsys) -> None:
    result = main(["validate", str(EXAMPLE), "--json"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert result == 0
    assert output == {
        "conditions": 16,
        "generated_cases": 40,
        "measured_attempts": 960,
        "path": str(EXAMPLE),
        "valid": True,
        "warmup_attempts": 32,
    }


def test_validate_missing_file(capsys, tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    result = main(["validate", str(missing)])

    captured = capsys.readouterr()
    assert result == 2
    assert "Configuration invalid" in captured.err
    assert "cannot read" in captured.err

