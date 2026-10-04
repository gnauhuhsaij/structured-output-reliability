# Metric Contract

Status: frozen for MVP v0.1

Metric contract version: 1.0.0

Last updated: 2026-10-04

## 1. Purpose

This document defines the metrics emitted by the project. It is normative for
v0.1: the implementation and tests must follow these definitions, and a change
to a formula or denominator requires a metric-contract version change.

The contract is designed to prevent three common reporting errors:

- excluding failed requests from correctness denominators;
- treating parseable JSON as schema-valid or semantically correct;
- reporting unavailable timing data as zero.

## 2. Evaluation units

### 2.1 Condition

A condition is one fully resolved combination of:

- endpoint and model;
- concurrency;
- prompt-length target;
- expected-output-length target;
- temperature;
- request-seed policy;
- structured-decoding mode;
- optional backend and quantization labels.

Metrics are computed separately for every condition. Results from different
conditions must not be silently pooled.

### 2.2 Case, repetition, and attempt

A `case_id` identifies one generated prompt, schema, and expected JSON value.
A `repeat_id` identifies one complete repetition of the condition. An `attempt`
is one `(condition_id, case_id, repeat_id)` request.

Warmup requests are saved with `phase = "warmup"` but are excluded from all
metrics in this document. Only `phase = "measure"` attempts are measured.

For each condition, record:

- `planned_attempts`;
- `started_attempts`;
- `terminal_attempts`;
- `run_complete`.

The primary denominator `N` is `started_attempts`. Every started attempt must
receive a terminal record, including timeout and cancellation records. If the
process ends before that invariant is satisfied, the run is incomplete and
regression gates must not pass.

Retries are disabled in v0.1. A future retry would be a new attempt and could
not replace or hide the original failure.

## 3. Per-attempt stage results

Scoring is a pipeline. Later stages do not redefine earlier ones.

### 3.1 Request success

`request_success = true` when all of the following hold:

- no client transport exception occurred;
- the request did not exceed its timeout;
- the service returned a successful HTTP status;
- the response or event stream matched the required Chat Completions protocol;
- a streaming response terminated normally.

Empty content, a refusal, `finish_reason = "length"`, malformed JSON content, or
a semantically wrong answer does not turn an otherwise valid API exchange into
a request failure. Those conditions are recorded separately.

### 3.2 Content extraction

The assistant content is reconstructed exactly from the protocol response. Role
events, empty stream events, usage-only events, and reasoning fields are not
appended to assistant content.

No Markdown fence removal, substring extraction, brace matching, or repair is
performed for the primary metrics.

### 3.3 Strict JSON parsing

`json_parsed = true` only when the complete assistant content is one valid JSON
value, ignoring only leading and trailing JSON whitespace.

The parser must reject:

- prose before or after the value;
- Markdown code fences;
- duplicate object keys;
- `NaN`, `Infinity`, and `-Infinity`;
- truncated content;
- invalid encoding or syntax.

The parsed value may be any JSON type. The case schema determines whether that
type is acceptable.

Optional diagnostics may attempt repair or embedded-JSON extraction, but repaired
values never count toward primary parse, schema, field, or exact correctness.

### 3.4 JSON Schema validation

`schema_valid = true` only when `json_parsed = true` and the parsed value passes
the exact schema attached to the case.

The MVP uses JSON Schema Draft 2020-12 and a pinned validator version. Generated
schemas must be self-contained; remote references are prohibited. Schemas are
validated before a run begins. An invalid test schema is an experiment setup
error, not a model failure.

### 3.5 Field comparison

Expected leaf values are addressed by JSON Pointer. Each expected leaf is one
field unit. An expected empty object or empty array is itself one field unit so
that every generated case has at least one scorable unit.

Default comparisons are:

- string: exact Unicode value;
- boolean: exact JSON boolean type and value;
- null: exact null;
- integer: exact integer type and value;
- number: exact numeric value unless the case declares absolute or relative
  tolerance;
- array: order-sensitive;
- object: recursive comparison by key.

No string-to-number, number-to-boolean, or other type coercion is allowed. In
particular, JSON `true` is not equal to JSON `1`.

If a field is missing, or an ancestor has the wrong type, every affected
expected leaf is incorrect. Field comparison runs on every parsed value even if
the value is schema-invalid; this preserves diagnostic information.

Unexpected fields and array items are not added to the field denominator. They
are detected by the schema when prohibited and always cause exact-object
comparison to fail.

### 3.6 Exact-object comparison

`exact_correct = true` only when:

- `schema_valid = true`; and
- actual and expected JSON values have the same recursive structure, types, and
  values under the case's declared numeric and array comparison policies.

Object key order and insignificant JSON whitespace do not matter. Unexpected
object keys or array items make the result inexact even if the schema permits
them.

## 4. Primary correctness metrics

All primary rates include every measured attempt in their denominator.

| Metric | Numerator | Denominator | Failed attempt behavior |
|---|---|---|---|
| `request_success_rate` | attempts with `request_success` | `N` | counted as false |
| `json_parse_rate` | attempts with `json_parsed` | `N` | counted as false |
| `schema_valid_rate` | attempts with `schema_valid` | `N` | counted as false |
| `exact_correct_rate` | attempts with `exact_correct` | `N` | counted as false |

