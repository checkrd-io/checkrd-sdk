from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.control_pricing_updated_event_pricing_envelope import ControlPricingUpdatedEventPricingEnvelope


T = TypeVar("T", bound="ControlPricingUpdatedEvent")


@_attrs_define
class ControlPricingUpdatedEvent:
    """`pricing_updated` SSE event payload — emitted whenever a new pricing
    bundle version is activated. The price-table analogue of
    [`ControlPolicyUpdatedEvent`].

    `pricing_envelope` is a DSSE envelope (`PricingBundle` payload type) that
    the SDK verifies in-WASM against its pinned trust list before installing.
    A tampered price table is an integrity attack on money, so it rides the
    same strong-from-the-ground-up distribution path as the policy bundle:
    there is no unsigned pricing path.

        Attributes:
            version (int): Monotonic pricing-bundle version. Used by the SDK to enforce rollback
                protection: a bundle with `version <= last_pricing_version` is rejected
                (the TUF "never replace with a lower version number" rule).
            hash_ (str): SHA-256 of the pricing bundle, lowercase hex.
            pricing_envelope (ControlPricingUpdatedEventPricingEnvelope): DSSE envelope wrapping the canonical
                `PricingBundle` JSON.
                Verified in-WASM by the SDK.
    """

    version: int
    hash_: str
    pricing_envelope: ControlPricingUpdatedEventPricingEnvelope
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        version = self.version

        hash_ = self.hash_

        pricing_envelope = self.pricing_envelope.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "version": version,
                "hash": hash_,
                "pricing_envelope": pricing_envelope,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.control_pricing_updated_event_pricing_envelope import ControlPricingUpdatedEventPricingEnvelope

        d = dict(src_dict)
        version = d.pop("version")

        hash_ = d.pop("hash")

        pricing_envelope = ControlPricingUpdatedEventPricingEnvelope.from_dict(d.pop("pricing_envelope"))

        control_pricing_updated_event = cls(
            version=version,
            hash_=hash_,
            pricing_envelope=pricing_envelope,
        )

        control_pricing_updated_event.additional_properties = d
        return control_pricing_updated_event

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
