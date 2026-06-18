from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="CreateKeyRequestScope")


@_attrs_define
class CreateKeyRequestScope:
    """Scope of the key. Stripe-style: `"all"` for unrestricted (only
    minted by `checkrd login` device flow); `"read_only"` for the
    read-everything preset; `"restricted"` with a per-resource
    matrix for fine-grained access. Resources omitted from a
    `restricted` map default to no access.

    Wire shape:
    - `{"kind": "all"}`
    - `{"kind": "read_only"}`
    - `{"kind": "restricted", "resources": {"agents": "write", "policies": "read"}}`

    """

    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        create_key_request_scope = cls()

        create_key_request_scope.additional_properties = d
        return create_key_request_scope

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
