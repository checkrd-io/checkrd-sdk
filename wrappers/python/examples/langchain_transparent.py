"""LangChain runs through Checkrd with zero extra wiring.

LangChain's model integrations (ChatOpenAI, ChatAnthropic, ChatCohere,
...) delegate to the underlying vendor SDKs, whose HTTP transports are
patched by `checkrd.instrument()`. The policy engine sees every
outbound call without any LangChain-specific adapter.

Install::

    pip install checkrd openai langchain-openai

Run::

    export OPENAI_API_KEY=sk-...
    export CHECKRD_API_KEY=ck_live_...
    export CHECKRD_AGENT_ID=...    # UUID from your dashboard
    python langchain_transparent.py
"""
from __future__ import annotations

import checkrd
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI


def main() -> None:
    # ``init()`` reads CHECKRD_API_KEY + CHECKRD_AGENT_ID from the
    # environment, fetches your dashboard's published policy, and
    # installs it before the vendor SDK ever ships its first byte.
    checkrd.init()
    checkrd.instrument()

    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    response = llm.invoke([HumanMessage(content="Hello in five words.")])
    print(response.content)


if __name__ == "__main__":
    main()
