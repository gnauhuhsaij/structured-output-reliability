# Upstream Capability Audit

Audit date: 2026-10-04

Decision: upstream contribution is viable; do not duplicate the open schema-validation work

## Snapshot

This audit examined:

- [`vllm-project/perf-eval` at `086bacd`](https://github.com/vllm-project/perf-eval/tree/086bacd132e4f04fa112df5610bfae2db01ba5cd)
  (2026-09-24);
- [`vllm-project/vllm` at `ce49174`](https://github.com/vllm-project/vllm/tree/ce49174247fd84c322f293a31cbe1d0460aaa7fe)
  (2026-10-04);
- open vLLM issue
  [`#55441`](https://github.com/vllm-project/vllm/issues/55441);
- open vLLM pull request
  [`#55447`](https://github.com/vllm-project/vllm/pull/55447).

Read-only GitHub issue/PR searches for `structured output` and `json schema`
in `vllm-project/perf-eval` returned no matching issue or pull request. The
absence of a match is a point-in-time observation, not proof that maintainers
have not discussed the idea elsewhere.

## Executive conclusion

The proposed project is not redundant, but its first upstream contribution must
be narrower than the original idea.

vLLM already has a capable structured-output serving benchmark with concurrency,
arrival-rate control, streaming latency metrics, throughput, and latency-SLO
goodput. `perf-eval` already has production-oriented YAML orchestration,
repetitions, artifact retention, GPU profiles, and nightly execution.

The missing layer is the connection between them and the richer reliability
contract:

- `perf-eval` does not run the structured-output benchmark;
- current vLLM `main` does not validate JSON against its request schema;
- an open PR already addresses basic schema validation and reference equality;
- neither project provides field-level accuracy, repeat consistency, confidence
  intervals, quality-aware goodput, joined per-request evidence, or Markdown
  regression reports.

Therefore:

1. Do not open another issue or PR whose main proposal is “validate generated
   JSON against its schema.” That work is already covered by vLLM `#55441` and
   `#55447`.
2. The best first upstream proposal is a `perf-eval` structured-output workload
   that invokes vLLM's existing benchmark and retains its artifacts.
3. A later vLLM contribution may separate parse, schema, and reference metrics
   and emit joined per-request evidence, after `#55447` is merged or closed.
4. Generic external-endpoint execution and rich offline reports should remain
   in this project unless `perf-eval` maintainers explicitly want them.

## `perf-eval`: capabilities and boundaries

### What it already provides

`perf-eval` is a vLLM release-quality orchestrator rather than a generic remote
endpoint runner. A workload describes a model, hardware profile, server
configuration, and one or more evaluation blocks. Its documented blocks are
`lm_eval`, `vllm_bench`, `aiperf`, and `bfcl`.

Evidence:

- The [README recipe schema](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/README.md#L23-L31)
  lists those four evaluation types.
- The [workload parser](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/lib/parse_workload.py#L467-L485)
  rejects a workload that lacks all four.
- The [orchestrator](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/lib/run.sh#L31-L56)
  starts a local vLLM server before dispatching benchmarks.

Existing strengths relevant to this project:

- YAML recipes;
- model and hardware metadata;
- Docker and native server runtimes;
- concurrency sweeps;
- exact random input/output-length workloads;
- warmup arguments passed to `vllm bench serve`;
- odd-numbered complete-run repetitions;
- retention of every repetition plus a median aggregate;
- Buildkite discovery and nightly execution;
- raw performance artifacts and dashboard ingestion;
- CPU-only parser and pipeline tests.

The [README](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/README.md#L71-L114)
documents concurrency sweeps, repetitions, warmups, and artifact retention. The
[benchmark wrapper](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/lib/run_vllm_bench.sh#L123-L213)
shows the concrete execution and validation flow.

### Important limitations

There is no structured-output evaluation block. The `vllm_bench` wrapper only
accepts `random` and `speed_bench`; every other dataset returns an unsupported
dataset error. Its parser also reserves the endpoint, dataset, lengths,
concurrency, and result-path options for the wrapper.

Consequences:

- A YAML recipe cannot currently invoke
  `benchmark_serving_structured_output.py`.
- `vllm_bench.args` cannot work around the limitation by replacing the managed
  dataset or endpoint fields.
- Repetition support applies to `vllm_bench`, not automatically to the separate
  structured-output script.
- `perf-eval` starts and owns a local vLLM service; it is not designed around an
  arbitrary user-supplied external URL.
- Performance artifacts are integrated into the perf dashboard, while `aiperf`
  artifacts are only uploaded. A new workload needs an explicit decision about
  whether its results are artifact-only or ingested.

Relevant source:

- [accepted `vllm_bench` fields and reserved arguments](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/lib/parse_workload.py#L25-L44)
- [`random`/`speed_bench` dispatch](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/lib/run_vllm_bench.sh#L147-L181)

### Contribution conventions

Any workload-schema change must update the README in the same PR. Local checks
cover parser behavior, shell syntax, ingestion tests, and pipeline generation;
real runs require the upstream GPU Buildkite environment. The repository also
requires disclosure of material AI assistance in both PR and commit metadata.

Source: [`CLAUDE.md`](https://github.com/vllm-project/perf-eval/blob/086bacd132e4f04fa112df5610bfae2db01ba5cd/CLAUDE.md#L1-L40)

## vLLM structured-output benchmark: capabilities and boundaries

### What it already provides

`benchmarks/benchmark_serving_structured_output.py` already supports:

- OpenAI-compatible serving backends selected from vLLM's shared asynchronous
  request functions;
- a configurable base URL and endpoint;
- JSON, unique-JSON-schema, grammar, regex, choice, and XGrammar benchmark
  datasets;
- structured and unstructured request mixtures;
- maximum concurrency;
- unbounded or rate-limited request arrival;
- Gamma-distributed burstiness;
- generated-output length limits;
- deterministic client-side sampling and prefix generation;
- streaming TTFT, TPOT, inter-token latency, and end-to-end latency;
- configurable latency percentiles;
- request and token throughput;
- latency-SLO goodput;
- saved aggregate JSON containing generated outputs, expected outputs, timing
  arrays, and errors.

Source:

- [request generation and traffic model](https://github.com/vllm-project/vllm/blob/ce49174247fd84c322f293a31cbe1d0460aaa7fe/benchmarks/benchmark_serving_structured_output.py#L108-L322)
- [metrics and load execution](https://github.com/vllm-project/vllm/blob/ce49174247fd84c322f293a31cbe1d0460aaa7fe/benchmarks/benchmark_serving_structured_output.py#L324-L547)
- [saved outputs and CLI](https://github.com/vllm-project/vllm/blob/ce49174247fd84c322f293a31cbe1d0460aaa7fe/benchmarks/benchmark_serving_structured_output.py#L578-L639)

### Correctness behavior on current `main`

The current JSON correctness function:

1. removes spaces and newlines from the response;
2. greedily extracts text from the first `{` to the last `}`;
3. calls `json.loads`;
4. returns `True` for any parseable extracted object.

It does not validate the object against the request schema and does not compare
the parsed value with the reference completion. Consequently, the displayed
`correct_rate(%)` is effectively permissive JSON-object extractability, not JSON
Schema validity or semantic correctness.

Source: [current `evaluate`](https://github.com/vllm-project/vllm/blob/ce49174247fd84c322f293a31cbe1d0460aaa7fe/benchmarks/benchmark_serving_structured_output.py#L662-L703)

### Existing fix in progress

Issue [`#55441`](https://github.com/vllm-project/vllm/issues/55441) precisely
documents the incorrect metric. PR
[`#55447`](https://github.com/vllm-project/vllm/pull/55447) is open and, as of
this audit, blocked pending review. It adds:

- JSON Schema validation through `jsonschema` for structured requests;
- semantic JSON equality for reference-bearing structured requests;
- preservation of schema and structured/unstructured status in saved output;
- unit tests for valid, invalid, reference, whitespace, and unstructured cases.

The PR deliberately retains the pre-existing greedy `{.*}` extraction behavior.
It still reports a single correctness percentage rather than separate parse,
schema, and semantic metrics.

### Other gaps relative to this project

- `--seed` controls client-side randomness; it is not sent as a model decoding
  seed.
- Chat and completion requests use a hard-coded temperature of `0.0`; the
  structured benchmark exposes no temperature sweep.
- The script sends vLLM's backend-specific `structured_outputs` extra body. It
  does not offer a standard `response_format` experiment arm.
- JSON and `json-unique` cases ask for any example satisfying a schema; they do
  not carry a generated exact answer, so field accuracy cannot be computed.
- `xgrammar_bench` has reference completions, but the dataset is downloaded and
  is not a deterministic program-generated suite owned by the experiment.
- There is no repeat-consistency calculation.
- There are no confidence intervals.
- Goodput is latency-only and does not require schema or semantic correctness.
- Generated/expected/correctness objects and timing/error arrays are stored in
  separate lists. Their positions correspond, but there is no explicit
  per-attempt record joining condition, case, timing, raw response, error, and
  score.
- Results are one aggregate JSON document rather than incrementally flushed
  JSONL.
- There is no CSV or Markdown report and no baseline regression gate.
- Authentication uses the conventional `OPENAI_API_KEY`; the environment
  variable name is not configurable in this script.

The hard-coded request temperature and lack of a decoding seed are visible in
[`backend_request_func.py`](https://github.com/vllm-project/vllm/blob/ce49174247fd84c322f293a31cbe1d0460aaa7fe/benchmarks/backend_request_func.py#L23-L37)
and its [Chat Completions request](https://github.com/vllm-project/vllm/blob/ce49174247fd84c322f293a31cbe1d0460aaa7fe/benchmarks/backend_request_func.py#L366-L410).

## Capability matrix

Legend: **yes** = directly available; **partial** = usable with an important
contract gap; **pending** = covered by an open unmerged PR; **no** = absent.

| Requirement | `perf-eval` | vLLM structured benchmark | Remaining work |
|---|---:|---:|---|
| YAML experiment recipe | yes | no | generic project schema still needed |
| Arbitrary external base URL | no | partial | authentication and portability layer |
| Chat Completions | partial | yes | standardize endpoint contract |
| Fixed-concurrency load | yes | yes | reuse |
| Prompt-length sweep | yes for random tokens | partial via random prefix | generated semantic cases at target length |
| Output-length control | yes | yes | distinguish target, limit, and actual |
| Temperature sweep | pass-through in `vllm bench` | no | add request parameter |
| Model decoding seed | pass-through where CLI supports it | no | add request parameter and record support |
| Constrained/unconstrained comparison | no integrated workload | yes by ratio | add explicit portable modes |
| Request success rate | yes | yes | define denominator consistently |
| Strict full-content JSON parse rate | no | no | new metric |
| JSON Schema validity | no | pending `#55447` | track PR; do not duplicate |
| Field-level accuracy | no | no | new scorer and generated ground truth |
| Exact-object accuracy | no | pending for reference dataset | generalize with owned cases |
| Repeat consistency | repetitions only for perf aggregate | no | case-linked repetitions and canonicalization |
| TTFT/TPOT/E2E percentiles | yes | yes | reuse or normalize |
| Throughput | yes | yes | reuse or normalize |
| Quality-aware goodput | no | latency-only | combine correctness and SLOs |
| Confidence intervals | no | no | statistical aggregation |
| Joined per-attempt evidence | no | no | JSONL record contract |
| JSON/CSV/Markdown output | JSON only | JSON only | reporting layer |
| Baseline regression gates | dashboard comparison | no | local/CI comparison command |

## Recommended upstream contribution boundary

### Proposal A: `perf-eval` structured-output workload

This is the preferred first issue and contribution.

Add a new optional workload block, tentatively named `structured_output`, that:

- invokes vLLM's existing `benchmark_serving_structured_output.py` against the
  locally managed server;
- accepts a small, validated subset of script arguments;
- supports a concurrency sweep and odd-numbered repetitions;
- saves each raw result under the workload results directory;
- retains an aggregate result as a Buildkite artifact;
- initially avoids dashboard ingestion unless maintainers provide the metric
  schema they want;
- adds parser, shell-wrapper, and pipeline tests;
- updates the README in the same PR.

The first proposal should not require:

- arbitrary remote endpoints;
- the project's full generic YAML schema;
- Markdown reporting;
- new dashboards;
- field-level scoring;
- changes to server lifecycle management.

Those additions would make the PR harder to review and are not necessary to
prove that structured-output correctness can run beside performance workloads.

### Proposal B: vLLM metric decomposition and request evidence

After `#55447` reaches a stable outcome, propose a separate small change that:

- reports JSON parse rate, schema-valid rate, and reference-match rate as
  separate counters;
- makes parsing semantics explicit instead of calling all three “correctness”;
- stores one joined per-request record with response, schema/reference,
  structured flag, error, token counts, TTFT, TPOT, and end-to-end latency;
- preserves the existing aggregate keys when feasible for compatibility.

Strict full-content JSON parsing may be proposed separately because `#55447`
intentionally retains embedded-object extraction.

### Project-owned layer

Keep these capabilities in this repository until upstream maintainers ask for
them:

- generic external OpenAI-compatible endpoints;
- configurable authentication environment variable;
- deterministic program-generated exact-answer cases;
- field-level comparison policies;
- repeat consistency;
- confidence intervals;
- quality-aware goodput;
- offline rescoring;
- CSV and Markdown reports;
- baseline comparison and CI gates.

## Decision gate

The audit passes the upstream-viability gate.

The next communication should be a focused `perf-eval` issue describing
Proposal A and explicitly referencing vLLM `#55441`/`#55447`. No upstream issue
or PR was created during this audit. Implementation should not begin inside
either upstream repository until maintainers confirm whether a new workload
block is acceptable.
