import datetime
from http import HTTPStatus
from typing import Any
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.paginated_audit_log import PaginatedAuditLog
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    resource_type: str | Unset = UNSET,
    action: str | Unset = UNSET,
    actor_id: UUID | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    search: str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    json_cursor: str | Unset = UNSET
    if not isinstance(cursor, Unset):
        json_cursor = str(cursor)
    params["cursor"] = json_cursor

    params["resource_type"] = resource_type

    params["action"] = action

    json_actor_id: str | Unset = UNSET
    if not isinstance(actor_id, Unset):
        json_actor_id = str(actor_id)
    params["actor_id"] = json_actor_id

    json_from_: str | Unset = UNSET
    if not isinstance(from_, Unset):
        json_from_ = from_.isoformat()
    params["from"] = json_from_

    json_to: str | Unset = UNSET
    if not isinstance(to, Unset):
        json_to = to.isoformat()
    params["to"] = json_to

    params["search"] = search

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/audit-log",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | PaginatedAuditLog | None:
    if response.status_code == 200:
        response_200 = PaginatedAuditLog.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = ErrorResponse.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if response.status_code == 402:
        response_402 = ErrorResponse.from_dict(response.json())

        return response_402

    if response.status_code == 403:
        response_403 = ErrorResponse.from_dict(response.json())

        return response_403

    if response.status_code == 404:
        response_404 = ErrorResponse.from_dict(response.json())

        return response_404

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | PaginatedAuditLog]:
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
    resource_type: str | Unset = UNSET,
    action: str | Unset = UNSET,
    actor_id: UUID | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    search: str | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAuditLog]:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        resource_type (str | Unset):
        action (str | Unset):
        actor_id (UUID | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAuditLog]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
        resource_type=resource_type,
        action=action,
        actor_id=actor_id,
        from_=from_,
        to=to,
        search=search,
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
    resource_type: str | Unset = UNSET,
    action: str | Unset = UNSET,
    actor_id: UUID | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    search: str | Unset = UNSET,
) -> ErrorResponse | PaginatedAuditLog | None:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        resource_type (str | Unset):
        action (str | Unset):
        actor_id (UUID | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAuditLog
    """

    return sync_detailed(
        client=client,
        limit=limit,
        cursor=cursor,
        resource_type=resource_type,
        action=action,
        actor_id=actor_id,
        from_=from_,
        to=to,
        search=search,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    resource_type: str | Unset = UNSET,
    action: str | Unset = UNSET,
    actor_id: UUID | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    search: str | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAuditLog]:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        resource_type (str | Unset):
        action (str | Unset):
        actor_id (UUID | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAuditLog]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
        resource_type=resource_type,
        action=action,
        actor_id=actor_id,
        from_=from_,
        to=to,
        search=search,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: UUID | Unset = UNSET,
    resource_type: str | Unset = UNSET,
    action: str | Unset = UNSET,
    actor_id: UUID | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    search: str | Unset = UNSET,
) -> ErrorResponse | PaginatedAuditLog | None:
    """
    Args:
        limit (int | Unset):
        cursor (UUID | Unset):
        resource_type (str | Unset):
        action (str | Unset):
        actor_id (UUID | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAuditLog
    """

    return (
        await asyncio_detailed(
            client=client,
            limit=limit,
            cursor=cursor,
            resource_type=resource_type,
            action=action,
            actor_id=actor_id,
            from_=from_,
            to=to,
            search=search,
        )
    ).parsed
