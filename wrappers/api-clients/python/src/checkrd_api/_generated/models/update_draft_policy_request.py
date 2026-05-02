from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="UpdateDraftPolicyRequest")


@_attrs_define
class UpdateDraftPolicyRequest:
    """`PATCH /v1/agents/{agent_id}/policies/{version}` request body.

    Only non-active (draft) versions can be edited. Activating a
    version freezes its YAML — subsequent edits require creating a
    new version.

        Attributes:
            yaml_content (str): Replacement YAML for the draft version. Validated server-side.
    """

    yaml_content: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        yaml_content = self.yaml_content

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "yaml_content": yaml_content,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        yaml_content = d.pop("yaml_content")

        update_draft_policy_request = cls(
            yaml_content=yaml_content,
        )

        update_draft_policy_request.additional_properties = d
        return update_draft_policy_request

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
