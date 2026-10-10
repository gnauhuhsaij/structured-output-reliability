from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from structured_output_reliability.cli import main
from structured_output_reliability.config import load_experiment, load_schema
from structured_output_reliability.generator import canonical_json, generate_cases
from structured_output_reliability.runner import (
    RunnerError,
    build_conditions,
    build_request_body,
    run_experiment,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHARACTER_EXAMPLE = REPOSITORY_ROOT / "examples" / "character-experiment.yaml"


class _MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.bodies: list[dict[str, Any]] = []
        self.authorization_headers: list[str | None] = []


class _DaemonThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


@contextmanager
def mock_openai_server() -> Iterator[tuple[str, _MockState]]:
    state = _MockState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            with state.lock:
                state.active += 1
                state.max_active = max(state.max_active, state.active)
                state.bodies.append(body)
                state.authorization_headers.append(self.headers.get("Authorization"))
            try:
                self._respond(body)
            finally:
                with state.lock:
                    state.active -= 1

        def _respond(self, body: dict[str, Any]) -> None:
            scenario = body["model"]
            if scenario == "timeout":
                time.sleep(0.1)
                self._write_json(200, _chat_response(body))
                return
            if scenario == "http-error":
                self._write_json(503, {"error": {"message": "planned failure"}})
                return
            if scenario == "protocol-error":
                self._write_json(200, {"choices": []})
                return
            if scenario == "invalid-json":
                self._write_json(200, _chat_response(body, content="{"))
                return
            if scenario == "schema-invalid":
                actual = _expected_from_request(body)
                actual["count"] = "wrong type"
                self._write_json(
                    200, _chat_response(body, content=canonical_json(actual))
                )
                return
            if scenario == "field-incorrect":
                actual = _expected_from_request(body)
                actual["count"] += 1
                self._write_json(
                    200, _chat_response(body, content=canonical_json(actual))
                )
                return
            if scenario == "stream":
                self._write_stream(body)
                return
            time.sleep(0.03)
            self._write_json(200, _chat_response(body))

        def _write_json(self, status: int, payload: Any) -> None:
            encoded = canonical_json(payload).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _write_stream(self, body: dict[str, Any]) -> None:
            expected = canonical_json(_expected_from_request(body))
            midpoint = len(expected) // 2
            events = [
                {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": expected[:midpoint]}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": expected[midpoint:]}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            ]
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for index, event in enumerate(events):
                    if index == 1:
                        time.sleep(0.01)
                    self.wfile.write(f"data: {canonical_json(event)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = _DaemonThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _chat_response(body: dict[str, Any], *, content: str | None = None) -> dict[str, Any]:
    if content is None:
        content = canonical_json(_expected_from_request(body))
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
        },
    }


def _expected_from_request(body: dict[str, Any]) -> Any:
    user_content = body["messages"][-1]["content"]
    source = user_content.split("SOURCE_JSON:", 1)[1]
    source = source.split("\nEverything after PADDING_START", 1)[0]
    return json.loads(source)


def _write_config(
    tmp_path: Path,
    *,
    base_url: str,
    model: str = "exact",
    stream: bool = False,
    count: int = 1,
    concurrency: int = 1,
    warmups: int = 0,
    api_key_env: str | None = None,
    timeout_seconds: float = 1,
    response_format: bool = False,
) -> Path:
    config = load_experiment(CHARACTER_EXAMPLE)
    config["name"] = f"mock-{model}"
    config["endpoint"].update(
        {
            "base_url": base_url,
            "model": model,
            "api_key_env": api_key_env,
            "timeout_seconds": timeout_seconds,
        }
    )
    config["cases"]["count"] = count
    config["run"].update(
        {
            "repetitions": 1,
            "warmup_requests_per_condition": warmups,
            "stream": stream,
        }
    )
    config["matrix"]["concurrency"] = [concurrency]
    if response_format:
        config["structured_modes"] = [
            {"id": "json_schema", "type": "response_format", "strict": True}
        ]
        config["matrix"]["structured_modes"] = ["json_schema"]
    config["output"]["root"] = str(tmp_path / "runs")
    config_path = tmp_path / f"{model}.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_command_executes_all_cases_with_fixed_concurrency(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with mock_openai_server() as (base_url, state):
        config_path = _write_config(
            tmp_path,
            base_url=base_url,
            count=4,
            concurrency=2,
            warmups=1,
            response_format=True,
        )
        exit_code = main(["run", str(config_path), "--json"])

    output = json.loads(capsys.readouterr().out)
    run_directory = Path(output["run_directory"])
    assert exit_code == 0
    assert output["completed"] is True
    assert output["condition_count"] == 1
    assert output["measured_attempts"] == 4
    assert output["warmup_attempts"] == 1
    assert output["request_failures"] == 0
    assert state.max_active == 2
    assert len(state.bodies) == 5
    assert all("response_format" in body for body in state.bodies)
    assert all(header is None for header in state.authorization_headers)

    manifest = json.loads((run_directory / "manifest.json").read_text())
    conditions = _jsonl(run_directory / "conditions.jsonl")
    attempts = _jsonl(run_directory / "responses.jsonl")
    assert manifest["run_complete"] is True
    assert len(_jsonl(run_directory / "cases.jsonl")) == 4
    assert len(conditions) == 1
    assert len(attempts) == 5
    assert len({attempt["attempt_id"] for attempt in attempts}) == 5
    assert len({attempt["sequence_index"] for attempt in attempts}) == 5
    assert all(attempt["score"]["exact_correct"] for attempt in attempts)
    assert all(attempt["timing"]["ttft_ns"] is None for attempt in attempts)

    condition_validator = Draft202012Validator(
        load_schema("condition"), format_checker=FormatChecker()
    )
    attempt_validator = Draft202012Validator(
        load_schema("attempt"), format_checker=FormatChecker()
    )
    condition_validator.validate(conditions[0])
    for attempt in attempts:
        attempt_validator.validate(attempt)


