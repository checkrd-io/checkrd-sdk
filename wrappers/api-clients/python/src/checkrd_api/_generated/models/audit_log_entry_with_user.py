from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.audit_log_entry_with_user_details import AuditLogEntryWithUserDetails


T = TypeVar("T", bound="AuditLogEntryWithUser")


@_attrs_define
class AuditLogEntryWithUser:
    """Audit log entry with the actor's display name when applicable.

    Each entry has both `actor_id` and `actor_kind` (mirrors the unified
    `Principal` model on the server). Dashboards branch on `actor_kind`
    to render attribution:

    - `"user"`    — `user_email` / `user_name` populated from a JOIN to `users`.
    - `"api_key"` — `actor_id` is the `api_keys.id`; render the key prefix.
    - `"system"`  — `actor_id` is null; show "Checkrd platform" / source attribution.

    Returned from `GET /v1/audit-log` and
    `GET /v1/audit-log/{resource_type}/{resource_id}`.

        Attributes:
            id (UUID):
            action (str): Action verb, e.g., `agent.created`, `policy.activated`,
                `kill_switch.toggled`.
            resource_type (str): Logical resource bucket the action targeted, e.g., `agent`,
                `policy`, `api_key`.
            resource_id (UUID):
            details (AuditLogEntryWithUserDetails): Free-form JSON payload with action-specific context (names,
                before/after diffs, kill-switch reasons, etc.).
            created_at (datetime.datetime):
            actor_kind (str): Closed-enum tag for which kind of principal performed the
                action: `"user"`, `"api_key"`, or `"system"`. The DB column has
                a CHECK constraint on these values; if a new variant is added
                server-side, update this doc and dashboards in lockstep.
            ip_address (None | str | Unset): Source IP address of the request that triggered the action,
                when available.
            actor_id (None | Unset | UUID): `users.id` when `actor_kind == "user"`, `api_keys.id` when
                `actor_kind == "api_key"`, null when `actor_kind == "system"`.
            user_email (None | str | Unset): Email of the user who performed the action. Populated only when
                `actor_kind == "user"`.
            user_name (None | str | Unset): Display name of the user who performed the action. Populated
                only when `actor_kind == "user"`.
    """

    id: UUID
    action: str
    resource_type: str
    resource_id: UUID
    details: AuditLogEntryWithUserDetails
    created_at: datetime.datetime
    actor_kind: str
    ip_address: None | str | Unset = UNSET
    actor_id: None | Unset | UUID = UNSET
    user_email: None | str | Unset = UNSET
    user_name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        action = self.action

        resource_type = self.resource_type

        resource_id = str(self.resource_id)

        details = self.details.to_dict()

        created_at = self.created_at.isoformat()

        actor_kind = self.actor_kind

        ip_address: None | str | Unset
        if isinstance(self.ip_address, Unset):
            ip_address = UNSET
        else:
            ip_address = self.ip_address

        actor_id: None | str | Unset
        if isinstance(self.actor_id, Unset):
            actor_id = UNSET
        elif isinstance(self.actor_id, UUID):
            actor_id = str(self.actor_id)
        else:
            actor_id = self.actor_id

        user_email: None | str | Unset
        if isinstance(self.user_email, Unset):
            user_email = UNSET
        else:
            user_email = self.user_email

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
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": details,
                "created_at": created_at,
                "actor_kind": actor_kind,
            }
        )
        if ip_address is not UNSET:
            field_dict["ip_address"] = ip_address
        if actor_id is not UNSET:
            field_dict["actor_id"] = actor_id
        if user_email is not UNSET:
            field_dict["user_email"] = user_email
        if user_name is not UNSET:
            field_dict["user_name"] = user_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.audit_log_entry_with_user_details import AuditLogEntryWithUserDetails

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        action = d.pop("action")

        resource_type = d.pop("resource_type")

        resource_id = UUID(d.pop("resource_id"))

        details = AuditLogEntryWithUserDetails.from_dict(d.pop("details"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        actor_kind = d.pop("actor_kind")

        def _parse_ip_address(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ip_address = _parse_ip_address(d.pop("ip_address", UNSET))

        def _parse_actor_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                actor_id_type_0 = UUID(data)

                return actor_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        actor_id = _parse_actor_id(d.pop("actor_id", UNSET))

        def _parse_user_email(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        user_email = _parse_user_email(d.pop("user_email", UNSET))

        def _parse_user_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        user_name = _parse_user_name(d.pop("user_name", UNSET))

        audit_log_entry_with_user = cls(
            id=id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            created_at=created_at,
            actor_kind=actor_kind,
            ip_address=ip_address,
            actor_id=actor_id,
            user_email=user_email,
            user_name=user_name,
        )

        audit_log_entry_with_user.additional_properties = d
        return audit_log_entry_with_user

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
