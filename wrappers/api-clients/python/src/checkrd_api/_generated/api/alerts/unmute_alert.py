from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.alert_rule import AlertRule
from ...models.error_response import ErrorResponse
from ...types import Response


def _get_kwargs(
    alert_id: UUID,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/alerts/{alert_id}/unmute".format(
            alert_id=quote(str(alert_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AlertRule | ErrorResponse | None:
    if response.status_code == 200:
        response_200 = AlertRule.from_dict(response.json())

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
) -> Response[AlertRule | ErrorResponse]:
    """
    Args:
        alert_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AlertRule | ErrorResponse]
    """

    kwargs = _get_kwargs(
        alert_id=alert_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
) -> AlertRule | ErrorResponse | None:
    """
    Args:
        alert_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AlertRule | ErrorResponse
    """

    return sync_detailed(
        alert_id=alert_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
) -> Response[AlertRule | ErrorResponse]:
    """
    Args:
        alert_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AlertRule | ErrorResponse]
    """

    kwargs = _get_kwargs(
        alert_id=alert_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    alert_id: UUID,
    *,
    client: AuthenticatedClient,
) -> AlertRule | ErrorResponse | None:
    """
    Args:
        alert_id (UUID):

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
        )
    ).parsed
