from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.policy import Policy
from ...models.update_draft_policy_request import UpdateDraftPolicyRequest
from ...types import Response


def _get_kwargs(
    agent_id: UUID,
    version: int,
    *,
    body: UpdateDraftPolicyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/v1/agents/{agent_id}/policies/{version}".format(
            agent_id=quote(str(agent_id), safe=""),
            version=quote(str(version), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ErrorResponse | Policy | None:
    if response.status_code == 200:
        response_200 = Policy.from_dict(response.json())

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

    if response.status_code == 409:
        response_409 = ErrorResponse.from_dict(response.json())

        return response_409

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | Policy]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    agent_id: UUID,
    version: int,
    *,
    client: AuthenticatedClient,
    body: UpdateDraftPolicyRequest,
) -> Response[ErrorResponse | Policy]:
    """
    Args:
        agent_id (UUID):
        version (int):
        body (UpdateDraftPolicyRequest): `PATCH /v1/agents/{agent_id}/policies/{version}` request
            body.

            Only non-active (draft) versions can be edited. Activating a
            version freezes its YAML — subsequent edits require creating a
            new version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | Policy]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        version=version,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    agent_id: UUID,
    version: int,
    *,
    client: AuthenticatedClient,
    body: UpdateDraftPolicyRequest,
) -> ErrorResponse | Policy | None:
    """
    Args:
        agent_id (UUID):
        version (int):
        body (UpdateDraftPolicyRequest): `PATCH /v1/agents/{agent_id}/policies/{version}` request
            body.

            Only non-active (draft) versions can be edited. Activating a
            version freezes its YAML — subsequent edits require creating a
            new version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | Policy
    """

    return sync_detailed(
        agent_id=agent_id,
        version=version,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    agent_id: UUID,
    version: int,
    *,
    client: AuthenticatedClient,
    body: UpdateDraftPolicyRequest,
) -> Response[ErrorResponse | Policy]:
    """
    Args:
        agent_id (UUID):
        version (int):
        body (UpdateDraftPolicyRequest): `PATCH /v1/agents/{agent_id}/policies/{version}` request
            body.

            Only non-active (draft) versions can be edited. Activating a
            version freezes its YAML — subsequent edits require creating a
            new version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | Policy]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        version=version,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    agent_id: UUID,
    version: int,
    *,
    client: AuthenticatedClient,
    body: UpdateDraftPolicyRequest,
) -> ErrorResponse | Policy | None:
    """
    Args:
        agent_id (UUID):
        version (int):
        body (UpdateDraftPolicyRequest): `PATCH /v1/agents/{agent_id}/policies/{version}` request
            body.

            Only non-active (draft) versions can be edited. Activating a
            version freezes its YAML — subsequent edits require creating a
            new version.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | Policy
    """

    return (
        await asyncio_detailed(
            agent_id=agent_id,
            version=version,
            client=client,
            body=body,
        )
    ).parsed
