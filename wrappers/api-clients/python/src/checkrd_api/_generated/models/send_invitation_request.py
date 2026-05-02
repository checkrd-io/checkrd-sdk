from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="SendInvitationRequest")


@_attrs_define
class SendInvitationRequest:
    """Request body for `POST /v1/orgs/{org_id}/invitations`.

    Attributes:
        email (str): Email address of the invitee. Validated against a strict
            regex; case is preserved on the wire and only normalized
            internally for uniqueness checks. Example: alice@example.com.
        role (str): Role the invitee will receive on acceptance. One of
            `"owner" | "admin" | "member" | "viewer"`. Example: member.
    """

    email: str
    role: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        email = self.email

        role = self.role

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "email": email,
                "role": role,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        role = d.pop("role")

        send_invitation_request = cls(
            email=email,
            role=role,
        )

        send_invitation_request.additional_properties = d
        return send_invitation_request

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
