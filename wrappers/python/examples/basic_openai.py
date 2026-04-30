"""Basic OpenAI instrumentation.

Install::

    pip install checkrd openai

Run::

    export OPENAI_API_KEY=sk-...
    export CHECKRD_API_KEY=ck_live_...   # optional
    python basic_openai.py
"""
from __future__ import annotations

import os

import checkrd
from openai import OpenAI


def main() -> None:
    # One-time global setup. Reads CHECKRD_API_KEY from the environment.
    checkrd.init(policy="policy.yaml")
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
