# Five-run OpenAI baseline (2026-10-15)

This is an exploratory result from one model service, one generated case set,
and five sequential runs. It is a reproducible example of the tool's analysis,
not evidence that one decoding mode is universally faster or more reliable.

## Experiment

| Setting | Value |
|---|---|
| Endpoint | OpenAI-compatible Chat Completions API (`https://api.openai.com/v1`) |
| Model | `gpt-4o-mini-2024-07-18` |
| Cases | 24 distinct generated cases: six each of `flat_object`, `nested_object`, `array_object`, and `mixed_types` |
| Case-set SHA-256 | `d808a8fd37012aecd765510243889a5570a05b3e3495eb213f0e4e7c8ea94843` |
| Matrix | Concurrency 1 or 4 × prompt-only or strict JSON Schema response format |
| Other settings | 2,048-character prompt target; 256-character expected-output target; `max_completion_tokens: 512`; temperature 0; request seed 42; streaming with server token usage |
| Repetitions | Two measured attempts per case and condition, plus one warmup per condition |
| Run order | Five distinct deterministic condition-order seeds: 0, 1, 2, 3, 4 |
| Total | 960 measured attempts and 20 warmups |

The baseline configuration is
[`examples/openai-baseline.yaml`](examples/openai-baseline.yaml). The exact
five-run workflow and offline comparison are in
[`scripts/replicate_baseline.py`](scripts/replicate_baseline.py) and
[`scripts/compare_replications.py`](scripts/compare_replications.py).

## Observed results

All 960 measured attempts were exactly correct; there were no request
failures. This is an observed count, not proof that the service's true error
rate is zero. Each condition averaged 49.25 output tokens over its 240
measured attempts, so the latency differences below are not explained by
different mean output lengths.

Positive latency changes mean condition B was slower than A. Each run's change
averages matched case-level differences. The across-run mean and 95% Student-t
interval use the **five whole-run changes** as observations (four degrees of
freedom), not the 960 individual requests. Throughput is the geometric mean
of each run's B/A request-throughput ratio; its interval is computed on the
log-ratio scale.

| Comparison (A → B) | Run 1 E2E | Run 2 E2E | Run 3 E2E | Run 4 E2E | Run 5 E2E | Mean E2E change [95% interval] | Throughput ratio [95% interval] |
|---|---:|---:|---:|---:|---:|---:|---:|
| Concurrency 1 → 4, JSON Schema | +56.1 ms | −157.0 ms | +101.6 ms | −3.6 ms | +18.9 ms | +3.2 ms [−118.5, +124.9] | 3.61× [3.22, 4.04] |
| Concurrency 1 → 4, prompt-only | +45.4 ms | −97.2 ms | +241.7 ms | +75.9 ms | +98.7 ms | +72.9 ms [−77.7, +223.5] | 3.31× [2.63, 4.18] |
| Prompt-only → JSON Schema, concurrency 1 | −63.3 ms | +40.1 ms | −29.2 ms | +11.4 ms | +12.5 ms | −5.7 ms [−56.1, +44.7] | 1.01× [0.96, 1.05] |
| Prompt-only → JSON Schema, concurrency 4 | −52.7 ms | −19.6 ms | −169.3 ms | −68.1 ms | −67.3 ms | −75.4 ms [−145.0, −5.8] | 1.09× [0.92, 1.30] |

Concurrency 4 consistently delivered more requests per second than
concurrency 1, though the observed speedup was below the ideal 4×. Its mean
E2E latency change varied substantially by run. At concurrency 4, JSON Schema
mode had lower mean E2E latency in all five runs; this is a follow-up signal,
not a settled performance claim. The five-run TPOT comparison for that pair
also favored JSON Schema (mean −1.6 ms per output token; exploratory 95%
interval [−2.8, −0.4] ms), while its TTFT interval included zero.

## Interpretation limits

- Five sequential runs in one testing session are a small sample of possible
  service states; condition order was varied, but conditions were not
  interleaved within runs. Temporal drift remains possible.
- The intervals assume roughly independent, approximately normal run-level
  differences and are **not adjusted for multiple comparisons**. A single
  interval excluding zero should not be treated as a confirmatory finding.
- The cases, model, endpoint, and decoding settings are fixed. The 100%
  observed correctness rate creates a ceiling effect for this workload; a
  harder, pre-specified workload is needed to investigate reliability drops.
- Raw run artifacts are intentionally excluded from Git because they contain
  complete requests and responses. The aggregate values above are public;
  readers can reproduce the procedure with their own service credentials,
  but cannot independently re-score these historical requests from this repo.

To reproduce the workflow, first validate the cases offline, then follow the
replication commands in the [README](README.md). Never put an API key in YAML,
Git, or a visible shell command.
