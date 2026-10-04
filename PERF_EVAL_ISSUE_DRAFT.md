# Proposed GitHub issue

Repository: `vllm-project/perf-eval`

Status: local draft only; not published

## Title

```text
[Feature] Run vLLM's structured-output serving benchmark from YAML workloads
```

## Body

### Motivation

`perf-eval` currently combines serving-performance benchmarks, model-accuracy
evaluation, and BFCL function-calling evaluation in the same model/hardware
recipes. vLLM also ships
`benchmarks/benchmark_serving_structured_output.py`, but `perf-eval` has no
workload block that invokes it.

That leaves structured-output serving regressions outside the existing nightly
and release-candidate workflow. The structured-output benchmark already measures
concurrency, request rate, TTFT, TPOT, end-to-end latency, throughput, and
latency-SLO goodput. Recent vLLM issue
[#55441](https://github.com/vllm-project/vllm/issues/55441) and PR
[#55447](https://github.com/vllm-project/vllm/pull/55447) also show why its
correctness result is useful to preserve beside the performance data.

Would a small, optional structured-output workload block fit `perf-eval`?

### Proposed minimal shape

For example:

```yaml
structured_output:
  configs:
    - name: json-schema-128-out
      dataset: json
      output_len: 128
      num_prompts: 500
      max_concurrency: [1, 64, 256]
      repetitions: 3
      args:
        structured_output_ratio: 1.0
        seed: 42
        disable_tqdm: true
```

The wrapper would own the model, tokenizer, local server URL, Chat Completions
endpoint, concurrency, prompt count, output length, and result paths. Remaining
supported script options would be passed through `args`, following the existing
`vllm_bench`/`aiperf` normalization pattern.

Each concurrency value would become a concrete run. Every raw repetition would
be retained under `results/<workload>/`; an aggregate file could follow the
existing odd-repetition policy if maintainers want one.

For an initial PR, results could be Buildkite artifacts only, like the first
`aiperf` integration, with no dashboard-ingestion change.

### Proposed implementation scope

- Add and validate one optional `structured_output.configs` workload block.
- Add a small runner for `benchmark_serving_structured_output.py` against the
  vLLM server already managed by `perf-eval`.
- Support concurrency sweeps and odd-numbered repetitions.
- Retain raw result JSON for every repetition.
- Add parser/expansion and runner tests that do not require a GPU.
- Update the README in the same change.
- Validate one opt-in workload through Buildkite before enabling any nightly
  recipe.

### Explicit non-goals for the first contribution

- Reimplement the vLLM structured-output benchmark.
- Add generic remote-endpoint support to `perf-eval`.
- Change vLLM's correctness scorer in this repository.
- Add field-level semantic scoring or new datasets.
- Add dashboard ingestion or a new dashboard.
- Add Markdown reporting or regression-threshold policy.
- Enable the workload in every existing recipe.

### Existing overlapping work

vLLM [#55447](https://github.com/vllm-project/vllm/pull/55447) is already adding
JSON Schema validation and reference-completion comparison to the benchmark in
response to [#55441](https://github.com/vllm-project/vllm/issues/55441). This
proposal would consume the upstream benchmark rather than duplicate that scorer
work.

The integration can land independently while remaining opt-in, or wait for
`#55447`, depending on whether maintainers want the first stored correctness
artifacts to use the stronger scoring semantics.

### Questions

1. Is a separate `structured_output` block preferable to extending
   `vllm_bench`?
2. Is artifact-only output acceptable for the first PR, following the initial
   `aiperf` integration pattern?
3. Should the first opt-in workload wait for vLLM `#55447` to merge?

If this direction fits the repository, I can send a focused PR containing the
parser, runner, CPU-only tests, README update, and one opt-in workload.
