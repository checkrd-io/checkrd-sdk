# Checkrd JavaScript examples

Runnable TypeScript examples demonstrating common Checkrd deployment
patterns. Each file is self-contained — install the SDK
(`npm install checkrd`) plus any vendor library the example mentions,
then run with `tsx` (or `npx ts-node`).

| File | What it shows |
|---|---|
| [`basic-openai.ts`](./basic-openai.ts) | 5-line instrumentation of the OpenAI SDK |
| [`vercel-ai-sdk.ts`](./vercel-ai-sdk.ts) | Middleware integration with the Vercel AI SDK |
| [`otlp-honeycomb.ts`](./otlp-honeycomb.ts) | Dual-export telemetry to Honeycomb via OTLP |
| [`air-gapped.ts`](./air-gapped.ts) | Tier 3 deployment with no control plane |
| [`edge-runtime.ts`](./edge-runtime.ts) | Cloudflare Workers / Vercel Edge deployment |

## Environment

Every example expects at minimum a policy file. The scripts read
`policy.yaml` from the current working directory — a minimal one:

```yaml
agent: example-agent
default: allow
rules: []
```

For cloud-mode examples, set:

```bash
export CHECKRD_API_KEY="ck_live_..."
```

No Checkrd account? The examples all run in observation-only mode
without an API key: policy is evaluated locally, telemetry is logged
rather than shipped.
