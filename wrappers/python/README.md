# checkrd

[![PyPI](https://img.shields.io/pypi/v/checkrd.svg)](https://pypi.org/project/checkrd/)
[![Python](https://img.shields.io/pypi/pyversions/checkrd.svg)](https://pypi.org/project/checkrd/)
[![License](https://img.shields.io/pypi/l/checkrd.svg)](./LICENSE)

**The control plane your AI agents are missing.** A drop-in `httpx`
wrapper that enforces what your agent is allowed to call, kills it
instantly when something goes wrong, and ships signed audit logs you
can hand to compliance — without changing the agent code.

```python
from checkrd import Checkrd
import httpx

http = Checkrd().wrap(httpx.Client())  # Pass to OpenAI / Anthropic / your client. Done.
```

---

## Why checkrd

- **Stop runaway agents at the network edge.** A YAML policy denies the
  call before the bytes leave the machine — no agent code change, no
  vendor SDK fork. Wraps both `httpx.Client` and `httpx.AsyncClient`,
  works with every AI client that takes a transport option (OpenAI,
  Anthropic, Cohere, Mistral, Groq, Together, Google GenAI…).
- **Kill switch in &lt; 1s.** Toggle from the dashboard and every
  running agent stops mid-stream. Useful when your agent decides to
  refund every customer at 3am.
- **Cryptographically signed telemetry.** Every request flow is logged
  with [Ed25519 + RFC 9421 + DSSE](./SECURITY.md). Audit trail your
  security team will actually trust — and your control plane verifies
  byte-for-byte before persisting.
- **Strict-from-the-ground-up.** Fork-safe via
  [`os.register_at_fork`](https://docs.python.org/3/library/os.html#os.register_at_fork),
  fail-closed body inspection limits, integrity-verified WASM core
  (SHA-256 pinned at build time), `mypy --strict` and `pyright`
  CI-gated. The [threat model](./THREAT-MODEL.md) lists what we defend
  against explicitly.
- **Three deployment tiers.** **Cloud** (managed control plane), **self-host**
  (your VPC, our software), **air-gapped** (file-watcher backend
  via the optional `[watchdog]` extra — sub-millisecond reaction to
  policy file changes via inotify / FSEvents).

Policy evaluation runs in a WebAssembly sandbox (Rust core, wasmtime).
Published benchmark numbers will land with the 1.0 release.

---

## Install

```bash
pip install checkrd
```

## Quick Start

```python
from checkrd import Checkrd
import httpx

checkrd = Checkrd(api_key="ck_live_xyz", agent_id="sales-agent")

http = checkrd.wrap(httpx.Client())
response = http.get("https://api.stripe.com/v1/charges")
```

`Checkrd()` reads config from env when you don't pass arguments —
`CHECKRD_API_KEY`, `CHECKRD_BASE_URL`, `CHECKRD_AGENT_ID`,
`CHECKRD_API_VERSION`, `CHECKRD_SECURITY_MODE`. In a well-configured
deployment the above becomes a one-liner:

```python
http = Checkrd().wrap(httpx.Client())
```

Async mirror takes `httpx.AsyncClient`:

```python
from checkrd import AsyncCheckrd
import httpx

async with AsyncCheckrd() as checkrd:
    http = checkrd.wrap(httpx.AsyncClient())
    response = await http.get("https://api.stripe.com/v1/charges")
```

Per-scope overrides via the OpenAI-SDK `with_options` pattern:

```python
strict = checkrd.with_options(security_mode="strict")
v2 = checkrd.with_options(api_version="2026-05-01")
```

### Backwards-compatible functional API

The top-level `wrap()` / `wrap_async()` / `init()` / `instrument_*()`
functions remain for callers on the pre-0.3 surface; the class
delegates to them internally:

```python
from checkrd import wrap
import httpx

client = wrap(
    httpx.Client(),
    agent_id="sales-agent",
    policy="policy.yaml",
)
```

## Policy Format

Policies are YAML files that define what your agent is allowed to do:

```yaml
agent: sales-agent
default: deny

rules:
  - name: read-contacts
    allow:
      method: [GET]
      url: "api.salesforce.com/*/sobjects/Contact/*"

  - name: create-small-charges
    allow:
      method: [POST]
      url: "api.stripe.com/v1/charges"
    body:
      jsonpath: "$.amount"
      max: 50000

  - name: block-all-deletes
    deny:
      method: [DELETE]
      url: "*"

  - name: rate-limit
    limit:
      calls_per_minute: 60
      per: endpoint

  - name: business-hours-only
    deny:
      time_outside: "09:00-17:00"
      timezone: "UTC"
```

## Configuration

```python
# From a YAML file
client = wrap(httpx.Client(), agent_id="agent", policy="./policy.yaml")

# From a dict
client = wrap(httpx.Client(), agent_id="agent", policy={
    "agent": "my-agent",
    "default": "deny",
    "rules": [{"name": "allow-all-get", "allow": {"method": ["GET"], "url": "*"}}],
})

# From default location (~/.checkrd/policy.yaml)
client = wrap(httpx.Client(), agent_id="agent")

# Override config directory via environment variable
# CHECKRD_CONFIG_DIR=/app/config
```

## Error Handling

Denied requests raise `CheckrdPolicyDenied`:

```python
from checkrd import wrap, CheckrdPolicyDenied

client = wrap(httpx.Client(), agent_id="agent", policy=policy)

try:
    client.delete("https://api.stripe.com/v1/charges/ch_xxx")
except CheckrdPolicyDenied as e:
    print(e.reason)      # "denied by rule 'block-all-deletes'"
    print(e.request_id)  # UUID for correlation with telemetry
```

## Async Support

```python
from checkrd import wrap_async
import httpx

client = wrap_async(httpx.AsyncClient(), agent_id="agent", policy=policy)
response = await client.get("https://api.stripe.com/v1/charges")
```

## Dry-Run Mode

Observe policy decisions without blocking requests. Use this to roll out Checkrd safely:

```python
client = wrap(httpx.Client(), agent_id="agent", policy=policy, enforce=False)

# Denied requests are logged as warnings but still forwarded
response = client.delete("https://api.stripe.com/v1/charges/ch_xxx")
# WARNING: checkrd: req-xxx would be denied (dry-run): denied by rule 'block-all-deletes'
```

## Production Setup (with Checkrd Cloud)

In production, **don't rely on the SDK's auto-generated dev key**. Generate one key per agent with the `checkrd` CLI, distribute it via your secrets manager, and load it explicitly:

```bash
# Operator generates the key once
$ checkrd keygen
# Generated by `checkrd keygen`
# Public key: 3d4017c3...
export CHECKRD_AGENT_KEY=TM0Imyj/lto5tsNG7BFOD1uKMZ81q6Yk2oz27U+4pvs=

# Put the secret in your manager (k8s example):
kubectl create secret generic checkrd-sales-agent \
    --from-literal=CHECKRD_AGENT_KEY="$(checkrd keygen --private-only)" \
    --from-literal=CHECKRD_API_KEY="ck_live_..."
```

```python
import os
import httpx
from checkrd import wrap, LocalIdentity

client = wrap(
    httpx.Client(),
    agent_id="sales-agent",
    identity=LocalIdentity.from_env(),  # reads CHECKRD_AGENT_KEY
    api_key=os.environ["CHECKRD_API_KEY"],
    control_plane_url="https://api.checkrd.io",
)
```

All replicas of the agent read the **same** key from the secret. The first replica registers the public key with the control plane; subsequent replicas with the matching key are no-ops. One key per agent. One row in the database. Forever.

For secrets-manager SDK integration (boto3, google-cloud-secret-manager, azure-keyvault), use `LocalIdentity.from_bytes(secret_bytes)`:

```python
import boto3
from checkrd import LocalIdentity, wrap

raw = boto3.client("secretsmanager").get_secret_value(
    SecretId="checkrd/sales-agent"
)["SecretBinary"]
identity = LocalIdentity.from_bytes(raw)
client = wrap(httpx.Client(), agent_id="sales-agent", identity=identity, ...)
```

## Offline / Air-Gapped Setup

The WASM core has zero I/O dependencies, so the SDK runs without any control plane. This is the right model for regulated industries (defense, healthcare, certain finance) and any deployment where you don't want telemetry leaving your network.

```python
import httpx
from checkrd import wrap
from checkrd.sinks import JsonFileSink

client = wrap(
    httpx.Client(),
    agent_id="sales-agent",
    policy="/etc/checkrd/sales-agent.yaml",
    policy_watch=True,                                # hot-reload on file change
    killswitch_file="/var/lib/checkrd/killswitch",   # touch to disable
    telemetry_sink=JsonFileSink("/var/log/checkrd/sales-agent.jsonl"),
)
```

What you get:

- Policy enforcement at the network layer (the core value)
- Hot policy reload via the file watcher (no restart needed)
- Kill switch via `touch /var/lib/checkrd/killswitch` (and `rm` to re-enable)
- Telemetry as JSON lines, consumable by Vector / Fluent Bit / Promtail / Loki / fluentd / your log shipper of choice (use `logrotate` for rotation)
- No identity needed — there's no control plane to authenticate to, so no key management

What you give up:

- No centralized dashboard (use Grafana / Kibana / your existing observability stack)
- No real-time cross-instance kill switch (each host is independent)

### Custom telemetry sinks

Implement your own sink (OTLP exporter, Kafka producer, Sentry, Datadog Agent, etc.) by satisfying the `TelemetrySink` Protocol:

```python
from typing import Any
from checkrd.sinks import TelemetrySink
from checkrd import wrap

class MyOtlpSink:
    def enqueue(self, event: dict[str, Any]) -> None: ...
    def stop(self) -> None: ...

client = wrap(httpx.Client(), agent_id="X", telemetry_sink=MyOtlpSink(), ...)
```

Sinks must be thread-safe and non-blocking. Buffer internally and flush asynchronously — the SDK calls `enqueue()` from the request thread.

## Health Check

The `healthy()` function returns a status dict suitable for K8s readiness probes and monitoring:

```python
import checkrd

checkrd.init(policy="policy.yaml")

status = checkrd.healthy()
# {
#     "status": "healthy",        # "healthy" | "degraded" | "disabled"
#     "engine_loaded": True,
#     "control_plane_connected": None,
#     "agent_id": "sales-agent",
#     "enforce": True,
#     "last_eval_at": "2026-04-12T10:30:00Z",
#     "telemetry": {              # pipeline self-diagnostics (when batcher is active)
#         "sent": 4500,
#         "dropped_backpressure": 0,
#         "dropped_send_error": 12,
#         "pending": 42,
#     }
# }
```

Use in a Flask/FastAPI health endpoint:

```python
@app.get("/healthz")
def healthz():
    return checkrd.healthy()
```

## Lifecycle Hooks

Hooks let you observe or intercept policy decisions without modifying the transport:

```python
from checkrd import wrap, CheckrdEvent

def on_deny(event: CheckrdEvent) -> None:
    """Fire an alert when a request is denied."""
    print(f"DENIED: {event.method} {event.url} — {event.deny_reason}")
    # Send to Slack, PagerDuty, etc.

def on_allow(event: CheckrdEvent) -> None:
    """Track allowed requests for metrics."""
    metrics.increment("checkrd.allowed", tags=[f"url:{event.url}"])

def before_request(event: CheckrdEvent) -> CheckrdEvent | None:
    """Return None to skip policy evaluation entirely (pass-through)."""
    if event.url.startswith("https://internal."):
        return None  # skip evaluation for internal services
    return event

client = wrap(
    httpx.Client(),
    agent_id="agent",
    policy=policy,
    on_deny=on_deny,
    on_allow=on_allow,
    before_request=before_request,
)
```

Hook exceptions are caught and logged at WARNING level — a crashing hook never takes down a request.

**Security note:** Hooks receive sanitized headers — credential-bearing headers (`Authorization`, `X-API-Key`, `Cookie`, `Proxy-Authorization`) are stripped before reaching hook callbacks. This prevents user-written hooks from accidentally logging third-party API keys. The WASM policy engine still receives full headers (sandboxed, no I/O) for policy matching.

## Security Mode (fail-closed default)

Checkrd ships with a **fail-closed default**: if the WASM engine cannot
load, `init()` / `wrap()` raises `CheckrdInitError` rather than silently
passing traffic through unchecked. The security layer must not disable
itself.

```python
# Default — fail-closed. WASM init failure raises.
with checkrd.init(policy="policy.yaml"):
    checkrd.instrument()

# Opt-in to fail-open for gradual rollout. Logs a warning, passes
# traffic through without policy evaluation on engine error.
with checkrd.init(policy="policy.yaml", security_mode="permissive"):
    checkrd.instrument()
```

Env-var override: `CHECKRD_SECURITY_MODE=permissive`.

Requests with bodies over **1 MB** — the WASM inspection limit — are
denied in strict mode with `reason="body exceeds 1MB inspection limit"`
rather than silently skipping body matchers. Permissive mode logs a
warning and passes through.

## Graceful Degradation

Beyond the engine init path above, the SDK never crashes your
application on runtime failures:

- **Policy file missing** — SDK enters observation mode (allow-all,
  telemetry-only).
- **Control plane unreachable** — SDK keeps running with the last known
  policy. SSE reconnects with exponential backoff.
- **Telemetry batcher fails** — Events are dropped with a warning.
  Requests are unaffected.
- **Hook raises an exception** — Caught at WARNING level. Request
  continues normally.

Check the current mode:

```python
status = checkrd.healthy()
if status["status"] == "degraded":
    print("Running in pass-through mode (security_mode='permissive')")
```

## Auto-Instrumentation

Patch all detected AI libraries with zero code changes:

```python
import checkrd

checkrd.init(api_key="ck_live_...", policy="policy.yaml")
checkrd.instrument()  # patches openai, anthropic, cohere, groq, ...

# Use AI SDKs normally — every request goes through Checkrd
from openai import OpenAI
client = OpenAI()
response = client.chat.completions.create(model="gpt-4", messages=[...])
```

`init()` returns a context manager for automatic cleanup:

```python
with checkrd.init(policy="policy.yaml"):
    checkrd.instrument()
    # ... use instrumented clients ...
# shutdown() called automatically on exit
```

Instrument individual libraries:

```python
checkrd.instrument_openai()
checkrd.instrument_anthropic()
```

Supported: OpenAI, Anthropic, Cohere, Groq, Mistral, Together, Google GenAI.

Libraries that aren't installed are silently skipped. Calling `instrument()` twice is safe (idempotent).

## Framework Adapters

Vendor instrumentation works at the HTTP layer. For framework-native integration — `BaseCallbackHandler` for LangChain, `TracingProcessor` + `Guardrail` for OpenAI Agents, hooks for the Claude Agent SDK, handler-wrap and `ClientSession` subclass for MCP — Checkrd ships dedicated adapters under `checkrd.integrations.*`. Each uses the framework's documented public extension point — no monkey-patching, no internal-API risk.

| Framework                          | Adapter                                                            | Install                                  |
| ---------------------------------- | ------------------------------------------------------------------ | ---------------------------------------- |
| LangChain / LangGraph              | `from checkrd.integrations.langchain import CheckrdCallbackHandler` | `pip install 'checkrd[langchain]'`        |
| OpenAI Agents SDK                  | `from checkrd.integrations.openai_agents import CheckrdInputGuardrail, CheckrdTracingProcessor` | `pip install 'checkrd[openai-agents]'`    |
| Anthropic Claude Agent SDK         | `from checkrd.integrations.claude_agent_sdk import attach_to_options` | `pip install 'checkrd[claude-agent-sdk]'` |
| Model Context Protocol (MCP)       | `from checkrd.integrations.mcp import wrap_call_tool_handler, CheckrdClientSession` | `pip install 'checkrd[mcp]'`              |

Each adapter is documented at <https://checkrd.io/docs/integrations>. Operators write one policy YAML and the same rules fire across vendor instrumentors and framework adapters using framework-prefixed synthetic URLs (`langchain.local/...`, `openai-agents.local/...`, `claude-agent.local/...`, `<server-name>/tools/...`).

## File Watchers

For deployments without a control plane, watch files for live policy reload and kill switch:

```python
client = wrap(
    httpx.Client(),
    agent_id="agent",
    policy="/etc/checkrd/policy.yaml",
    policy_watch=True,                              # reload on file change
    policy_watch_interval_secs=5.0,                 # poll every 5s (default)
    killswitch_file="/var/lib/checkrd/killswitch",  # touch to kill, rm to re-enable
)
```

The policy watcher polls file mtime and hot-reloads on change. If the new policy is malformed, the previous policy is kept and a warning is logged.

## Testing

Use `mock_wrap()` for WASM-free unit tests — no binary required:

```python
from checkrd.testing import mock_wrap
import httpx

# Default deny
client = mock_wrap(httpx.Client(), default="deny")

# Custom policy function
client = mock_wrap(httpx.Client(), policy_fn=lambda method, url, headers, body: method == "GET")

# With hooks
client = mock_wrap(httpx.Client(), default="allow", on_deny=my_handler)
```

## Disabling

Bypass all policy evaluation without code changes:

```bash
CHECKRD_DISABLED=1 python my_agent.py
```

## Logging

Checkrd logs to the `checkrd` Python logger:

```python
import logging

# See all policy decisions
logging.getLogger("checkrd").setLevel(logging.INFO)

# See evaluation timing (microseconds per request)
logging.getLogger("checkrd").setLevel(logging.DEBUG)
```

Log levels:
- `DEBUG` -- evaluation timing per request
- `INFO` -- allowed requests with status code and latency
- `WARNING` -- denied requests, dry-run denials

## Telemetry Signing

When `wrap()` is called with both `control_plane_url` and `api_key`, every
telemetry batch sent to the Checkrd control plane is cryptographically signed
using the agent's Ed25519 identity:

```python
from checkrd import wrap
import httpx

client = wrap(
    httpx.Client(),
    agent_id="sales-agent",
    policy="policy.yaml",
    control_plane_url="https://api.checkrd.io",
    api_key="ck_live_...",
)
```

The signing happens transparently inside the background telemetry batcher,
using the agent's Ed25519 key managed by the default `LocalIdentity` (or your
custom `IdentityProvider`). Each signed request carries four standards-conformant
headers:

- **`Signature-Input`** and **`Signature`** ([RFC 9421 HTTP Message Signatures](https://www.rfc-editor.org/rfc/rfc9421.html))
  bind the request method, target URI, body digest, and signing agent ID into
  an Ed25519 signature. The control plane verifies against the agent's
  registered public key.
- **`Content-Digest`** ([RFC 9530](https://www.rfc-editor.org/rfc/rfc9530.html))
  carries the SHA-256 of the exact request body bytes, so any tampering with
  the body invalidates the signature.
- **`X-Checkrd-Signer-Agent`** carries the agent UUID for public-key lookup
  on the verifier side. Bound into the signature so it can't be swapped to
  impersonate another agent.

The signing path is anchored against authoritative test vectors:

- Ed25519 primitive: [RFC 8032 §7.1 known-answer vectors](https://www.rfc-editor.org/rfc/rfc8032.html#section-7.1)
  and [Project Wycheproof v1](https://github.com/C2SP/wycheproof) (150 vectors
  covering malleability, small-order keys, and other implementation-bug classes).
- HTTP Message Signatures: [RFC 9421 §B.2.6](https://www.rfc-editor.org/rfc/rfc9421.html#name-signing-a-request-using-ed2)
  end-to-end Ed25519 worked example. Our pipeline produces the spec's exact
  base string and the spec's exact published signature value byte-for-byte.
- Cross-implementation interop: a test in `tests/test_batcher.py` signs a
  batch via the SDK, then independently verifies the signature using the
  PyCA [`cryptography`](https://cryptography.io) library's Ed25519 verifier.
  This proves the SDK's signing path is interoperable with any RFC-conformant
  third-party implementation.

If your `IdentityProvider` doesn't have a local private key (e.g. an external
KMS / HSM provider where signing happens elsewhere), the batcher logs a
one-shot warning and falls back to unsigned telemetry. The control plane's
ingestion service can be configured for `off`, `warn`, or `required` signature
modes via `TELEMETRY_SIGNATURE_MODE`, allowing safe rollout.

## Security

- **Fail-closed by default** (`security_mode="strict"`). Engine init
  failures raise `CheckrdInitError`; the security layer never silently
  disables itself. Opt-in pass-through via `security_mode="permissive"`.
- **Body-size block in strict mode.** Requests with bodies over the 1 MB
  WASM inspection limit are denied rather than proceeding with body
  matchers skipped (would otherwise be a trivial bypass).
- WASM core runs in a sandbox — no filesystem, network, or system call
  access.
- WASM binary integrity verified via SHA-256 at load time (`_wasm_integrity.py`).
- Request/response bodies are never stored or transmitted in telemetry.
- Credential headers (`Authorization`, `X-API-Key`, `Cookie`,
  `Proxy-Authorization`, `X-Checkrd-API-Key`) are stripped from hook
  callbacks. Note: the WASM engine still sees full headers for policy
  matching (sandboxed, no I/O).
- Identity key files are created with `0600` permissions. Existing files
  with more permissive modes trigger a warning on load. Private key
  material in the wasmtime linear memory is overwritten after init;
  transient Python-heap copies remain until GC and are not claimed to
  be fully zeroized.
- Policy trust override (`CHECKRD_POLICY_TRUST_OVERRIDE_JSON` +
  `CHECKRD_ALLOW_TRUST_OVERRIDE=1`) is **local-dev only**. The
  production trust root is still being bootstrapped — until then, only
  the override path works. Never enable the override in production.
- Telemetry batches are signed with Ed25519 (RFC 9421 + 9530, see
  "Telemetry Signing" above) and verified by the control plane.
- Wheels published via **PyPI Trusted Publishing** with **Sigstore**
  signatures on each release.
- PEP 561 `py.typed` marker ships for `mypy` / `pyright` consumers.
- See [SECURITY.md](SECURITY.md) for vulnerability reporting and our
  coordinated-disclosure process.

## License

Apache-2.0
