import datetime
from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.alert_history_bucket import AlertHistoryBucket
from ...models.error_response import ErrorResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    period: str | Unset = UNSET,
    from_: datetime.datetime | Unset = UNSET,
    to: datetime.datetime | Unset = UNSET,
    alert_rule_id: str | Unset = UNSET,
    agent_id: str | Unset = UNSET,
    new_state: str | Unset = UNSET,
    bucket: str | Unset = UNSET,
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

    params["bucket"] = bucket

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/alert-history/timeseries",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | list[AlertHistoryBucket] | None:
    if response.status_code == 200:
        response_200 = []
        _response_200 = response.json()
        for response_200_item_data in _response_200:
            response_200_item = AlertHistoryBucket.from_dict(response_200_item_data)

            response_200.append(response_200_item)

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
) -> Response[ErrorResponse | list[AlertHistoryBucket]]:
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
    bucket: str | Unset = UNSET,
) -> Response[ErrorResponse | list[AlertHistoryBucket]]:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        bucket (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | list[AlertHistoryBucket]]
    """

    kwargs = _get_kwargs(
        period=period,
        from_=from_,
        to=to,
        alert_rule_id=alert_rule_id,
        agent_id=agent_id,
        new_state=new_state,
        bucket=bucket,
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
    bucket: str | Unset = UNSET,
) -> ErrorResponse | list[AlertHistoryBucket] | None:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        bucket (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | list[AlertHistoryBucket]
    """

    return sync_detailed(
        client=client,
        period=period,
        from_=from_,
        to=to,
        alert_rule_id=alert_rule_id,
        agent_id=agent_id,
        new_state=new_state,
        bucket=bucket,
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
    bucket: str | Unset = UNSET,
) -> Response[ErrorResponse | list[AlertHistoryBucket]]:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        bucket (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | list[AlertHistoryBucket]]
    """

    kwargs = _get_kwargs(
        period=period,
        from_=from_,
        to=to,
        alert_rule_id=alert_rule_id,
        agent_id=agent_id,
        new_state=new_state,
        bucket=bucket,
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
    bucket: str | Unset = UNSET,
) -> ErrorResponse | list[AlertHistoryBucket] | None:
    """
    Args:
        period (str | Unset):
        from_ (datetime.datetime | Unset):
        to (datetime.datetime | Unset):
        alert_rule_id (str | Unset):
        agent_id (str | Unset):
        new_state (str | Unset):
        bucket (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | list[AlertHistoryBucket]
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
            bucket=bucket,
        )
    ).parsed