Every report must display both the ratio and `(numerator, denominator)`.

Conditional diagnostics may include:

```text
schema_valid_given_parse = schema_valid / json_parsed
exact_correct_given_schema = exact_correct / schema_valid
```

They must be labelled conditional and must never replace the primary rates.
When a conditional denominator is zero, the value is unavailable rather than
zero.

## 5. Field-level accuracy

Let `F_a` be the number of expected field units for attempt `a`, and `C_a` the
number correctly reproduced. For any unparsed attempt, `C_a = 0` while `F_a`
remains the case's expected field count.

```text
micro_field_accuracy = sum(C_a) / sum(F_a)
```

For macro accuracy, first average repetitions within each case, then give every
case equal weight:

```text
attempt_field_accuracy(a) = C_a / F_a
case_field_accuracy(c) = mean(attempt_field_accuracy for attempts of case c)
macro_field_accuracy = mean(case_field_accuracy over cases)
```

Micro accuracy emphasizes cases with more fields. Macro accuracy gives every
case equal weight. Both are required.

## 6. Repeat consistency

Consistency is calculated only when a condition has at least two planned
repetitions per case.

For each case, construct all unordered pairs of measured repetitions. A pair is
consistent only when both attempts parsed successfully and their parsed JSON
values are semantically equal under the case comparison policy. A pair involving
a request failure or parse failure is inconsistent, including two identical
failures.

```text
case_consistency = consistent_pairs / all_planned_repeat_pairs
repeat_consistency_rate = mean(case_consistency over cases)
```

This is intentionally independent of correctness: two equal but wrong JSON
objects are consistent. Reports should show consistency next to exact accuracy,
not combine them.

The diagnostic `all_repeats_correct_case_rate` is the fraction of cases for
which every measured repetition is exactly correct. It is unavailable for an
incomplete run.

## 7. Timing metrics

All client timings use a monotonic high-resolution clock and are stored in
nanoseconds. Reports convert them to milliseconds.

### 7.1 End-to-end latency

```text
e2e_latency = terminal_response_time - network_start_time
```

`network_start_time` is captured immediately before initiating the HTTP
operation, after the attempt acquires its concurrency slot. Time waiting for a
slot is recorded separately as `client_queue_time` and is not part of primary
end-to-end latency.

Primary end-to-end latency distributions include request-successful attempts
only. An all-attempt observed-duration diagnostic may also be reported so
timeouts and errors remain visible.

### 7.2 Time to first token

TTFT is available only for streaming attempts:

```text
TTFT = first_content_time - network_start_time
```

`first_content_time` is the arrival of the first non-empty assistant-content
delta. Role-only, reasoning-only, and usage-only events do not end TTFT.

If no content arrives, or the request is non-streaming, TTFT is unavailable.

### 7.3 Time per output token

For a successful streaming attempt with at least two output tokens:

```text
TPOT = (e2e_latency - TTFT) / (output_tokens - 1)
```

TPOT is unavailable—not zero—when TTFT is unavailable or fewer than two output
tokens were produced.

### 7.4 Token counts

Use server-reported completion-token usage when present. Otherwise use the
configured local tokenizer. Each attempt records `token_count_source` as
`server`, `local_tokenizer`, or `unavailable`.

Token-derived metrics must not pool attempts using incompatible tokenizers or
silently compare server and local counts as if they came from one source.

### 7.5 Percentiles

Report median and p95 end-to-end latency, TTFT, and TPOT where applicable. The
point estimate uses linear quantile interpolation.

A percentile is unavailable with zero eligible observations. A p95 based on
fewer than 20 eligible observations is suppressed; with 20–99 observations it
is emitted with `low_confidence = true`.

## 8. Throughput and goodput

For one complete condition repetition, the measured wall interval begins at
the first measured request's `network_start_time` and ends at the final measured
attempt's terminal time.

```text
attempt_throughput = measured_attempts / measured_wall_seconds
request_throughput = successful_requests / measured_wall_seconds
output_token_throughput = successful_request_output_tokens / measured_wall_seconds
```

`attempt_throughput` captures offered work completed by the client loop.
`request_throughput` excludes request failures. Output-token throughput is
unavailable if required token counts are unavailable.

### 8.1 Quality-aware goodput

An attempt qualifies for quality goodput when:

- `exact_correct = true`; and
- every configured latency SLO is measurable and satisfied.

```text
quality_goodput = qualifying_attempts / measured_wall_seconds
```

With no configured latency SLO, quality goodput is the rate of exactly correct
responses. If an SLO refers to an unavailable metric—for example TTFT in a
non-streaming run—quality goodput is unavailable for that condition.

Supported v0.1 request-level SLOs are maximum E2E latency, TTFT, and TPOT.

## 9. Confidence intervals

The default confidence level is 95%. Every interval records its method,
confidence level, sample count, and lower and upper bounds.

### 9.1 Binary attempt rates

