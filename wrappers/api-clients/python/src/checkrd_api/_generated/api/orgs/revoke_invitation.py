from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.invitation import Invitation
from ...models.problem_details import ProblemDetails
from ...types import UNSET, Response, Unset


def _get_kwargs(
    org_id: UUID,
    invitation_id: UUID,
    *,
    idempotency_key: str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(idempotency_key, Unset):
        headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/orgs/{org_id}/invitations/{invitation_id}/revoke".format(
            org_id=quote(str(org_id), safe=""),
            invitation_id=quote(str(invitation_id), safe=""),
        ),
    }

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Invitation | ProblemDetails | None:
    if response.status_code == 200:
        response_200 = Invitation.from_dict(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = ProblemDetails.from_dict(response.json())

        return response_401

    if response.status_code == 403:
        response_403 = ProblemDetails.from_dict(response.json())

        return response_403

    if response.status_code == 404:
        response_404 = ProblemDetails.from_dict(response.json())

        return response_404

    if response.status_code == 409:
        response_409 = ProblemDetails.from_dict(response.json())

        return response_409

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Invitation | ProblemDetails]:
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
    idempotency_key: str | Unset = UNSET,
) -> Response[Invitation | ProblemDetails]:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):
        idempotency_key (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Invitation | ProblemDetails]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        invitation_id=invitation_id,
        idempotency_key=idempotency_key,
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
    idempotency_key: str | Unset = UNSET,
) -> Invitation | ProblemDetails | None:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):
        idempotency_key (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Invitation | ProblemDetails
    """

    return sync_detailed(
        org_id=org_id,
        invitation_id=invitation_id,
        client=client,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    invitation_id: UUID,
    *,
    client: AuthenticatedClient,
    idempotency_key: str | Unset = UNSET,
) -> Response[Invitation | ProblemDetails]:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):
        idempotency_key (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Invitation | ProblemDetails]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        invitation_id=invitation_id,
        idempotency_key=idempotency_key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    invitation_id: UUID,
    *,
    client: AuthenticatedClient,
    idempotency_key: str | Unset = UNSET,
) -> Invitation | ProblemDetails | None:
    r"""Revoke a pending invitation. Requires the Admin role.

     WorkOS-first ordering: the upstream invitation is revoked before
    the local row, so a partial failure leaves the email link dead
    (the safe direction). Only `\"pending\"` invitations can be
    revoked; accepted/revoked/expired return
    `invitation_invalid_state` (409).

    Args:
        org_id (UUID):
        invitation_id (UUID):
        idempotency_key (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Invitation | ProblemDetails
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            invitation_id=invitation_id,
            client=client,
            idempotency_key=idempotency_key,
        )
    ).parsed
