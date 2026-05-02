from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PortalResponse")


@_attrs_define
class PortalResponse:
    """`POST /v1/billing/portal` response body.

    Returns a Stripe Customer Portal URL where the user can update
    payment method, view invoices, and cancel/upgrade their
    subscription.

        Attributes:
            url (str): Stripe Customer Portal URL. Single-use, expires after the
                portal session ends. Always points to a Stripe-hosted page —
                `billing.stripe.com/...`. Example: https://billing.stripe.com/p/session/xxx.
    """

    url: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        url = self.url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "url": url,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        url = d.pop("url")

        portal_response = cls(
            url=url,
        )

        portal_response.additional_properties = d
        return portal_response

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
