from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PlanLimits")


@_attrs_define
class PlanLimits:
    """Resource counts and feature flags for a billing tier. Wire-shape
    mirror of `checkrd_shared::PlanLimits` (which can't derive
    `ToSchema` because it has to compile to `wasm32-wasip1`).

    Both structs serialize identically — the api crate converts at
    the boundary.

        Attributes:
            max_agents (int):
            max_members (int):
            max_api_keys (int):
            max_events_per_month (int): Monthly event cap. `u64::MAX` for the unlimited tier.
                Serialized as `number` for TS — JS numbers are f64 with an
                upper bound of 2^53-1, still plenty for monthly events.
            data_retention_days (int):
            sso_enabled (bool):
            audit_log_enabled (bool):
    """

    max_agents: int
    max_members: int
    max_api_keys: int
    max_events_per_month: int
    data_retention_days: int
    sso_enabled: bool
    audit_log_enabled: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        max_agents = self.max_agents

        max_members = self.max_members

        max_api_keys = self.max_api_keys

        max_events_per_month = self.max_events_per_month

        data_retention_days = self.data_retention_days

        sso_enabled = self.sso_enabled

        audit_log_enabled = self.audit_log_enabled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "max_agents": max_agents,
                "max_members": max_members,
                "max_api_keys": max_api_keys,
                "max_events_per_month": max_events_per_month,
                "data_retention_days": data_retention_days,
                "sso_enabled": sso_enabled,
                "audit_log_enabled": audit_log_enabled,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        max_agents = d.pop("max_agents")

        max_members = d.pop("max_members")

        max_api_keys = d.pop("max_api_keys")

        max_events_per_month = d.pop("max_events_per_month")

        data_retention_days = d.pop("data_retention_days")

        sso_enabled = d.pop("sso_enabled")

        audit_log_enabled = d.pop("audit_log_enabled")

        plan_limits = cls(
            max_agents=max_agents,
            max_members=max_members,
            max_api_keys=max_api_keys,
            max_events_per_month=max_events_per_month,
            data_retention_days=data_retention_days,
            sso_enabled=sso_enabled,
            audit_log_enabled=audit_log_enabled,
        )

        plan_limits.additional_properties = d
        return plan_limits

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
