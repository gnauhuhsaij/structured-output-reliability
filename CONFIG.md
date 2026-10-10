# Configuration and Record Contract

Status: frozen for MVP v0.1

Configuration contract version: 1.0.0

Last updated: 2026-10-04

## 1. Purpose

This document defines the YAML experiment input and the JSONL records preserved
by every run. It is normative for v0.1. The machine-readable JSON Schemas are in
[`schemas/`](schemas/), and a complete input is in
[`examples/experiment.yaml`](examples/experiment.yaml).

The contract has three goals:

- the same resolved input produces the same cases and condition order;
- every measured attempt remains independently auditable;
- reports can be rebuilt without contacting the model service.

## 2. Experiment YAML

The input document is validated by `schemas/experiment.schema.json`. Unknown
keys are rejected. Schema defaults are documentation only: the loader must apply
them explicitly and save the result as `config.resolved.yaml`.

### 2.1 Endpoint and credentials

`endpoint.base_url` identifies an already running OpenAI-compatible service.
`endpoint.path` defaults to `/chat/completions`. `endpoint.model` is sent in each
request.

`endpoint.api_key_env` contains an environment-variable name, never a key. The
loader may read that variable at execution time, but its value must not appear
in the resolved configuration, request records, logs, or reports. A missing
named variable is a setup error before measured traffic starts.

`endpoint.quantization` is optional comparison metadata. The runner does not
provision or inspect quantization.

### 2.2 Case generation and length units

`cases.count` is the number of unique cases generated for each
`(prompt_length, output_shape)` cell. The total number of records in
`cases.jsonl` is therefore `count * len(prompt_lengths) * len(output_shapes)`.
Each condition runs only the cases from its matching length cell, so every
condition has the same planned case count. `cases.families` selects built-in
deterministic generator families; families are assigned round-robin in listed
order within each cell. Each family must have a versioned implementation and
must generate a self-contained Draft 2020-12 JSON Schema, an expected JSON
value, and a prompt that asks for that value.

`cases.length_unit` is either:

- `tokens`: requires `tokenizer`; generated prompt lengths are exact under the
  configured tokenizer;
- `characters`: does not require a tokenizer and is labelled non-exact because
  character counts are only a proxy for model context length.

`matrix.prompt_lengths` supplies the target for the chosen unit.
`matrix.output_shapes[].target_length` describes the target size of the expected
serialized JSON in the same unit. `max_completion_tokens` is the separate API
limit. The generator records actual lengths; it must not claim that a requested
limit equals actual model output length.

For token targets, prompt length includes the configured tokenizer's chat
template. If that template is unavailable, token targeting is a setup error
rather than a reason to silently count only message text.

Character prompt length is the sum of Unicode code points in each message's
`content`; role names and the request wrapper are excluded. Character output
length is the number of Unicode code points in the canonical expected JSON.
The generator still makes `actual` equal `target` for character cases, while
the record's `exact` field remains false to prevent treating that proxy as an
exact model-token measurement.

Token prompt length is the length returned after applying the tokenizer's chat
template with `add_generation_prompt=true`. Token output length is measured by
encoding the canonical expected JSON without added special tokens. An exact
target that cannot be reached by deterministic padding is a setup error.

### 2.3 Run behavior

The runner uses closed-loop fixed concurrency. Every Cartesian-product entry in
`matrix` becomes one condition. The fixed axis order is `concurrency`,
`prompt_lengths`, `output_shapes`, `temperatures`, `request_seeds`, then
`structured_modes`; values retain YAML list order, and the rightmost axis varies
fastest. Duplicate axis values are rejected.

`run.seed` is the root seed for case generation and deterministic execution
order. `run.repetitions` is the number of measured repetitions per case.
`run.warmup_requests_per_condition` is saved but excluded from metrics.
`run.stream` defaults to true. Retries do not exist in contract version 1.0.0.

### 2.4 Structured-output modes

Every `matrix.structured_modes` entry references one unique item in
`structured_modes`:

- `prompt_only` adds no API constraint;
- `response_format` sends the case schema through the portable JSON Schema
  response-format field;
- `extra_body` deep-copies a declarative object and inserts the case schema at
  `schema_json_pointer` before merging it into the request body.

`extra_body` is backend-specific and is labelled that way in reports. Its JSON
Pointer must select a location inside `body`; it cannot reference files, invoke
callbacks, interpolate shell commands, or execute code.

For every case, the base request body is constructed from `model`, `messages`,
`temperature`, `stream`, `max_completion_tokens`, and `seed` when the seed is
not null. `response_format` adds:

```json
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "<case_id>",
      "strict": true,
      "schema": "<case.json_schema>"
    }
  }
}
```

The configured `strict` value replaces `true` above. An `extra_body` fragment is
merged only after schema insertion and may not overwrite core request fields.

### 2.5 SLOs, baselines, and gates

Optional `slos` define the latency requirements used by quality-aware goodput.
TTFT and TPOT SLOs are valid only for streaming runs.

An optional `baseline_run` points to a prior run directory. Gates with
`max_drop` or `max_increase_ratio` compare against that baseline. `minimum` and
`maximum` gates can run without one. `estimate` selects the point estimate or a
confidence-interval bound, allowing conservative CI gates such as a minimum on
`lower_ci`.

Gate predicates are inclusive. `minimum` requires `estimate >= value`;
`maximum` requires `estimate <= value`; `max_drop` requires the fractional drop
`(baseline - current) / baseline <= value`; and `max_increase_ratio` requires
`current / baseline <= value`. A zero baseline makes either relative predicate
unavailable and therefore unable to pass.

