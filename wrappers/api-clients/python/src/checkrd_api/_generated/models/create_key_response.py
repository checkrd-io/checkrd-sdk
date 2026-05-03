from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="CreateKeyResponse")


@_attrs_define
class CreateKeyResponse:
    """`POST /v1/keys` response body. The `key` field is the full
    `ck_live_…` / `ck_test_…` value and is shown **exactly once**.
    After this response the dashboard only ever exposes `key_prefix`.

        Attributes:
            id (UUID):
            name (str):
            key (str): The full key — show once, never again. Treat as a credential
                and store it securely (e.g., in a secret manager).
            key_prefix (str): Short prefix (e.g., `ck_live_abc12345`) used to identify the
                key in dashboards and audit logs.
    """

    id: UUID
    name: str
    key: str
    key_prefix: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        name = self.name

        key = self.key

        key_prefix = self.key_prefix

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "key": key,
                "key_prefix": key_prefix,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        name = d.pop("name")

        key = d.pop("key")

        key_prefix = d.pop("key_prefix")

        create_key_response = cls(
            id=id,
            name=name,
            key=key,
            key_prefix=key_prefix,
        )

        create_key_response.additional_properties = d
        return create_key_response

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
