"""Basic OpenAI instrumentation.

Install::

    pip install checkrd openai

Run::

    export OPENAI_API_KEY=sk-...
    export CHECKRD_API_KEY=ck_live_...
    export CHECKRD_AGENT_ID=...    # UUID from your dashboard
    python basic_openai.py

The SDK boots, fetches the agent's published policy bundle from the
control plane, installs it, and starts enforcing — all inside
``checkrd.init()``. The dashboard is the source of truth; the SDK
never reads a local ``policy.yaml`` when a control-plane API key is
set (it would silently shadow the published bundle, which is exactly
the kind of thing Checkrd is designed to prevent).
"""
from __future__ import annotations

import os

import checkrd
from openai import OpenAI


def main() -> None:
    # One-time global setup. Reads CHECKRD_API_KEY and
    # CHECKRD_AGENT_ID from the environment, then fetches your
    # dashboard's published policy bundle before returning.
    checkrd.init()
    checkrd.instrument()

    # Every `OpenAI()` created after `instrument()` is transparently
    # routed through the Checkrd policy engine.
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Hello in five words."}],
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
