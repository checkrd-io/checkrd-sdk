from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.invitation import Invitation
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    invitation_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/orgs/{org_id}/invitations/{invitation_id}/revoke".format(
            org_id=quote(str(org_id), safe=""),
            invitation_id=quote(str(invitation_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | Invitation | None:
    if response.status_code == 200:
        response_200 = Invitation.from_dict(response.json())

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

    if response.status_code == 409:
        response_409 = ErrorResponse.from_dict(response.json())

        return response_409

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | Invitation]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    org_id: UUID,
    invitation_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | Invitation]:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | Invitation]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        invitation_id=invitation_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    invitation_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | Invitation | None:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | Invitation
    """

    return sync_detailed(
        org_id=org_id,
        invitation_id=invitation_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    invitation_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[ErrorResponse | Invitation]:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | Invitation]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        invitation_id=invitation_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    invitation_id: UUID,
    *,
    client: AuthenticatedClient,
) -> ErrorResponse | Invitation | None:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | Invitation
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            invitation_id=invitation_id,
            client=client,
        )
    ).parsed
