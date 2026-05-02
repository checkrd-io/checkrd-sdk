from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AutoFix")


@_attrs_define
class AutoFix:
    """A single auto-fix suggestion attached to an `AnalyzeFinding`.

    Attributes:
        description (str): One-line description of the suggested change.
        new_yaml (str): Replacement YAML. Round-trips through serde so it is always
            valid; YAML comments are dropped in the process.
    """

    description: str
    new_yaml: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        description = self.description

        new_yaml = self.new_yaml

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "description": description,
                "new_yaml": new_yaml,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        description = d.pop("description")

        new_yaml = d.pop("new_yaml")

        auto_fix = cls(
            description=description,
            new_yaml=new_yaml,
        )

        auto_fix.additional_properties = d
        return auto_fix

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
