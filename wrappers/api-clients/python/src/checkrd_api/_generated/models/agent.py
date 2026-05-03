from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="Agent")


@_attrs_define
class Agent:
    """An agent — one autonomous AI process under policy enforcement.

    Returned from `GET /v1/agents`, `GET /v1/agents/{id}`,
    `POST /v1/agents`, `PUT /v1/agents/{id}`,
    `POST /v1/agents/{id}/kill-switch`.

        Attributes:
            id (UUID):
            org_id (UUID):
            name (str):
            slug (str):
            status (str):
            kill_switch_active (bool):
            created_at (datetime.datetime):
            description (None | str | Unset):
            public_key (None | str | Unset):
            active_policy_mode (None | str | Unset): The enforcement mode of the agent's currently active policy.
                `"dry_run"` means policy decisions are logged but always allowed.
                `"enforce"` (or `null` when no active policy exists) means decisions
                are applied normally. Derived from the active policy's YAML `mode:` key.
    """

    id: UUID
    org_id: UUID
    name: str
    slug: str
    status: str
    kill_switch_active: bool
    created_at: datetime.datetime
    description: None | str | Unset = UNSET
    public_key: None | str | Unset = UNSET
    active_policy_mode: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        org_id = str(self.org_id)

        name = self.name

        slug = self.slug

        status = self.status

        kill_switch_active = self.kill_switch_active

        created_at = self.created_at.isoformat()

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        public_key: None | str | Unset
        if isinstance(self.public_key, Unset):
            public_key = UNSET
        else:
            public_key = self.public_key

        active_policy_mode: None | str | Unset
        if isinstance(self.active_policy_mode, Unset):
            active_policy_mode = UNSET
        else:
            active_policy_mode = self.active_policy_mode

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "org_id": org_id,
                "name": name,
                "slug": slug,
                "status": status,
                "kill_switch_active": kill_switch_active,
                "created_at": created_at,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if public_key is not UNSET:
            field_dict["public_key"] = public_key
        if active_policy_mode is not UNSET:
            field_dict["active_policy_mode"] = active_policy_mode

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        org_id = UUID(d.pop("org_id"))

        name = d.pop("name")

        slug = d.pop("slug")

        status = d.pop("status")

        kill_switch_active = d.pop("kill_switch_active")

        created_at = isoparse(d.pop("created_at"))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_public_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        public_key = _parse_public_key(d.pop("public_key", UNSET))

        def _parse_active_policy_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        active_policy_mode = _parse_active_policy_mode(d.pop("active_policy_mode", UNSET))

        agent = cls(
            id=id,
            org_id=org_id,
            name=name,
            slug=slug,
            status=status,
            kill_switch_active=kill_switch_active,
            created_at=created_at,
            description=description,
            public_key=public_key,
            active_policy_mode=active_policy_mode,
        )

        agent.additional_properties = d
        return agent

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
