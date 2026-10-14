from __future__ import annotations

import json
from pathlib import Path

import yaml

from structured_output_reliability.cli import main
from structured_output_reliability.config import load_experiment
from structured_output_reliability.generator import (
    canonical_json,
    generate_cases,
    write_cases_jsonl,
)

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
    assert "Case generation not checked" in captured.out
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


def test_validate_check_cases_generates_in_memory_without_file(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    result = main(["validate", str(CHARACTER_EXAMPLE), "--check-cases"])

    captured = capsys.readouterr()
    assert result == 0
    assert "Case generation passed: 4 cases in memory" in captured.out
    assert "No case file written." in captured.out
    assert captured.err == ""
    assert list(tmp_path.iterdir()) == []

    result = main(["validate", str(CHARACTER_EXAMPLE), "--check-cases", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["valid"] is True
    assert output["case_generation_checked"] is True
    assert output["generated_cases"] == 4
    assert len(output["case_set_sha256"]) == 64
    assert list(tmp_path.iterdir()) == []


def test_validate_check_cases_reports_generation_error(
    capsys, tmp_path: Path
) -> None:
    document = yaml.safe_load(CHARACTER_EXAMPLE.read_text(encoding="utf-8"))
    document["matrix"]["output_shapes"][0]["target_length"] = 1
    config_path = tmp_path / "too-short.yaml"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    result = main(["validate", str(config_path), "--check-cases", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["valid"] is False
    assert output["case_generation_checked"] is True
    assert "minimum is" in output["issues"][0]["message"]
    assert list(tmp_path.iterdir()) == [config_path]


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


def test_score_command(capsys, tmp_path: Path) -> None:
    generated = generate_cases(load_experiment(CHARACTER_EXAMPLE))
    case = generated.records[0]
    cases_path = tmp_path / "cases.jsonl"
    response_path = tmp_path / "response.txt"
    write_cases_jsonl(generated, cases_path)
    response_path.write_text(canonical_json(case["expected"]), encoding="utf-8")

    result = main(
        [
            "score",
            str(cases_path),
            "--case-id",
            case["case_id"],
            "--response",
            str(response_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Primary outcome: exact_correct" in captured.out
    assert "Exact correct: True" in captured.out
    assert "Correct fields: 4/4" in captured.out
    assert captured.err == ""


def test_score_command_json_for_invalid_content(capsys, tmp_path: Path) -> None:
    generated = generate_cases(load_experiment(CHARACTER_EXAMPLE))
    case = generated.records[0]
    cases_path = tmp_path / "cases.jsonl"
    response_path = tmp_path / "response.txt"
    write_cases_jsonl(generated, cases_path)
    response_path.write_text("```json\n{}\n```", encoding="utf-8")

    result = main(
        [
            "score",
            str(cases_path),
            "--case-id",
            case["case_id"],
            "--response",
            str(response_path),
            "--json",
        ]
    )

    score = json.loads(capsys.readouterr().out)
    assert result == 0
    assert score["json_parsed"] is False
    assert score["primary_outcome"] == "invalid_json"
    assert score["total_fields"] == 4


def test_score_command_request_failure(capsys, tmp_path: Path) -> None:
    generated = generate_cases(load_experiment(CHARACTER_EXAMPLE))
    case = generated.records[0]
    cases_path = tmp_path / "cases.jsonl"
    write_cases_jsonl(generated, cases_path)

    result = main(
        [
            "score",
            str(cases_path),
            "--case-id",
            case["case_id"],
            "--request-failure",
            "timeout",
            "--json",
        ]
    )

    score = json.loads(capsys.readouterr().out)
    assert result == 0
    assert score["request_success"] is False
    assert score["primary_outcome"] == "timeout"


def test_score_command_missing_case_is_setup_error(capsys, tmp_path: Path) -> None:
    generated = generate_cases(load_experiment(CHARACTER_EXAMPLE))
    cases_path = tmp_path / "cases.jsonl"
    response_path = tmp_path / "response.txt"
    write_cases_jsonl(generated, cases_path)
    response_path.write_text("{}", encoding="utf-8")

    result = main(
        [
            "score",
            str(cases_path),
            "--case-id",
            "case_0000000000000000",
            "--response",
            str(response_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "was not found" in captured.err
