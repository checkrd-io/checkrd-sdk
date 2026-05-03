from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.device_token_response_type_3_status import DeviceTokenResponseType3Status

T = TypeVar("T", bound="DeviceTokenResponseType3")


@_attrs_define
class DeviceTokenResponseType3:
    """TTL elapsed (or unknown / already-consumed code, bundled
    here so an attacker probing device codes can't distinguish
    "never existed" from "stolen-but-redeemed"). Terminal.

        Attributes:
            status (DeviceTokenResponseType3Status):
    """

    status: DeviceTokenResponseType3Status
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "status": status,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = DeviceTokenResponseType3Status(d.pop("status"))

        device_token_response_type_3 = cls(
            status=status,
        )

        device_token_response_type_3.additional_properties = d
        return device_token_response_type_3

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