`request_success_rate`, `json_parse_rate`, `schema_valid_rate`, and
`exact_correct_rate` use a two-sided Wilson score interval with
`z = 1.959963984540054`.

When `N = 0`, the estimate and interval are unavailable.

### 9.2 Field accuracy, consistency, and latency

These metrics use a deterministic percentile cluster bootstrap:

- sampling unit: `case_id`;
- all repetitions and observations belonging to a sampled case stay together;
- resamples: 10,000;
- interval: 2.5th and 97.5th bootstrap percentiles;
- seed: derived from the experiment seed, condition ID, metric name, and metric
  contract version.

The cluster method prevents repeated requests for one case from being treated as
independent cases.

Intervals are unavailable with fewer than two distinct cases. Reports mark them
`low_confidence` with fewer than 20 distinct cases.

### 9.3 Throughput and goodput

Throughput and goodput intervals bootstrap complete condition repetitions, not
individual requests, because their denominators are shared wall-clock intervals.

- fewer than three complete repetitions: interval unavailable;
- three or four repetitions: interval emitted with `low_confidence = true`;
- five or more repetitions: ordinary bootstrap interval;
- resamples: 10,000 with a deterministic derived seed.

The point estimate is the total qualifying count across repetitions divided by
the total measured wall time across repetitions. The interval resamples complete
repetitions and recomputes that ratio.

## 10. Error and diagnostic fields

Each attempt stores independent stage fields plus one primary outcome. Primary
outcome precedence is:

```text
cancelled
timeout
transport_error
http_error
protocol_error
empty_content
invalid_json
schema_invalid
field_incorrect
exact_correct
```

The primary outcome is for grouping and debugging; metric computation uses the
independent booleans and counts. For example, an HTTP-successful empty response
has `request_success = true`, `json_parsed = false`, and primary outcome
`empty_content`.

Additional diagnostics include:

- HTTP status;
- sanitized exception type and message;
- refusal flag;
- finish reason;
- truncation flag;
- response bytes;
- input/output token counts and their sources;
- client queue time;
- raw chunk count;
- whether the requested seed and structured-decoding mode were sent.

## 11. Aggregation and missing data

- `null` means unavailable; zero means a measured zero.
- Failed attempts are zeroes for primary correctness and field metrics, not
  missing observations.
- Failed attempts are excluded from primary successful-request latency
  distributions but remain visible in success rates and error diagnostics.
- Conditions with different parameter values are never pooled.
- Warmups are never included.
- Incomplete runs may produce exploratory summaries but cannot satisfy gates or
  serve as baselines.
- Every aggregate stores its numerator/denominator or eligible observation
  count.

## 12. Baseline comparisons and gates

Baseline and candidate runs must share the metric-contract major version and
the same generated case-set hash. Reports may show unmatched conditions, but a
regression gate compares only exactly matched condition IDs.

Gates declare whether they use the point estimate or a conservative confidence
bound:

- minimum quality gate: conservative mode evaluates the lower bound;
- maximum latency gate: conservative mode evaluates the upper bound;
- maximum allowed drop from baseline: compare `candidate - baseline`, with a
  negative value representing degradation for quality metrics;
- maximum allowed increase from baseline: compare candidate relative to
  baseline for latency metrics.

No default threshold is inferred. Every threshold and gate policy must be
present in the resolved configuration and reproduced in the report.

## 13. Required aggregate names

The following names are stable for v0.1:

```text
request_success_rate
json_parse_rate
schema_valid_rate
micro_field_accuracy
macro_field_accuracy
exact_correct_rate
repeat_consistency_rate
median_e2e_latency_ms
p95_e2e_latency_ms
median_ttft_ms
p95_ttft_ms
median_tpot_ms
p95_tpot_ms
attempt_throughput_rps
request_throughput_rps
output_token_throughput_tps
quality_goodput_rps
```

Machine-readable results must use these names. Display labels may be more
descriptive.

## 14. Normative examples

### 14.1 Correctness denominators

Suppose a condition has 100 attempts:

- 2 transport failures;
- 3 successful API responses with invalid JSON;
- 5 parsed responses that violate the schema;
- 10 schema-valid responses with wrong expected values;
- 80 exactly correct responses.

Then:

```text
request_success_rate = 98 / 100 = 0.98
json_parse_rate       = 95 / 100 = 0.95
schema_valid_rate     = 90 / 100 = 0.90
exact_correct_rate    = 80 / 100 = 0.80
```

The schema-valid rate is not `90/95`, although `90/95` may be shown separately
as `schema_valid_given_parse`.

### 14.2 Repeat consistency

For one case with three repetitions producing parsed values `A`, `A`, and `B`,
there are three unordered pairs. One pair matches:

```text
case_consistency = 1 / 3
```

If the third repetition is a timeout instead of `B`, the value remains `1/3`:
the two pairs involving the failed attempt are inconsistent.

### 14.3 Unavailable timing

A non-streaming response completes in 500 ms with 20 output tokens:

```text
e2e_latency_ms = 500
ttft_ms = null
tpot_ms = null
```

Neither TTFT nor TPOT may be reported as zero.
