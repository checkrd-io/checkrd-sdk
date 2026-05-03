from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="AlertRuleWithAgent")


@_attrs_define
class AlertRuleWithAgent:
    """Alert rule joined with agent name for display in list views.

    Returned (paginated) from `GET /v1/alerts`.

        Attributes:
            id (UUID):
            agent_id (UUID):
            agent_name (str): Denormalized so the dashboard doesn't re-query per row.
            condition_type (str):
            threshold (float):
            window_minutes (int):
            channel (str):
            channel_target (str):
            cooldown_minutes (int):
            notification_count (int):
            is_enabled (bool):
            created_at (datetime.datetime):
            alert_state (str):
            pending_evaluations (int):
            consecutive_hits (int):
            created_by (None | Unset | UUID):
            last_triggered_at (datetime.datetime | None | Unset):
            last_value (float | None | Unset):
            last_evaluated_at (datetime.datetime | None | Unset):
            muted_until (datetime.datetime | None | Unset):
    """

    id: UUID
    agent_id: UUID
    agent_name: str
    condition_type: str
    threshold: float
    window_minutes: int
    channel: str
    channel_target: str
    cooldown_minutes: int
    notification_count: int
    is_enabled: bool
    created_at: datetime.datetime
    alert_state: str
    pending_evaluations: int
    consecutive_hits: int
    created_by: None | Unset | UUID = UNSET
    last_triggered_at: datetime.datetime | None | Unset = UNSET
    last_value: float | None | Unset = UNSET
    last_evaluated_at: datetime.datetime | None | Unset = UNSET
    muted_until: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        agent_id = str(self.agent_id)

        agent_name = self.agent_name

        condition_type = self.condition_type

        threshold = self.threshold

        window_minutes = self.window_minutes

        channel = self.channel

        channel_target = self.channel_target

        cooldown_minutes = self.cooldown_minutes

        notification_count = self.notification_count

        is_enabled = self.is_enabled

        created_at = self.created_at.isoformat()

        alert_state = self.alert_state

        pending_evaluations = self.pending_evaluations

        consecutive_hits = self.consecutive_hits

        created_by: None | str | Unset
        if isinstance(self.created_by, Unset):
            created_by = UNSET
        elif isinstance(self.created_by, UUID):
            created_by = str(self.created_by)
        else:
            created_by = self.created_by

        last_triggered_at: None | str | Unset
        if isinstance(self.last_triggered_at, Unset):
            last_triggered_at = UNSET
        elif isinstance(self.last_triggered_at, datetime.datetime):
            last_triggered_at = self.last_triggered_at.isoformat()
        else:
            last_triggered_at = self.last_triggered_at

        last_value: float | None | Unset
        if isinstance(self.last_value, Unset):
            last_value = UNSET
        else:
            last_value = self.last_value

        last_evaluated_at: None | str | Unset
        if isinstance(self.last_evaluated_at, Unset):
            last_evaluated_at = UNSET
        elif isinstance(self.last_evaluated_at, datetime.datetime):
            last_evaluated_at = self.last_evaluated_at.isoformat()
        else:
            last_evaluated_at = self.last_evaluated_at

        muted_until: None | str | Unset
        if isinstance(self.muted_until, Unset):
            muted_until = UNSET
        elif isinstance(self.muted_until, datetime.datetime):
            muted_until = self.muted_until.isoformat()
        else:
            muted_until = self.muted_until

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "condition_type": condition_type,
                "threshold": threshold,
                "window_minutes": window_minutes,
                "channel": channel,
                "channel_target": channel_target,
                "cooldown_minutes": cooldown_minutes,
                "notification_count": notification_count,
                "is_enabled": is_enabled,
                "created_at": created_at,
                "alert_state": alert_state,
                "pending_evaluations": pending_evaluations,
                "consecutive_hits": consecutive_hits,
            }
        )
        if created_by is not UNSET:
            field_dict["created_by"] = created_by
        if last_triggered_at is not UNSET:
            field_dict["last_triggered_at"] = last_triggered_at
        if last_value is not UNSET:
            field_dict["last_value"] = last_value
        if last_evaluated_at is not UNSET:
            field_dict["last_evaluated_at"] = last_evaluated_at
        if muted_until is not UNSET:
            field_dict["muted_until"] = muted_until

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        agent_id = UUID(d.pop("agent_id"))

        agent_name = d.pop("agent_name")

        condition_type = d.pop("condition_type")

        threshold = d.pop("threshold")

        window_minutes = d.pop("window_minutes")

        channel = d.pop("channel")

        channel_target = d.pop("channel_target")

        cooldown_minutes = d.pop("cooldown_minutes")

        notification_count = d.pop("notification_count")

        is_enabled = d.pop("is_enabled")

        created_at = isoparse(d.pop("created_at"))

        alert_state = d.pop("alert_state")

        pending_evaluations = d.pop("pending_evaluations")

        consecutive_hits = d.pop("consecutive_hits")

        def _parse_created_by(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                created_by_type_0 = UUID(data)

                return created_by_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        created_by = _parse_created_by(d.pop("created_by", UNSET))

        def _parse_last_triggered_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_triggered_at_type_0 = isoparse(data)

                return last_triggered_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_triggered_at = _parse_last_triggered_at(d.pop("last_triggered_at", UNSET))

        def _parse_last_value(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_value = _parse_last_value(d.pop("last_value", UNSET))

        def _parse_last_evaluated_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_evaluated_at_type_0 = isoparse(data)

                return last_evaluated_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_evaluated_at = _parse_last_evaluated_at(d.pop("last_evaluated_at", UNSET))

        def _parse_muted_until(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                muted_until_type_0 = isoparse(data)

                return muted_until_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        muted_until = _parse_muted_until(d.pop("muted_until", UNSET))

        alert_rule_with_agent = cls(
            id=id,
            agent_id=agent_id,
            agent_name=agent_name,
            condition_type=condition_type,
            threshold=threshold,
            window_minutes=window_minutes,
            channel=channel,
            channel_target=channel_target,
            cooldown_minutes=cooldown_minutes,
            notification_count=notification_count,
            is_enabled=is_enabled,
            created_at=created_at,
            alert_state=alert_state,
            pending_evaluations=pending_evaluations,
            consecutive_hits=consecutive_hits,
            created_by=created_by,
            last_triggered_at=last_triggered_at,
            last_value=last_value,
            last_evaluated_at=last_evaluated_at,
            muted_until=muted_until,
        )

        alert_rule_with_agent.additional_properties = d
        return alert_rule_with_agent

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
