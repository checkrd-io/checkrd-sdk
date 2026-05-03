from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.organization import Organization


T = TypeVar("T", bound="ListOrgsResponse")


@_attrs_define
class ListOrgsResponse:
    """Response body for `GET /v1/orgs`. Lists every org the authenticated
    user is a member of, plus the currently-active org from the JWT so
    the dashboard's switcher can render without an extra `whoami` call.

        Attributes:
            organizations (list[Organization]):
            active_org_id (UUID): The org id encoded into the caller's session JWT — i.e., the
                org their requests are scoped to until they switch.
    """

    organizations: list[Organization]
    active_org_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        organizations = []
        for organizations_item_data in self.organizations:
            organizations_item = organizations_item_data.to_dict()
            organizations.append(organizations_item)

        active_org_id = str(self.active_org_id)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "organizations": organizations,
                "active_org_id": active_org_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.organization import Organization

        d = dict(src_dict)
        organizations = []
        _organizations = d.pop("organizations")
        for organizations_item_data in _organizations:
            organizations_item = Organization.from_dict(organizations_item_data)

            organizations.append(organizations_item)

        active_org_id = UUID(d.pop("active_org_id"))

        list_orgs_response = cls(
            organizations=organizations,
            active_org_id=active_org_id,
        )

        list_orgs_response.additional_properties = d
        return list_orgs_response

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
