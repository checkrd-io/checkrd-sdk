from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.policy_test_summary_response import PolicyTestSummaryResponse
from ...models.problem_details import ProblemDetails
from ...models.test_policy_request import TestPolicyRequest
from ...types import UNSET, Response, Unset


def _get_kwargs(
    agent_id: UUID,
    *,
    body: TestPolicyRequest,
    idempotency_key: str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(idempotency_key, Unset):
        headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/agents/{agent_id}/policies/test".format(
            agent_id=quote(str(agent_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PolicyTestSummaryResponse | ProblemDetails | None:
    if response.status_code == 200:
        response_200 = PolicyTestSummaryResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = ProblemDetails.from_dict(response.json())

        return response_400

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
) -> Response[PolicyTestSummaryResponse | ProblemDetails]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
    body: TestPolicyRequest,
    idempotency_key: str | Unset = UNSET,
) -> Response[PolicyTestSummaryResponse | ProblemDetails]:
    """
    Args:
        agent_id (UUID):
        idempotency_key (str | Unset):
        body (TestPolicyRequest): `POST /v1/agents/{agent_id}/policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from
            `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PolicyTestSummaryResponse | ProblemDetails]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        body=body,
        idempotency_key=idempotency_key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
    body: TestPolicyRequest,
    idempotency_key: str | Unset = UNSET,
) -> PolicyTestSummaryResponse | ProblemDetails | None:
    """
    Args:
        agent_id (UUID):
        idempotency_key (str | Unset):
        body (TestPolicyRequest): `POST /v1/agents/{agent_id}/policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from
            `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PolicyTestSummaryResponse | ProblemDetails
    """

    return sync_detailed(
        agent_id=agent_id,
        client=client,
        body=body,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
    body: TestPolicyRequest,
    idempotency_key: str | Unset = UNSET,
) -> Response[PolicyTestSummaryResponse | ProblemDetails]:
    """
    Args:
        agent_id (UUID):
        idempotency_key (str | Unset):
        body (TestPolicyRequest): `POST /v1/agents/{agent_id}/policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from
            `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PolicyTestSummaryResponse | ProblemDetails]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        body=body,
        idempotency_key=idempotency_key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    agent_id: UUID,
    *,
    client: AuthenticatedClient,
    body: TestPolicyRequest,
    idempotency_key: str | Unset = UNSET,
) -> PolicyTestSummaryResponse | ProblemDetails | None:
    """
    Args:
        agent_id (UUID):
        idempotency_key (str | Unset):
        body (TestPolicyRequest): `POST /v1/agents/{agent_id}/policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from
            `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PolicyTestSummaryResponse | ProblemDetails
    """

    return (
        await asyncio_detailed(
            agent_id=agent_id,
            client=client,
            body=body,
            idempotency_key=idempotency_key,
        )
    ).parsed
