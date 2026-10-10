"""Execute generated cases against an OpenAI-compatible endpoint."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from structured_output_reliability import __version__
from structured_output_reliability.config import expand_matrix, load_experiment, load_schema
from structured_output_reliability.generator import (
    GeneratedCaseSet,
    GenerationError,
    HuggingFaceLengthMeasurer,
    canonical_json,
    generate_cases,
    write_cases_jsonl,
)
from structured_output_reliability.metrics import (
    AggregationError,
    build_summary,
    summarize_run,
)
from structured_output_reliability.report import ReportingError, generate_report
from structured_output_reliability.scorer import score_response

CONFIGURATION_CONTRACT_VERSION = "1.0.0"
METRIC_CONTRACT_VERSION = "1.0.0"
CONDITION_SCHEMA_VERSION = "1.0.0"
ATTEMPT_SCHEMA_VERSION = "1.0.0"


class RunnerError(RuntimeError):
    """Raised when an experiment cannot be prepared or preserved safely."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """The location and terminal counts for one completed endpoint run."""

    run_id: str
    run_directory: Path
    condition_count: int
    measured_attempts: int
    warmup_attempts: int
    request_failures: int
    report_path: Path
    gates_configured: int
    gates_passed: bool


@dataclass(frozen=True, slots=True)
class _ProtocolResult:
    response: dict[str, Any]
    tokens: dict[str, Any]
    ttft_ns: int | None
    terminal_ns: int


class _ProtocolError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        response: dict[str, Any] | None = None,
        stage: str = "protocol",
    ):
        super().__init__(message)
        self.response = response
        self.stage = stage


class _JsonlWriter:
    def __init__(self, path: Path, *, flush_every: int):
        self._handle = path.open("x", encoding="utf-8", newline="\n")
        self._flush_every = flush_every
        self._pending = 0

    def write(self, record: Mapping[str, Any]) -> None:
        self._handle.write(canonical_json(record))
        self._handle.write("\n")
        self._pending += 1
        if self._pending >= self._flush_every:
            self._handle.flush()
            self._pending = 0

    def close(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()

    def __enter__(self) -> _JsonlWriter:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def run_experiment(config_path: str | Path) -> RunResult:
    """Run every configured condition and preserve terminal attempt records."""

    source_path = Path(config_path)
    config = load_experiment(source_path)
    if "baseline_run" in config:
        baseline_path = Path(config["baseline_run"]).expanduser()
        if not baseline_path.is_absolute():
            baseline_path = (source_path.resolve().parent / baseline_path).resolve()
        config["baseline_run"] = str(baseline_path)
        try:
            baseline_summary = build_summary(baseline_path)
        except AggregationError as exc:
            raise RunnerError(f"baseline run is invalid: {exc}") from exc
        if any(
            gate["kind"] in {"max_drop", "max_increase_ratio"}
            for gate in config["gates"]
        ) and baseline_summary["run_complete"] is not True:
            raise RunnerError("baseline run is incomplete and cannot serve gates")
    api_key = _load_api_key(config["endpoint"])

    local_tokenizer: HuggingFaceLengthMeasurer | None = None
    if config["cases"]["length_unit"] == "tokens":
        local_tokenizer = HuggingFaceLengthMeasurer.from_config(config)
    generated = generate_cases(config, measurer=local_tokenizer)
    conditions = build_conditions(config, generated)

    config_sha256 = hashlib.sha256(
        canonical_json(config).encode("utf-8")
    ).hexdigest()
    run_id = _new_run_id(config_sha256)
    run_directory = Path(config["output"]["root"]).expanduser() / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RunnerError(f"run directory already exists: {run_directory}") from exc
    except OSError as exc:
        raise RunnerError(f"could not create run directory {run_directory}: {exc}") from exc

    started_at = _utc_now()
    measured_attempts = 0
    warmup_attempts = 0
    request_failures = 0
    expected_measured = len(conditions) * config["cases"]["count"] * config["run"]["repetitions"]
    expected_warmup = len(conditions) * config["run"]["warmup_requests_per_condition"]

    manifest = {
        "manifest_version": "1.0.0",
        "run_id": run_id,
        "experiment_name": config["name"],
        "completion_state": "running",
        "run_complete": False,
        "started_at": started_at,
        "finished_at": None,
        "tool": {"name": "structured-output-reliability", "version": __version__},
        "python_version": sys.version.split()[0],
        "configuration_contract_version": CONFIGURATION_CONTRACT_VERSION,
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "config_sha256": config_sha256,
        "generated_cases_sha256": generated.sha256,
        "condition_count": len(conditions),
        "planned_measured_attempts": expected_measured,
        "planned_warmup_attempts": expected_warmup,
        "terminal_measured_attempts": 0,
        "terminal_warmup_attempts": 0,
        "request_failures": 0,
    }

    try:
        _write_run_inputs(source_path, config, generated, conditions, run_directory)
        _atomic_write_json(run_directory / "manifest.json", manifest)
        with _JsonlWriter(
            run_directory / "responses.jsonl",
            flush_every=config["output"]["flush_every"],
        ) as writer:
            for condition in conditions:
                cases = select_condition_cases(generated.records, condition)
                records = _run_condition(
                    run_id=run_id,
                    condition=condition,
                    cases=cases,
                    api_key=api_key,
                    include_raw_response=config["output"]["include_raw_response"],
                    include_stream_events=config["output"]["include_stream_events"],
                    local_tokenizer=local_tokenizer,
                )
                for record in records:
                    writer.write(record)
                    if record["phase"] == "measure":
                        measured_attempts += 1
                    else:
                        warmup_attempts += 1
                    if not record["score"]["request_success"]:
                        request_failures += 1

        run_complete = (
            measured_attempts == expected_measured
            and warmup_attempts == expected_warmup
        )
        manifest.update(
            {
                "completion_state": "complete" if run_complete else "incomplete",
                "run_complete": run_complete,
                "finished_at": _utc_now(),
                "terminal_measured_attempts": measured_attempts,
                "terminal_warmup_attempts": warmup_attempts,
                "request_failures": request_failures,
            }
        )
        _atomic_write_json(run_directory / "manifest.json", manifest)
        summary_result = summarize_run(run_directory)
    except BaseException:
        manifest.update(
            {
                "completion_state": "incomplete",
                "run_complete": False,
                "finished_at": _utc_now(),
                "terminal_measured_attempts": measured_attempts,
                "terminal_warmup_attempts": warmup_attempts,
                "request_failures": request_failures,
            }
        )
        try:
            _atomic_write_json(run_directory / "manifest.json", manifest)
        except OSError:
            pass
        raise

    if not manifest["run_complete"]:
        raise RunnerError(f"run ended without every planned attempt: {run_directory}")
    try:
        report_result = generate_report(
            run_directory, candidate_summary=summary_result.summary
        )
    except ReportingError as exc:
        raise RunnerError(f"run completed but report generation failed: {exc}") from exc
    return RunResult(
        run_id=run_id,
        run_directory=run_directory,
        condition_count=len(conditions),
        measured_attempts=measured_attempts,
        warmup_attempts=warmup_attempts,
        request_failures=request_failures,
        report_path=report_result.report_path,
        gates_configured=report_result.gates_configured,
        gates_passed=report_result.gates_passed,
    )


def build_conditions(
    config: Mapping[str, Any], generated: GeneratedCaseSet
) -> tuple[dict[str, Any], ...]:
    """Resolve matrix points into validated, stable condition records."""

    records: list[dict[str, Any]] = []
    validator = Draft202012Validator(
        load_schema("condition"), format_checker=FormatChecker()
    )
    for point in expand_matrix(config):
        matching_cases = _matching_cases(generated.records, point)
        case_set_sha256 = _records_sha256(matching_cases)
        execution_order_seed = _derived_integer(
            {
                "root_seed": config["run"]["seed"],
                "matrix_point": point,
                "case_set_sha256": case_set_sha256,
            }
        )
        comparison_payload: dict[str, Any] = {
            "configuration_contract_version": CONFIGURATION_CONTRACT_VERSION,
            "metric_contract_version": METRIC_CONTRACT_VERSION,
            "case_schema_version": "1.0.0",
            "case_set_sha256": case_set_sha256,
            "concurrency": point["concurrency"],
            "prompt_length": point["prompt_length"],
            "output_shape": point["output_shape"],
            "temperature": point["temperature"],
            "request_seed": point["request_seed"],
            "structured_mode": point["structured_mode"],
            "stream": config["run"]["stream"],
            "repetitions": config["run"]["repetitions"],
        }
        if "slos" in config:
            comparison_payload["slos"] = copy.deepcopy(config["slos"])
        comparison_key = "cmp_" + _sha256(comparison_payload)[:16]

        record: dict[str, Any] = {
            "condition_schema_version": CONDITION_SCHEMA_VERSION,
            "configuration_contract_version": CONFIGURATION_CONTRACT_VERSION,
            "metric_contract_version": METRIC_CONTRACT_VERSION,
            "case_schema_version": "1.0.0",
            "comparison_key": comparison_key,
            "endpoint": copy.deepcopy(config["endpoint"]),
            "tokenizer": copy.deepcopy(config.get("tokenizer")),
            "concurrency": point["concurrency"],
            "prompt_length": copy.deepcopy(point["prompt_length"]),
            "output_shape": copy.deepcopy(point["output_shape"]),
            "temperature": point["temperature"],
            "request_seed": point["request_seed"],
            "structured_mode": copy.deepcopy(point["structured_mode"]),
            "stream": config["run"]["stream"],
            "repetitions": config["run"]["repetitions"],
            "warmup_requests": config["run"]["warmup_requests_per_condition"],
            "execution_order_seed": execution_order_seed,
            "case_set_sha256": case_set_sha256,
        }
        if "slos" in config:
            record["slos"] = copy.deepcopy(config["slos"])
        record["condition_id"] = "cond_" + _sha256(record)[:16]
        errors = sorted(
            validator.iter_errors(record), key=lambda error: list(error.absolute_path)
        )
        if errors:
            raise RunnerError(f"generated invalid condition: {errors[0].message}")
        records.append(record)
    return tuple(records)


def select_condition_cases(
    cases: Sequence[Mapping[str, Any]], condition: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Return the cases belonging to one prompt/output-length cell."""

    selected = _matching_cases(cases, condition)
    if _records_sha256(selected) != condition["case_set_sha256"]:
        raise RunnerError(
            f"case-set hash mismatch for condition {condition['condition_id']}"
        )
    return tuple(copy.deepcopy(dict(case)) for case in selected)


