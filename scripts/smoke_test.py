#!/usr/bin/env python3
"""Run one complete local experiment against a deterministic mock endpoint."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import yaml


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@contextmanager
def _mock_endpoint() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            requests.append(body)
            user_content = body["messages"][-1]["content"]
            source = user_content.split("SOURCE_JSON:", 1)[1]
            source = source.split("\nEverything after PADDING_START", 1)[0]
            content = _canonical_json(json.loads(source))
            payload = {
                "id": "chatcmpl-smoke",
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
            encoded = _canonical_json(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = _Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _sor_executable() -> str:
    beside_python = Path(sys.executable).with_name("sor")
    if beside_python.is_file():
        return str(beside_python)
    discovered = shutil.which("sor")
    if discovered is None:
        raise RuntimeError("sor is not installed; run: pip install -e '.[dev]'")
    return discovered


def _run_json(*arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        [_sor_executable(), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"sor {' '.join(arguments)} failed with {completed.returncode}: {detail}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sor emitted invalid JSON: {completed.stdout!r}") from exc


def _config(base_url: str, output_root: Path) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "name": "one-case-smoke",
        "endpoint": {
            "base_url": base_url,
            "model": "smoke-exact",
            "api_key_env": None,
            "timeout_seconds": 5,
        },
        "cases": {
            "count": 1,
            "length_unit": "characters",
            "families": ["flat_object"],
        },
        "run": {
            "seed": 20261010,
            "repetitions": 1,
            "warmup_requests_per_condition": 0,
            "stream": False,
        },
        "structured_modes": [
            {"id": "json_schema", "type": "response_format", "strict": True}
        ],
        "matrix": {
            "concurrency": [1],
            "prompt_lengths": [2048],
            "output_shapes": [
                {"target_length": 256, "max_completion_tokens": 128}
            ],
            "temperatures": [0],
            "request_seeds": [42],
            "structured_modes": ["json_schema"],
        },
        "output": {"root": str(output_root)},
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sor-smoke-") as temporary:
        root = Path(temporary)
        with _mock_endpoint() as (base_url, requests):
            config_path = root / "experiment.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    _config(base_url, root / "runs"),
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            validation = _run_json("validate", str(config_path), "--json")
            if validation["measured_attempts"] != 1:
                raise RuntimeError(f"unexpected validation result: {validation}")
            run = _run_json("run", str(config_path), "--json")

        if len(requests) != 1 or "response_format" not in requests[0]:
            raise RuntimeError("mock endpoint did not receive one constrained request")
        run_directory = Path(run["run_directory"])
        report = _run_json("report", str(run_directory), "--json")
        summary = json.loads((run_directory / "summary.json").read_text(encoding="utf-8"))
        metrics = summary["conditions"][0]["metrics"]
        expected = {
            "request_success_rate": 1,
            "json_parse_rate": 1,
            "schema_valid_rate": 1,
            "exact_correct_rate": 1,
            "micro_field_accuracy": 1,
        }
        observed = {name: metrics[name]["value"] for name in expected}
        if observed != expected:
            raise RuntimeError(f"unexpected summary metrics: {observed}")
        required_artifacts = {
            "manifest.json",
            "cases.jsonl",
            "conditions.jsonl",
            "responses.jsonl",
            "summary.json",
            "summary.csv",
            "report.md",
        }
        missing = sorted(
            name for name in required_artifacts if not (run_directory / name).is_file()
        )
        if missing:
            raise RuntimeError(f"missing run artifacts: {', '.join(missing)}")
        if report["gates_passed"] is not True:
            raise RuntimeError(f"unexpected report result: {report}")

    print("Smoke test passed: 1 condition, 1 case, 1 exact-correct response")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
