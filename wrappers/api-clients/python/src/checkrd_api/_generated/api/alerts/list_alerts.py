from http import HTTPStatus
from typing import Any
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.paginated_alert_rules import PaginatedAlertRules
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    alert_state: str | Unset = UNSET,
    condition_type: str | Unset = UNSET,
    is_enabled: bool | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    json_cursor: str | Unset = UNSET
    if not isinstance(cursor, Unset):
        json_cursor = str(cursor)
    params["cursor"] = json_cursor

    json_agent_id: str | Unset = UNSET
    if not isinstance(agent_id, Unset):
        json_agent_id = str(agent_id)
    params["agent_id"] = json_agent_id

    params["alert_state"] = alert_state

    params["condition_type"] = condition_type

    params["is_enabled"] = is_enabled

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/alerts",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | PaginatedAlertRules | None:
    if response.status_code == 200:
        response_200 = PaginatedAlertRules.from_dict(response.json())

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
) -> Response[ErrorResponse | PaginatedAlertRules]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    alert_state: str | Unset = UNSET,
    condition_type: str | Unset = UNSET,
    is_enabled: bool | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAlertRules]:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        agent_id (UUID | Unset):
        alert_state (str | Unset):
        condition_type (str | Unset):
        is_enabled (bool | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAlertRules]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
        agent_id=agent_id,
        alert_state=alert_state,
        condition_type=condition_type,
        is_enabled=is_enabled,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    alert_state: str | Unset = UNSET,
    condition_type: str | Unset = UNSET,
    is_enabled: bool | Unset = UNSET,
) -> ErrorResponse | PaginatedAlertRules | None:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        agent_id (UUID | Unset):
        alert_state (str | Unset):
        condition_type (str | Unset):
        is_enabled (bool | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAlertRules
    """

    return sync_detailed(
        client=client,
        limit=limit,
        cursor=cursor,
        agent_id=agent_id,
        alert_state=alert_state,
        condition_type=condition_type,
        is_enabled=is_enabled,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    alert_state: str | Unset = UNSET,
    condition_type: str | Unset = UNSET,
    is_enabled: bool | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAlertRules]:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        agent_id (UUID | Unset):
        alert_state (str | Unset):
        condition_type (str | Unset):
        is_enabled (bool | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAlertRules]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
        agent_id=agent_id,
        alert_state=alert_state,
        condition_type=condition_type,
        is_enabled=is_enabled,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    alert_state: str | Unset = UNSET,
    condition_type: str | Unset = UNSET,
    is_enabled: bool | Unset = UNSET,
) -> ErrorResponse | PaginatedAlertRules | None:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        agent_id (UUID | Unset):
        alert_state (str | Unset):
        condition_type (str | Unset):
        is_enabled (bool | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAlertRules
    """

    return (
        await asyncio_detailed(
            client=client,
            limit=limit,
            cursor=cursor,
            agent_id=agent_id,
            alert_state=alert_state,
            condition_type=condition_type,
            is_enabled=is_enabled,
        )
    ).parsed
