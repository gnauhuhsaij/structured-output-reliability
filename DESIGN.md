# Structured-Output Reliability Under Load

Status: MVP scope frozen for v0.1

Last updated: 2026-10-04

## Problem

Model-serving benchmarks usually answer either “is it fast?” or “is the answer
correct?” They rarely show whether structured-output correctness degrades under
load, or preserve the per-request evidence needed to connect a malformed answer
to its latency and experiment condition.

This project is a small, reproducible regression tool that asks:

> As concurrency, context length, requested output size, and decoding settings
> change, does an OpenAI-compatible model service continue to return timely,
> schema-valid, and semantically correct structured outputs?

It is deliberately not a general evaluation framework.

## Primary user and workflow

The primary user is an engineer comparing two model-serving configurations,
model versions, quantizations, or structured-decoding modes.

The intended workflow is:

1. Provide a service URL and YAML experiment file.
2. Generate deterministic cases with known JSON answers.
3. Run the same cases under a small matrix of load and decoding conditions.
4. Preserve every request, response, error, timing, and score.
5. Produce machine-readable summaries and a Markdown report.
6. Optionally compare with a baseline and fail CI on a regression.

The target service is already running. Server deployment and lifecycle management
are outside the MVP.

## MVP scope

### Inputs

- one OpenAI-compatible base URL;
- one YAML experiment configuration;
- an API key obtained from a named environment variable;
- deterministic, program-generated test cases;
- an optional local tokenizer for exact prompt/output length targeting.

### Experiment dimensions

- fixed concurrency;
- prompt-length target;
- expected-output-length target;
- temperature;
- request seed;
- structured decoding on/off;
- endpoint/model configuration;
- optional quantization label supplied as metadata.

The MVP uses closed-loop fixed concurrency. Request-rate distributions,
trace replay, and distributed load generation are deferred.

### Metrics

- request success rate;
- strict JSON parse rate;
- JSON Schema valid rate;
- field-level accuracy;
- exact-object accuracy;
- repeat consistency;
- end-to-end latency, TTFT, TPOT, and p95 latency;
- request and output-token throughput;
- quality-aware goodput;
- 95% confidence intervals.

Exact formulas, denominators, edge cases, and interval methods are defined in
[`METRICS.md`](METRICS.md). Primary correctness metrics never silently exclude
failed requests.

### Outputs

Each run produces:

- resolved configuration and run manifest;
- generated cases as JSONL;
- complete per-attempt responses as JSONL, including failures;
- aggregate JSON and CSV;
- a Markdown report.

Saved artifacts must be sufficient to recompute scores and reports without
calling the model again. Secrets must never be persisted.

## Compatibility boundary

### API

v0.1 targets only OpenAI-compatible Chat Completions:

```text
POST {base_url}/chat/completions
```

Responses API, legacy Completions, tool calls, and multi-turn conversations are
not part of v0.1.

### Streaming

Streaming is the default because TTFT and TPOT depend on chunk arrival times.
Non-streaming experiments remain valid, but TTFT and TPOT must be reported as
unavailable rather than zero.

### Structured decoding

The MVP recognizes three modes:

1. `prompt_only`: request JSON through the prompt without an API constraint.
2. `response_format`: send a JSON Schema through the service's compatible
   response-format field.
3. `extra_body`: merge a declarative backend-specific request fragment supplied
   in YAML, for example a vLLM structured-output option.

Only the first two modes are considered portable. Reports must identify
`extra_body` conditions as backend-specific. Configuration cannot execute user
code or callbacks.

### Length control

Exact token-length sweeps require a configured tokenizer. Without one, the tool
may target character length and use server-reported usage when available, but it
must label the condition approximate and must not claim exact cross-model token
equivalence.

Requested output length means the target size of the generated expected object
plus the API completion limit. It is not assumed to equal the model's actual
output length.

## Reproducibility requirements

- One experiment seed derives all case and execution-order seeds.
- The same generator version, configuration, and seed produce identical cases.
- Case and condition order are deterministic.
- Warmup traffic is identified and excluded from measured aggregates.
- Retries are disabled by default; failures remain measurements.
- Partial raw results are flushed incrementally.
- Run metadata includes tool, schema, Python, tokenizer, model, and configuration
  versions or identities when available.
- Correctness and performance remain joinable at the individual attempt level.

## Explicit non-goals

The MVP will not include:

- a Web UI, hosted service, or public leaderboard;
- model training or fine-tuning;
- automatic model deployment, autoscaling, or quantization;
- distributed or multi-host load generation;
- multimodal or multi-turn evaluation;
- function-calling evaluation;
- large private or manually labelled datasets;
- an LLM judge;
- broad natural-language capability evaluation;
- cost accounting;
- automatic support for every vendor-specific API;
- dashboards or a historical result database.

Quantization is a label for comparing services already provisioned by the user,
not a feature implemented by the runner.

## Implementation boundary

The design keeps four concerns separate:

1. deterministic case generation;
2. endpoint execution and timing;
3. per-response scoring;
4. aggregation, comparison, and reporting.

This allows the scorer or workload to be contributed to vLLM without requiring
maintainers to adopt an unrelated evaluation framework.

The preferred path is:

1. audit vLLM's structured-output benchmark and `perf-eval`;
2. propose the smallest useful upstream contribution;
3. implement against that boundary;
4. publish independently only if the generic endpoint scope does not fit
   upstream.

## MVP acceptance criteria

v0.1 is complete only when:

1. One command runs a YAML-defined experiment against an already running Chat
   Completions endpoint.
2. A fixed seed produces identical generated cases.
3. Every measured attempt is saved, including transport, API, parse, schema, and
   semantic failures.
4. JSON parsing is strict and performs no repair for the primary metric.
5. JSON Schema validity and field correctness are scored independently.
6. Streaming runs measure TTFT and TPOT; non-streaming runs mark them unavailable.
7. Summaries include sample counts and confidence intervals.
8. JSON, CSV, and Markdown results can be regenerated offline.
9. A deterministic mock server covers success, malformed JSON, schema failure,
   semantic error, timeout, and delayed-stream paths.
10. A configured regression gate can fail a CI job.
11. At least two real serving configurations are compared.
12. The README reproduces a small end-to-end run with one command.

## Deferred decisions

These decisions do not block the design and must not expand v0.1:

- final package and executable name;
- Responses API and Completions API support;
- open-loop Poisson traffic and trace replay;
- server-side telemetry;
- vendor adapters beyond declarative `extra_body`;
- semantic grading of free text;
- schema fuzzing beyond the built-in generated families;
- automatic endpoint provisioning;
- hosted reporting.
