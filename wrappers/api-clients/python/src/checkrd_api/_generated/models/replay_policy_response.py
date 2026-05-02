from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ReplayPolicyResponse")


@_attrs_define
class ReplayPolicyResponse:
    """`POST /v1/agents/{agent_id}/policies/replay` response body.

    Attributes:
        total_events (int): Total events evaluated (capped by the request `limit`).
        would_allow (int): Number that the candidate policy would allow.
        would_deny (int): Number that the candidate policy would deny.
        would_rate_limit (int): Approximate. Replay creates a fresh `RateLimiter` per event
            (stateless), so counters do not accumulate across the window.
        delta_event_ids (list[str]): `request_id`s where the candidate's verdict differs from the
            recorded `policy_result` for that event.
    """

    total_events: int
    would_allow: int
    would_deny: int
    would_rate_limit: int
    delta_event_ids: list[str]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total_events = self.total_events

        would_allow = self.would_allow

        would_deny = self.would_deny

        would_rate_limit = self.would_rate_limit

        delta_event_ids = self.delta_event_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "total_events": total_events,
                "would_allow": would_allow,
                "would_deny": would_deny,
                "would_rate_limit": would_rate_limit,
                "delta_event_ids": delta_event_ids,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        total_events = d.pop("total_events")

        would_allow = d.pop("would_allow")

        would_deny = d.pop("would_deny")

        would_rate_limit = d.pop("would_rate_limit")

        delta_event_ids = cast(list[str], d.pop("delta_event_ids"))

        replay_policy_response = cls(
            total_events=total_events,
            would_allow=would_allow,
            would_deny=would_deny,
            would_rate_limit=would_rate_limit,
            delta_event_ids=delta_event_ids,
        )

        replay_policy_response.additional_properties = d
        return replay_policy_response

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
