from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.success_response import SuccessResponse
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    member_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/v1/orgs/{org_id}/members/{member_id}".format(
            org_id=quote(str(org_id), safe=""),
            member_id=quote(str(member_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | SuccessResponse | None:
    if response.status_code == 200:
        response_200 = SuccessResponse.from_dict(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

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
) -> Response[ErrorResponse | SuccessResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    org_id: UUID,
    member_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | SuccessResponse]:
    """Remove a member from a workspace. Requires the Admin role.

     Cannot remove an owner — the DELETE statement is scoped with
    `role != 'owner'`, so attempting to remove an owner returns 404
    the same as a non-existent membership. To demote an owner first,
    use `PUT /v1/orgs/{org_id}/members/{member_id}/role`.

    Args:
        org_id (UUID):
        member_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | SuccessResponse]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        member_id=member_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    member_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | SuccessResponse | None:
    """Remove a member from a workspace. Requires the Admin role.

     Cannot remove an owner — the DELETE statement is scoped with
    `role != 'owner'`, so attempting to remove an owner returns 404
    the same as a non-existent membership. To demote an owner first,
    use `PUT /v1/orgs/{org_id}/members/{member_id}/role`.

    Args:
        org_id (UUID):
        member_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | SuccessResponse
    """

    return sync_detailed(
        org_id=org_id,
        member_id=member_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    member_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | SuccessResponse]:
    """Remove a member from a workspace. Requires the Admin role.

     Cannot remove an owner — the DELETE statement is scoped with
    `role != 'owner'`, so attempting to remove an owner returns 404
    the same as a non-existent membership. To demote an owner first,
    use `PUT /v1/orgs/{org_id}/members/{member_id}/role`.

    Args:
        org_id (UUID):
        member_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | SuccessResponse]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        member_id=member_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    member_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | SuccessResponse | None:
    """Remove a member from a workspace. Requires the Admin role.

     Cannot remove an owner — the DELETE statement is scoped with
    `role != 'owner'`, so attempting to remove an owner returns 404
    the same as a non-existent membership. To demote an owner first,
    use `PUT /v1/orgs/{org_id}/members/{member_id}/role`.

    Args:
        org_id (UUID):
        member_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | SuccessResponse
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            member_id=member_id,
            client=client,
        )
    ).parsed
