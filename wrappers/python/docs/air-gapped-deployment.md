# Air-gapped deployment (Tier 3)

The Checkrd SDK is designed to run correctly in environments where
outbound traffic to `api.checkrd.io` is blocked — regulated
enterprises, classified deployments, on-prem research labs, customer
VPCs with egress allowlists. This guide walks through an end-to-end
Tier 3 setup.

## What "Tier 3" means

Checkrd defines three deployment tiers:

| Tier | Control plane | Telemetry | Policy source |
|---|---|---|---|
| 1 (Cloud) | Managed at `api.checkrd.io` | Shipped to Checkrd Cloud | Dashboard or YAML |
| 2 (Self-host) | Customer-run API + ClickHouse | Shipped to customer stack | Dashboard or YAML |
| 3 (Air-gapped) | **None** | **Local file / OTel / syslog** | **YAML only** |

In Tier 3 there is no outbound Checkrd traffic. Policy evaluation
runs entirely in the WASM core inside your process; telemetry is
serialised to a sink you own (file, OTLP collector, Kafka, etc.).

## Prerequisites

- Python 3.9+ with `pip`.
- The `checkrd` wheel (pre-download from PyPI or mirror to your
  internal package index).
- A policy file in YAML — see the [Policy format](../README.md#policy-format)
  section of the main README.
- An observability destination you control (filesystem, Vector,
  Fluent Bit, OTLP collector, syslog, Kafka, ...).

## 1. Install from an internal mirror

Most air-gapped environments proxy PyPI through an internal
repository (Artifactory, Nexus, devpi, etc.). Install with the
normal `pip install`:

```bash
pip install checkrd
```

If there is no mirror, download the wheel on a connected machine and
transport it in:

```bash
# on connected machine
pip download checkrd -d ./wheels
# copy ./wheels to the air-gapped host, then:
pip install --no-index --find-links ./wheels checkrd
```

The wheel bundles the `checkrd_core.wasm` binary. No runtime network
fetch is required for the WASM itself — it ships in-wheel and is
SHA-256-verified on import.

## 2. Author a policy file

```yaml
# /etc/checkrd/agent.yaml
agent: production-agent
default: deny

rules:
  - name: allow-anthropic-reads
    allow:
      method: [GET, POST]
      url: "api.anthropic.com/v1/messages"

  - name: block-deletes
    deny:
      method: [DELETE]
      url: "*"
```

Keep the file in version control. Treat changes to it like code.

## 3. Wire up a telemetry sink

Pick a destination. Three common patterns:

### JSON Lines on disk

Simplest — every event is one JSON object per line. Compatible with
Vector, Fluent Bit, Promtail, Loki, logrotate.

```python
import checkrd
from checkrd.sinks import JsonFileSink

checkrd.init(
    policy="/etc/checkrd/agent.yaml",
    telemetry_sink=JsonFileSink(path="/var/log/checkrd/events.jsonl"),
)
checkrd.instrument()
```

### OTLP to an internal collector

If your stack already has an OpenTelemetry Collector, forward to it.
Any OTLP/HTTP endpoint works — Grafana Tempo, Jaeger, Datadog Agent
running with OTLP receivers, Honeycomb Tier 2 self-hosted, etc.

```bash
pip install 'checkrd[otlp]'
```

```python
from checkrd.sinks import OtlpSink

checkrd.init(
    policy="/etc/checkrd/agent.yaml",
    telemetry_sink=OtlpSink(
        endpoint="http://otel-collector.internal:4318",
        service_name="checkrd-agent",
    ),
)
```

### Custom sink (Kafka, S3, syslog, ...)

Implement the `TelemetrySink` protocol:

```python
from checkrd.sinks import TelemetrySink

class KafkaSink:
    def __init__(self, producer):
        self._producer = producer

    def enqueue(self, event):
        self._producer.send("checkrd-events", value=event)

    def close(self):
        self._producer.flush()

checkrd.init(policy="/etc/checkrd/agent.yaml", telemetry_sink=KafkaSink(...))
```

`enqueue()` must be non-blocking — buffer internally if your
destination performs I/O.

## 4. Identity in air-gapped mode

Telemetry signing is still meaningful even without a control plane —
downstream consumers can verify batches came from the agent and
haven't been tampered with. Two identity options:

### Generate a keypair on first run

Default behaviour. The SDK generates an Ed25519 keypair, signs every
batch, and writes nothing to the network. Public-key distribution is
your problem: export it and share with verifiers out-of-band.

```python
from checkrd.identity import LocalIdentity

identity = LocalIdentity.generate()
print("public key:", identity.public_key.hex())
# Distribute the public key to any downstream verifier.

checkrd.init(policy="/etc/checkrd/agent.yaml", identity=identity)
```

### Load from a managed secret

If you have an internal secrets system (Vault, 1Password CLI,
`pass`), pull the key at startup:

```python
from checkrd.identity import LocalIdentity

key_bytes = load_from_vault("checkrd/production-agent/key")
identity = LocalIdentity.from_bytes(key_bytes)
checkrd.init(policy="/etc/checkrd/agent.yaml", identity=identity)
```

The `CHECKRD_AGENT_KEY` environment variable (base64-encoded 32-byte
private key) is a convenient alternative when your secret manager
materialises values as env vars.

### External signer (HSM / KMS)

When the private key must never leave a hardware boundary, implement
`ExternalIdentity` and sign batches out-of-band. See
[`examples/custom_identity_kms.py`](../examples/custom_identity_kms.py).

## 5. Kill-switch via filesystem

Without a control plane there is no SSE kill switch. Use a file
watcher instead:

```bash
# in /etc/checkrd/killswitch — absent = active, present = paused
touch /etc/checkrd/killswitch   # pause all outbound
rm   /etc/checkrd/killswitch   # resume
```

```python
checkrd.init(
    policy="/etc/checkrd/agent.yaml",
    killswitch_file="/etc/checkrd/killswitch",
    killswitch_interval=2.0,  # seconds between checks
)
```

The file is polled on a background thread; toggling it takes effect
within one poll interval. Same semantics as the cloud kill switch:
every outbound request immediately denies while the file exists.

## 6. Policy reloads

Similarly, without a control plane there is no SSE policy update.
Watch the policy file on disk:

```python
checkrd.init(
    policy="/etc/checkrd/agent.yaml",
    policy_watch=True,
    policy_watch_interval=5.0,
)
```

The SDK re-reads the file when its mtime changes. Invalid YAML is
rejected with a warning; the previous good policy stays in effect.

## 7. Readiness checks

`checkrd.healthy()` returns a dict suitable for `/healthz`:

```python
from fastapi import FastAPI
app = FastAPI()

@app.get("/healthz")
def healthz():
    return checkrd.healthy()
```

Tier 3 fields of interest:

- `status` — `healthy`, `degraded`, or `disabled`
- `engine_loaded` — `True` when the WASM core is ready
- `enforce` — matches the effective mode

Because there is no control plane in Tier 3, `control_plane_connected`
is always `False`; this is expected.

## 8. Logs, metrics, and audit

Redirect `logging.getLogger("checkrd")` to wherever your other Python
service logs end up. The SDK redacts every credential-bearing header
before it reaches a handler, so these logs are safe to ingest into
any system including shared log search:

```python
import logging

logging.getLogger("checkrd").setLevel(logging.INFO)
```

## Verification checklist

Before calling the deployment live:

- [ ] `pip install checkrd` succeeds from your internal mirror.
- [ ] `python -c "import checkrd; checkrd.init(policy='...')"` runs
      without errors.
- [ ] A test request is evaluated correctly (allow and deny paths
      both exercised).
- [ ] Telemetry events appear at the configured sink destination.
- [ ] Killswitch file toggling visibly pauses and resumes requests.
- [ ] Policy file reload picks up changes without a process restart.
- [ ] No outbound traffic to `api.checkrd.io` observed in egress
      logs (`tcpdump`, firewall, proxy logs).

## What's not supported in Tier 3

- Hosted dashboard (there's no control plane to back it).
- Cross-tenant policy aggregation.
- Signed policy bundle distribution from Checkrd Cloud (you manage
  policy files via your own VCS / config management).
- Automatic public-key registration with the control plane — if a
  downstream verifier needs the public key, distribute it
  out-of-band.

Everything the SDK does on the request path — policy evaluation,
kill switch, rate limiting, telemetry signing, logging, hooks —
works identically to Tier 1 / Tier 2.
