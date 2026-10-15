# v0.1.0 release notes (draft)

This release is not tagged or published yet.

Structured Output Reliability is a small CLI for reproducible regression
tests of OpenAI-compatible Chat Completions services. It jointly records
structured-output correctness and serving performance under a configured
matrix of conditions.

## Included in v0.1.0

- YAML validation, deterministic case generation, and an offline
  `--check-cases` preflight that writes no case file or sends no requests.
- Fixed-concurrency execution with warmups, streaming and non-streaming
  responses, incremental JSONL evidence, and terminal progress updates.
- Strict JSON parsing, JSON Schema validation, field and exact-value scoring,
  repeat consistency, latency, throughput, quality goodput, and confidence
  intervals. A supplementary non-padding field metric distinguishes task
  fields from the generator's synthetic length filler.
- JSON/CSV summaries, Markdown reports, exploratory paired contrasts, and
  optional regression gates against absolute thresholds or a saved baseline.
- A local mock-server smoke test, Python 3.11/3.14 CI, wheel/sdist build, and
  a container image.

The [five-run baseline](RESULTS.md) and [load-stress pilot](STRESS_RESULTS.md)
show how to publish reusable experiment configurations with aggregate results
and interpretation limits. Their measurements apply only to the documented
model, service, cases, and testing sessions; they are not general performance
claims.

## Known limits

- One OpenAI-compatible Chat Completions endpoint per experiment. Cross-model
  comparison, server deployment, and distributed load generation are outside
  this release.
- Built-in cases cover four structural families. Long output targets are
  largely filled with synthetic padding; exact correctness includes that
  padding and must not be read as semantic-field accuracy alone.
- Within-run latency intervals resample case IDs, not independent service
  runs. Throughput ratios from a single run are descriptive; fixed condition
  order and service-state drift limit causal interpretation.
- Raw requests and responses are saved locally under ignored `runs/`; inspect
  them before sharing, because configured prompts and model outputs may contain
  sensitive data.

## Getting started

Install from a checkout with `python -m pip install -e '.[dev]'`, then run
`sor validate examples/openai-smoke.yaml --check-cases`. The full workflow,
configuration contract, and metric definitions are in [README.md](README.md),
[CONFIG.md](CONFIG.md), and [METRICS.md](METRICS.md).
