# Structured Output Reliability

Structured Output Reliability is a small, reproducible regression tool for
measuring whether an OpenAI-compatible model service continues to produce
correct structured output under load.

The project is currently implementing its v0.1 MVP. It can validate an
experiment file, deterministically expand its test matrix, generate
self-contained cases, execute them against an OpenAI-compatible Chat
Completions endpoint under fixed concurrency, and strictly score every saved
assistant response. It also aggregates condition-level correctness,
consistency, latency, throughput, goodput, and confidence intervals offline.
Every completed run also receives a Markdown report, and configured regression
gates can fail a CI command without changing the run's completed status.
Reports also include a supplementary non-padding field accuracy so synthetic
length-filling errors can be distinguished from errors in task fields; strict
exact correctness and quality goodput remain unchanged.

## Reproducible experiments

The YAML files below are reusable experiment configurations, not a claim that
every supported matrix dimension has already been evaluated. Each live
experiment changes selected dimensions and holds the others fixed so its
comparisons remain interpretable. Automated tests in `tests/` instead check
that the CLI and scoring code work; they are not model-service findings.

| Experiment | Question and varied conditions | Status | Evidence |
|---|---|---|---|
| Five-run baseline | How do concurrency (1 vs. 4) and output mode (prompt-only vs. strict JSON Schema) relate to reliability and performance for the same 24 cases? | Completed: five sequential runs, 960 measured attempts | [Config](examples/openai-baseline.yaml) · [Results and limitations](RESULTS.md) |
| Load-stress pilot | What happens to exact correctness and quality goodput at concurrency 1 vs. 8 in JSON Schema mode? Prompt-only is a secondary comparator; the other settings are fixed. | Completed: one run, 768 measured attempts | [Config](examples/openai-stress.yaml) · [Plan](STRESS_PLAN.md) · [Results and limitations](STRESS_RESULTS.md) |

The baseline observed 960/960 exactly correct responses. Its concurrency-4
conditions had higher request throughput than concurrency-1 conditions, but
the small, sequential sample does not establish a general performance or
reliability effect. The stress pilot observed 15 exact-value failures in
synthetic padding, all in JSON Schema mode; it did not show a Schema violation
or a request failure. It uses longer prompts and outputs but the same schema
families and field/path counts, so it is primarily a serving-load test, not a
test of harder extraction logic.

When adding a completed experiment, publish its question, YAML, case-set
hash, condition matrix, request counts, analysis method, aggregate results,
and limitations. Label offline-validated or planned configurations separately
until live responses have actually been collected. Keep API keys and raw
request/response artifacts out of Git; `runs/` is ignored.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the deterministic end-to-end smoke test with no API key or external model:

```bash
python scripts/smoke_test.py
```