def build_request_body(
    condition: Mapping[str, Any], case: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the exact Chat Completions request body for one attempt."""

    body: dict[str, Any] = {
        "model": condition["endpoint"]["model"],
        "messages": copy.deepcopy(case["messages"]),
        "temperature": condition["temperature"],
        "stream": condition["stream"],
        "max_completion_tokens": condition["output_shape"]["max_completion_tokens"],
    }
    if condition["request_seed"] is not None:
        body["seed"] = condition["request_seed"]

    mode = condition["structured_mode"]
    if mode["type"] == "response_format":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": case["case_id"],
                "strict": mode["strict"],
                "schema": copy.deepcopy(case["json_schema"]),
            },
        }
    elif mode["type"] == "extra_body":
        fragment = copy.deepcopy(mode["body"])
        _set_json_pointer(
            fragment,
            mode["schema_json_pointer"],
            copy.deepcopy(case["json_schema"]),
        )
        body.update(fragment)
    return body


def _run_condition(
    *,
    run_id: str,
    condition: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    api_key: str | None,
    include_raw_response: bool,
    include_stream_events: bool,
    local_tokenizer: HuggingFaceLengthMeasurer | None,
) -> Iterator[dict[str, Any]]:
    sequence_index = 0

    warmup_count = condition["warmup_requests"]
    if warmup_count:
        warmup_cases = [cases[index % len(cases)] for index in range(warmup_count)]
        warmup_rng = random.Random(
            _derived_integer(
                {"execution_order_seed": condition["execution_order_seed"], "phase": "warmup"}
            )
        )
        warmup_rng.shuffle(warmup_cases)
        batch = _execute_batch(
            run_id=run_id,
            condition=condition,
            work=[(case, index) for index, case in enumerate(warmup_cases)],
            phase="warmup",
            first_sequence_index=sequence_index,
            api_key=api_key,
            include_raw_response=include_raw_response,
            include_stream_events=include_stream_events,
            local_tokenizer=local_tokenizer,
        )
        yield from batch
        sequence_index += len(warmup_cases)

    for repeat_id in range(condition["repetitions"]):
        ordered_cases = list(cases)
        repetition_rng = random.Random(
            _derived_integer(
                {
                    "execution_order_seed": condition["execution_order_seed"],
                    "phase": "measure",
                    "repeat_id": repeat_id,
                }
            )
        )
        repetition_rng.shuffle(ordered_cases)
        batch = _execute_batch(
            run_id=run_id,
            condition=condition,
            work=[(case, repeat_id) for case in ordered_cases],
            phase="measure",
            first_sequence_index=sequence_index,
            api_key=api_key,
            include_raw_response=include_raw_response,
            include_stream_events=include_stream_events,
            local_tokenizer=local_tokenizer,
        )
        yield from batch
        sequence_index += len(ordered_cases)


def _execute_batch(
    *,
    run_id: str,
    condition: Mapping[str, Any],
    work: Sequence[tuple[Mapping[str, Any], int]],
    phase: str,
    first_sequence_index: int,
    api_key: str | None,
    include_raw_response: bool,
    include_stream_events: bool,
    local_tokenizer: HuggingFaceLengthMeasurer | None,
) -> Iterator[dict[str, Any]]:
    futures: list[Future[dict[str, Any]]] = []
    next_sequence = first_sequence_index
    with ThreadPoolExecutor(
        max_workers=condition["concurrency"],
        thread_name_prefix="sor-request",
    ) as executor:
        for case, repeat_id in work:
            queued_ns = time.perf_counter_ns()
            sequence_index = next_sequence
            next_sequence += 1
            futures.append(
                executor.submit(
                    _execute_attempt,
                    run_id=run_id,
                    condition=condition,
                    case=case,
                    repeat_id=repeat_id,
                    phase=phase,
                    sequence_index=sequence_index,
                    queued_ns=queued_ns,
                    api_key=api_key,
                    include_raw_response=include_raw_response,
                    include_stream_events=include_stream_events,
                    local_tokenizer=local_tokenizer,
                )
            )
        for future in as_completed(futures):
            yield future.result()


def _execute_attempt(
    *,
    run_id: str,
    condition: Mapping[str, Any],
    case: Mapping[str, Any],
    repeat_id: int,
    phase: str,
    sequence_index: int,
    queued_ns: int,
    api_key: str | None,
    include_raw_response: bool,
    include_stream_events: bool,
    local_tokenizer: HuggingFaceLengthMeasurer | None,
) -> dict[str, Any]:
    slot_acquired_ns = time.perf_counter_ns()
    body = build_request_body(condition, case)
    body_bytes = canonical_json(body).encode("utf-8")
    url = _endpoint_url(condition["endpoint"])
    request_record = {
        "method": "POST",
        "url": url,
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "body": body,
        "seed_sent": "seed" in body,
        "structured_constraint_sent": condition["structured_mode"]["type"] != "prompt_only",
    }
    headers = {
        "Accept": "text/event-stream" if condition["stream"] else "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"structured-output-reliability/{__version__}",
    }
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        url,
        data=body_bytes,
        headers=headers,
        method="POST",
    )

    attempt_id = "attempt_" + _sha256(
        [
            run_id,
            condition["condition_id"],
            case["case_id"],
            repeat_id,
            phase,
            sequence_index,
        ]
    )[:16]
    client_queue_ns = slot_acquired_ns - queued_ns
    network_start_ns = time.perf_counter_ns()
    started_at = _utc_now()
    response_record = _empty_response()
    tokens = _empty_tokens()
    error: dict[str, Any] | None = None
    request_failure: str | None = None
    ttft_ns: int | None = None
    response_terminal_ns: int | None = None

    try:
        with urllib.request.urlopen(
            request, timeout=condition["endpoint"]["timeout_seconds"]
        ) as http_response:
            status = http_response.status
            if condition["stream"]:
                protocol_result = _read_streaming_response(
                    http_response,
                    http_status=status,
                    network_start_ns=network_start_ns,
                    include_raw_response=include_raw_response,
                    include_stream_events=include_stream_events,
                    secret=api_key,
                )
            else:
                protocol_result = _read_json_response(
                    http_response,
                    http_status=status,
                    include_raw_response=include_raw_response,
                    secret=api_key,
                )
            response_record = protocol_result.response
            tokens = protocol_result.tokens
            ttft_ns = protocol_result.ttft_ns
            response_terminal_ns = protocol_result.terminal_ns
    except urllib.error.HTTPError as exc:
        request_failure = "http_error"
        response_record = _http_error_response(
            exc,
            include_raw_response=include_raw_response,
            secret=api_key,
        )
        error = _error_record(
            stage="http",
            error_type="HTTPError",
            message=f"HTTP {exc.code}",
            retriable=exc.code == 429 or exc.code >= 500,
            secret=api_key,
        )
    except _ProtocolError as exc:
        request_failure = "protocol_error"
        if exc.response is not None:
            response_record = exc.response
        error = _error_record(
            stage=exc.stage,
            error_type=type(exc).__name__.removeprefix("_"),
            message=str(exc),
            retriable=False,
            secret=api_key,
        )
    except (TimeoutError, socket.timeout) as exc:
        request_failure = "timeout"
        error = _error_record(
            stage="timeout",
            error_type=type(exc).__name__,
            message=str(exc) or "request timed out",
            retriable=True,
            secret=api_key,
        )
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            request_failure = "timeout"
            stage = "timeout"
        else:
            request_failure = "transport_error"
            stage = "transport"
        error = _error_record(
            stage=stage,
            error_type=type(exc.reason).__name__,
            message=str(exc.reason),
            retriable=True,
            secret=api_key,
        )
    except (ConnectionError, OSError) as exc:
        request_failure = "transport_error"
        error = _error_record(
            stage="transport",
            error_type=type(exc).__name__,
            message=str(exc),
            retriable=True,
            secret=api_key,
        )

    finished_ns = time.perf_counter_ns()
    finished_at = _utc_now()
    observed_duration_ns = (
        response_terminal_ns if response_terminal_ns is not None else finished_ns
    ) - network_start_ns
    if request_failure is None:
        if tokens["token_count_source"] == "unavailable" and local_tokenizer is not None:
            tokens = _local_token_counts(local_tokenizer, case, response_record["content"])
        e2e_latency_ns: int | None = observed_duration_ns
        score = score_response(case, response_record["content"])
    else:
        e2e_latency_ns = None
        score = score_response(case, None, request_failure=request_failure)

    output_tokens = tokens["output"]
    tpot_ns: float | None = None
    if (
        request_failure is None
        and condition["stream"]
        and ttft_ns is not None
        and isinstance(output_tokens, int)
        and output_tokens >= 2
    ):
        tpot_ns = (observed_duration_ns - ttft_ns) / (output_tokens - 1)

    record = {
        "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
        "run_id": run_id,
        "condition_id": condition["condition_id"],
        "case_id": case["case_id"],
        "attempt_id": attempt_id,
        "repeat_id": repeat_id,
        "phase": phase,
        "sequence_index": sequence_index,
        "terminal": True,
        "started_at": started_at,
        "finished_at": finished_at,
        "request": request_record,
        "response": response_record,
        "timing": {
            "client_queue_ns": client_queue_ns,
            "observed_duration_ns": observed_duration_ns,
            "e2e_latency_ns": e2e_latency_ns,
            "ttft_ns": ttft_ns if request_failure is None else None,
            "tpot_ns": tpot_ns,
        },
        "tokens": tokens,
        "score": score,
        "error": error,
        "diagnostics": {},
    }
    errors = sorted(
        Draft202012Validator(
            load_schema("attempt"), format_checker=FormatChecker()
        ).iter_errors(record),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise RunnerError(f"generated invalid attempt record: {errors[0].message}")
    return record


def _read_json_response(
    http_response: Any,
    *,
    http_status: int,
    include_raw_response: bool,
    secret: str | None,
) -> _ProtocolResult:
    raw_bytes = http_response.read()
    terminal_ns = time.perf_counter_ns()
    response = _empty_response()
    response.update(
        {
            "http_status": http_status,
            "response_bytes": len(raw_bytes),
            "raw_chunk_count": 1,
        }
    )
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        response["raw_response"] = _redact_text(
            raw_bytes.decode("utf-8", errors="replace"), secret
        ) if include_raw_response else None
        raise _ProtocolError(
            f"response body is not valid UTF-8 JSON: {exc}", response=response
        ) from exc

    safe_payload = _redact_value(payload, secret)
    response["raw_response"] = safe_payload if include_raw_response else None
    try:
        content, finish_reason, refusal = _extract_chat_choice(payload, streaming=False)
    except _ProtocolError as exc:
        exc.response = response
        raise
    response.update(
        {
            "protocol_valid": True,
            "content": _redact_text(content, secret) if content is not None else None,
            "finish_reason": finish_reason,
            "refusal": _redact_text(refusal, secret) if refusal is not None else None,
            "refusal_detected": refusal is not None,
            "truncated": finish_reason == "length",
        }
    )
    return _ProtocolResult(
        response=response,
        tokens=_extract_usage(payload.get("usage"), tokenizer=None),
        ttft_ns=None,
        terminal_ns=terminal_ns,
    )


def _read_streaming_response(
    http_response: Any,
    *,
    http_status: int,
    network_start_ns: int,
    include_raw_response: bool,
    include_stream_events: bool,
    secret: str | None,
) -> _ProtocolResult:
    response = _empty_response()
    response.update({"http_status": http_status, "stream_terminated_normally": False})
    event_name: str | None = None
    data_lines: list[str] = []
    content_parts: list[str] = []
    refusal_parts: list[str] = []
    finish_reason: str | None = None
    usage: Any = None
    parsed_events: list[dict[str, Any]] = []
    raw_payloads: list[Any] = []
    response_bytes = 0
    json_chunk_count = 0
    ttft_ns: int | None = None
    terminal_ns: int | None = None
    done = False

    def dispatch(arrival_ns: int) -> None:
        nonlocal event_name, data_lines, finish_reason, usage, json_chunk_count
        nonlocal ttft_ns, terminal_ns, done
        if not data_lines:
            event_name = None
            return
        data_text = "\n".join(data_lines)
        data_lines = []
        current_event = event_name
        event_name = None
        if data_text == "[DONE]":
            done = True
            terminal_ns = arrival_ns
            parsed_data: Any = "[DONE]"
        else:
            try:
                payload = json.loads(data_text)
            except json.JSONDecodeError as exc:
                raise _ProtocolError(
                    f"stream event data is not valid JSON: {exc}", stage="stream"
                ) from exc
            if not isinstance(payload, dict):
                raise _ProtocolError("stream event JSON must be an object", stage="stream")
            parsed_data = _redact_value(payload, secret)
            json_chunk_count += 1
            if payload.get("usage") is not None:
                usage = payload["usage"]
            choices = payload.get("choices")
            if choices not in (None, []):
                content, chunk_finish, refusal = _extract_chat_choice(
                    payload, streaming=True
                )
                if content:
                    if ttft_ns is None:
                        ttft_ns = arrival_ns - network_start_ns
                    content_parts.append(content)
                if refusal:
                    refusal_parts.append(refusal)
                if chunk_finish is not None:
                    finish_reason = chunk_finish
        raw_payloads.append(parsed_data)
        parsed_events.append(
            {
                "arrival_offset_ns": arrival_ns - network_start_ns,
                "event": current_event,
                "data": parsed_data,
            }
        )

    try:
        for raw_line in http_response:
            arrival_ns = time.perf_counter_ns()
            response_bytes += len(raw_line)
            try:
                line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
            except UnicodeError as exc:
                raise _ProtocolError(
                    f"stream contains invalid UTF-8: {exc}", stage="stream"
                ) from exc
            if line == "":
                dispatch(arrival_ns)
                if done:
                    break
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            dispatch(time.perf_counter_ns())
        if not done:
            raise _ProtocolError(
                "stream ended without a [DONE] event", stage="stream"
            )
    except _ProtocolError as exc:
        response.update(
            {
                "content": _redact_text("".join(content_parts), secret) or None,
                "finish_reason": finish_reason,
                "refusal": _redact_text("".join(refusal_parts), secret) or None,
                "refusal_detected": bool(refusal_parts),
                "truncated": finish_reason == "length",
                "response_bytes": response_bytes,
                "raw_chunk_count": json_chunk_count,
                "raw_response": raw_payloads if include_raw_response else None,
                "stream_events": parsed_events if include_stream_events else None,
            }
        )
        exc.response = response
        raise

    response.update(
        {
            "protocol_valid": True,
            "content": _redact_text("".join(content_parts), secret),
            "finish_reason": finish_reason,
            "refusal": _redact_text("".join(refusal_parts), secret) or None,
            "refusal_detected": bool(refusal_parts),
            "truncated": finish_reason == "length",
            "response_bytes": response_bytes,
            "raw_chunk_count": json_chunk_count,
            "stream_terminated_normally": True,
            "raw_response": raw_payloads if include_raw_response else None,
            "stream_events": parsed_events if include_stream_events else None,
        }
    )
    return _ProtocolResult(
        response=response,
        tokens=_extract_usage(usage, tokenizer=None),
        ttft_ns=ttft_ns,
        terminal_ns=terminal_ns if terminal_ns is not None else time.perf_counter_ns(),
    )


def _extract_chat_choice(
    payload: Any, *, streaming: bool
) -> tuple[str | None, str | None, str | None]:
    if not isinstance(payload, dict):
        raise _ProtocolError("response JSON must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _ProtocolError("response must contain a non-empty choices array")
    choice = choices[0]
    container_name = "delta" if streaming else "message"
    container = choice.get(container_name)
    if not isinstance(container, dict):
        raise _ProtocolError(f"first choice must contain an object {container_name}")
    content = container.get("content")
    if content is not None and not isinstance(content, str):
        raise _ProtocolError("assistant content must be a string or null")
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise _ProtocolError("finish_reason must be a string or null")
    refusal = container.get("refusal")
    if refusal is not None and not isinstance(refusal, str):
        raise _ProtocolError("assistant refusal must be a string or null")
    return content, finish_reason, refusal


def _extract_usage(usage: Any, *, tokenizer: str | None) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        return _empty_tokens()
    input_tokens = _nonnegative_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    output_tokens = _nonnegative_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    total_tokens = _nonnegative_int(usage.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return _empty_tokens()
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input": input_tokens,
        "output": output_tokens,
        "total": total_tokens,
        "token_count_source": "server",
        "tokenizer": tokenizer,
    }


def _local_token_counts(
    tokenizer: HuggingFaceLengthMeasurer,
    case: Mapping[str, Any],
    content: str | None,
) -> dict[str, Any]:
    if content is None:
        return _empty_tokens()
    try:
        output_tokens = len(
            tokenizer.tokenizer.encode(content, add_special_tokens=False)
        )
    except Exception:
        return _empty_tokens()
    input_tokens = case["prompt_length"]["actual"]
    return {
        "input": input_tokens,
        "output": output_tokens,
        "total": input_tokens + output_tokens,
        "token_count_source": "local_tokenizer",
        "tokenizer": getattr(tokenizer.tokenizer, "name_or_path", None),
    }


def _http_error_response(
    error: urllib.error.HTTPError,
    *,
    include_raw_response: bool,
    secret: str | None,
) -> dict[str, Any]:
    response = _empty_response()
    try:
        raw_bytes = error.read()
    except OSError:
        raw_bytes = b""
    raw_value: Any = None
    if include_raw_response and raw_bytes:
        text = raw_bytes.decode("utf-8", errors="replace")
        try:
            raw_value = json.loads(text)
        except json.JSONDecodeError:
            raw_value = text
        raw_value = _redact_value(raw_value, secret)
    response.update(
        {
            "http_status": error.code,
            "response_bytes": len(raw_bytes),
            "raw_chunk_count": 1 if raw_bytes else 0,
            "raw_response": raw_value,
        }
    )
    return response


def _empty_response() -> dict[str, Any]:
    return {
        "http_status": None,
        "protocol_valid": False,
        "content": None,
        "finish_reason": None,
        "refusal": None,
        "refusal_detected": False,
        "truncated": False,
        "response_bytes": 0,
        "raw_chunk_count": 0,
        "stream_terminated_normally": None,
        "raw_response": None,
        "stream_events": None,
    }


def _empty_tokens() -> dict[str, Any]:
    return {
        "input": None,
        "output": None,
        "total": None,
        "token_count_source": "unavailable",
        "tokenizer": None,
    }


def _matching_cases(
    cases: Sequence[Mapping[str, Any]], point: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    prompt_target = point["prompt_length"]["target"]
    output_target = point["output_shape"]["target_length"]
    return [
        case
        for case in cases
        if case["prompt_length"]["target"] == prompt_target
        and case["expected_output_length"]["target"] == output_target
    ]


def _write_run_inputs(
    source_path: Path,
    config: Mapping[str, Any],
    generated: GeneratedCaseSet,
    conditions: Sequence[Mapping[str, Any]],
    run_directory: Path,
) -> None:
    try:
        source_text = source_path.read_text(encoding="utf-8")
        (run_directory / "config.input.yaml").write_text(source_text, encoding="utf-8")
        (run_directory / "config.resolved.yaml").write_text(
            yaml.safe_dump(
                dict(config),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        write_cases_jsonl(generated, run_directory / "cases.jsonl")
        with _JsonlWriter(run_directory / "conditions.jsonl", flush_every=1) as writer:
            for condition in conditions:
                writer.write(condition)
    except (OSError, UnicodeError, GenerationError) as exc:
        raise RunnerError(f"could not preserve run inputs: {exc}") from exc


def _load_api_key(endpoint: Mapping[str, Any]) -> str | None:
    variable_name = endpoint["api_key_env"]
    if variable_name is None:
        return None
    value = os.environ.get(variable_name)
    if not value:
        raise RunnerError(
            f"endpoint credential environment variable {variable_name!r} is not set"
        )
    return value


def _endpoint_url(endpoint: Mapping[str, Any]) -> str:
    return endpoint["base_url"].rstrip("/") + "/" + endpoint["path"].lstrip("/")


def _set_json_pointer(document: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    ]
    current: dict[str, Any] = document
    for token in tokens[:-1]:
        current = current[token]
    current[tokens[-1]] = value


def _records_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(list(records)).encode("utf-8")).hexdigest()


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _derived_integer(value: Any) -> int:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _new_run_id(config_sha256: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}_{config_sha256[:12]}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(canonical_json(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _error_record(
    *,
    stage: str,
    error_type: str,
    message: str,
    retriable: bool,
    secret: str | None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "type": error_type or "UnknownError",
        "message": _redact_text(message, secret),
        "retriable": retriable,
    }


def _redact_text(value: str, secret: str | None) -> str:
    if secret:
        return value.replace(secret, "[REDACTED]")
    return value


def _redact_value(value: Any, secret: str | None) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return _redact_text(value, secret)
    if isinstance(value, list):
        return [_redact_value(child, secret) for child in value]
    if isinstance(value, dict):
        return {key: _redact_value(child, secret) for key, child in value.items()}
    return value


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
