from __future__ import annotations

import json
from pathlib import Path

from structured_output_reliability.cli import main

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPOSITORY_ROOT / "examples" / "experiment.yaml"
CHARACTER_EXAMPLE = REPOSITORY_ROOT / "examples" / "character-experiment.yaml"


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


def test_generate_command(capsys, tmp_path: Path) -> None:
    output = tmp_path / "cases.jsonl"

    result = main(
        ["generate", str(CHARACTER_EXAMPLE), "--output", str(output)]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Generated 4 cases" in captured.out
    assert "Case-set SHA-256:" in captured.out
    assert captured.err == ""
    assert len(output.read_text().splitlines()) == 4


def test_generate_command_json_and_overwrite_protection(
    capsys, tmp_path: Path
) -> None:
    output = tmp_path / "cases.jsonl"
    assert (
        main(
            [
                "generate",
                str(CHARACTER_EXAMPLE),
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    success = json.loads(capsys.readouterr().out)
    assert success["generated"] is True
    assert success["case_count"] == 4
    assert len(success["case_set_sha256"]) == 64

    result = main(
        [
            "generate",
            str(CHARACTER_EXAMPLE),
            "--output",
            str(output),
            "--json",
        ]
    )
    failure = json.loads(capsys.readouterr().out)
    assert result == 2
    assert failure["generated"] is False
    assert "--force" in failure["issues"][0]["message"]
