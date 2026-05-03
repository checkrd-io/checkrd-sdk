from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.audit_log_entry_with_user import AuditLogEntryWithUser
from ...models.error_response import ErrorResponse
from ...types import Response


def _get_kwargs(
    resource_type: str,
    resource_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/audit-log/{resource_type}/{resource_id}".format(
            resource_type=quote(str(resource_type), safe=""),
            resource_id=quote(str(resource_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | list[AuditLogEntryWithUser] | None:
    if response.status_code == 200:
        response_200 = []
        _response_200 = response.json()
        for response_200_item_data in _response_200:
            response_200_item = AuditLogEntryWithUser.from_dict(response_200_item_data)

            response_200.append(response_200_item)

        return response_200

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
) -> Response[ErrorResponse | list[AuditLogEntryWithUser]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    resource_type: str,
    resource_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | list[AuditLogEntryWithUser]]:
    """
    Args:
        resource_type (str):
        resource_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | list[AuditLogEntryWithUser]]
    """

    kwargs = _get_kwargs(
        resource_type=resource_type,
        resource_id=resource_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    resource_type: str,
    resource_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | list[AuditLogEntryWithUser] | None:
    """
    Args:
        resource_type (str):
        resource_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | list[AuditLogEntryWithUser]
    """

    return sync_detailed(
        resource_type=resource_type,
        resource_id=resource_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    resource_type: str,
    resource_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | list[AuditLogEntryWithUser]]:
    """
    Args:
        resource_type (str):
        resource_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | list[AuditLogEntryWithUser]]
    """

    kwargs = _get_kwargs(
        resource_type=resource_type,
        resource_id=resource_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    resource_type: str,
    resource_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | list[AuditLogEntryWithUser] | None:
    """
    Args:
        resource_type (str):
        resource_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | list[AuditLogEntryWithUser]
    """

    return (
        await asyncio_detailed(
            resource_type=resource_type,
            resource_id=resource_id,
            client=client,
        )
    ).parsed
