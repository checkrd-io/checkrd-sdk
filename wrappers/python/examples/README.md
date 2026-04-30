# Checkrd Python examples

Runnable examples demonstrating common Checkrd deployment patterns.
Each file is self-contained — install the SDK (`pip install checkrd`)
plus any vendor library the example mentions, then run the script.

| File | What it shows |
|---|---|
| [`basic_openai.py`](./basic_openai.py) | 5-line instrumentation of the OpenAI SDK |
| [`langchain_transparent.py`](./langchain_transparent.py) | LangChain works transparently via vendor SDK instrumentation |
| [`otlp_datadog.py`](./otlp_datadog.py) | Dual-export telemetry to Datadog via OTLP |
| [`air_gapped.py`](./air_gapped.py) | Tier 3 deployment with no control plane |
| [`custom_identity_kms.py`](./custom_identity_kms.py) | External identity provider (AWS KMS pattern) |

## Environment

Every example expects at minimum a policy file. The scripts read from
a `policy.yaml` in the current working directory — a minimal one:

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
