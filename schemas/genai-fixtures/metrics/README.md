# GenAI OTLP metrics golden fixtures

Shared parity contract for the OTLP **metrics** export (M-15) — the two OTel
GenAI client instruments both SDK OtlpSinks emit. One fixture set, two runtimes,
identical exported histogram data points. See `../README.md` for the extractor
fixtures this parallels.

The two instruments (OTel GenAI metrics, semconv 1.41.x — verified against the
`open-telemetry/semantic-conventions-genai` repo on 2026-07-03):

| Instrument | Type | Unit | Explicit bucket bounds |
|---|---|---|---|
| `gen_ai.client.token.usage` | Histogram | `{token}` | `[1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864]` |
| `gen_ai.client.operation.duration` | Histogram | `s` | `[0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]` |

`gen_ai.client.token.usage` is recorded **twice per call** — once with
`gen_ai.token.type = "input"` (value = input tokens) and once with `"output"`.
Cached / reasoning counts are **not** separate `token.type` values (they stay
span attributes); the histogram carries only `input` / `output`.

## Fixture shape

```jsonc
{
  "name": "basic_single_call",
  "events": [
    { "gen_ai.provider.name": "openai", "gen_ai.operation.name": "chat",
      "gen_ai.request.model": "gpt-4o",
      "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
      "latency_ms": 1200 }
  ],
  "expected": {
    "gen_ai.client.token.usage": {
      "unit": "{token}", "bounds": [ ... ],
      "data_points": [
        { "attributes": { "gen_ai.provider.name": "openai", "gen_ai.operation.name": "chat",
                          "gen_ai.request.model": "gpt-4o", "gen_ai.token.type": "input" },
          "count": 1, "sum": 1000, "bucket_counts": [ ... 15 entries ... ] },
        { "attributes": { ..., "gen_ai.token.type": "output" }, "count": 1, "sum": 500, "bucket_counts": [...] }
      ]
    },
    "gen_ai.client.operation.duration": { "unit": "s", "bounds": [ ... ], "data_points": [ ... ] }
  }
}
```

The test records every event (in order) into the sink's meter, then exports and
matches each instrument's data points against `expected` — **order-independent
by attribute set**. `bucket_counts` has `len(bounds) + 1` entries; a value `v`
falls in bucket `i` where `i` is the smallest index with `v <= bounds[i]`, or the
overflow bucket `len(bounds)` when `v > bounds[-1]` (OTel left-open,
right-closed). `duration_s = latency_ms / 1000`. Model attribute is
`gen_ai.request.model` (prefer response model when the extractor emits one, but
the fixtures use request-model for a single stable dimension).

## Recording rules (both SDKs — must match exactly)

- `token.usage{input}` records `gen_ai.usage.input_tokens` (fallback flat
  `gen_ai_input_tokens`) when present; `{output}` records
  `gen_ai.usage.output_tokens` (fallback `gen_ai_output_tokens`). A missing count
  records nothing for that series (`partial_missing_output` pins this).
- `operation.duration` records `latency_ms / 1000` for every event that has a
  latency, regardless of tokens.
- Attributes: `gen_ai.provider.name`, `gen_ai.operation.name`,
  `gen_ai.request.model`, plus `gen_ai.token.type` on `token.usage` only. Omit an
  attribute whose source key is absent (keep both SDKs identical on omission).
- Never throw on a malformed / partial event — a metering glitch must not break
  the host call.

Verify parity with the dump-diff described in `[[project_genai_parity_fixtures]]`
(record the same events in both SDKs, export, diff the data points).
