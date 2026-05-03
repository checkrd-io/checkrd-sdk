from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.invitation import Invitation
from ...models.send_invitation_request import SendInvitationRequest
from ...types import Response


def _get_kwargs(
    org_id: UUID,
    *,
    body: SendInvitationRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/orgs/{org_id}/invitations".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | Invitation | None:
    if response.status_code == 200:
        response_200 = Invitation.from_dict(response.json())

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

    if response.status_code == 409:
        response_409 = ErrorResponse.from_dict(response.json())

        return response_409

    if response.status_code == 429:
        response_429 = ErrorResponse.from_dict(response.json())

        return response_429

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
    *,
    client: AuthenticatedClient,
    body: SendInvitationRequest,
) -> Response[ErrorResponse | Invitation]:
    """Send an invitation to join a workspace. Requires the Admin role.

     Side effects: creates a WorkOS organization on first use, sends
    the invitation email via WorkOS, and persists a local pending
    invitation row. All three are atomic from the caller's
    perspective — the WorkOS round-trip is wrapped by a Postgres
    transaction with an advisory lock that serializes concurrent
    sends from the same workspace.

    Per-org rate limit (Free: 5/hr, Team: 20/hr, Enterprise: none) and
    seat-cap enforcement (members + pending invites <= plan limit).
    Returns `conflict_already_invited` (409) if a pending invitation
    already exists for the email.

    Args:
        org_id (UUID):
        body (SendInvitationRequest): Request body for `POST /v1/orgs/{org_id}/invitations`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | Invitation]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    body: SendInvitationRequest,
) -> ErrorResponse | Invitation | None:
    """Send an invitation to join a workspace. Requires the Admin role.

     Side effects: creates a WorkOS organization on first use, sends
    the invitation email via WorkOS, and persists a local pending
    invitation row. All three are atomic from the caller's
    perspective — the WorkOS round-trip is wrapped by a Postgres
    transaction with an advisory lock that serializes concurrent
    sends from the same workspace.

    Per-org rate limit (Free: 5/hr, Team: 20/hr, Enterprise: none) and
    seat-cap enforcement (members + pending invites <= plan limit).
    Returns `conflict_already_invited` (409) if a pending invitation
    already exists for the email.

    Args:
        org_id (UUID):
        body (SendInvitationRequest): Request body for `POST /v1/orgs/{org_id}/invitations`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | Invitation
    """

    return sync_detailed(
        org_id=org_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    body: SendInvitationRequest,
) -> Response[ErrorResponse | Invitation]:
    """Send an invitation to join a workspace. Requires the Admin role.

     Side effects: creates a WorkOS organization on first use, sends
    the invitation email via WorkOS, and persists a local pending
    invitation row. All three are atomic from the caller's
    perspective — the WorkOS round-trip is wrapped by a Postgres
    transaction with an advisory lock that serializes concurrent
    sends from the same workspace.

    Per-org rate limit (Free: 5/hr, Team: 20/hr, Enterprise: none) and
    seat-cap enforcement (members + pending invites <= plan limit).
    Returns `conflict_already_invited` (409) if a pending invitation
    already exists for the email.

    Args:
        org_id (UUID):
        body (SendInvitationRequest): Request body for `POST /v1/orgs/{org_id}/invitations`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | Invitation]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    body: SendInvitationRequest,
) -> ErrorResponse | Invitation | None:
    """Send an invitation to join a workspace. Requires the Admin role.

     Side effects: creates a WorkOS organization on first use, sends
    the invitation email via WorkOS, and persists a local pending
    invitation row. All three are atomic from the caller's
    perspective — the WorkOS round-trip is wrapped by a Postgres
    transaction with an advisory lock that serializes concurrent
    sends from the same workspace.

    Per-org rate limit (Free: 5/hr, Team: 20/hr, Enterprise: none) and
    seat-cap enforcement (members + pending invites <= plan limit).
    Returns `conflict_already_invited` (409) if a pending invitation
    already exists for the email.

    Args:
        org_id (UUID):
        body (SendInvitationRequest): Request body for `POST /v1/orgs/{org_id}/invitations`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | Invitation
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
        )
    ).parsed
