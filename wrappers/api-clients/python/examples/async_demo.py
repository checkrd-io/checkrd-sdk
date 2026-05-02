#!/usr/bin/env python3
"""Async parity demo — `AsyncCheckrd` exposes the same surface as the
sync client, every method is a coroutine, and ``async for`` walks
paginated lists transparently."""

from __future__ import annotations

import asyncio
import os

from checkrd_api import AsyncCheckrd


async def main() -> None:
    async with AsyncCheckrd(api_key=os.environ["CHECKRD_API_KEY"]) as client:
        agent = await client.agents.create(name="async-demo-agent")
        print(f"created {agent.id} ({agent.name})")

        async for a in client.agents.list():
            print(f"  {a.id}\t{a.name}")

        # Trigger the kill switch and clean up.
        await client.agents.toggle_kill_switch(
            agent.id,
            active=True,
            reason="demo cleanup",
        )
        await client.agents.delete(agent.id)
        print(f"\ndeleted {agent.id}")


if __name__ == "__main__":
    asyncio.run(main())
