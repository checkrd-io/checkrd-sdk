from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.device_token_request import DeviceTokenRequest
from ...models.device_token_response_type_0 import DeviceTokenResponseType0
from ...models.device_token_response_type_1 import DeviceTokenResponseType1
from ...models.device_token_response_type_2 import DeviceTokenResponseType2
from ...models.device_token_response_type_3 import DeviceTokenResponseType3
from ...models.device_token_response_type_4 import DeviceTokenResponseType4
from ...models.error_response import ErrorResponse
from ...types import Response


def _get_kwargs(
    *,
    body: DeviceTokenRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "//auth/cli/device/token",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> (
    DeviceTokenResponseType0
    | DeviceTokenResponseType1
    | DeviceTokenResponseType2
    | DeviceTokenResponseType3
    | DeviceTokenResponseType4
    | ErrorResponse
    | None
):
    if response.status_code == 200:

        def _parse_response_200(
            data: object,
        ) -> (
            DeviceTokenResponseType0
            | DeviceTokenResponseType1
            | DeviceTokenResponseType2
            | DeviceTokenResponseType3
            | DeviceTokenResponseType4
        ):
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_device_token_response_type_0 = DeviceTokenResponseType0.from_dict(data)

                return componentsschemas_device_token_response_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_device_token_response_type_1 = DeviceTokenResponseType1.from_dict(data)

                return componentsschemas_device_token_response_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_device_token_response_type_2 = DeviceTokenResponseType2.from_dict(data)

                return componentsschemas_device_token_response_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_device_token_response_type_3 = DeviceTokenResponseType3.from_dict(data)

                return componentsschemas_device_token_response_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            componentsschemas_device_token_response_type_4 = DeviceTokenResponseType4.from_dict(data)

            return componentsschemas_device_token_response_type_4

        response_200 = _parse_response_200(response.json())

        return response_200

    if response.status_code == 500:
        response_500 = ErrorResponse.from_dict(response.json())

        return response_500

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[
    DeviceTokenResponseType0
    | DeviceTokenResponseType1
    | DeviceTokenResponseType2
    | DeviceTokenResponseType3
    | DeviceTokenResponseType4
    | ErrorResponse
]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeviceTokenRequest,
) -> Response[
    DeviceTokenResponseType0
    | DeviceTokenResponseType1
    | DeviceTokenResponseType2
    | DeviceTokenResponseType3
    | DeviceTokenResponseType4
    | ErrorResponse
]:
    """Public — the device_code itself is the bearer secret. CLI polls this every `interval` seconds until
    a terminal status (`approved`, `denied`, `expired`).

    Args:
        body (DeviceTokenRequest): `POST /auth/cli/device/token` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeviceTokenResponseType0 | DeviceTokenResponseType1 | DeviceTokenResponseType2 | DeviceTokenResponseType3 | DeviceTokenResponseType4 | ErrorResponse]
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
    body: DeviceTokenRequest,
) -> (
    DeviceTokenResponseType0
    | DeviceTokenResponseType1
    | DeviceTokenResponseType2
    | DeviceTokenResponseType3
    | DeviceTokenResponseType4
    | ErrorResponse
    | None
):
    """Public — the device_code itself is the bearer secret. CLI polls this every `interval` seconds until
    a terminal status (`approved`, `denied`, `expired`).

    Args:
        body (DeviceTokenRequest): `POST /auth/cli/device/token` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeviceTokenResponseType0 | DeviceTokenResponseType1 | DeviceTokenResponseType2 | DeviceTokenResponseType3 | DeviceTokenResponseType4 | ErrorResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeviceTokenRequest,
) -> Response[
    DeviceTokenResponseType0
    | DeviceTokenResponseType1
    | DeviceTokenResponseType2
    | DeviceTokenResponseType3
    | DeviceTokenResponseType4
    | ErrorResponse
]:
    """Public — the device_code itself is the bearer secret. CLI polls this every `interval` seconds until
    a terminal status (`approved`, `denied`, `expired`).

    Args:
        body (DeviceTokenRequest): `POST /auth/cli/device/token` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeviceTokenResponseType0 | DeviceTokenResponseType1 | DeviceTokenResponseType2 | DeviceTokenResponseType3 | DeviceTokenResponseType4 | ErrorResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DeviceTokenRequest,
) -> (
    DeviceTokenResponseType0
    | DeviceTokenResponseType1
    | DeviceTokenResponseType2
    | DeviceTokenResponseType3
    | DeviceTokenResponseType4
    | ErrorResponse
    | None
):
    """Public — the device_code itself is the bearer secret. CLI polls this every `interval` seconds until
    a terminal status (`approved`, `denied`, `expired`).

    Args:
        body (DeviceTokenRequest): `POST /auth/cli/device/token` request body.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeviceTokenResponseType0 | DeviceTokenResponseType1 | DeviceTokenResponseType2 | DeviceTokenResponseType3 | DeviceTokenResponseType4 | ErrorResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
