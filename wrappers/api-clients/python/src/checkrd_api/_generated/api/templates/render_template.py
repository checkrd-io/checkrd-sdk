from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.render_template_request import RenderTemplateRequest
from ...models.render_template_response import RenderTemplateResponse
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: RenderTemplateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/policy-templates/{id}/render".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | RenderTemplateResponse | None:
    if response.status_code == 200:
        response_200 = RenderTemplateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = ErrorResponse.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | RenderTemplateResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    body: RenderTemplateRequest,
) -> Response[ErrorResponse | RenderTemplateResponse]:
    """
    Args:
        id (str):
        body (RenderTemplateRequest): `POST /v1/policy-templates/{id}/render` request body.

            `parameters` is a free-form JSON object; keys must match the
            `TemplateParam.name` entries returned by the listing endpoint.
            Types are validated server-side against `TemplateParam.param_type`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | RenderTemplateResponse]
    """

    kwargs = _get_kwargs(
        id=id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    id: str,
    *,
    client: AuthenticatedClient,
    body: RenderTemplateRequest,
) -> ErrorResponse | RenderTemplateResponse | None:
    """
    Args:
        id (str):
        body (RenderTemplateRequest): `POST /v1/policy-templates/{id}/render` request body.

            `parameters` is a free-form JSON object; keys must match the
            `TemplateParam.name` entries returned by the listing endpoint.
            Types are validated server-side against `TemplateParam.param_type`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | RenderTemplateResponse
    """

    return sync_detailed(
        id=id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    body: RenderTemplateRequest,
) -> Response[ErrorResponse | RenderTemplateResponse]:
    """
    Args:
        id (str):
        body (RenderTemplateRequest): `POST /v1/policy-templates/{id}/render` request body.

            `parameters` is a free-form JSON object; keys must match the
            `TemplateParam.name` entries returned by the listing endpoint.
            Types are validated server-side against `TemplateParam.param_type`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | RenderTemplateResponse]
    """

    kwargs = _get_kwargs(
        id=id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    id: str,
    *,
    client: AuthenticatedClient,
    body: RenderTemplateRequest,
) -> ErrorResponse | RenderTemplateResponse | None:
    """
    Args:
        id (str):
        body (RenderTemplateRequest): `POST /v1/policy-templates/{id}/render` request body.

            `parameters` is a free-form JSON object; keys must match the
            `TemplateParam.name` entries returned by the listing endpoint.
            Types are validated server-side against `TemplateParam.param_type`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | RenderTemplateResponse
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
