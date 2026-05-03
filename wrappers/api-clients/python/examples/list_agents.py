#!/usr/bin/env python3
"""List every agent in the caller's workspace, paginating transparently."""

from __future__ import annotations

import os

from checkrd_api import Checkrd


def main() -> None:
    client = Checkrd(api_key=os.environ["CHECKRD_API_KEY"])

    # The for-loop fetches subsequent pages on demand.
    count = 0
    for agent in client.agents.list():
        print(f"{agent.id}\t{agent.name}\t{'killed' if agent.kill_switch_active else 'live'}")
        count += 1

    print(f"\n{count} agents in workspace.")


if __name__ == "__main__":
    main()
