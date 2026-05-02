from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="DeviceTokenResponseType4Data")


@_attrs_define
class DeviceTokenResponseType4Data:
    """Approved + key issued. Terminal.

    Attributes:
        token (str): The full API key — show once, store in keychain, never
            echo back to the user. Matches the `POST /v1/keys` shape.
        active_org_id (UUID): Org the key is scoped to. Surfaced so the CLI can
            include it in `whoami` without an extra round trip.
    """

    token: str
    active_org_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        token = self.token

        active_org_id = str(self.active_org_id)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "token": token,
                "active_org_id": active_org_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        token = d.pop("token")

        active_org_id = UUID(d.pop("active_org_id"))

        device_token_response_type_4_data = cls(
            token=token,
            active_org_id=active_org_id,
        )

        device_token_response_type_4_data.additional_properties = d
        return device_token_response_type_4_data

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
