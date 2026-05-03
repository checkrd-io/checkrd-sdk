import datetime
from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.paginated_alert_history import PaginatedAlertHistory
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    period: str | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    alert_rule_id: str | Unset = UNSET,
    agent_id: str | Unset = UNSET,
    new_state: str | Unset = UNSET,
    cursor: str | Unset = UNSET,
    limit: int | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["period"] = period

    json_from_: str | Unset = UNSET
    if not isinstance(from_, Unset):
        json_from_ = from_.isoformat()
    params["from"] = json_from_

    json_to: str | Unset = UNSET
    if not isinstance(to, Unset):
        json_to = to.isoformat()
    params["to"] = json_to

    params["alert_rule_id"] = alert_rule_id

    params["agent_id"] = agent_id

    params["new_state"] = new_state

    params["cursor"] = cursor

    params["limit"] = limit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/alert-history",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | PaginatedAlertHistory | None:
    if response.status_code == 200:
        response_200 = PaginatedAlertHistory.from_dict(response.json())

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

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | PaginatedAlertHistory]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    period: str | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    alert_rule_id: str | Unset = UNSET,
    agent_id: str | Unset = UNSET,
    new_state: str | Unset = UNSET,
    cursor: str | Unset = UNSET,
    limit: int | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAlertHistory]:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        cursor (str | Unset):
        limit (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAlertHistory]
    """

    kwargs = _get_kwargs(
        period=period,
        from_=from_,
        to=to,
        alert_rule_id=alert_rule_id,
        agent_id=agent_id,
        new_state=new_state,
        cursor=cursor,
        limit=limit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    period: str | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    alert_rule_id: str | Unset = UNSET,
    agent_id: str | Unset = UNSET,
    new_state: str | Unset = UNSET,
    cursor: str | Unset = UNSET,
    limit: int | Unset = UNSET,
) -> ErrorResponse | PaginatedAlertHistory | None:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        cursor (str | Unset):
        limit (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAlertHistory
    """

    return sync_detailed(
        client=client,
        period=period,
        from_=from_,
        to=to,
        alert_rule_id=alert_rule_id,
        agent_id=agent_id,
        new_state=new_state,
        cursor=cursor,
        limit=limit,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    period: str | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    alert_rule_id: str | Unset = UNSET,
    agent_id: str | Unset = UNSET,
    new_state: str | Unset = UNSET,
    cursor: str | Unset = UNSET,
    limit: int | Unset = UNSET,
) -> Response[ErrorResponse | PaginatedAlertHistory]:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        cursor (str | Unset):
        limit (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | PaginatedAlertHistory]
    """

    kwargs = _get_kwargs(
        period=period,
        from_=from_,
        to=to,
        alert_rule_id=alert_rule_id,
        agent_id=agent_id,
        new_state=new_state,
        cursor=cursor,
        limit=limit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    period: str | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    alert_rule_id: str | Unset = UNSET,
    agent_id: str | Unset = UNSET,
    new_state: str | Unset = UNSET,
    cursor: str | Unset = UNSET,
    limit: int | Unset = UNSET,
) -> ErrorResponse | PaginatedAlertHistory | None:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        cursor (str | Unset):
        limit (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | PaginatedAlertHistory
    """

    return (
        await asyncio_detailed(
            client=client,
            period=period,
            from_=from_,
            to=to,
            alert_rule_id=alert_rule_id,
            agent_id=agent_id,
            new_state=new_state,
            cursor=cursor,
            limit=limit,
        )
    ).parsed
