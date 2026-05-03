from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="AlertStateHistoryEntry")


@_attrs_define
class AlertStateHistoryEntry:
    """State transition history entry (sub-resource of an alert rule).

    Attributes:
        id (UUID):
        previous_state (str):
        new_state (str):
        threshold (float):
        reason (str):
        evaluated_at (datetime.datetime):
        observed_value (float | None | Unset):
    """

    id: UUID
    previous_state: str
    new_state: str
    threshold: float
    reason: str
    evaluated_at: datetime.datetime
    observed_value: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        previous_state = self.previous_state

        new_state = self.new_state

        threshold = self.threshold

        reason = self.reason

        evaluated_at = self.evaluated_at.isoformat()

        observed_value: float | None | Unset
        if isinstance(self.observed_value, Unset):
            observed_value = UNSET
        else:
            observed_value = self.observed_value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "previous_state": previous_state,
                "new_state": new_state,
                "threshold": threshold,
                "reason": reason,
                "evaluated_at": evaluated_at,
            }
        )
        if observed_value is not UNSET:
            field_dict["observed_value"] = observed_value

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        previous_state = d.pop("previous_state")

        new_state = d.pop("new_state")

        threshold = d.pop("threshold")

        reason = d.pop("reason")

        evaluated_at = isoparse(d.pop("evaluated_at"))

        def _parse_observed_value(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        observed_value = _parse_observed_value(d.pop("observed_value", UNSET))

        alert_state_history_entry = cls(
            id=id,
            previous_state=previous_state,
            new_state=new_state,
            threshold=threshold,
            reason=reason,
            evaluated_at=evaluated_at,
            observed_value=observed_value,
        )

        alert_state_history_entry.additional_properties = d
        return alert_state_history_entry

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