def test_streaming_run_records_ttft_tpot_and_events(tmp_path: Path) -> None:
    with mock_openai_server() as (base_url, _):
        config_path = _write_config(
            tmp_path,
            base_url=base_url,
            model="stream",
            stream=True,
        )
        result = run_experiment(config_path)

    attempt = _jsonl(result.run_directory / "responses.jsonl")[0]
    assert attempt["score"]["exact_correct"] is True
    assert attempt["response"]["stream_terminated_normally"] is True
    assert attempt["response"]["raw_chunk_count"] == 5
    assert attempt["response"]["stream_events"][-1]["data"] == "[DONE]"
    assert attempt["timing"]["ttft_ns"] > 0
    assert attempt["timing"]["tpot_ns"] >= 0
    assert attempt["tokens"] == {
        "input": 10,
        "output": 2,
        "total": 12,
        "token_count_source": "server",
        "tokenizer": None,
    }


def test_extra_body_mode_inserts_a_fresh_case_schema() -> None:
    config = load_experiment(CHARACTER_EXAMPLE)
    config["structured_modes"] = [
        {
            "id": "backend_schema",
            "type": "extra_body",
            "body": {"structured_outputs": {"json": None}},
            "schema_json_pointer": "/structured_outputs/json",
        }
    ]
    config["matrix"]["structured_modes"] = ["backend_schema"]
    generated = generate_cases(config)
    condition = build_conditions(config, generated)[0]

    body = build_request_body(condition, generated.records[0])

    assert body["structured_outputs"]["json"] == generated.records[0]["json_schema"]
    assert condition["structured_mode"]["body"]["structured_outputs"]["json"] is None


@pytest.mark.parametrize(
    ("model", "expected_outcome", "request_success"),
    [
        ("invalid-json", "invalid_json", True),
        ("schema-invalid", "schema_invalid", True),
        ("field-incorrect", "field_incorrect", True),
        ("protocol-error", "protocol_error", False),
        ("http-error", "http_error", False),
        ("timeout", "timeout", False),
    ],
)
def test_mock_failure_scenarios_are_classified_deterministically(
    tmp_path: Path,
    model: str,
    expected_outcome: str,
    request_success: bool,
) -> None:
    with mock_openai_server() as (base_url, _):
        config_path = _write_config(
            tmp_path,
            base_url=base_url,
            model=model,
            timeout_seconds=0.02 if model == "timeout" else 1,
        )
        result = run_experiment(config_path)

    attempt = _jsonl(result.run_directory / "responses.jsonl")[0]
    assert attempt["score"]["primary_outcome"] == expected_outcome
    assert attempt["score"]["request_success"] is request_success
    assert result.request_failures == (0 if request_success else 1)


def test_named_api_key_is_sent_but_never_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "test-secret-never-save"
    monkeypatch.setenv("SOR_TEST_API_KEY", secret)
    with mock_openai_server() as (base_url, state):
        config_path = _write_config(
            tmp_path,
            base_url=base_url,
            api_key_env="SOR_TEST_API_KEY",
        )
        result = run_experiment(config_path)

    assert state.authorization_headers == [f"Bearer {secret}"]
    for path in result.run_directory.iterdir():
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8")


def test_missing_named_api_key_fails_before_creating_run_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SOR_MISSING_API_KEY", raising=False)
    config_path = _write_config(
        tmp_path,
        base_url="http://127.0.0.1:1/v1",
        api_key_env="SOR_MISSING_API_KEY",
    )

    with pytest.raises(RunnerError, match="SOR_MISSING_API_KEY"):
        run_experiment(config_path)

    assert not (tmp_path / "runs").exists()
