from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="AlertNotificationEntry")


@_attrs_define
class AlertNotificationEntry:
    """Notification delivery log entry (sub-resource of an alert rule).

    Attributes:
        id (UUID):
        channel (str):
        channel_target (str):
        observed_value (float):
        threshold (float):
        condition_type (str):
        alert_state (str):
        status (str): Delivery status: `pending`, `sent`, `failed`, etc.
        attempt (int):
        created_at (datetime.datetime):
        error_message (None | str | Unset):
        delivered_at (datetime.datetime | None | Unset):
    """

    id: UUID
    channel: str
    channel_target: str
    observed_value: float
    threshold: float
    condition_type: str
    alert_state: str
    status: str
    attempt: int
    created_at: datetime.datetime
    error_message: None | str | Unset = UNSET
    delivered_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        channel = self.channel

        channel_target = self.channel_target

        observed_value = self.observed_value

        threshold = self.threshold

        condition_type = self.condition_type

        alert_state = self.alert_state

        status = self.status

        attempt = self.attempt

        created_at = self.created_at.isoformat()

        error_message: None | str | Unset
        if isinstance(self.error_message, Unset):
            error_message = UNSET
        else:
            error_message = self.error_message

        delivered_at: None | str | Unset
        if isinstance(self.delivered_at, Unset):
            delivered_at = UNSET
        elif isinstance(self.delivered_at, datetime.datetime):
            delivered_at = self.delivered_at.isoformat()
        else:
            delivered_at = self.delivered_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "channel": channel,
                "channel_target": channel_target,
                "observed_value": observed_value,
                "threshold": threshold,
                "condition_type": condition_type,
                "alert_state": alert_state,
                "status": status,
                "attempt": attempt,
                "created_at": created_at,
            }
        )
        if error_message is not UNSET:
            field_dict["error_message"] = error_message
        if delivered_at is not UNSET:
            field_dict["delivered_at"] = delivered_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        channel = d.pop("channel")

        channel_target = d.pop("channel_target")

        observed_value = d.pop("observed_value")

        threshold = d.pop("threshold")

        condition_type = d.pop("condition_type")

        alert_state = d.pop("alert_state")

        status = d.pop("status")

        attempt = d.pop("attempt")

        created_at = isoparse(d.pop("created_at"))

        def _parse_error_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_message = _parse_error_message(d.pop("error_message", UNSET))

        def _parse_delivered_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                delivered_at_type_0 = isoparse(data)

                return delivered_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        delivered_at = _parse_delivered_at(d.pop("delivered_at", UNSET))

        alert_notification_entry = cls(
            id=id,
            channel=channel,
            channel_target=channel_target,
            observed_value=observed_value,
            threshold=threshold,
            condition_type=condition_type,
            alert_state=alert_state,
            status=status,
            attempt=attempt,
            created_at=created_at,
            error_message=error_message,
            delivered_at=delivered_at,
        )

        alert_notification_entry.additional_properties = d
        return alert_notification_entry

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
