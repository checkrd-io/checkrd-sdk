from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ingest_request import IngestRequest
from ...models.ingest_response import IngestResponse
from ...models.problem_details import ProblemDetails
from ...types import Response


def _get_kwargs(
    *,
    body: IngestRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/telemetry",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> IngestResponse | ProblemDetails | None:
    if response.status_code == 200:
        response_200 = IngestResponse.from_dict(response.json())

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
) -> Response[IngestResponse | ProblemDetails]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: IngestRequest,
) -> Response[IngestResponse | ProblemDetails]:
    """
    Args:
        body (IngestRequest): `POST /v1/telemetry` request body.

            Sent by the SDK in batches of up to 1,000 events per request. The
            optional `sdk_version` is recorded on the row in ClickHouse so the
            dashboard can surface "events received from SDK X.Y.Z" — useful
            for spotting clients stuck on a buggy release.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IngestResponse | ProblemDetails]
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
    client: AuthenticatedClient,
    body: IngestRequest,
) -> IngestResponse | ProblemDetails | None:
    """
    Args:
        body (IngestRequest): `POST /v1/telemetry` request body.

            Sent by the SDK in batches of up to 1,000 events per request. The
            optional `sdk_version` is recorded on the row in ClickHouse so the
            dashboard can surface "events received from SDK X.Y.Z" — useful
            for spotting clients stuck on a buggy release.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IngestResponse | ProblemDetails
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: IngestRequest,
) -> Response[IngestResponse | ProblemDetails]:
    """
    Args:
        body (IngestRequest): `POST /v1/telemetry` request body.

            Sent by the SDK in batches of up to 1,000 events per request. The
            optional `sdk_version` is recorded on the row in ClickHouse so the
            dashboard can surface "events received from SDK X.Y.Z" — useful
            for spotting clients stuck on a buggy release.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IngestResponse | ProblemDetails]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: IngestRequest,
) -> IngestResponse | ProblemDetails | None:
    """
    Args:
        body (IngestRequest): `POST /v1/telemetry` request body.

            Sent by the SDK in batches of up to 1,000 events per request. The
            optional `sdk_version` is recorded on the row in ClickHouse so the
            dashboard can surface "events received from SDK X.Y.Z" — useful
            for spotting clients stuck on a buggy release.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IngestResponse | ProblemDetails
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
