from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="AlertHistoryEntry")


@_attrs_define
class AlertHistoryEntry:
    """A single alert state transition.

    Returned (paginated) from `GET /v1/alert-history`.

        Attributes:
            id (UUID):
            alert_rule_id (UUID):
            agent_id (UUID):
            agent_name (str): Denormalized so clients don't re-query per row.
            condition_type (str): Logical condition family (e.g., `rate_limit`, `error_rate`).
            previous_state (str): Prior state (`ok`, `no_data`, `pending`, `firing`, `resolved`).
            new_state (str): New state (`ok`, `no_data`, `pending`, `firing`, `resolved`).
            threshold (float): Threshold the rule was evaluated against.
            reason (str): Human-readable explanation of why the transition fired.
            evaluated_at (datetime.datetime):
            observed_value (float | None | Unset): Most-recent observed value at evaluation time. May be null if
                the rule had no data.
    """

    id: UUID
    alert_rule_id: UUID
    agent_id: UUID
    agent_name: str
    condition_type: str
    previous_state: str
    new_state: str
    threshold: float
    reason: str
    evaluated_at: datetime.datetime
    observed_value: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        alert_rule_id = str(self.alert_rule_id)

        agent_id = str(self.agent_id)

        agent_name = self.agent_name

        condition_type = self.condition_type

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
                "alert_rule_id": alert_rule_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "condition_type": condition_type,
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

        alert_rule_id = UUID(d.pop("alert_rule_id"))

        agent_id = UUID(d.pop("agent_id"))

        agent_name = d.pop("agent_name")

        condition_type = d.pop("condition_type")

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

        alert_history_entry = cls(
            id=id,
            alert_rule_id=alert_rule_id,
            agent_id=agent_id,
            agent_name=agent_name,
            condition_type=condition_type,
            previous_state=previous_state,
            new_state=new_state,
            threshold=threshold,
            reason=reason,
            evaluated_at=evaluated_at,
            observed_value=observed_value,
        )

        alert_history_entry.additional_properties = d
        return alert_history_entry

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
