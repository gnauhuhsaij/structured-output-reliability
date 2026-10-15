# Structured Output Reliability

**A reproducible CLI for testing whether an OpenAI-compatible model service still returns correct JSON under load.**

`sor` generates deterministic cases, runs them across a YAML-defined matrix,
and reports structured-output correctness alongside latency and throughput.
It is a focused regression tool, not a general-purpose model benchmark.

At a glance:

- **Vary:** concurrency, prompt/output length, temperature/seed, and
  structured-output mode.
- **Measure:** request success, JSON/Schema validity, field and exact-value
  correctness, repeat consistency, latency, TTFT, TPOT, throughput, and
  quality goodput.
- **Keep:** raw JSONL responses, JSON/CSV summaries, a Markdown report, and
  confidence intervals.

**Start here:** [Quick start](#quick-start) · [Example findings](#reproducible-experiments) · [CLI workflows](#cli-workflows) · [Metric definitions](METRICS.md)

## Quick start

Install from a checkout and run the end-to-end smoke test. The smoke test uses
a temporary local OpenAI-compatible server; it needs no API key or external
model and removes its temporary run directory after success.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python scripts/smoke_test.py
```

Check that the included live experiment can generate *all* its cases, without
writing a case file or contacting the endpoint:

```bash
sor validate examples/openai-smoke.yaml --check-cases
```

To run it against the configured OpenAI-compatible service, set the key in the
same shell and then execute the experiment:

```bash
read -r -s "OPENAI_API_KEY?Paste the raw API key (no quotes): "
echo
export OPENAI_API_KEY
sor run examples/openai-smoke.yaml
```

The hidden prompt keeps the key out of shell history. Paste only the raw key:
smart quotes and trailing whitespace cause credential validation to fail.
For an unauthenticated local service, set `endpoint.api_key_env: null` in the
YAML. Never put an API key directly in the configuration file.

Each run gets its own directory under `runs/`, including `responses.jsonl`,
`summary.json`, `summary.csv`, and `report.md`. The `runs/` directory is ignored
by Git; inspect raw artifacts before sharing them.

## Reproducible experiments

These configurations are examples of *specific comparisons*, not evidence
that every supported matrix dimension has been evaluated. Automated tests
in `tests/` verify the software, not model-service behavior.

| Experiment | Comparison | Evidence |
|---|---|---|
| Five-run baseline | Concurrency 1 vs. 4 and prompt-only vs. strict JSON Schema, on the same 24 cases; five sequential runs, 960 measured attempts | [YAML](examples/openai-baseline.yaml) · [Results and limitations](RESULTS.md) |
| Load-stress pilot | Concurrency 1 vs. 8 in JSON Schema mode, with prompt-only as a secondary comparator; one run, 768 measured attempts | [YAML](examples/openai-stress.yaml) · [Plan](STRESS_PLAN.md) · [Results and limitations](STRESS_RESULTS.md) |

**What we observed:** The baseline had 960/960 exactly correct responses;
concurrency 4 had higher request throughput than concurrency 1. The stress
pilot had 15 exact-value failures in synthetic padding, all in JSON Schema
mode, but no Schema violations or request failures. These are findings for the
documented service, cases, and sessions—not general or causal claims. The
stress pilot increased prompt and output lengths, not the difficulty of the
underlying field-extraction task.

To add a completed experiment, publish its question, YAML, case-set hash,
condition matrix, request counts, analysis method, aggregates, and limitations.
Keep planned or offline-only configurations distinct from live findings.

## CLI workflows

### Validate or generate cases offline

Static validation checks the YAML and prints the number of conditions, cases,
measured attempts, and warmups. `--json` makes the result machine-readable.

```bash
sor validate examples/experiment.yaml
sor validate examples/experiment.yaml --check-cases
```

The second command also generates and checks every case *in memory*; neither
command sends requests or requires a key. Token-length case generation may
need the optional tokenizer dependency. Endpoint readiness is checked by
`sor run`, not by validation.

To save a deterministic, self-contained JSONL case set instead:

```bash
sor generate examples/character-experiment.yaml --output artifacts/cases.jsonl
```

Generation prints the canonical case-set SHA-256. The resolved configuration
and root seed determine the same records, IDs, order, and hash. An existing
file is preserved unless you pass `--force`.

### Run a matrix and inspect its results

```bash
sor run examples/character-experiment.yaml
```

`sor run` sends real HTTP requests. It executes warmups and measured attempts
at closed-loop fixed concurrency, supports streaming SSE and non-streaming
responses, and saves each terminal result incrementally. Progress goes to
stderr: current condition, completed attempts, outcomes, and a waiting
heartbeat. With `--json`, stdout stays machine-readable.

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

The report and summaries are produced automatically. Rebuild them offline
from the saved evidence, or evaluate the report's regression gates again:

```bash
sor summarize runs/<run_id>
sor report runs/<run_id>
```

The aggregator verifies record schemas and configuration/case-set hashes,
excludes warmups, retains failed attempts in correctness denominators, and
keeps conditions separate. It does not treat unavailable timing metrics as
zero. For a complete run, the report includes exploratory paired comparisons
for conditions differing only in concurrency or structured-output mode on the
same cases. See [METRICS.md](METRICS.md) for the exact calculations and limits.

### Score a saved response

Score the *exact* assistant content in a UTF-8 file against a generated case:

```bash
sor score artifacts/cases.jsonl \
  --case-id case_1ef862e16b62bb77 \
  --response response.txt
```

`--json` returns the complete score record. Primary scoring never strips
Markdown fences, extracts embedded JSON, coerces types, or repairs content.
To record a request that failed before content evaluation, use
`--request-failure timeout` instead of `--response`; the failed attempt still
counts in correctness denominators.

## Larger experiments and regression gates

The [baseline configuration](examples/openai-baseline.yaml) has four conditions,
24 cases, and two repetitions: 192 measured requests plus four warmups.
Check it offline before sending requests:

```bash
sor validate examples/openai-baseline.yaml --check-cases
sor run examples/openai-baseline.yaml
```

To repeat that case set five times with reproducible condition orders, first
prepare a plan without sending requests. Then execute or resume the printed
plan path in the shell with `OPENAI_API_KEY` set:

```bash
python scripts/replicate_baseline.py --prepare-only
python scripts/replicate_baseline.py --plan runs/replication-.../plan.json
python scripts/compare_replications.py runs/replication-.../plan.json
```

The default plan sends 960 measured requests and 20 warmups, with 15 seconds
between runs. It saves condition order and completed run paths, but not the
API key. Keep `run.seed` fixed to preserve the cases; vary only
`run.condition_order_seed` to permute condition order. The comparison script
rebuilds cross-run JSON and Markdown analysis offline. Its whole-run paired
intervals are exploratory with only five runs and are not causal or
multiple-testing-adjusted evidence.

The completed [load-stress plan](STRESS_PLAN.md) specifies 96 cases and 768
measured requests. Its [results](STRESS_RESULTS.md) include correctness and
quality goodput, with request failures kept visible. To preflight its cases:

```bash
sor validate examples/openai-stress.yaml --check-cases
```

Regression gates can compare a candidate with an absolute threshold or a
complete saved baseline. For example:

```yaml
gates:
  - metric: schema_valid_rate
    kind: minimum
    value: 0.95
    estimate: lower_ci
```

```yaml
baseline_run: ./runs/20261010T120000000000Z_0123456789ab
gates:
  - metric: exact_correct_rate
    kind: max_drop
    value: 0.02
    estimate: point
```

Relative `baseline_run` paths resolve from the experiment YAML's directory
when the run starts and are saved as absolute paths in `config.resolved.yaml`.
`sor run`, `sor summarize`, and `sor report` exit with code `1` for a failed
or unavailable gate, and `2` for setup, artifact, or reporting errors. An
ordinary gate failure leaves the run manifest complete.

## Notes and limits

- The built-in cases cover four structural families. Long outputs use
  synthetic padding; exact correctness includes that padding. The report's
  supplementary non-padding field accuracy helps separate padding errors
  from task-field errors. Strict exact correctness and quality goodput are
  unchanged.
- The smoke configuration uses generator version `1.1.0`, which places inert
  prompt padding before the source JSON. Version `1.0.0` remains available
  for reproducing older runs.
- The smoke configuration requests a final streaming usage chunk with
  `run.stream_include_usage: true` for token throughput and TPOT. If your
  service does not support it, omit the option; unavailable token metrics
  remain missing rather than zero.
- Character-length experiments work with the base installation. For exact
  token targeting, install `python -m pip install -e '.[dev,tokenizer]'`.
  The configured Hugging Face tokenizer and chat template may be downloaded
  on first use; no model weights are loaded.
- Within-run latency comparisons bootstrap matched case IDs for 95%
  intervals on mean E2E, TTFT, and TPOT changes. Single-run throughput ratios
  are descriptive only. Fixed execution order and temporal drift limit
  causal interpretation; see [METRICS.md](METRICS.md).

## Container and project references

Build and verify the container image:

```bash
docker build -t structured-output-reliability .
docker run --rm structured-output-reliability --version
```

To use a host experiment file, mount a writable directory at `/workspace`.
The endpoint URL must be reachable from inside the container, and the mounted
output directory must be writable by the container user (UID 10001).

```bash
docker run --rm \
  -v "$PWD:/workspace" \
  structured-output-reliability \
  validate /workspace/examples/character-experiment.yaml
```

GitHub Actions tests Python 3.11 and 3.14, runs the local smoke test, builds
the wheel and source distribution, and checks the Docker image.

| Reference | What it defines |
|---|---|
| [DESIGN.md](DESIGN.md) | Frozen v0.1 MVP scope and non-goals |
| [CONFIG.md](CONFIG.md) | YAML, case, condition, and run-record formats |
| [METRICS.md](METRICS.md) | Metric formulas, denominators, and comparison method |
| [schemas/](schemas/) | Normative JSON Schemas |
| [LICENSE](LICENSE) | MIT license |
