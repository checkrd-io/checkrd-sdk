from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.event_usage import EventUsage
    from ..models.resource_count import ResourceCount


T = TypeVar("T", bound="ResourceUsage")


@_attrs_define
class ResourceUsage:
    """Resource counts for a workspace, joined to its plan limits.

    Attributes:
        agents (ResourceCount): Current count + limit pair for resource-bounded entities.
        api_keys (ResourceCount): Current count + limit pair for resource-bounded entities.
        members (ResourceCount): Current count + limit pair for resource-bounded entities.
        events_this_month (EventUsage): Monthly event usage. Separate type from `ResourceCount` because
            the limit is `u64` (Free: 100K, Team: 1M, Enterprise: `u64::MAX`)
            — `ResourceCount`'s `u32` limit can't represent the unlimited
            tier.
    """

    agents: ResourceCount
    api_keys: ResourceCount
    members: ResourceCount
    events_this_month: EventUsage
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agents = self.agents.to_dict()

        api_keys = self.api_keys.to_dict()

        members = self.members.to_dict()

        events_this_month = self.events_this_month.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agents": agents,
                "api_keys": api_keys,
                "members": members,
                "events_this_month": events_this_month,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.event_usage import EventUsage
        from ..models.resource_count import ResourceCount

        d = dict(src_dict)
        agents = ResourceCount.from_dict(d.pop("agents"))

        api_keys = ResourceCount.from_dict(d.pop("api_keys"))

        members = ResourceCount.from_dict(d.pop("members"))

        events_this_month = EventUsage.from_dict(d.pop("events_this_month"))

        resource_usage = cls(
            agents=agents,
            api_keys=api_keys,
            members=members,
            events_this_month=events_this_month,
        )

        resource_usage.additional_properties = d
        return resource_usage

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
