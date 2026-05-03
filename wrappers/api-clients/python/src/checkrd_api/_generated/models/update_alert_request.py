from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UpdateAlertRequest")


@_attrs_define
class UpdateAlertRequest:
    """`PUT /v1/alerts/{alert_id}` request body. All evaluation
    parameters are required (full replace, not patch).

        Attributes:
            condition_type (str):
            threshold (float):
            channel (str):
            channel_target (str):
            window_minutes (int | None | Unset):
            cooldown_minutes (int | None | Unset):
    """

    condition_type: str
    threshold: float
    channel: str
    channel_target: str
    window_minutes: int | None | Unset = UNSET
    cooldown_minutes: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
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

        update_alert_request = cls(
            condition_type=condition_type,
            threshold=threshold,
            channel=channel,
            channel_target=channel_target,
            window_minutes=window_minutes,
            cooldown_minutes=cooldown_minutes,
        )

        update_alert_request.additional_properties = d
        return update_alert_request

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
