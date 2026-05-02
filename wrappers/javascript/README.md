# checkrd

[![npm](https://img.shields.io/npm/v/checkrd.svg)](https://www.npmjs.com/package/checkrd)
[![types](https://img.shields.io/npm/types/checkrd.svg)](https://www.npmjs.com/package/checkrd)
[![license](https://img.shields.io/npm/l/checkrd.svg)](./LICENSE)

**The control plane your AI agents are missing.** A drop-in `fetch` wrapper that
enforces what your agent is allowed to call, kills it instantly when something
goes wrong, and ships signed audit logs you can hand to compliance — without
changing the agent code.

```typescript
import { Checkrd } from 'checkrd';
const myFetch = new Checkrd().wrap(globalThis.fetch);
// Pass `myFetch` to OpenAI / Anthropic / your client of choice. Done.
```

---

## Why checkrd

- **Stop runaway agents at the network edge.** A YAML policy denies the
  call before the bytes leave the machine — no agent code change, no
  vendor SDK fork. Wraps `fetch` so it works with every Node, Edge, or
  browser AI client that takes a fetch option.
- **Kill switch in &lt; 1s.** Toggle from the dashboard and every running
  agent stops mid-stream. Useful when your agent decides to refund every
  customer at 3am.
- **Cryptographically signed telemetry.** Every request flow is logged
  with [RFC 9421 HTTP Message Signatures + DSSE envelopes](./SECURITY.md).
  Audit trail your security team will actually trust.
- **No source-map leaks, no token-stuffing, no SSRF surprises.** Source
  maps stay out of the published tarball, response redirects fail closed
  by default, stream-capture has a hard memory budget. The
  [threat model](./THREAT-MODEL.md) lists what we defend against
  explicitly and what we don't.
- **Runs everywhere your agent runs.** Node 18+, Cloudflare Workers,
  Vercel Edge, Deno, Bun, modern browsers (with explicit
  `dangerouslyAllowBrowser`). Verified by [a real edge-runtime VM
  smoke test](./tests/edge_runtime.test.ts) on every PR — not "should
  work, in theory".

---

## Install

```bash
npm install checkrd
```

## Quick Start

```typescript
import { Checkrd } from 'checkrd';

const checkrd = new Checkrd({
  apiKey: 'ck_live_xyz',
  agentId: 'sales-agent',
});

const myFetch = checkrd.wrap(globalThis.fetch);
const response = await myFetch('https://api.openai.com/v1/chat/completions', {
  method: 'POST',
  body: JSON.stringify({ model: 'gpt-4o', messages: [/*...*/] }),
});
```

`new Checkrd()` reads config from env when you don't pass arguments —
`CHECKRD_API_KEY`, `CHECKRD_BASE_URL`, `CHECKRD_AGENT_ID`,
`CHECKRD_API_VERSION`. In a well-configured deployment it becomes:

```typescript
const myFetch = new Checkrd().wrap(globalThis.fetch);
```

### Vendor SDK integration

Global monkey-patch every `new OpenAI()` / `new Anthropic()` call:

```typescript
const checkrd = new Checkrd({ apiKey: 'ck_live_xyz' });
checkrd.instrumentOpenAI();
checkrd.instrumentAnthropic();

// Every subsequent new OpenAI() / new Anthropic() transparently runs
// through Checkrd — no other code changes required.
import OpenAI from 'openai';
const client = new OpenAI();
```

### Edge runtimes

Cloudflare Workers, Vercel Edge, Deno — use `wrapAsync` which loads
the WASM core via `WebAssembly.compileStreaming`:

```typescript
const myFetch = await new Checkrd({ apiKey: 'ck_live_xyz' }).wrapAsync();
```

### Per-scope overrides

Immutable clone with merged options (OpenAI SDK `.withOptions()` pattern):

```typescript
const strict = checkrd.withOptions({ securityMode: 'strict' });
const v2 = checkrd.withOptions({ apiVersion: '2026-05-01' });
```

### Backwards-compatible functional API

The top-level `wrap()` / `wrapAsync()` / `init()` / `instrumentOpenAI()`
functions remain for callers on the pre-0.3 surface. The class
delegates to them internally:

```typescript
import { wrap } from 'checkrd';

const myFetch = wrap(globalThis.fetch, { agentId: 'sales-agent' });
```

## Framework adapters

Vendor instrumentation works at the HTTP layer. For framework-native
integration — `BaseCallbackHandler` for LangChain.js, `TracingProcessor` +
`Guardrail` for OpenAI Agents, hooks for the Claude Agent SDK, middleware
for the AI SDK / Mastra / MCP — Checkrd ships dedicated adapters under
subpath exports. Each uses the framework's documented public extension
point — no monkey-patching, no internal-API risk.

| Framework                                                             | Subpath                       |
| --------------------------------------------------------------------- | ----------------------------- |
| Vercel AI SDK (`LanguageModelV2Middleware`)                           | `checkrd/ai-sdk`              |
| LangChain.js / LangGraph (`BaseCallbackHandler`)                      | `checkrd/langchain`           |
| OpenAI Agents SDK (`TracingProcessor` + guardrails)                   | `checkrd/openai-agents`       |
| Anthropic Claude Agent SDK (`attachToOptions` + hook factories)       | `checkrd/claude-agent-sdk`    |
| Mastra (`wrapMastraAgent` + `Telemetry`)                              | `checkrd/mastra`              |
| Model Context Protocol (MCP) (`wrapMcpClient` / `wrapMcpServer`)      | `checkrd/mcp`                 |
| Next.js (`initCheckrd` + `checkrdRoute` + `checkrdAction`)            | `checkrd/next`                |
| Hono (`checkrdHono` middleware)                                       | `checkrd/hono`                |
| Cloudflare Workers (`withCheckrd` HOC)                                | `checkrd/cloudflare`          |

Each adapter is documented at <https://checkrd.io/docs/integrations>.
Operators write one policy YAML and the same rules fire across vendor
instrumentors and framework adapters using framework-prefixed
synthetic URLs (`ai-sdk://...`, `langchain.local/...`,
`openai-agents.local/...`, `claude-agent.local/...`).

Each framework's peer is declared `optional` in
`peerDependenciesMeta`, so consumers only install what they actually
use. Importing a subpath without the peer installed produces a
clear `Cannot find module '@langchain/core'` error rather than a
runtime crash.

## Security

- **[`SECURITY.md`](./SECURITY.md)** — vulnerability disclosure, supply-
  chain posture (npm OIDC provenance, no source maps in tarball),
  fail-closed defaults.
- **[`THREAT-MODEL.md`](./THREAT-MODEL.md)** — what we defend against
  (forged telemetry, MITM control plane, SSRF via redirect, browser
  bundle exposure) and what we explicitly don't (compromised host
  process, stolen API key).
- **[`crates/core/SECURITY.md`](../../crates/core/SECURITY.md)** —
  WASM core's threat model and integrity-verification recipe (shipped
  byte-identical in this package as `dist/checkrd_core.wasm`).
- **[`API-STABILITY.md`](./API-STABILITY.md)** — what's covered by
  SemVer and what's `_underscore`-prefixed and free to change.
- **[`CHANGELOG.md`](./CHANGELOG.md)** — Keep-a-Changelog format,
  every release links its security-relevant entries.

Vulnerability reports: <security@checkrd.io>. Acknowledgement within
2 business days, fix windows scaled to severity. PGP key at
`https://checkrd.io/.well-known/security.asc`.

## What a real policy looks like

Policies are YAML — same format the Python SDK uses, same WASM core
evaluates them in &lt; 200 µs:

```yaml
agent: sales-agent
default: deny

rules:
  - name: allow-openai-chat
    allow:
      method: [POST]
      url: "api.openai.com/v1/chat/completions"
    body:
      jsonpath: "$.model"
      in: ["gpt-4o-mini", "gpt-4o"]   # Block expensive models

  - name: spend-cap
    limit:
      calls_per_minute: 60
      per: global

  - name: block-deletes-everywhere
    deny:
      method: [DELETE]
      url: "*"
```

When a wrapped `fetch` makes a call that hits a `deny` rule, the
request never leaves the process. The SDK throws a typed
`CheckrdPolicyDenied` your handler can catch.

## Error handling

```typescript
import { Checkrd, CheckrdPolicyDenied, RateLimitError } from 'checkrd';

const myFetch = new Checkrd().wrap(globalThis.fetch);

try {
  await myFetch('https://api.stripe.com/v1/charges', { method: 'DELETE' });
} catch (err) {
  if (err instanceof CheckrdPolicyDenied) {
    // Policy blocked it. ``err.reason`` carries the matched rule name;
    // ``err.requestId`` correlates with telemetry for support tickets.
    console.warn(`blocked: ${err.reason} (req=${err.requestId})`);
  } else if (err instanceof RateLimitError) {
    // Control plane rate-limited the SDK's outbound flow.
    // ``err.retryAfterSecs`` is the server-provided hint.
  } else {
    throw err;
  }
}
```

The full error hierarchy mirrors Stripe / OpenAI conventions:
`APIError` → `APIStatusError` → status-code subclasses (`BadRequestError`,
`AuthenticationError`, `RateLimitError`, etc.). Every error carries
`.code`, `.requestId`, and `.docsUrl` so you can drop them straight
into a support ticket.
