from http import HTTPStatus
from typing import Any
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.paginated_telemetry_events import PaginatedTelemetryEvents
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    limit: int | Unset = UNSET,
    cursor: str | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    method: str | Unset = UNSET,
    host: str | Unset = UNSET,
    status_code: int | Unset = UNSET,
    status_class: str | Unset = UNSET,
    policy_result: str | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
    trace_id: str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    params["cursor"] = cursor

    json_agent_id: str | Unset = UNSET
    if not isinstance(agent_id, Unset):
        json_agent_id = str(agent_id)
    params["agent_id"] = json_agent_id

    params["method"] = method

    params["host"] = host

    params["status_code"] = status_code

    params["status_class"] = status_class

    params["policy_result"] = policy_result

    params["from"] = from_

    params["to"] = to

    params["trace_id"] = trace_id

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/dashboard/events",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | PaginatedTelemetryEvents | None:
    if response.status_code == 200:
        response_200 = PaginatedTelemetryEvents.from_dict(response.json())

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
) -> Response[ErrorResponse | PaginatedTelemetryEvents]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: str | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    method: str | Unset = UNSET,
    host: str | Unset = UNSET,
    status_code: int | Unset = UNSET,
    status_class: str | Unset = UNSET,
    policy_result: str | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
    trace_id: str | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedTelemetryEvents]:
    """
    Args:
        limit (int | Unset):
        cursor (str | Unset):
        agent_id (UUID | Unset):
        method (str | Unset):
        host (str | Unset):
        status_code (int | Unset):
        status_class (str | Unset):
        policy_result (str | Unset):
        from_ (str | Unset):
        to (str | Unset):
        trace_id (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedTelemetryEvents]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
        agent_id=agent_id,
        method=method,
        host=host,
        status_code=status_code,
        status_class=status_class,
        policy_result=policy_result,
        from_=from_,
        to=to,
        trace_id=trace_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: str | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    method: str | Unset = UNSET,
    host: str | Unset = UNSET,
    status_code: int | Unset = UNSET,
    status_class: str | Unset = UNSET,
    policy_result: str | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
    trace_id: str | Unset = UNSET,
) -> ErrorResponse | PaginatedTelemetryEvents | None:
    """
    Args:
        limit (int | Unset):
        cursor (str | Unset):
        agent_id (UUID | Unset):
        method (str | Unset):
        host (str | Unset):
        status_code (int | Unset):
        status_class (str | Unset):
        policy_result (str | Unset):
        from_ (str | Unset):
        to (str | Unset):
        trace_id (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedTelemetryEvents
    """

    return sync_detailed(
        client=client,
        limit=limit,
        cursor=cursor,
        agent_id=agent_id,
        method=method,
        host=host,
        status_code=status_code,
        status_class=status_class,
        policy_result=policy_result,
        from_=from_,
        to=to,
        trace_id=trace_id,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: str | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    method: str | Unset = UNSET,
    host: str | Unset = UNSET,
    status_code: int | Unset = UNSET,
    status_class: str | Unset = UNSET,
    policy_result: str | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
    trace_id: str | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedTelemetryEvents]:
    """
    Args:
        limit (int | Unset):
        cursor (str | Unset):
        agent_id (UUID | Unset):
        method (str | Unset):
        host (str | Unset):
        status_code (int | Unset):
        status_class (str | Unset):
        policy_result (str | Unset):
        from_ (str | Unset):
        to (str | Unset):
        trace_id (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedTelemetryEvents]
    """

    kwargs = _get_kwargs(
        limit=limit,
        cursor=cursor,
        agent_id=agent_id,
        method=method,
        host=host,
        status_code=status_code,
        status_class=status_class,
        policy_result=policy_result,
        from_=from_,
        to=to,
        trace_id=trace_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = UNSET,
    cursor: str | Unset = UNSET,
    agent_id: UUID | Unset = UNSET,
    method: str | Unset = UNSET,
    host: str | Unset = UNSET,
    status_code: int | Unset = UNSET,
    status_class: str | Unset = UNSET,
    policy_result: str | Unset = UNSET,
    from_: str | Unset = UNSET,
    to: str | Unset = UNSET,
    trace_id: str | Unset = UNSET,
) -> ErrorResponse | PaginatedTelemetryEvents | None:
    """
    Args:
        limit (int | Unset):
        cursor (str | Unset):
        agent_id (UUID | Unset):
        method (str | Unset):
        host (str | Unset):
        status_code (int | Unset):
        status_class (str | Unset):
        policy_result (str | Unset):
        from_ (str | Unset):
        to (str | Unset):
        trace_id (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedTelemetryEvents
    """

    return (
        await asyncio_detailed(
            client=client,
            limit=limit,
            cursor=cursor,
            agent_id=agent_id,
            method=method,
            host=host,
            status_code=status_code,
            status_class=status_class,
            policy_result=policy_result,
            from_=from_,
            to=to,
            trace_id=trace_id,
        )
    ).parsed
