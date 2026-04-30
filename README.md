# Checkrd SDKs

[![PyPI](https://img.shields.io/pypi/v/checkrd?label=pypi&color=blue)](https://pypi.org/project/checkrd/)
[![npm](https://img.shields.io/npm/v/checkrd?color=blue)](https://www.npmjs.com/package/checkrd)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Test](https://github.com/checkrd-io/checkrd-sdk/actions/workflows/test.yml/badge.svg)](https://github.com/checkrd-io/checkrd-sdk/actions/workflows/test.yml)
[![CodeQL](https://github.com/checkrd-io/checkrd-sdk/actions/workflows/codeql.yml/badge.svg)](https://github.com/checkrd-io/checkrd-sdk/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/checkrd-io/checkrd-sdk/badge)](https://scorecard.dev/viewer/?uri=github.com/checkrd-io/checkrd-sdk)

Policy enforcement, kill switch, and signed telemetry for AI agent API
calls. Drop-in instrumentors wrap the major LLM SDKs (OpenAI, Anthropic,
Cohere, Mistral, Groq, Google GenAI, Together) and intercept outbound
HTTP traffic before it leaves the agent process. Every request is
evaluated against a signed policy in a sandboxed WebAssembly engine; the
decision and request metadata are signed with Ed25519 and shipped to
your control plane.

For the hosted control plane and dashboard, see
[checkrd.io](https://checkrd.io). The SDKs work standalone — point them
at any control plane that speaks the documented wire protocol, or run
them in air-gapped mode with a local policy file and no network egress.

## Documentation

- API reference: [docs.checkrd.io](https://docs.checkrd.io)
- Python SDK: [`wrappers/python/`](wrappers/python/README.md)
- JavaScript SDK: [`wrappers/javascript/`](wrappers/javascript/README.md)
- Threat model: [Python](wrappers/python/THREAT-MODEL.md) · [JavaScript](wrappers/javascript/THREAT-MODEL.md)
- WASM engine internals: [`wrappers/python/WASM-CORE.md`](wrappers/python/WASM-CORE.md)

## Install

```sh
pip install checkrd      # Python 3.9+
npm install checkrd      # Node 18+, Bun, Deno, Cloudflare Workers, Vercel Edge
```

## Quickstart

```python
import openai
from checkrd import Checkrd

checkrd = Checkrd(api_key="ck_live_...")
checkrd.instrument()  # patches openai, anthropic, cohere, mistral, groq, google.genai

client = openai.OpenAI()
client.chat.completions.create(
    model="gpt-5.2",
    messages=[{"role": "user", "content": "Hello"}],
)
```

```typescript
import OpenAI from "openai";
import { Checkrd } from "checkrd";

const checkrd = new Checkrd({ apiKey: "ck_live_..." });
await checkrd.instrument();

const client = new OpenAI();
await client.chat.completions.create({
  model: "gpt-5.2",
  messages: [{ role: "user", content: "Hello" }],
});
```

A request that violates the active policy raises `PolicyDeniedError`
before the outbound HTTP call is made. A request blocked by the kill
switch raises `KillSwitchActiveError`.

## What's in this repository

| Path | Package | Purpose |
|---|---|---|
| [`wrappers/python`](wrappers/python) | [`checkrd` on PyPI](https://pypi.org/project/checkrd/) | Python SDK. Sync + async clients, vendor instrumentors, CLI. |
| [`wrappers/javascript`](wrappers/javascript) | [`checkrd` on npm](https://www.npmjs.com/package/checkrd) | JavaScript SDK. Cross-runtime (Node, Bun, Deno, Workers, Edge, browser). |
| [`crates/core`](crates/core) | — | WASM policy engine. Compiled to `wasm32-wasip1` and embedded in each SDK. |
| [`crates/shared`](crates/shared) | — | Wire-format types shared between the engine and the SDKs. |
| [`schemas`](schemas) | — | JSON Schema for policy YAML and telemetry events. |

The two SDKs are intentionally one-for-one in behaviour. Anything
verifiable on one side (rate-limit invariants, glob-match specificity,
DSSE envelope parsing) has a matching test on the other side and a
property test on the WASM core itself.

## Architecture

Each SDK embeds the same `checkrd_core.wasm` binary, compiled from
`crates/core` in this repository. The wrapper handles all I/O — vendor
SDK instrumentation, HTTP transport, telemetry batching, control-stream
SSE — and calls into the WASM engine for the security-critical work:

- Policy evaluation (kill switch → rate limits → deny → allow → default)
- DSSE-signed policy bundle verification
- Ed25519 telemetry signing per RFC 9421 and RFC 9530

The WASM module is pure computation: zero I/O, no clock, no filesystem,
no network. Every wrapper instance has its own wasmtime store, so
policies and rate-limit counters are not shared across `Checkrd`
instances in the same process.

## Building from source

```sh
# Build the WASM engine and stage it in both wrappers.
cargo build --package checkrd-core --target wasm32-wasip1 --release
./scripts/copy-wasm.sh

# Python
cd wrappers/python && pip install -e ".[test]" && pytest

# JavaScript
cd wrappers/javascript && npm ci && npm test
```

Requires a recent Rust toolchain with the `wasm32-wasip1` target
installed (`rustup target add wasm32-wasip1`).

## Versioning

Both SDKs follow [SemVer](https://semver.org/). The `0.x` series is
pre-1.0; minor releases may contain breaking changes, documented in
each package's `CHANGELOG.md`. Releases are tagged `python-vX.Y.Z`
and `javascript-vX.Y.Z` respectively, and published to PyPI / npm
with provenance attestations linking back to the tagged commit.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and pull requests
are welcome. For security-relevant issues, follow the disclosure
process in [SECURITY.md](SECURITY.md) instead of filing a public
issue.

## License

Apache 2.0. See [LICENSE](LICENSE).
