# Pre-specified stress experiment (v1)

Status: pre-specified before the first full live run. A complete run finished
on 2026-10-15; see [`STRESS_RESULTS.md`](STRESS_RESULTS.md). The protocol below
is preserved as planned. If the YAML or analysis changes afterward, record
the reason and treat the changed run as a new experiment.

## Question and comparison

The primary question is: for the same generated cases in strict JSON Schema
mode, how do **exact-correct rate** and **quality goodput** change when closed-
loop concurrency rises from 1 to 8? This deliberately considers correctness
and useful throughput together. The primary contrast is `concurrency 8 − 1`
for `exact_correct_rate` and the `concurrency 8 / 1` ratio for
`quality_goodput_rps`.

This is an estimation exercise, not a pre-declared superiority or equivalence
test. Report the counts and uncertainty even if the point estimates are equal.
Do not infer equal underlying reliability from two observed 100% rates.

Secondary outcomes are request success, JSON parse rate, Schema valid rate,
field accuracy, repeat consistency, TTFT, TPOT, E2E latency, request
throughput, and output-token throughput. The prompt-only conditions are
secondary comparators for the cost or benefit of constrained decoding. Any
other interesting contrast discovered after looking at the run is exploratory.

## Fixed workload

[`examples/openai-stress.yaml`](examples/openai-stress.yaml) specifies:

- `gpt-4o-mini-2024-07-18` at the OpenAI Chat Completions endpoint;
- 96 distinct deterministic cases, 24 per family, generated with seed
  `20261015` and generator version `1.1.0` (case-set SHA-256:
  `782efebfdc5e797898834e3db08fdc0dc5575c4c0768534deca511a7faafb8d1`);
- a target prompt length of 8,192 characters and a target expected JSON length
  of 512 characters, with a 1,024-completion-token ceiling;
- temperature 0, request seed 42, streaming and server-reported token usage;
- concurrency 1 and 8 × prompt-only and strict JSON Schema mode;
- two measured repetitions per case and condition, plus one warmup per
  condition; condition-order seed 0.

The full plan is **four conditions, 96 cases, 768 measured attempts, and four
warmups**. Cases are matched across all conditions. This is one testing
session, not evidence about different days, models, or service providers.
The longer targets increase request and response size, but the four generator
families retain the same field/path counts as the baseline. This tests
serving-load reliability more than semantic extraction difficulty; another
100% result would motivate new case families, not a claim of universal
correctness.

## Counting and interpretation rules

All 192 measured attempts in a condition stay in the denominator of its
primary exact-correct rate. A timeout, transport error, HTTP error, invalid
JSON, Schema violation, or incorrect field is not silently discarded. Report
those failure classes separately: 429/5xx/timeout is evidence about service
availability under load, not automatically about the model's ability to
generate valid JSON. Failed requests contribute no quality goodput.

Use the project's existing per-condition Wilson intervals for binary attempt
rates and its existing throughput/goodput definitions. With only two complete
repetitions per condition, quality goodput is a **descriptive point estimate**:
the project does not emit a throughput/goodput interval until there are at
least three repetitions. For this single run, show the matched-case latency
comparisons as exploratory; do not attach a request-level interval to
whole-condition throughput. In particular, 192
attempts per condition are not 192 independent case types: there are 96 cases
repeated twice. This pilot is not powered to prove a small reliability
difference or equivalence if all conditions score 100%.

Do not stop early because a result looks favorable or unfavorable. Abort only
for a setup/safety problem such as invalid credentials, unexpected spend,
or sustained endpoint rejection; mark an aborted run incomplete and do not
report it as a finished comparison.

## Before live traffic

Run `sor validate examples/openai-stress.yaml --check-cases` to generate and
validate every case in memory without sending or saving requests. Inspect the
four condition count, 768 measured attempts, four warmups, and case-set hash.
Then decide whether the live request volume and service rate limits are
acceptable. Keep the API key in an environment variable, never in this YAML
or a visible shell command. The earlier five-run baseline remains unchanged.
