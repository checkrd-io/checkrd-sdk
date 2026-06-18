from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem_details import ProblemDetails
from ...models.telemetry_event_row import TelemetryEventRow
from ...types import Response


def _get_kwargs(
    request_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/dashboard/events/{request_id}".format(
            request_id=quote(str(request_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProblemDetails | TelemetryEventRow | None:
    if response.status_code == 200:
        response_200 = TelemetryEventRow.from_dict(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = ProblemDetails.from_dict(response.json())

        return response_401

    if response.status_code == 404:
        response_404 = ProblemDetails.from_dict(response.json())

        return response_404

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProblemDetails | TelemetryEventRow]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    request_id: str,
    *,
    client: AuthenticatedClient,
) -> Response[ProblemDetails | TelemetryEventRow]:
    """Look up a single telemetry event by its request_id, scoped to the
    authenticated user's org. Returns 404 if no matching event exists.

     The request_id is the UUID emitted by the SDK per intercepted request
    and stored in ClickHouse. It is org-scoped — an event from another org
    with the same request_id is never returned.

    Args:
        request_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProblemDetails | TelemetryEventRow]
    """

    kwargs = _get_kwargs(
        request_id=request_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    request_id: str,
    *,
    client: AuthenticatedClient,
) -> ProblemDetails | TelemetryEventRow | None:
    """Look up a single telemetry event by its request_id, scoped to the
    authenticated user's org. Returns 404 if no matching event exists.

     The request_id is the UUID emitted by the SDK per intercepted request
    and stored in ClickHouse. It is org-scoped — an event from another org
    with the same request_id is never returned.

    Args:
        request_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProblemDetails | TelemetryEventRow
    """

    return sync_detailed(
        request_id=request_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    request_id: str,
    *,
    client: AuthenticatedClient,
) -> Response[ProblemDetails | TelemetryEventRow]:
    """Look up a single telemetry event by its request_id, scoped to the
    authenticated user's org. Returns 404 if no matching event exists.

     The request_id is the UUID emitted by the SDK per intercepted request
    and stored in ClickHouse. It is org-scoped — an event from another org
    with the same request_id is never returned.

    Args:
        request_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProblemDetails | TelemetryEventRow]
    """

    kwargs = _get_kwargs(
        request_id=request_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    request_id: str,
    *,
    client: AuthenticatedClient,
) -> ProblemDetails | TelemetryEventRow | None:
    """Look up a single telemetry event by its request_id, scoped to the
    authenticated user's org. Returns 404 if no matching event exists.

     The request_id is the UUID emitted by the SDK per intercepted request
    and stored in ClickHouse. It is org-scoped — an event from another org
    with the same request_id is never returned.

    Args:
        request_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProblemDetails | TelemetryEventRow
    """

    return (
        await asyncio_detailed(
            request_id=request_id,
            client=client,
        )
    ).parsed
