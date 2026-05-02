from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="Invitation")


@_attrs_define
class Invitation:
    """An invitation row — local source-of-truth for the WorkOS-backed
    invitation lifecycle. Returned by `POST /v1/orgs/{org_id}/invitations`,
    `POST .../invitations/{id}/revoke`, `POST .../invitations/{id}/resend`,
    and embedded in `InvitationListResponse`.

    `status` is one of `"pending" | "accepted" | "revoked" | "expired"`;
    terminal states (everything except `"pending"`) cannot be acted on.

        Attributes:
            id (UUID):
            org_id (UUID):
            workos_invitation_id (str): Opaque WorkOS invitation identifier. Used internally to
                reconcile webhook deliveries; clients can ignore it.
            email (str): Email address as the inviter typed it (case preserved).
            email_normalized (str): `lower(trim(email))` — used for uniqueness checks. Always
                matches `lower(trim(email))` on the DB side.
            role (str): `"owner" | "admin" | "member" | "viewer"`.
            status (str): `"pending" | "accepted" | "revoked" | "expired"`. Example: pending.
            sent_at (datetime.datetime):
            expires_at (datetime.datetime):
            created_at (datetime.datetime):
            updated_at (datetime.datetime):
            sent_by_user_id (None | Unset | UUID): User who triggered the send. `null` for backfilled rows or
                system-generated invites.
            accepted_at (datetime.datetime | None | Unset):
            revoked_at (datetime.datetime | None | Unset):
            accepted_by_user_id (None | Unset | UUID): Set when the invitation reaches `"accepted"`. Populated from
                the WorkOS webhook payload, then resolved against
                `users.workos_user_id`.
    """

    id: UUID
    org_id: UUID
    workos_invitation_id: str
    email: str
    email_normalized: str
    role: str
    status: str
    sent_at: datetime.datetime
    expires_at: datetime.datetime
    created_at: datetime.datetime
    updated_at: datetime.datetime
    sent_by_user_id: None | Unset | UUID = UNSET
    accepted_at: datetime.datetime | None | Unset = UNSET
    revoked_at: datetime.datetime | None | Unset = UNSET
    accepted_by_user_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        org_id = str(self.org_id)

        workos_invitation_id = self.workos_invitation_id

        email = self.email

        email_normalized = self.email_normalized

        role = self.role

        status = self.status

        sent_at = self.sent_at.isoformat()

        expires_at = self.expires_at.isoformat()

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        sent_by_user_id: None | str | Unset
        if isinstance(self.sent_by_user_id, Unset):
            sent_by_user_id = UNSET
        elif isinstance(self.sent_by_user_id, UUID):
            sent_by_user_id = str(self.sent_by_user_id)
        else:
            sent_by_user_id = self.sent_by_user_id

        accepted_at: None | str | Unset
        if isinstance(self.accepted_at, Unset):
            accepted_at = UNSET
        elif isinstance(self.accepted_at, datetime.datetime):
            accepted_at = self.accepted_at.isoformat()
        else:
            accepted_at = self.accepted_at

        revoked_at: None | str | Unset
        if isinstance(self.revoked_at, Unset):
            revoked_at = UNSET
        elif isinstance(self.revoked_at, datetime.datetime):
            revoked_at = self.revoked_at.isoformat()
        else:
            revoked_at = self.revoked_at

        accepted_by_user_id: None | str | Unset
        if isinstance(self.accepted_by_user_id, Unset):
            accepted_by_user_id = UNSET
        elif isinstance(self.accepted_by_user_id, UUID):
            accepted_by_user_id = str(self.accepted_by_user_id)
        else:
            accepted_by_user_id = self.accepted_by_user_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "org_id": org_id,
                "workos_invitation_id": workos_invitation_id,
                "email": email,
                "email_normalized": email_normalized,
                "role": role,
                "status": status,
                "sent_at": sent_at,
                "expires_at": expires_at,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
        if sent_by_user_id is not UNSET:
            field_dict["sent_by_user_id"] = sent_by_user_id
        if accepted_at is not UNSET:
            field_dict["accepted_at"] = accepted_at
        if revoked_at is not UNSET:
            field_dict["revoked_at"] = revoked_at
        if accepted_by_user_id is not UNSET:
            field_dict["accepted_by_user_id"] = accepted_by_user_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        org_id = UUID(d.pop("org_id"))

        workos_invitation_id = d.pop("workos_invitation_id")

        email = d.pop("email")

        email_normalized = d.pop("email_normalized")

        role = d.pop("role")

        status = d.pop("status")

        sent_at = isoparse(d.pop("sent_at"))

        expires_at = isoparse(d.pop("expires_at"))

        created_at = isoparse(d.pop("created_at"))

        updated_at = isoparse(d.pop("updated_at"))

        def _parse_sent_by_user_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                sent_by_user_id_type_0 = UUID(data)

                return sent_by_user_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        sent_by_user_id = _parse_sent_by_user_id(d.pop("sent_by_user_id", UNSET))

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

        def _parse_revoked_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                revoked_at_type_0 = isoparse(data)

                return revoked_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        revoked_at = _parse_revoked_at(d.pop("revoked_at", UNSET))

        def _parse_accepted_by_user_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                accepted_by_user_id_type_0 = UUID(data)

                return accepted_by_user_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        accepted_by_user_id = _parse_accepted_by_user_id(d.pop("accepted_by_user_id", UNSET))

        invitation = cls(
            id=id,
            org_id=org_id,
            workos_invitation_id=workos_invitation_id,
            email=email,
            email_normalized=email_normalized,
            role=role,
            status=status,
            sent_at=sent_at,
            expires_at=expires_at,
            created_at=created_at,
            updated_at=updated_at,
            sent_by_user_id=sent_by_user_id,
            accepted_at=accepted_at,
            revoked_at=revoked_at,
            accepted_by_user_id=accepted_by_user_id,
        )

        invitation.additional_properties = d
        return invitation

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
