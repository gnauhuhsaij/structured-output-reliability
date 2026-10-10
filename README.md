# Structured Output Reliability

Structured Output Reliability is a small, reproducible regression tool for
measuring whether an OpenAI-compatible model service continues to produce
correct structured output under load.

The project is currently implementing its v0.1 MVP. It can validate an
experiment file, deterministically expand its test matrix, and generate the
self-contained cases that will later be sent to a model service.

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
be set. Runtime readiness will be checked by the future `run` command.

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

## Contracts

- [`DESIGN.md`](DESIGN.md) freezes the MVP scope.
- [`CONFIG.md`](CONFIG.md) defines configuration and record formats.
- [`METRICS.md`](METRICS.md) defines metric formulas and denominators.
- [`schemas/`](schemas/) contains the normative JSON Schemas.
