# Structured Output Reliability

Structured Output Reliability is a small, reproducible regression tool for
measuring whether an OpenAI-compatible model service continues to produce
correct structured output under load.

The project is currently implementing its v0.1 MVP. The first executable slice
validates an experiment file and deterministically expands its test matrix.

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

## Contracts

- [`DESIGN.md`](DESIGN.md) freezes the MVP scope.
- [`CONFIG.md`](CONFIG.md) defines configuration and record formats.
- [`METRICS.md`](METRICS.md) defines metric formulas and denominators.
- [`schemas/`](schemas/) contains the normative JSON Schemas.