It starts a temporary local OpenAI-compatible endpoint and verifies the complete
path from YAML validation through request execution, scoring, aggregation, and
Markdown reporting. The temporary run directory is removed after success.

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
Case generation not checked; use sor validate <config> --check-cases before a live run.
```

Use `--json` for machine-readable output. Plain validation is static. Add
`--check-cases` to generate and validate every case in memory without saving a
file or contacting the endpoint:

```bash
sor validate examples/openai-smoke.yaml --check-cases
```

Neither validation mode needs the API-key environment variable. Token-length
case generation may need the optional tokenizer dependency. Runtime endpoint
readiness is checked by `sor run`.

Generate a small offline workload with one case from every built-in family:

```bash
sor generate examples/character-experiment.yaml --output artifacts/cases.jsonl
```

The command writes one complete case per line and prints the full SHA-256 of
the canonical case set. Generation is deterministic: the resolved
configuration and root seed produce the same records, IDs, order, and hash.
Existing files are not replaced unless `--force` is supplied. This command also
does not contact the configured model endpoint.

After the offline check, set `OPENAI_API_KEY` in the same shell to run the
included live experiment:

```bash
read -r -s "OPENAI_API_KEY?Paste the raw API key (no quotes): "
echo
export OPENAI_API_KEY
sor run examples/openai-smoke.yaml
```

The hidden prompt keeps the key out of shell history. Paste only the raw key;
quotation marks (including smart quotes) and trailing whitespace are not part
of it. The runner rejects malformed credential text before creating a run.

The smoke experiment uses generator version `1.1.0`, which places inert prompt
padding before the source JSON so it cannot look like a continuation of the
expected output. Version `1.0.0` remains available for reproducing older runs.
It also sets `run.stream_include_usage: true` to request the final streaming
usage chunk used for token throughput and TPOT. Some OpenAI-compatible services
may not support that option; omit it for those endpoints, in which case missing
token metrics remain unavailable rather than zero.

After the smoke run, the larger baseline keeps the same four conditions but
uses 24 distinct cases (six per family) and two repetitions: 192 measured
requests plus four warmups. Check all cases offline before sending real requests:

```bash
sor validate examples/openai-baseline.yaml --check-cases
sor run examples/openai-baseline.yaml
```

The baseline is a separate run and does not overwrite the smoke artifacts.

To repeat that exact case set five times with five different, reproducible
condition orders, prepare a plan without sending requests:

```bash
python scripts/replicate_baseline.py --prepare-only
```

The command prints a `runs/replication-.../plan.json` path. In the terminal
where `OPENAI_API_KEY` is set, execute or resume that specific plan:

```bash
python scripts/replicate_baseline.py --plan runs/replication-.../plan.json
```

The plan saves each condition order and completed run directory. The default
five-run plan sends 960 measured requests and 20 warmups, with 15 seconds
between runs; it does not save the API key. Keep `run.seed` fixed to preserve
the generated cases, and vary only `run.condition_order_seed` to permute the
condition order.

After the five runs complete, rebuild the cross-run JSON and Markdown analysis
from their saved artifacts without calling the API:

```bash
python scripts/compare_replications.py runs/replication-.../plan.json
```

Its intervals use whole-run paired differences as observations. With five runs
they are exploratory and should not be read as causal or multiple-testing-
adjusted evidence.

The completed load-stress pilot is specified in [`STRESS_PLAN.md`](STRESS_PLAN.md)
and [`examples/openai-stress.yaml`](examples/openai-stress.yaml); its aggregate
results are in [`STRESS_RESULTS.md`](STRESS_RESULTS.md). Validate all 96 cases
offline before reproducing its 768 measured requests:

```bash
sor validate examples/openai-stress.yaml --check-cases
```

This validation sends no API requests and needs no API key. The stress plan
pre-specified a joint correctness/goodput question and kept rate-limit and
other request failures visible instead of silently excluding them.

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

While running, `sor` prints progress to stderr: total cases and attempts,
current matrix condition, completed warmup and measured attempts, each reported
case's outcome, and a waiting heartbeat after 10 seconds without a completed
request. Small runs show every attempt; larger runs show about 50 progress
checkpoints. With `--json`, stdout remains a single machine-readable result.

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
  summary.json
  summary.csv
  report.md
```

The summary and Markdown report are generated automatically after a run. They
can be reproduced from the saved evidence without contacting the model service:

```bash
sor summarize runs/<run_id>
```

The aggregator verifies record schemas and configuration/case-set hashes,
excludes warmups, keeps failed attempts in correctness denominators, and never
pools different conditions. To regenerate the report explicitly and evaluate
its gates:

```bash
sor report runs/<run_id>
```

For a complete run, the report also shows exploratory pairwise comparisons
between conditions that differ only in concurrency or structured-output mode
and share the same generated cases. It averages repeated attempts per case,
then bootstraps matched case IDs for 95% intervals on mean E2E, TTFT, and TPOT
changes. Throughput ratios are descriptive only: a single run cannot provide
a run-to-run throughput interval. Fixed condition execution order and temporal
drift also mean these comparisons do not establish causation. See
[`METRICS.md`](METRICS.md) for the exact method.

Absolute gates use only the candidate run:

```yaml
gates:
  - metric: schema_valid_rate
    kind: minimum
    value: 0.95
    estimate: lower_ci
```

Relative gates point to a complete prior run. Relative paths are resolved from
the experiment YAML's directory when `sor run` starts and saved as absolute
paths in `config.resolved.yaml`:

```yaml
baseline_run: ./runs/20261010T120000000000Z_0123456789ab
gates:
  - metric: exact_correct_rate
    kind: max_drop
    value: 0.02
    estimate: point
```

`sor run`, `sor summarize`, and `sor report` return exit code `1` when a
configured gate fails or is unavailable. Setup, artifact, and reporting errors
return `2`. The run manifest remains complete after an ordinary gate failure.

## Container

Build the CLI image and verify it:

```bash
docker build -t structured-output-reliability .
docker run --rm structured-output-reliability --version
```

Mount an experiment directory at `/workspace` to run a real configuration. The
configured endpoint URL must be reachable from inside the container, and the
mounted output directory must be writable by the container user (UID 10001):

```bash
docker run --rm \
  -v "$PWD:/workspace" \
  structured-output-reliability \
  validate /workspace/examples/character-experiment.yaml
```

The GitHub Actions workflow tests Python 3.11 and 3.14, runs the end-to-end
smoke test, builds the wheel and source distribution, and verifies the Docker
image.

## Contracts

- [`DESIGN.md`](DESIGN.md) freezes the MVP scope.
- [`CONFIG.md`](CONFIG.md) defines configuration and record formats.
- [`METRICS.md`](METRICS.md) defines metric formulas and denominators.
- [`schemas/`](schemas/) contains the normative JSON Schemas.
