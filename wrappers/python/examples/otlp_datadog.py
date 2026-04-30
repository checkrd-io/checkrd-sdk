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

    # CompositeSink fans events out to both the control plane and OTLP.
    # Pass `telemetry_sink=` to override the default single-destination
    # sink.
    checkrd.init(
        policy="policy.yaml",
        api_key=os.environ["CHECKRD_API_KEY"],
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
