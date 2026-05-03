from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="TopHost")


@_attrs_define
class TopHost:
    """`GET /v1/dashboard/hosts` response item.

    Top external API hosts the workspace's agents are calling, with a
    breakdown of allow/deny/error counts per host.

        Attributes:
            url_host (str): Public hostname (e.g., `"api.stripe.com"`).
            call_count (int):
            allowed_count (int):
            denied_count (int):
            error_count (int):
    """

    url_host: str
    call_count: int
    allowed_count: int
    denied_count: int
    error_count: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        url_host = self.url_host

        call_count = self.call_count

        allowed_count = self.allowed_count

        denied_count = self.denied_count

        error_count = self.error_count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "url_host": url_host,
                "call_count": call_count,
                "allowed_count": allowed_count,
                "denied_count": denied_count,
                "error_count": error_count,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        url_host = d.pop("url_host")

        call_count = d.pop("call_count")

        allowed_count = d.pop("allowed_count")

        denied_count = d.pop("denied_count")

        error_count = d.pop("error_count")

        top_host = cls(
            url_host=url_host,
            call_count=call_count,
            allowed_count=allowed_count,
            denied_count=denied_count,
            error_count=error_count,
        )

        top_host.additional_properties = d
        return top_host

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
