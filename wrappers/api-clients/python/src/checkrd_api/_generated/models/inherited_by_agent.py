from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="InheritedByAgent")


@_attrs_define
class InheritedByAgent:
    """Single row returned by `GET /v1/org-policies/inherited-by`.

    Attributes:
        agent_id (UUID):
        name (str): Display name of the agent.
        active_policy_version (int | None | Unset): Version of the agent's currently active per-agent policy. `None`
            when the agent has no agent-level policy of its own (it inherits
            the org policy directly).
        active_policy_mode (None | str | Unset): Enforcement mode of the agent's active per-agent policy
            (`"dry_run"` or `"enforce"`). Extracted from the YAML `mode:`
            key. `None` when the agent has no active policy.
    """

    agent_id: UUID
    name: str
    active_policy_version: int | None | Unset = UNSET
    active_policy_mode: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = str(self.agent_id)

        name = self.name

        active_policy_version: int | None | Unset
        if isinstance(self.active_policy_version, Unset):
            active_policy_version = UNSET
        else:
            active_policy_version = self.active_policy_version

        active_policy_mode: None | str | Unset
        if isinstance(self.active_policy_mode, Unset):
            active_policy_mode = UNSET
        else:
            active_policy_mode = self.active_policy_mode

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
                "name": name,
            }
        )
        if active_policy_version is not UNSET:
            field_dict["active_policy_version"] = active_policy_version
        if active_policy_mode is not UNSET:
            field_dict["active_policy_mode"] = active_policy_mode

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = UUID(d.pop("agent_id"))

        name = d.pop("name")

        def _parse_active_policy_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        active_policy_version = _parse_active_policy_version(d.pop("active_policy_version", UNSET))

        def _parse_active_policy_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        active_policy_mode = _parse_active_policy_mode(d.pop("active_policy_mode", UNSET))

        inherited_by_agent = cls(
            agent_id=agent_id,
            name=name,
            active_policy_version=active_policy_version,
            active_policy_mode=active_policy_mode,
        )

        inherited_by_agent.additional_properties = d
        return inherited_by_agent

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
