from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.invitation_list_response import InvitationListResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    org_id: UUID,
    *,
    after: str | Unset = UNSET,
    limit: int | Unset = UNSET,
    status: str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["after"] = after

    params["limit"] = limit

    params["status"] = status

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/orgs/{org_id}/invitations".format(
            org_id=quote(str(org_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | InvitationListResponse | None:
    if response.status_code == 200:
        response_200 = InvitationListResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = ErrorResponse.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if response.status_code == 403:
        response_403 = ErrorResponse.from_dict(response.json())

        return response_403

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | InvitationListResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    after: str | Unset = UNSET,
    limit: int | Unset = UNSET,
    status: str | Unset = UNSET,
) -> Response[ErrorResponse | InvitationListResponse]:
    """List a workspace's invitations, newest first. Forward-only
    cursor pagination on `(created_at DESC, id DESC)`.

     Filter by status with `?status=pending` (single) or
    `?status=pending,accepted` (multiple). Unknown statuses return
    400 (`invalid_query_parameter`) rather than silently empty pages.

    Args:
        org_id (UUID):
        after (str | Unset):
        limit (int | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | InvitationListResponse]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        after=after,
        limit=limit,
        status=status,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    after: str | Unset = UNSET,
    limit: int | Unset = UNSET,
    status: str | Unset = UNSET,
) -> ErrorResponse | InvitationListResponse | None:
    """List a workspace's invitations, newest first. Forward-only
    cursor pagination on `(created_at DESC, id DESC)`.

     Filter by status with `?status=pending` (single) or
    `?status=pending,accepted` (multiple). Unknown statuses return
    400 (`invalid_query_parameter`) rather than silently empty pages.

    Args:
        org_id (UUID):
        after (str | Unset):
        limit (int | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | InvitationListResponse
    """

    return sync_detailed(
        org_id=org_id,
        client=client,
        after=after,
        limit=limit,
        status=status,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    after: str | Unset = UNSET,
    limit: int | Unset = UNSET,
    status: str | Unset = UNSET,
) -> Response[ErrorResponse | InvitationListResponse]:
    """List a workspace's invitations, newest first. Forward-only
    cursor pagination on `(created_at DESC, id DESC)`.

     Filter by status with `?status=pending` (single) or
    `?status=pending,accepted` (multiple). Unknown statuses return
    400 (`invalid_query_parameter`) rather than silently empty pages.

    Args:
        org_id (UUID):
        after (str | Unset):
        limit (int | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | InvitationListResponse]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        after=after,
        limit=limit,
        status=status,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    after: str | Unset = UNSET,
    limit: int | Unset = UNSET,
    status: str | Unset = UNSET,
) -> ErrorResponse | InvitationListResponse | None:
    """List a workspace's invitations, newest first. Forward-only
    cursor pagination on `(created_at DESC, id DESC)`.

     Filter by status with `?status=pending` (single) or
    `?status=pending,accepted` (multiple). Unknown statuses return
    400 (`invalid_query_parameter`) rather than silently empty pages.

    Args:
        org_id (UUID):
        after (str | Unset):
        limit (int | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | InvitationListResponse
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            after=after,
            limit=limit,
            status=status,
        )
    ).parsed
