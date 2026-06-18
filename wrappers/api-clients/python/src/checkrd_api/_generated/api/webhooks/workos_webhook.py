from http import HTTPStatus
from typing import Any, cast

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem_details import ProblemDetails
from ...models.workos_webhook_event import WorkosWebhookEvent
from ...types import Response


def _get_kwargs(
    *,
    body: WorkosWebhookEvent,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "//webhooks/workos",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Any | ProblemDetails | None:
    if response.status_code == 200:
        response_200 = cast(Any, None)
        return response_200

    if response.status_code == 400:
        response_400 = ProblemDetails.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ProblemDetails.from_dict(response.json())

        return response_401

    if response.status_code == 503:
        response_503 = ProblemDetails.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | ProblemDetails]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkosWebhookEvent,
) -> Response[Any | ProblemDetails]:
    """Receives invitation lifecycle events from WorkOS — verified via Svix HMAC signature header (`workos-
    signature`). The OpenAPI security entry is empty because authentication is body-bound, not bearer-
    token. The handler reads only `id` (idempotency) and `event` (dispatch); all security-relevant
    fields (organization_id, role, email) come from our local `invitations` table, never the payload.

    Args:
        body (WorkosWebhookEvent): `POST /webhooks/workos` request body.

            Minimal envelope of an inbound WorkOS event. The handler reads
            only the `id` (for idempotency dedup), the `event` discriminator
            (for dispatch), and the opaque `data` blob (decoded per
            event-type). Everything else (organization_id, role, email)
            comes from our own `invitations` table — the payload is never
            trusted as a source of truth for security-relevant fields. See
            `routes/webhooks.rs` for the structural rationale.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ProblemDetails]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: WorkosWebhookEvent,
) -> Any | ProblemDetails | None:
    """Receives invitation lifecycle events from WorkOS — verified via Svix HMAC signature header (`workos-
    signature`). The OpenAPI security entry is empty because authentication is body-bound, not bearer-
    token. The handler reads only `id` (idempotency) and `event` (dispatch); all security-relevant
    fields (organization_id, role, email) come from our local `invitations` table, never the payload.

    Args:
        body (WorkosWebhookEvent): `POST /webhooks/workos` request body.

            Minimal envelope of an inbound WorkOS event. The handler reads
            only the `id` (for idempotency dedup), the `event` discriminator
            (for dispatch), and the opaque `data` blob (decoded per
            event-type). Everything else (organization_id, role, email)
            comes from our own `invitations` table — the payload is never
            trusted as a source of truth for security-relevant fields. See
            `routes/webhooks.rs` for the structural rationale.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ProblemDetails
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkosWebhookEvent,
) -> Response[Any | ProblemDetails]:
    """Receives invitation lifecycle events from WorkOS — verified via Svix HMAC signature header (`workos-
    signature`). The OpenAPI security entry is empty because authentication is body-bound, not bearer-
    token. The handler reads only `id` (idempotency) and `event` (dispatch); all security-relevant
    fields (organization_id, role, email) come from our local `invitations` table, never the payload.

    Args:
        body (WorkosWebhookEvent): `POST /webhooks/workos` request body.

            Minimal envelope of an inbound WorkOS event. The handler reads
            only the `id` (for idempotency dedup), the `event` discriminator
            (for dispatch), and the opaque `data` blob (decoded per
            event-type). Everything else (organization_id, role, email)
            comes from our own `invitations` table — the payload is never
            trusted as a source of truth for security-relevant fields. See
            `routes/webhooks.rs` for the structural rationale.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ProblemDetails]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WorkosWebhookEvent,
) -> Any | ProblemDetails | None:
    """Receives invitation lifecycle events from WorkOS — verified via Svix HMAC signature header (`workos-
    signature`). The OpenAPI security entry is empty because authentication is body-bound, not bearer-
    token. The handler reads only `id` (idempotency) and `event` (dispatch); all security-relevant
    fields (organization_id, role, email) come from our local `invitations` table, never the payload.

    Args:
        body (WorkosWebhookEvent): `POST /webhooks/workos` request body.

            Minimal envelope of an inbound WorkOS event. The handler reads
            only the `id` (for idempotency dedup), the `event` discriminator
            (for dispatch), and the opaque `data` blob (decoded per
            event-type). Everything else (organization_id, role, email)
            comes from our own `invitations` table — the payload is never
            trusted as a source of truth for security-relevant fields. See
            `routes/webhooks.rs` for the structural rationale.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ProblemDetails
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
