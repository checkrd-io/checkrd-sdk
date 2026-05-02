from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.merged_effective_policy_response_source_map import MergedEffectivePolicyResponseSourceMap


T = TypeVar("T", bound="MergedEffectivePolicyResponse")


@_attrs_define
class MergedEffectivePolicyResponse:
    """`GET /v1/agents/{agent_id}/policies/merged-effective` response body.

    Carries the merged YAML alongside per-line source attribution so
    the dashboard can render origin annotations (org / agent / both).

        Attributes:
            yaml (str): Canonical YAML of the merged effective policy.
            source_map (MergedEffectivePolicyResponseSourceMap): One entry per line of `yaml`, tagged with its source
                (`"org"`, `"agent"`, or `"both"`).
            org_yaml (str): Raw org YAML used in the merge (empty string when none exists).
            agent_yaml (str): Raw agent YAML used in the merge (empty string when none exists).
            has_org_policy (bool): `true` when an active org policy was merged in.
            org_policy_version (int | None | Unset): Version of the org policy that was merged. `None` when no org
                policy exists.
            agent_policy_version (int | None | Unset): Version of the per-agent active policy. `None` when the agent
                has none.
    """

    yaml: str
    source_map: MergedEffectivePolicyResponseSourceMap
    org_yaml: str
    agent_yaml: str
    has_org_policy: bool
    org_policy_version: int | None | Unset = UNSET
    agent_policy_version: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        yaml = self.yaml

        source_map = self.source_map.to_dict()

        org_yaml = self.org_yaml

        agent_yaml = self.agent_yaml

        has_org_policy = self.has_org_policy

        org_policy_version: int | None | Unset
        if isinstance(self.org_policy_version, Unset):
            org_policy_version = UNSET
        else:
            org_policy_version = self.org_policy_version

        agent_policy_version: int | None | Unset
        if isinstance(self.agent_policy_version, Unset):
            agent_policy_version = UNSET
        else:
            agent_policy_version = self.agent_policy_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "yaml": yaml,
                "source_map": source_map,
                "org_yaml": org_yaml,
                "agent_yaml": agent_yaml,
                "has_org_policy": has_org_policy,
            }
        )
        if org_policy_version is not UNSET:
            field_dict["org_policy_version"] = org_policy_version
        if agent_policy_version is not UNSET:
            field_dict["agent_policy_version"] = agent_policy_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.merged_effective_policy_response_source_map import MergedEffectivePolicyResponseSourceMap

        d = dict(src_dict)
        yaml = d.pop("yaml")

        source_map = MergedEffectivePolicyResponseSourceMap.from_dict(d.pop("source_map"))

        org_yaml = d.pop("org_yaml")

        agent_yaml = d.pop("agent_yaml")

        has_org_policy = d.pop("has_org_policy")

        def _parse_org_policy_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        org_policy_version = _parse_org_policy_version(d.pop("org_policy_version", UNSET))

        def _parse_agent_policy_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        agent_policy_version = _parse_agent_policy_version(d.pop("agent_policy_version", UNSET))

        merged_effective_policy_response = cls(
            yaml=yaml,
            source_map=source_map,
            org_yaml=org_yaml,
            agent_yaml=agent_yaml,
            has_org_policy=has_org_policy,
            org_policy_version=org_policy_version,
            agent_policy_version=agent_policy_version,
        )

        merged_effective_policy_response.additional_properties = d
        return merged_effective_policy_response

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
