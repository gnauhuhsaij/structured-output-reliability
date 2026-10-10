# Structured Output Reliability

Structured Output Reliability is a small, reproducible regression tool for
measuring whether an OpenAI-compatible model service continues to produce
correct structured output under load.

The project is currently implementing its v0.1 MVP. It can validate an
experiment file, deterministically expand its test matrix, generate
self-contained cases, execute them against an OpenAI-compatible Chat
Completions endpoint under fixed concurrency, and strictly score every saved
assistant response.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Validate the included experiment:

```bash
sor validate examples/experiment.yaml
```

Expected output:

```text
Configuration valid: examples/experiment.yaml
16 conditions
40 generated cases
960 measured attempts
32 warmup attempts
```

Use `--json` for machine-readable output. Validation is static and does not
contact the configured endpoint or require its API-key environment variable to
be set. Runtime readiness is checked by `sor run`.

Generate a small offline workload with one case from every built-in family:

```bash
sor generate examples/character-experiment.yaml --output artifacts/cases.jsonl
```

The command writes one complete case per line and prints the full SHA-256 of
the canonical case set. Generation is deterministic: the resolved
configuration and root seed produce the same records, IDs, order, and hash.
Existing files are not replaced unless `--force` is supplied. This command also
does not contact the configured model endpoint.

Character-length experiments work with the base installation. Exact token
targeting uses the configured Hugging Face tokenizer and its chat template, so
install the optional dependency when needed:

```bash
python -m pip install -e '.[dev,tokenizer]'
```

Loading a tokenizer may download its files on first use. The tokenizer is used
only to count and size cases; no model weights are loaded.

Score the exact assistant content saved in a UTF-8 file against one generated
case:

```bash
sor score artifacts/cases.jsonl \
  --case-id case_1ef862e16b62bb77 \
  --response response.txt
```

Use `--json` for the complete score record. The primary path does not remove
Markdown fences, extract a JSON substring, coerce types, or repair malformed
content. A request that failed before content evaluation can also be recorded
without shrinking correctness denominators:

```bash
sor score artifacts/cases.jsonl \
  --case-id case_1ef862e16b62bb77 \
  --request-failure timeout \
  --json
```

Run the complete configured matrix against an already running service:

```bash
sor run examples/character-experiment.yaml
```

`sor run` sends real HTTP requests. Set `endpoint.api_key_env: null` for a
local service with no authentication, or set it to an environment-variable
name such as `OPENAI_API_KEY` for a service that requires a Bearer token. Never
put the key itself in YAML.

The runner uses closed-loop fixed concurrency, executes warmup and measured
attempts, supports streaming SSE and non-streaming responses, measures E2E
latency/TTFT/TPOT, and writes each terminal result incrementally. The current
run directory contains:

```text
<output.root>/<run_id>/
  config.input.yaml
  config.resolved.yaml
  manifest.json
  cases.jsonl
  conditions.jsonl
  responses.jsonl
```

Aggregate JSON/CSV and the Markdown report are the next implementation stage.

## Contracts

- [`DESIGN.md`](DESIGN.md) freezes the MVP scope.
- [`CONFIG.md`](CONFIG.md) defines configuration and record formats.
- [`METRICS.md`](METRICS.md) defines metric formulas and denominators.
- [`schemas/`](schemas/) contains the normative JSON Schemas.
