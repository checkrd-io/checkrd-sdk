from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.organization import Organization
from ...models.problem_details import ProblemDetails
from ...models.rename_org_request import RenameOrgRequest
from ...types import UNSET, Response, Unset


def _get_kwargs(
    org_id: UUID,
    *,
    body: RenameOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(idempotency_key, Unset):
        headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/v1/orgs/{org_id}".format(
            org_id=quote(str(org_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Organization | ProblemDetails | None:
    if response.status_code == 200:
        response_200 = Organization.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = ProblemDetails.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ProblemDetails.from_dict(response.json())

        return response_401

    if response.status_code == 403:
        response_403 = ProblemDetails.from_dict(response.json())

        return response_403

    if response.status_code == 404:
        response_404 = ProblemDetails.from_dict(response.json())

        return response_404

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Organization | ProblemDetails]:
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
    body: RenameOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Response[Organization | ProblemDetails]:
    """Rename a workspace. Requires the Admin role.

    Args:
        org_id (UUID):
        idempotency_key (str | Unset):
        body (RenameOrgRequest): Request body for `PUT /v1/orgs/{org_id}` (rename). Same
            constraints
            as create.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Organization | ProblemDetails]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
        idempotency_key=idempotency_key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    body: RenameOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Organization | ProblemDetails | None:
    """Rename a workspace. Requires the Admin role.

    Args:
        org_id (UUID):
        idempotency_key (str | Unset):
        body (RenameOrgRequest): Request body for `PUT /v1/orgs/{org_id}` (rename). Same
            constraints
            as create.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Organization | ProblemDetails
    """

    return sync_detailed(
        org_id=org_id,
        client=client,
        body=body,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    body: RenameOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Response[Organization | ProblemDetails]:
    """Rename a workspace. Requires the Admin role.

    Args:
        org_id (UUID):
        idempotency_key (str | Unset):
        body (RenameOrgRequest): Request body for `PUT /v1/orgs/{org_id}` (rename). Same
            constraints
            as create.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Organization | ProblemDetails]
    """

    kwargs = _get_kwargs(
        org_id=org_id,
        body=body,
        idempotency_key=idempotency_key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    org_id: UUID,
    *,
    client: AuthenticatedClient,
    body: RenameOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Organization | ProblemDetails | None:
    """Rename a workspace. Requires the Admin role.

    Args:
        org_id (UUID):
        idempotency_key (str | Unset):
        body (RenameOrgRequest): Request body for `PUT /v1/orgs/{org_id}` (rename). Same
            constraints
            as create.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Organization | ProblemDetails
    """

    return (
        await asyncio_detailed(
            org_id=org_id,
            client=client,
            body=body,
            idempotency_key=idempotency_key,
        )
    ).parsed
