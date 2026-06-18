from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_org_request import CreateOrgRequest
from ...models.organization import Organization
from ...models.problem_details import ProblemDetails
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    body: CreateOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(idempotency_key, Unset):
        headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/orgs",
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

    if response.status_code == 429:
        response_429 = ProblemDetails.from_dict(response.json())

        return response_429

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
    *,
    client: AuthenticatedClient,
    body: CreateOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Response[Organization | ProblemDetails]:
    """Create a new workspace owned by the caller.

     Per-user cap of 5 free workspaces (Supabase pattern); paid
    workspaces don't count against the cap. Sliding-window rate limit
    of 10 creates per hour per user via Redis (degrades open).

    Returns `org_count_exceeded` (400) when the free-org cap is hit
    and `org_rate_limited` (429) when the per-hour limit fires.

    Args:
        idempotency_key (str | Unset):
        body (CreateOrgRequest): Request body for `POST /v1/orgs`.

            Trimmed and validated server-side: 1-100 characters after trim.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Organization | ProblemDetails]
    """

    kwargs = _get_kwargs(
        body=body,
        idempotency_key=idempotency_key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: CreateOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Organization | ProblemDetails | None:
    """Create a new workspace owned by the caller.

     Per-user cap of 5 free workspaces (Supabase pattern); paid
    workspaces don't count against the cap. Sliding-window rate limit
    of 10 creates per hour per user via Redis (degrades open).

    Returns `org_count_exceeded` (400) when the free-org cap is hit
    and `org_rate_limited` (429) when the per-hour limit fires.

    Args:
        idempotency_key (str | Unset):
        body (CreateOrgRequest): Request body for `POST /v1/orgs`.

            Trimmed and validated server-side: 1-100 characters after trim.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Organization | ProblemDetails
    """

    return sync_detailed(
        client=client,
        body=body,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Response[Organization | ProblemDetails]:
    """Create a new workspace owned by the caller.

     Per-user cap of 5 free workspaces (Supabase pattern); paid
    workspaces don't count against the cap. Sliding-window rate limit
    of 10 creates per hour per user via Redis (degrades open).

    Returns `org_count_exceeded` (400) when the free-org cap is hit
    and `org_rate_limited` (429) when the per-hour limit fires.

    Args:
        idempotency_key (str | Unset):
        body (CreateOrgRequest): Request body for `POST /v1/orgs`.

            Trimmed and validated server-side: 1-100 characters after trim.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Organization | ProblemDetails]
    """

    kwargs = _get_kwargs(
        body=body,
        idempotency_key=idempotency_key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: CreateOrgRequest,
    idempotency_key: str | Unset = UNSET,
) -> Organization | ProblemDetails | None:
    """Create a new workspace owned by the caller.

     Per-user cap of 5 free workspaces (Supabase pattern); paid
    workspaces don't count against the cap. Sliding-window rate limit
    of 10 creates per hour per user via Redis (degrades open).

    Returns `org_count_exceeded` (400) when the free-org cap is hit
    and `org_rate_limited` (429) when the per-hour limit fires.

    Args:
        idempotency_key (str | Unset):
        body (CreateOrgRequest): Request body for `POST /v1/orgs`.

            Trimmed and validated server-side: 1-100 characters after trim.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Organization | ProblemDetails
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
            idempotency_key=idempotency_key,
        )
    ).parsed
