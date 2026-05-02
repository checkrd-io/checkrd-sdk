"""Checkrd Control Plane API client.

The single recommended entry point is :class:`Checkrd` (sync) or
:class:`AsyncCheckrd` (async). Both expose the same resource
surface — pick by program style. Mirrors the OpenAI / Anthropic /
Stripe Python SDK shape.

Quickstart::

    from checkrd_api import Checkrd

    client = Checkrd(api_key="ck_live_...")
    for agent in client.agents.list():
        print(agent.name, agent.kill_switch_active)

Async equivalent::

    import asyncio
    from checkrd_api import AsyncCheckrd

    async def main() -> None:
        async with AsyncCheckrd(api_key="ck_live_...") as client:
            async for agent in client.agents.list():
                print(agent.name)

    asyncio.run(main())
"""
from __future__ import annotations

__version__ = "0.1.1"

from ._client import (
    DEFAULT_API_VERSION,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECS,
    AsyncCheckrd,
    Checkrd,
)
from ._exceptions import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    CheckrdError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from ._pagination import AsyncPage, SyncPage

__all__ = [
    # Public version
    "__version__",
    # Clients
    "Checkrd",
    "AsyncCheckrd",
    # Pagination
    "SyncPage",
    "AsyncPage",
    # Errors
    "CheckrdError",
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    # Defaults — exposed so callers can read them without importing
    # private modules.
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_API_VERSION",
]
