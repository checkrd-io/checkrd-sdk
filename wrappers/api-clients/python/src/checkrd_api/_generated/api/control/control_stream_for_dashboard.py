from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...types import Response


def _get_kwargs(
    agent_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/agents/{agent_id}/control/dashboard-stream".format(
            agent_id=quote(str(agent_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ErrorResponse | str | None:
    if response.status_code == 200:
        response_200 = response.text
        return response_200

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if response.status_code == 404:
        response_404 = ErrorResponse.from_dict(response.json())

        return response_404

    if response.status_code == 500:
        response_500 = ErrorResponse.from_dict(response.json())

        return response_500

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ErrorResponse | str]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | str]:
    """JWT-authenticated SSE stream for the dashboard.

     Same payload shape as the SDK-facing `/control` stream (init event +
    `kill_switch` / `policy_updated` events) but uses the dashboard's JWT
    cookie auth instead of the SDK `X-API-Key` header. EventSource in
    browsers sends same-origin cookies automatically, so there's no
    client-side token plumbing — the cookie just needs to be present.

    Lives at a distinct path (`/control/dashboard-stream`) so dashboard
    middleware (CSRF / rate-limit tier) applies naturally via the
    `/v1/*` prefix, and so SDKs can't accidentally consume a JWT-gated
    endpoint.

    Args:
        agent_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | str]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | str | None:
    """JWT-authenticated SSE stream for the dashboard.

     Same payload shape as the SDK-facing `/control` stream (init event +
    `kill_switch` / `policy_updated` events) but uses the dashboard's JWT
    cookie auth instead of the SDK `X-API-Key` header. EventSource in
    browsers sends same-origin cookies automatically, so there's no
    client-side token plumbing — the cookie just needs to be present.

    Lives at a distinct path (`/control/dashboard-stream`) so dashboard
    middleware (CSRF / rate-limit tier) applies naturally via the
    `/v1/*` prefix, and so SDKs can't accidentally consume a JWT-gated
    endpoint.

    Args:
        agent_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | str
    """

    return sync_detailed(
        agent_id=agent_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | str]:
    """JWT-authenticated SSE stream for the dashboard.

     Same payload shape as the SDK-facing `/control` stream (init event +
    `kill_switch` / `policy_updated` events) but uses the dashboard's JWT
    cookie auth instead of the SDK `X-API-Key` header. EventSource in
    browsers sends same-origin cookies automatically, so there's no
    client-side token plumbing — the cookie just needs to be present.

    Lives at a distinct path (`/control/dashboard-stream`) so dashboard
    middleware (CSRF / rate-limit tier) applies naturally via the
    `/v1/*` prefix, and so SDKs can't accidentally consume a JWT-gated
    endpoint.

    Args:
        agent_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | str]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | str | None:
    """JWT-authenticated SSE stream for the dashboard.

     Same payload shape as the SDK-facing `/control` stream (init event +
    `kill_switch` / `policy_updated` events) but uses the dashboard's JWT
    cookie auth instead of the SDK `X-API-Key` header. EventSource in
    browsers sends same-origin cookies automatically, so there's no
    client-side token plumbing — the cookie just needs to be present.

    Lives at a distinct path (`/control/dashboard-stream`) so dashboard
    middleware (CSRF / rate-limit tier) applies naturally via the
    `/v1/*` prefix, and so SDKs can't accidentally consume a JWT-gated
    endpoint.

    Args:
        agent_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | str
    """

    return (
        await asyncio_detailed(
            agent_id=agent_id,
            client=client,
        )
    ).parsed
