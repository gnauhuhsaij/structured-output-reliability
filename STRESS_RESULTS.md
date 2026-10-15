# Load-stress pilot results (2026-10-15)

This is one completed run of the [pre-specified stress plan](STRESS_PLAN.md),
using [`examples/openai-stress.yaml`](examples/openai-stress.yaml). It compares
four conditions for one model service and one generated case set. The results
are exploratory, not evidence that concurrency caused a reliability change.

## Run and workload

| Item | Value |
|---|---|
| Complete run ID | `20261015T190106655502Z_8bf4b017f72d` |
| Model | `gpt-4o-mini-2024-07-18` via OpenAI-compatible Chat Completions |
| Case-set SHA-256 | `782efebfdc5e797898834e3db08fdc0dc5575c4c0768534deca511a7faafb8d1` |
| Cases and attempts | 96 distinct cases; 2 measured repetitions per case and condition; 768 measured attempts plus 4 warmups |
| Matrix | Concurrency 1 or 8 × prompt-only or strict JSON Schema mode |
| Fixed settings | 8,192-character prompt target; 512-character expected-output target; 1,024 maximum completion tokens; temperature 0; request seed 42 |

The run manifest records all 768 measured attempts as terminal and complete,
with zero request failures. A prior attempt with the same configuration
(`20261015T183828046396Z_8bf4b017f72d`) stopped after 268 of 768 measured
attempts. It is **not** pooled into any result below.

## Correctness and serving performance

| Concurrency | Mode | Exact correct | JSON parse / Schema valid | p95 E2E | Request throughput | Quality goodput |
|---:|---|---:|---:|---:|---:|---:|
| 1 | Prompt-only | 192/192 (100%) | 192/192 / 192/192 | 1,623.7 ms | 0.805 req/s | 0.805 correct req/s |
| 8 | Prompt-only | 192/192 (100%) | 192/192 / 192/192 | 1,741.2 ms | 4.674 req/s | 4.674 correct req/s |
| 1 | JSON Schema | 186/192 (96.88%) | 192/192 / 192/192 | 1,657.7 ms | 0.784 req/s | 0.759 correct req/s |
| 8 | JSON Schema | 183/192 (95.31%) | 192/192 / 192/192 | 1,715.3 ms | 5.762 req/s | 5.492 correct req/s |

In the pre-specified primary JSON Schema contrast, concurrency 8 had an
observed exact-correct rate 1.56 percentage points lower than concurrency 1
(183/192 vs. 186/192). Their per-condition 95% Wilson intervals were
[91.33%, 97.52%] and [93.35%, 98.56%], respectively. Among 192 matched
case/repetition pairs, 4 were incorrect at both levels, 2 only at concurrency
1, and 5 only at concurrency 8. These counts do not establish a load-induced
reliability drop.

Quality goodput in JSON Schema mode was 5.492 vs. 0.759 correct requests/s,
an observed 7.23× ratio. Request throughput was 7.35×. These are descriptive
whole-condition values from one run, with no run-level interval. The paired
mean E2E change for concurrency 1 → 8 was +52.4 ms, with a case-bootstrap 95%
interval of [−11.6, +110.0] ms; this interval captures case variation within
the run, not day-to-day service variation.

## Failure diagnosis

All 15 incorrect JSON Schema attempts came from `flat_object` cases. Each
had a single incorrect `/padding` value: a run of `x` characters exactly
eight characters shorter than expected. Every other field matched. The
schema specifies that padding is a string but does not constrain its length,
so these outputs were Schema-valid despite failing exact field comparison.
The embedded `SOURCE_JSON` matches the expected answer in all 96 saved cases,
so this is not a prompt/answer mismatch in the case generator. No measured
response ended because it reached the completion-token limit.

This is a real failure under the tool's exact-value scoring rule, but the
failure is in synthetic length-filling text, not in the substantive fields.
It should not be presented as evidence that the model misunderstood the
structured extraction task. A retrospective, non-primary diagnostic that
excludes only the built-in generator's `/padding` field scored **768/768 task
fields in every condition**. This added diagnostic does not change the
pre-specified exact-correct rates or quality-goodput results above. A future
experiment should test more varied task fields rather than relying on long
padding strings for difficulty.

## Limits and reproducibility

- This is one sequential run with fixed condition order. Differences may
  reflect service-state or time effects; the 96 cases repeated twice are not
  192 independent case types.
- The comparison uses one model, one date, one service, and the four existing
  generator families. Longer prompts and outputs did not add schema complexity.
- The prompt-only groups had no observed errors, but 192/192 does not prove
  perfect underlying reliability.
- Raw requests and responses remain in ignored local `runs/` directories and
  are not published. The complete local report at
  `runs/20261015T190106655502Z_8bf4b017f72d/report.md` can be regenerated
  from those artifacts; a fresh run requires the user's own API credential
  and may produce different measurements.

To reproduce the procedure, first validate the cases offline, then run the
configuration from a shell where `OPENAI_API_KEY` is set:

```bash
sor validate examples/openai-stress.yaml --check-cases
sor run examples/openai-stress.yaml
```
