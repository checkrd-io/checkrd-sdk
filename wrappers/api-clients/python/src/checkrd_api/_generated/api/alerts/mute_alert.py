from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.alert_rule import AlertRule
from ...models.error_response import ErrorResponse
from ...models.mute_alert_request import MuteAlertRequest
from ...types import Response


def _get_kwargs(
    alert_id: UUID,
    *,
    body: MuteAlertRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/alerts/{alert_id}/mute".format(
            alert_id=quote(str(alert_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AlertRule | ErrorResponse | None:
    if response.status_code == 200:
        response_200 = AlertRule.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = ErrorResponse.from_dict(response.json())

        return response_400

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
) -> Response[AlertRule | ErrorResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    body: MuteAlertRequest,
) -> Response[AlertRule | ErrorResponse]:
    """
    Args:
        alert_id (UUID):
        body (MuteAlertRequest): `POST /v1/alerts/{alert_id}/mute` request body. Provide either
            `until` (RFC 3339 timestamp) or `duration_minutes` (relative).
            `until` wins if both are set.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AlertRule | ErrorResponse]
    """

    kwargs = _get_kwargs(
        alert_id=alert_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    body: MuteAlertRequest,
) -> AlertRule | ErrorResponse | None:
    """
    Args:
        alert_id (UUID):
        body (MuteAlertRequest): `POST /v1/alerts/{alert_id}/mute` request body. Provide either
            `until` (RFC 3339 timestamp) or `duration_minutes` (relative).
            `until` wins if both are set.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AlertRule | ErrorResponse
    """

    return sync_detailed(
        alert_id=alert_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    body: MuteAlertRequest,
) -> Response[AlertRule | ErrorResponse]:
    """
    Args:
        alert_id (UUID):
        body (MuteAlertRequest): `POST /v1/alerts/{alert_id}/mute` request body. Provide either
            `until` (RFC 3339 timestamp) or `duration_minutes` (relative).
            `until` wins if both are set.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AlertRule | ErrorResponse]
    """

    kwargs = _get_kwargs(
        alert_id=alert_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
    body: MuteAlertRequest,
) -> AlertRule | ErrorResponse | None:
    """
    Args:
        alert_id (UUID):
        body (MuteAlertRequest): `POST /v1/alerts/{alert_id}/mute` request body. Provide either
            `until` (RFC 3339 timestamp) or `duration_minutes` (relative).
            `until` wins if both are set.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AlertRule | ErrorResponse
    """

    return (
        await asyncio_detailed(
            alert_id=alert_id,
            client=client,
            body=body,
        )
    ).parsed
