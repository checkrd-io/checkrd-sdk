from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateAlertRequest")


@_attrs_define
class CreateAlertRequest:
    """`POST /v1/alerts` request body.

    Attributes:
        agent_id (UUID):
        condition_type (str): One of `error_rate`, `latency_p99`, `denied_rate`,
            `volume_drop`. Example: error_rate.
        threshold (float): Numeric threshold the condition is evaluated against. Must
            be > 0. Example: 0.05.
        channel (str): Notification channel: `email` or `webhook`. Example: email.
        channel_target (str): Email address or webhook URL. Example: alerts@example.com.
        window_minutes (int | None | Unset): Evaluation window in minutes (default 15).
        cooldown_minutes (int | None | Unset): Minimum minutes between notifications for this rule (default
            60).
    """

    agent_id: UUID
    condition_type: str
    threshold: float
    channel: str
    channel_target: str
    window_minutes: int | None | Unset = UNSET
    cooldown_minutes: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = str(self.agent_id)

        condition_type = self.condition_type

        threshold = self.threshold

        channel = self.channel

        channel_target = self.channel_target

        window_minutes: int | None | Unset
        if isinstance(self.window_minutes, Unset):
            window_minutes = UNSET
        else:
            window_minutes = self.window_minutes

        cooldown_minutes: int | None | Unset
        if isinstance(self.cooldown_minutes, Unset):
            cooldown_minutes = UNSET
        else:
            cooldown_minutes = self.cooldown_minutes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
                "condition_type": condition_type,
                "threshold": threshold,
                "channel": channel,
                "channel_target": channel_target,
            }
        )
        if window_minutes is not UNSET:
            field_dict["window_minutes"] = window_minutes
        if cooldown_minutes is not UNSET:
            field_dict["cooldown_minutes"] = cooldown_minutes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = UUID(d.pop("agent_id"))

        condition_type = d.pop("condition_type")

        threshold = d.pop("threshold")

        channel = d.pop("channel")

        channel_target = d.pop("channel_target")

        def _parse_window_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        window_minutes = _parse_window_minutes(d.pop("window_minutes", UNSET))

        def _parse_cooldown_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cooldown_minutes = _parse_cooldown_minutes(d.pop("cooldown_minutes", UNSET))

        create_alert_request = cls(
            agent_id=agent_id,
            condition_type=condition_type,
            threshold=threshold,
            channel=channel,
            channel_target=channel_target,
            window_minutes=window_minutes,
            cooldown_minutes=cooldown_minutes,
        )

        create_alert_request.additional_properties = d
        return create_alert_request

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
