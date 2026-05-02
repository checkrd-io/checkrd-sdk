from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.org_replay_request import OrgReplayRequest
from ...models.org_replay_response import OrgReplayResponse
from ...types import Response


def _get_kwargs(
    *,
    body: OrgReplayRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/org-policies/replay",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | OrgReplayResponse | None:
    if response.status_code == 200:
        response_200 = OrgReplayResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = ErrorResponse.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | OrgReplayResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: OrgReplayRequest,
) -> Response[ErrorResponse | OrgReplayResponse]:
    """Replay the candidate org policy against recent events from *every*
    inheriting agent in the workspace. Aggregates verdict counts and
    surfaces the set of events whose verdict would change under the new
    policy. Stateless — a fresh `RateLimiter` per event matches the
    per-agent replay semantics (treat `would_rate_limit` as approximate).

     Any authenticated member can trigger this (viewer+). We enforce the
    same org-scope gate as the other org-policies routes via `live_role`.

    Args:
        body (OrgReplayRequest): `POST /v1/org-policies/replay` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | OrgReplayResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: OrgReplayRequest,
) -> ErrorResponse | OrgReplayResponse | None:
    """Replay the candidate org policy against recent events from *every*
    inheriting agent in the workspace. Aggregates verdict counts and
    surfaces the set of events whose verdict would change under the new
    policy. Stateless — a fresh `RateLimiter` per event matches the
    per-agent replay semantics (treat `would_rate_limit` as approximate).

     Any authenticated member can trigger this (viewer+). We enforce the
    same org-scope gate as the other org-policies routes via `live_role`.

    Args:
        body (OrgReplayRequest): `POST /v1/org-policies/replay` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | OrgReplayResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: OrgReplayRequest,
) -> Response[ErrorResponse | OrgReplayResponse]:
    """Replay the candidate org policy against recent events from *every*
    inheriting agent in the workspace. Aggregates verdict counts and
    surfaces the set of events whose verdict would change under the new
    policy. Stateless — a fresh `RateLimiter` per event matches the
    per-agent replay semantics (treat `would_rate_limit` as approximate).

     Any authenticated member can trigger this (viewer+). We enforce the
    same org-scope gate as the other org-policies routes via `live_role`.

    Args:
        body (OrgReplayRequest): `POST /v1/org-policies/replay` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | OrgReplayResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: OrgReplayRequest,
) -> ErrorResponse | OrgReplayResponse | None:
    """Replay the candidate org policy against recent events from *every*
    inheriting agent in the workspace. Aggregates verdict counts and
    surfaces the set of events whose verdict would change under the new
    policy. Stateless — a fresh `RateLimiter` per event matches the
    per-agent replay semantics (treat `would_rate_limit` as approximate).

     Any authenticated member can trigger this (viewer+). We enforce the
    same org-scope gate as the other org-policies routes via `live_role`.

    Args:
        body (OrgReplayRequest): `POST /v1/org-policies/replay` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | OrgReplayResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
