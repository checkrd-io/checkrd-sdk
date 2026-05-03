from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgMemberWithEmail")


@_attrs_define
class OrgMemberWithEmail:
    """A member of an organization, with denormalized user fields so
    the dashboard's member list doesn't need a second roundtrip.
    Returned by `GET /v1/orgs/{org_id}/members`.

        Attributes:
            id (UUID):
            org_id (UUID):
            user_id (UUID):
            role (str): Org-scoped role: `"owner" | "admin" | "member" | "viewer"`. Example: member.
            email (str):
            accepted_at (datetime.datetime | None | Unset): When the user accepted the invitation. `null` for legacy
                rows pre-dating the invitation flow.
            user_name (None | str | Unset): Display name from the user record. May be `null` if the
                user has not completed profile setup.
    """

    id: UUID
    org_id: UUID
    user_id: UUID
    role: str
    email: str
    accepted_at: datetime.datetime | None | Unset = UNSET
    user_name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        org_id = str(self.org_id)

        user_id = str(self.user_id)

        role = self.role

        email = self.email

        accepted_at: None | str | Unset
        if isinstance(self.accepted_at, Unset):
            accepted_at = UNSET
        elif isinstance(self.accepted_at, datetime.datetime):
            accepted_at = self.accepted_at.isoformat()
        else:
            accepted_at = self.accepted_at

        user_name: None | str | Unset
        if isinstance(self.user_name, Unset):
            user_name = UNSET
        else:
            user_name = self.user_name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "org_id": org_id,
                "user_id": user_id,
                "role": role,
                "email": email,
            }
        )
        if accepted_at is not UNSET:
            field_dict["accepted_at"] = accepted_at
        if user_name is not UNSET:
            field_dict["user_name"] = user_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        org_id = UUID(d.pop("org_id"))

        user_id = UUID(d.pop("user_id"))

        role = d.pop("role")

        email = d.pop("email")

        def _parse_accepted_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                accepted_at_type_0 = isoparse(data)

                return accepted_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        accepted_at = _parse_accepted_at(d.pop("accepted_at", UNSET))

        def _parse_user_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        user_name = _parse_user_name(d.pop("user_name", UNSET))

        org_member_with_email = cls(
            id=id,
            org_id=org_id,
            user_id=user_id,
            role=role,
            email=email,
            accepted_at=accepted_at,
            user_name=user_name,
        )

        org_member_with_email.additional_properties = d
        return org_member_with_email

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
