from http import HTTPStatus
from typing import Any
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.rule_hit import RuleHit
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    agent_id: UUID | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_agent_id: str | Unset = UNSET
    if not isinstance(agent_id, Unset):
        json_agent_id = str(agent_id)
    params["agent_id"] = json_agent_id

    params["from"] = from_

    params["to"] = to

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/dashboard/rule-hits",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | list[RuleHit] | None:
    if response.status_code == 200:
        response_200 = []
        _response_200 = response.json()
        for response_200_item_data in _response_200:
            response_200_item = RuleHit.from_dict(response_200_item_data)

            response_200.append(response_200_item)

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
) -> Response[ErrorResponse | list[RuleHit]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    agent_id: UUID | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
) -> Response[ErrorResponse | list[RuleHit]]:
    r"""Per-rule hit counts with a sparkline-ready hourly histogram.

     Stripe Radar pattern: \"rule X matched Y times in the last N hours\" with a
    24-point spark so dead rules are obvious at a glance. `agent_id` scopes
    to a single agent's evaluations; when omitted the response aggregates
    across every agent in the org.

    Args:
        agent_id (UUID | Unset):
        from_ (str | Unset):
        to (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | list[RuleHit]]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        from_=from_,
        to=to,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    agent_id: UUID | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
) -> ErrorResponse | list[RuleHit] | None:
    r"""Per-rule hit counts with a sparkline-ready hourly histogram.

     Stripe Radar pattern: \"rule X matched Y times in the last N hours\" with a
    24-point spark so dead rules are obvious at a glance. `agent_id` scopes
    to a single agent's evaluations; when omitted the response aggregates
    across every agent in the org.

    Args:
        agent_id (UUID | Unset):
        from_ (str | Unset):
        to (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | list[RuleHit]
    """

    return sync_detailed(
        client=client,
        agent_id=agent_id,
        from_=from_,
        to=to,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    agent_id: UUID | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
) -> Response[ErrorResponse | list[RuleHit]]:
    r"""Per-rule hit counts with a sparkline-ready hourly histogram.

     Stripe Radar pattern: \"rule X matched Y times in the last N hours\" with a
    24-point spark so dead rules are obvious at a glance. `agent_id` scopes
    to a single agent's evaluations; when omitted the response aggregates
    across every agent in the org.

    Args:
        agent_id (UUID | Unset):
        from_ (str | Unset):
        to (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | list[RuleHit]]
    """

    kwargs = _get_kwargs(
        agent_id=agent_id,
        from_=from_,
        to=to,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    agent_id: UUID | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
) -> ErrorResponse | list[RuleHit] | None:
    r"""Per-rule hit counts with a sparkline-ready hourly histogram.

     Stripe Radar pattern: \"rule X matched Y times in the last N hours\" with a
    24-point spark so dead rules are obvious at a glance. `agent_id` scopes
    to a single agent's evaluations; when omitted the response aggregates
    across every agent in the org.

    Args:
        agent_id (UUID | Unset):
        from_ (str | Unset):
        to (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | list[RuleHit]
    """

    return (
        await asyncio_detailed(
            client=client,
            agent_id=agent_id,
            from_=from_,
            to=to,
        )
    ).parsed
