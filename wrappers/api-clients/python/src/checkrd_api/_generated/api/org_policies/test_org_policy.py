from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.policy_test_summary_response import PolicyTestSummaryResponse
from ...models.test_org_policy_request import TestOrgPolicyRequest
from ...types import Response


def _get_kwargs(
    *,
    body: TestOrgPolicyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/org-policies/test",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | PolicyTestSummaryResponse | None:
    if response.status_code == 200:
        response_200 = PolicyTestSummaryResponse.from_dict(response.json())

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
) -> Response[ErrorResponse | PolicyTestSummaryResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: TestOrgPolicyRequest,
) -> Response[ErrorResponse | PolicyTestSummaryResponse]:
    """Run inline policy tests against an org policy YAML. Stateless — the
    handler validates + evaluates the candidate YAML directly via the core
    test runner, identical to the agent-scoped `test_policy`. Gated to
    viewer+ because tests never mutate state. See
    `POST /v1/agents/:id/policies/test` for the analogous agent endpoint.

    Args:
        body (TestOrgPolicyRequest): `POST /v1/org-policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PolicyTestSummaryResponse]
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
    body: TestOrgPolicyRequest,
) -> ErrorResponse | PolicyTestSummaryResponse | None:
    """Run inline policy tests against an org policy YAML. Stateless — the
    handler validates + evaluates the candidate YAML directly via the core
    test runner, identical to the agent-scoped `test_policy`. Gated to
    viewer+ because tests never mutate state. See
    `POST /v1/agents/:id/policies/test` for the analogous agent endpoint.

    Args:
        body (TestOrgPolicyRequest): `POST /v1/org-policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PolicyTestSummaryResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: TestOrgPolicyRequest,
) -> Response[ErrorResponse | PolicyTestSummaryResponse]:
    """Run inline policy tests against an org policy YAML. Stateless — the
    handler validates + evaluates the candidate YAML directly via the core
    test runner, identical to the agent-scoped `test_policy`. Gated to
    viewer+ because tests never mutate state. See
    `POST /v1/agents/:id/policies/test` for the analogous agent endpoint.

    Args:
        body (TestOrgPolicyRequest): `POST /v1/org-policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PolicyTestSummaryResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: TestOrgPolicyRequest,
) -> ErrorResponse | PolicyTestSummaryResponse | None:
    """Run inline policy tests against an org policy YAML. Stateless — the
    handler validates + evaluates the candidate YAML directly via the core
    test runner, identical to the agent-scoped `test_policy`. Gated to
    viewer+ because tests never mutate state. See
    `POST /v1/agents/:id/policies/test` for the analogous agent endpoint.

    Args:
        body (TestOrgPolicyRequest): `POST /v1/org-policies/test` request body.

            Either supply explicit `tests` in the request body, or omit and
            let the server extract a top-level `tests:` block from `yaml_content`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PolicyTestSummaryResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
