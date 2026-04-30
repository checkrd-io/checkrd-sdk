"""Tier 3 air-gapped deployment.

Policy evaluation happens entirely in-process. Telemetry is written to
a local JSON Lines file instead of shipped to any cloud service.
Suitable for environments where outbound traffic to api.checkrd.io is
blocked — regulated enterprises, classified deployments, on-prem labs.

Install::

    pip install checkrd openai

Run::

    export OPENAI_API_KEY=sk-...
    python air_gapped.py
"""
from __future__ import annotations

import os
from pathlib import Path

import checkrd
from checkrd.sinks import JsonFileSink
from openai import OpenAI


def main() -> None:
    log_path = Path("/tmp/checkrd-events.jsonl")

    # No `api_key` → SDK runs without talking to the control plane.
    # The JsonFileSink appends newline-delimited JSON, readable by
    # Vector / Fluent Bit / Promtail / journalctl wrappers.
    checkrd.init(
        policy="policy.yaml",
        telemetry_sink=JsonFileSink(path=str(log_path)),
    )
    checkrd.instrument()

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Hello in five words."}],
    )
    checkrd.shutdown()

    print(f"Events written to {log_path}")


if __name__ == "__main__":
    main()
