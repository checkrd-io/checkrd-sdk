# GenAI body-extraction golden fixtures

Language-neutral fixtures that **both** SDK extractors (`wrappers/python/src/checkrd/_genai_body.py`
and `wrappers/javascript/src/_genai_body.ts`) test against. One fixture set, two
runtimes, byte-identical expected output — this is the parity contract. A field
that drifts between Python and JS fails a fixture test in at least one of them.

Each `<provider>.json` is an array of cases:

```jsonc
{
  "name": "openai_chat_with_cache_and_reasoning",
  "provider": "openai",                       // gen_ai.provider.name the case exercises
  "request":  { "body": { /* raw provider request JSON */ } },        // optional
  "response": {
    "body":    { /* raw provider response JSON */ },                  // optional
    "headers": { "x-amzn-...": "1200" }                              // optional (Bedrock)
  },
  "expected_request_attrs":  { "gen_ai.request.model": "gpt-4o", "gen_ai.request.stream": false },
  "expected_response_attrs": { "gen_ai.usage.input_tokens": 1000, ... }
}
```

The extractor under test is fed `request.body` / `response.{body,headers}` and its
output dict MUST equal `expected_*_attrs` exactly (same keys, same values, no
extras). Attribute keys are the OTel `gen_ai.*` dotted names; the SDK maps them to
the flat wire keys (`gen_ai_input_tokens`, …) downstream.

## The inclusion-rule invariant (billing-critical)

OTel GenAI semconv treats the detail counters as **subsets of the totals**
(`gen_ai.usage.input_tokens` / `output_tokens`). The core's `settle_usage`
(crates/core, M-4) relies on this: it computes `fresh_input = input − cache_read −
cache_creation` and bills fresh at the input rate, cache-read at the cache-read
rate, cache-creation at the cache-write rate. So every extractor MUST normalize a
provider's native counts so that, in the emitted attributes:

```
gen_ai.usage.cache_read.input_tokens
  + gen_ai.usage.cache_creation.input_tokens   ≤  gen_ai.usage.input_tokens
gen_ai.usage.reasoning.output_tokens           ≤  gen_ai.usage.output_tokens
```

Providers differ in whether their native counts are already inclusive:

| Provider | input total | output total | cache_read | cache_creation | reasoning | Normalization |
|---|---|---|---|---|---|---|
| **OpenAI** | `usage.prompt_tokens` | `usage.completion_tokens` | `usage.prompt_tokens_details.cached_tokens` | — | `usage.completion_tokens_details.reasoning_tokens` | **none** — `prompt_tokens`/`completion_tokens` are already totals (cached ⊆ prompt, reasoning ⊆ completion). |
| **Anthropic** | `usage.input_tokens` **+** cache_read **+** cache_creation | `usage.output_tokens` | `usage.cache_read_input_tokens` | `usage.cache_creation_input_tokens` | — | **add cache into input** — Anthropic's `input_tokens` *excludes* cache, so the extractor sums them to make the total inclusive. |
| **Gemini** | `usageMetadata.promptTokenCount` | `usageMetadata.candidatesTokenCount` | `usageMetadata.cachedContentTokenCount` | — | `usageMetadata.thoughtsTokenCount` | **none** for the standard Gemini API — see the assumption below. |
| **Cohere** | `meta.billed_units.input_tokens` | `meta.billed_units.output_tokens` | — | — | — | **prefer `billed_units`** over `meta.tokens` (billed = what the customer is charged; `tokens` includes uncharged internal tokens). |
| **Bedrock** | `x-amzn-bedrock-input-token-count` (header) | `x-amzn-bedrock-output-token-count` (header) | — | — | — | **headers are authoritative** — string values parsed to int; the Anthropic-on-Bedrock body is ignored for token counts. |

### Worked invariant (Anthropic) — why the extractor and core agree

Anthropic returns `input_tokens=300, cache_read=1000, cache_creation=200`. The
extractor emits `input_tokens = 300 + 1000 + 200 = 1500`. The core then nets:
`fresh = 1500 − 1000 − 200 = 300` → bills 300 at input rate, 1000 at cache-read
rate, 200 at cache-write rate. This reproduces the Anthropic invoice line-for-line
and matches the `settle_cache_tokens_are_netted_out_of_fresh_input` test in
`crates/core/src/pricing.rs`.

### Gemini assumption (flagged — version-sensitive)

For the **standard Gemini API** (`generativelanguage.googleapis.com` →
`google.gemini`), current 2.5+ models report `candidatesTokenCount` *inclusive* of
thinking tokens, so `output = candidatesTokenCount` and
`reasoning = thoughtsTokenCount ⊆ output`. Vertex AI
(`aiplatform.googleapis.com` → `google.vertex_ai`) has historically differed; if a
deployment reports `candidatesTokenCount` *exclusive* of thoughts, this fixture and
the extractor must add `thoughtsTokenCount` into the output total. This is the one
provider semantic that is genuinely inconsistent across API versions; it is pinned
here so a change is a one-line fixture+extractor edit caught by these tests.

## Robustness contract

The extractor must **never throw** on hostile or malformed input — a missing field,
wrong type, non-object body, oversize body (> 1 MiB), or non-UTF-8 bytes all yield
an empty (or partial) attribute dict, never an exception. The `*_malformed` and
`*_partial` fixture cases pin this.
