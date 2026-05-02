from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MeResponse")


@_attrs_define
class MeResponse:
    """`GET /auth/me` response body.

    Identity payload for the authenticated user + active org context.
    The dashboard's `useAuth()` hook uses this to populate the
    ProfileMenu (email + name + avatar_url) plus the active-org
    context already in the JWT (active_org_id, role) so clients
    don't have to cross-reference two responses.

        Attributes:
            user_id (UUID):
            email (str):
            active_org_id (UUID):
            role (str): Caller's role in the active org. One of `owner`, `admin`,
                `member`, `viewer`.
            name (None | str | Unset):
            avatar_url (None | str | Unset):
    """

    user_id: UUID
    email: str
    active_org_id: UUID
    role: str
    name: None | str | Unset = UNSET
    avatar_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user_id = str(self.user_id)

        email = self.email

        active_org_id = str(self.active_org_id)

        role = self.role

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        avatar_url: None | str | Unset
        if isinstance(self.avatar_url, Unset):
            avatar_url = UNSET
        else:
            avatar_url = self.avatar_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user_id": user_id,
                "email": email,
                "active_org_id": active_org_id,
                "role": role,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if avatar_url is not UNSET:
            field_dict["avatar_url"] = avatar_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        user_id = UUID(d.pop("user_id"))

        email = d.pop("email")

        active_org_id = UUID(d.pop("active_org_id"))

        role = d.pop("role")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_avatar_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        avatar_url = _parse_avatar_url(d.pop("avatar_url", UNSET))

        me_response = cls(
            user_id=user_id,
            email=email,
            active_org_id=active_org_id,
            role=role,
            name=name,
            avatar_url=avatar_url,
        )

        me_response.additional_properties = d
        return me_response

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
