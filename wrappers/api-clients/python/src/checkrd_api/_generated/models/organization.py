from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="Organization")


@_attrs_define
class Organization:
    """An organization (workspace) — every Checkrd customer is one. Returned
    as the wire shape for `/v1/orgs` and embedded in a number of
    invitation / member responses.

    `plan_tier` is the lowercase string `"free"`, `"team"`, or
    `"enterprise"` (mirroring `checkrd_shared::PlanTier`'s serde repr).

        Attributes:
            id (UUID):
            name (str):
            slug (str): URL-safe slug derived at create time. Stable for the
                lifetime of the org.
            plan_tier (str): Billing plan: `"free" | "team" | "enterprise"`. Example: free.
    """

    id: UUID
    name: str
    slug: str
    plan_tier: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        name = self.name

        slug = self.slug

        plan_tier = self.plan_tier

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "slug": slug,
                "plan_tier": plan_tier,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        name = d.pop("name")

        slug = d.pop("slug")

        plan_tier = d.pop("plan_tier")

        organization = cls(
            id=id,
            name=name,
            slug=slug,
            plan_tier=plan_tier,
        )

        organization.additional_properties = d
        return organization

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
