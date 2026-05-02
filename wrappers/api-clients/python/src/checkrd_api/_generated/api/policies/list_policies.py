from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.paginated_policies import PaginatedPolicies
from ...types import UNSET, Response, Unset


def _get_kwargs(
    agent_id: UUID,
    *,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    json_cursor: str | Unset = UNSET
    if not isinstance(cursor, Unset):
        json_cursor = str(cursor)
    params["cursor"] = json_cursor

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/agents/{agent_id}/policies".format(
            agent_id=quote(str(agent_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | PaginatedPolicies | None:
    if response.status_code == 200:
        response_200 = PaginatedPolicies.from_dict(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if response.status_code == 404:
        response_404 = ErrorResponse.from_dict(response.json())

        return response_404

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | PaginatedPolicies]:
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
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedPolicies]:
    """
    Args:
        agent_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedPolicies]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        limit=limit,
        cursor=cursor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> ErrorResponse | PaginatedPolicies | None:
    """
    Args:
        agent_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedPolicies
    """

    return sync_detailed(
        agent_id=agent_id,
        client=client,
        limit=limit,
        cursor=cursor,
    ).parsed


async def asyncio_detailed(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedPolicies]:
    """
    Args:
        agent_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedPolicies]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        limit=limit,
        cursor=cursor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> ErrorResponse | PaginatedPolicies | None:
    """
    Args:
        agent_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedPolicies
    """

    return (
        await asyncio_detailed(
            agent_id=agent_id,
            client=client,
            limit=limit,
            cursor=cursor,
        )
    ).parsed
