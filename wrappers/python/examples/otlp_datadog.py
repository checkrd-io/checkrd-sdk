"""Dual-export telemetry: Checkrd control plane + Datadog via OTLP.

Sends every policy decision and request event to two destinations:

  1. The Checkrd control plane (kill-switch, policy distribution).
  2. Datadog (or any OTLP/HTTP endpoint) for correlation with your
     existing APM dashboards.

Install::

    pip install 'checkrd[otlp]' openai

Run::

    export OPENAI_API_KEY=sk-...
    export CHECKRD_API_KEY=ck_live_...
    export CHECKRD_AGENT_ID=...        # UUID from your dashboard
    export DD_API_KEY=...
    python otlp_datadog.py
"""
from __future__ import annotations

import os

import checkrd
from checkrd.sinks import CompositeSink, ControlPlaneSink, OtlpSink
from openai import OpenAI


def main() -> None:
    # The OtlpSink translates Checkrd events into OTel spans. Any
    # OTLP/HTTP-JSON collector works — here we target Datadog's
    # agentless intake.
    otlp = OtlpSink(
        endpoint="https://otlp.datadoghq.com:4318",
        headers={"DD-API-KEY": os.environ["DD_API_KEY"]},
        service_name="checkrd-example",
    )

    # CompositeSink fans events out to both the control plane and
    # OTLP. ``init()`` reads CHECKRD_API_KEY / CHECKRD_AGENT_ID
    # from the env, fetches your dashboard's published policy, and
    # installs it before returning — the dashboard is the source
    # of truth and ``policy=`` in app code is intentionally
    # refused alongside a control-plane API key.
    checkrd.init(
        telemetry_sink=CompositeSink([ControlPlaneSink(), otlp]),
    )
    checkrd.instrument()

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Hello in five words."}],
    )
    # Spans flush to Datadog on shutdown.
    checkrd.shutdown()


if __name__ == "__main__":
    main()
