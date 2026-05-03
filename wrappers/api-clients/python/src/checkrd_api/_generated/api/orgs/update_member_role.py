from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.success_response import SuccessResponse
from ...models.update_role_request import UpdateRoleRequest
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    member_id: UUID,
    *,
    body: UpdateRoleRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/v1/orgs/{org_id}/members/{member_id}/role".format(
            org_id=quote(str(org_id), safe=""),
            member_id=quote(str(member_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
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

    if response.status_code == 422:
        response_422 = ErrorResponse.from_dict(response.json())

        return response_422

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
    body: UpdateRoleRequest,
) -> Response[ErrorResponse | SuccessResponse]:
    r"""Update a member's role. Requires the Admin role.

     The mutation is linearizable: the implementation locks every
    owner row of the workspace before evaluating the
    \"is this the last owner\" check inside a single CTE statement,
    so two concurrent demotions of two distinct owners cannot both
    succeed. Demoting the last remaining owner returns a 422
    validation error (`last_owner`).

    Args:
        org_id (UUID):
        member_id (UUID):
        body (UpdateRoleRequest): Request body for `PUT
            /v1/orgs/{org_id}/members/{member_id}/role`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | SuccessResponse]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        member_id=member_id,
        body=body,
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
    body: UpdateRoleRequest,
) -> ErrorResponse | SuccessResponse | None:
    r"""Update a member's role. Requires the Admin role.

     The mutation is linearizable: the implementation locks every
    owner row of the workspace before evaluating the
    \"is this the last owner\" check inside a single CTE statement,
    so two concurrent demotions of two distinct owners cannot both
    succeed. Demoting the last remaining owner returns a 422
    validation error (`last_owner`).

    Args:
        org_id (UUID):
        member_id (UUID):
        body (UpdateRoleRequest): Request body for `PUT
            /v1/orgs/{org_id}/members/{member_id}/role`.

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
        body=body,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    member_id: UUID,
    *,
    client: AuthenticatedClient,
    body: UpdateRoleRequest,
) -> Response[ErrorResponse | SuccessResponse]:
    r"""Update a member's role. Requires the Admin role.

     The mutation is linearizable: the implementation locks every
    owner row of the workspace before evaluating the
    \"is this the last owner\" check inside a single CTE statement,
    so two concurrent demotions of two distinct owners cannot both
    succeed. Demoting the last remaining owner returns a 422
    validation error (`last_owner`).

    Args:
        org_id (UUID):
        member_id (UUID):
        body (UpdateRoleRequest): Request body for `PUT
            /v1/orgs/{org_id}/members/{member_id}/role`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | SuccessResponse]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        member_id=member_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    member_id: UUID,
    *,
    client: AuthenticatedClient,
    body: UpdateRoleRequest,
) -> ErrorResponse | SuccessResponse | None:
    r"""Update a member's role. Requires the Admin role.

     The mutation is linearizable: the implementation locks every
    owner row of the workspace before evaluating the
    \"is this the last owner\" check inside a single CTE statement,
    so two concurrent demotions of two distinct owners cannot both
    succeed. Demoting the last remaining owner returns a 422
    validation error (`last_owner`).

    Args:
        org_id (UUID):
        member_id (UUID):
        body (UpdateRoleRequest): Request body for `PUT
            /v1/orgs/{org_id}/members/{member_id}/role`.

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
            body=body,
        )
    ).parsed
