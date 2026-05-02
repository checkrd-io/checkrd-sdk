from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.paginated_alert_notifications import PaginatedAlertNotifications
from ...types import UNSET, Response, Unset


def _get_kwargs(
    alert_id: UUID,
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
        "url": "/v1/alerts/{alert_id}/notifications".format(
            alert_id=quote(str(alert_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | PaginatedAlertNotifications | None:
    if response.status_code == 200:
        response_200 = PaginatedAlertNotifications.from_dict(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | PaginatedAlertNotifications]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAlertNotifications]:
    """
    Args:
        alert_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAlertNotifications]
    """

    kwargs = _get_kwargs(
        alert_id=alert_id,
        limit=limit,
        cursor=cursor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> ErrorResponse | PaginatedAlertNotifications | None:
    """
    Args:
        alert_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAlertNotifications
    """

    return sync_detailed(
        alert_id=alert_id,
        client=client,
        limit=limit,
        cursor=cursor,
    ).parsed


async def asyncio_detailed(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAlertNotifications]:
    """
    Args:
        alert_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAlertNotifications]
    """

    kwargs = _get_kwargs(
        alert_id=alert_id,
        limit=limit,
        cursor=cursor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
) -> ErrorResponse | PaginatedAlertNotifications | None:
    """
    Args:
        alert_id (UUID):
        limit (int | Unset):
        cursor (UUID | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAlertNotifications
    """

    return (
        await asyncio_detailed(
            alert_id=alert_id,
            client=client,
            limit=limit,
            cursor=cursor,
        )
    ).parsed
