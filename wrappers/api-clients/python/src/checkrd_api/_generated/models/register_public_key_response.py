from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="RegisterPublicKeyResponse")


@_attrs_define
class RegisterPublicKeyResponse:
    """`POST /v1/agents/{agent_id}/public-key` response body.

    Attributes:
        registered (bool): `true` if this call set the key for the first time, `false` if the
            same key was already registered (idempotent replay).
        public_key (str): The public key that is now associated with the agent (lowercase hex).
    """

    registered: bool
    public_key: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        registered = self.registered

        public_key = self.public_key

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "registered": registered,
                "public_key": public_key,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        registered = d.pop("registered")

        public_key = d.pop("public_key")

        register_public_key_response = cls(
            registered=registered,
            public_key=public_key,
        )

        register_public_key_response.additional_properties = d
        return register_public_key_response

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
