"""Agent resource — wraps `/v1/agents/*`.

Mirrors OpenAI's ``client.fine_tuning.jobs.list()`` pattern:
``client.agents.list()`` returns an iterable page; ``retrieve()`` is
the singular fetcher; mutations (``create``, ``update``, ``delete``,
``toggle_kill_switch``, ``register_public_key``) take typed kwargs.

Models are imported from the generator output under
``_generated.models`` — the generator is the source of truth for
field shapes; this file is the source of truth for the ergonomic
surface.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .._generated.models import (
    Agent,
    CreateAgentRequest,
    DeleteResponse,
    KillSwitchRequest,
    PaginatedAgents,
    RegisterPublicKeyRequest,
    RegisterPublicKeyResponse,
    UpdateAgentRequest,
)
from .._pagination import AsyncPage, SyncPage

if TYPE_CHECKING:
    from .._client import AsyncCheckrd, Checkrd


def _agent_from_dict(item: dict) -> Agent:
    """Decode a single agent JSON object into the generated
    :class:`Agent` model. Used by both sync and async pagination
    so the ``data`` element type is consistent."""
    return Agent.from_dict(item)


class Agents:
    """Synchronous agent operations. Reached via ``client.agents``."""

    def __init__(self, client: "Checkrd") -> None:
        self._client = client

    def list(
        self,
        *,
        limit: int = 20,
        cursor: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> SyncPage[Agent]:
        """List agents in the caller's workspace.

        Returns a :class:`SyncPage` that is both the current page
        (``page.data``, ``page.has_more``) and an iterator that
        walks every page transparently. Most callers just::

            for agent in client.agents.list():
                print(agent.name)

        ``limit`` clamps the per-page size (1-100, default 20).
        ``cursor`` is opaque; pass the value of ``page.next_cursor``
        to resume from a previous list call.
        """
        return self._client._get_api_list(
            "/v1/agents",
            params={"limit": limit, "cursor": cursor},
            item_decoder=_agent_from_dict,
        )

    def retrieve(
        self,
        agent_id: str,
        *,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        """Fetch a single agent by ID. Raises
        :class:`~checkrd_api.NotFoundError` if the agent does not
        exist or belongs to a different workspace."""
        body = self._client._get(
            f"/v1/agents/{agent_id}",
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        """Create an agent in the caller's workspace.

        ``name`` must be unique within the workspace and 1-128
        characters. ``description`` is optional and surfaced on the
        dashboard.
        """
        request = CreateAgentRequest(name=name, description=description)
        body = self._client._post(
            "/v1/agents",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    def update(
        self,
        agent_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        """Update an agent's name and/or description. Omitted fields
        leave the existing values in place."""
        request = UpdateAgentRequest(name=name, description=description)
        body = self._client._put(
            f"/v1/agents/{agent_id}",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    def delete(
        self,
        agent_id: str,
        *,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> DeleteResponse:
        """Soft-delete an agent. Requires the Admin role."""
        body = self._client._delete(
            f"/v1/agents/{agent_id}",
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return DeleteResponse.from_dict(body)

    def toggle_kill_switch(
        self,
        agent_id: str,
        *,
        active: bool,
        reason: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        """Engage or release the kill switch.

        When ``active=True``, every outbound call from the agent's
        SDK starts denying within ~1 second (SSE notification, with
        polling fallback). ``reason`` shows up in the audit log.
        """
        request = KillSwitchRequest(active=active, reason=reason)
        body = self._client._post(
            f"/v1/agents/{agent_id}/kill-switch",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    def register_public_key(
        self,
        agent_id: str,
        *,
        public_key: str,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> RegisterPublicKeyResponse:
        """Bind an Ed25519 public key to the agent. Idempotent — a
        second registration of the same key is a no-op; a different
        key returns 409 Conflict.

        ``public_key`` is hex-encoded (64 chars). Once registered,
        all subsequent telemetry batches from this agent are verified
        against this key.
        """
        request = RegisterPublicKeyRequest(public_key=public_key)
        body = self._client._post(
            f"/v1/agents/{agent_id}/public-key",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return RegisterPublicKeyResponse.from_dict(body)


class AsyncAgents:
    """Asynchronous agent operations. Reached via ``client.agents``
    on :class:`AsyncCheckrd`. Mirrors :class:`Agents` exactly; every
    method is a coroutine."""

    def __init__(self, client: "AsyncCheckrd") -> None:
        self._client = client

    async def list(
        self,
        *,
        limit: int = 20,
        cursor: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> AsyncPage[Agent]:
        return await self._client._get_api_list(
            "/v1/agents",
            params={"limit": limit, "cursor": cursor},
            item_decoder=_agent_from_dict,
        )

    async def retrieve(
        self,
        agent_id: str,
        *,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        body = await self._client._get(
            f"/v1/agents/{agent_id}",
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    async def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        request = CreateAgentRequest(name=name, description=description)
        body = await self._client._post(
            "/v1/agents",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    async def update(
        self,
        agent_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        request = UpdateAgentRequest(name=name, description=description)
        body = await self._client._put(
            f"/v1/agents/{agent_id}",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    async def delete(
        self,
        agent_id: str,
        *,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> DeleteResponse:
        body = await self._client._delete(
            f"/v1/agents/{agent_id}",
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return DeleteResponse.from_dict(body)

    async def toggle_kill_switch(
        self,
        agent_id: str,
        *,
        active: bool,
        reason: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Agent:
        request = KillSwitchRequest(active=active, reason=reason)
        body = await self._client._post(
            f"/v1/agents/{agent_id}/kill-switch",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return Agent.from_dict(body)

    async def register_public_key(
        self,
        agent_id: str,
        *,
        public_key: str,
        extra_headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> RegisterPublicKeyResponse:
        request = RegisterPublicKeyRequest(public_key=public_key)
        body = await self._client._post(
            f"/v1/agents/{agent_id}/public-key",
            json_body=request.to_dict(),
            extra_headers=extra_headers,
            timeout=timeout,
        )
        return RegisterPublicKeyResponse.from_dict(body)
