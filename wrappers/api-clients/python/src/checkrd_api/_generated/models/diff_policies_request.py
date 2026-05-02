from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="DiffPoliciesRequest")


@_attrs_define
class DiffPoliciesRequest:
    """`POST /v1/agents/{agent_id}/policies/diff` request body.

    Attributes:
        base_version (int): Version number of the base (before) policy. Must exist for
            this agent or the call returns 404.
        candidate_yaml (str): YAML content of the candidate (after) policy. Validated
            server-side before diffing.
    """

    base_version: int
    candidate_yaml: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        base_version = self.base_version

        candidate_yaml = self.candidate_yaml

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "base_version": base_version,
                "candidate_yaml": candidate_yaml,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        base_version = d.pop("base_version")

        candidate_yaml = d.pop("candidate_yaml")

        diff_policies_request = cls(
            base_version=base_version,
            candidate_yaml=candidate_yaml,
        )

        diff_policies_request.additional_properties = d
        return diff_policies_request

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