## 3. Semantic validation

JSON Schema validates document shape. Before generating cases, the loader must
also reject a configuration when:

1. structured-mode IDs are not unique;
2. a matrix structured-mode ID is undefined;
3. token lengths are requested without a tokenizer;
4. a TTFT or TPOT SLO is set while streaming is disabled;
5. a baseline-relative gate is present without `baseline_run`;
6. an `extra_body` pointer is invalid, escapes its object, or would overwrite a
   non-placeholder scalar;
7. an `extra_body` fragment would overwrite a core request field;
8. a secret value appears in a field intended to contain only an environment
   variable name.

Schema errors and semantic errors are setup failures. They do not produce model
attempts or enter metric denominators.

## 4. Stable identifiers

Before hashing, objects are serialized as UTF-8 canonical JSON with sorted
object keys, no insignificant whitespace, and JSON-native values. Hashes use
SHA-256 and lower-case hexadecimal.

- `case_id` is `case_` plus the first 16 hex characters of the canonical case
  payload excluding `case_id`.
- `condition_id` is `cond_` plus the first 16 hex characters of the resolved
  condition payload excluding `condition_id`, including the configuration and
  metric contract versions.
- `comparison_key` is `cmp_` plus the first 16 hex characters of the condition
  comparison payload. It includes the case-set hash, load dimensions, request
  behavior, contract versions, and SLOs, but excludes endpoint URL, model,
  credential-variable name, quantization label, tokenizer identity, warmup
  count, and execution-order seed. Baseline conditions pair only when this key
  is equal.
- `attempt_id` is `attempt_` plus the first 16 hex characters of the canonical
  tuple `(run_id, condition_id, case_id, repeat_id, phase, sequence_index)`.
- `run_id` is a UTC basic timestamp with six fractional-second digits followed
  by the first 12 hex characters of the resolved-configuration hash. The
  timestamp distinguishes intentional reruns while the suffix proves their
  configuration identity. A writer must refuse to overwrite an existing run
  directory.

Each condition's `case_set_sha256` is the full SHA-256 of the canonical JSON
array of the complete case records selected for that condition, in generated
order. It is independent of JSONL whitespace and line endings. The manifest
separately records `generated_cases_sha256` for the complete `cases.jsonl` set.

The shortened hashes are labels, not security boundaries. Full hashes are
stored in the run manifest.

## 5. Run directory

A run is append-safe and contains at least:

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

`cases.jsonl`, `conditions.jsonl`, and `responses.jsonl` are validated by their
matching schemas. One physical line is one complete UTF-8 JSON object. Writers
flush at the configured interval and never rewrite an earlier response line.

`manifest.json` records full configuration and case-set hashes, tool version,
contract versions, Python version, dependency versions relevant to scoring,
tokenizer identity when present, start/end times, completion state, and artifact
checksums.

## 6. Case records

Each `cases.jsonl` line contains:

- generator identity and seed;
- the exact Chat Completions messages;
- a self-contained Draft 2020-12 schema;
- the expected JSON value and comparison policy;
- target and actual prompt/output lengths;
- expected field count and JSON Pointer paths.

The messages, schema, expected value, and comparison policy are sufficient to
rescore a saved response. Generated schemas are validated before traffic begins.

## 7. Condition records

Each `conditions.jsonl` line is one completely expanded matrix condition. It
contains no unresolved defaults or references. It records endpoint identity,
run parameters, length targets, decoding settings, the resolved structured mode,
and the case-set hash. The API key value is never included.

`condition_id` identifies the exact condition, including the service being
tested. `comparison_key` deliberately excludes service identity so that the same
workload can be paired across two model or serving configurations. A missing or
duplicate baseline match is a comparison error, not permission to compare the
nearest-looking condition.

Changing any behavior that can affect requests or metric interpretation must
change the condition hash. Cosmetic report settings do not.

## 8. Attempt records

Each `responses.jsonl` line is a terminal record for exactly one started attempt.
It joins request evidence, raw protocol evidence, timing, usage, and scores with
`run_id`, `condition_id`, and `case_id`.

The request body and response payload are preserved, but authentication headers
and other secrets are removed. For streaming responses, parsed SSE event payloads
and arrival offsets are saved when `output.include_stream_events` is true. Raw
bytes need not be retained in v0.1.

`score.json_parsed` disambiguates a successfully parsed JSON `null` from the
absence of a parsed value. Later-stage booleans are false when a prerequisite
stage failed. Field totals remain populated for failed attempts so the primary
denominator cannot shrink.

The writer and offline reader both enforce these cross-field invariants:

- `0 <= correct_fields <= total_fields`;
- `schema_valid` implies `json_parsed`, and `exact_correct` implies
  `schema_valid`;
- an unavailable token count is JSON `null`, and a known `total` equals known
  `input + output`;
- `primary_outcome` follows the precedence in `METRICS.md`;
- non-streaming attempts and streams with no content use `null` TTFT and TPOT;
- authentication data is absent from the saved request body and diagnostics.

Timing durations are monotonic-clock nanoseconds. Wall-clock timestamps exist
for provenance only. Unavailable TTFT or TPOT values are JSON `null`, never zero.

## 9. Versioning

Every record carries its own schema version. Additive changes require a minor
version; incompatible meaning or required-field changes require a major version.
A metric formula change separately requires a new metric-contract version.

Readers must reject unsupported major versions and may ignore explicitly
documented additive fields only after a schema upgrade. They must never guess a
metric meaning from an unknown version.
