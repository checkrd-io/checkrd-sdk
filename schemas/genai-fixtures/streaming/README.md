# Streaming terminal-frame golden fixtures

Companion to the body-extraction fixtures (`../README.md`), for the streaming
usage tap (M-11): OpenAI emits usage in the final `data:` chunk when the caller
sets `stream_options.include_usage`; Anthropic emits input usage in
`message_start` and cumulative output in the final `message_delta`. The tap reads
**only** these terminal/metadata frames — it never buffers content frames.

Each `<provider>.json` case:

```jsonc
{
  "name": "openai_stream_with_usage_and_reasoning",
  "provider": "openai",
  "sse_frames": [ "data: {...}\n\n", "data: [DONE]\n\n" ],   // raw SSE wire frames, in order
  "complete": true,                                           // false = stream abandoned before terminal usage frame
  "expected_usage_attrs": { "gen_ai.usage.input_tokens": 1000, ... },  // usage parsed from terminal frame(s)
  "expected_pricing_status": "untallied"                      // present only on abandoned/usage-less streams
}
```

The extractor is fed `sse_frames` in order (as it would see them off the wire) and,
at stream end, MUST produce `expected_usage_attrs` (empty on an abandoned stream).
The **same inclusion-rule normalization** as the body extractor applies — Anthropic's
`message_start` input excludes cache, so the emitted `input_tokens` is the inclusive
sum (e.g. `300 + 1000 + 200 = 1500`). See `../README.md`.

## Abandonment → `untallied`

If the stream ends without a terminal usage frame (client disconnect, truncation),
the engine MUST NOT estimate — the event is marked `pricing_status = "untallied"`
and the pre-flight reserve is released on settle-timeout (TDD §4.2 / §5.3). The
`*_abandoned` cases pin this: no usage attrs, `expected_pricing_status: "untallied"`.
Both SDKs must agree frame-for-frame; verify with the dump-diff in
`[[project_genai_parity_fixtures]]`.
