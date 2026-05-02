#!/usr/bin/env python3
"""Demonstrate the per-status error class hierarchy.

The SDK raises a typed subclass for every documented status code so
callers can ``except`` the specific case they care about. All
subclasses share an `APIError` base; everything that ever leaves the
SDK inherits from `CheckrdError`."""

from __future__ import annotations

import os

import checkrd_api
from checkrd_api import (
    AuthenticationError,
    Checkrd,
    NotFoundError,
    RateLimitError,
)


def main() -> None:
    client = Checkrd(api_key=os.environ.get("CHECKRD_API_KEY", "ck_live_invalid"))

    try:
        client.agents.retrieve("00000000-0000-0000-0000-000000000000")
    except AuthenticationError as e:
        print(f"401 — bad credentials. request_id={e.request_id}")
    except NotFoundError as e:
        print(f"404 — agent does not exist in this workspace. message={e.message}")
    except RateLimitError as e:
        print(f"429 — slow down. retry after: {e.response.headers.get('retry-after')}s")
    except checkrd_api.APIStatusError as e:
        print(f"unexpected status {e.status_code}: {e.message}")
    except checkrd_api.APIConnectionError:
        print("could not reach the control plane (DNS, TCP, TLS)")


if __name__ == "__main__":
    main()
