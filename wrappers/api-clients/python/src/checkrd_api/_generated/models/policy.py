from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="Policy")


@_attrs_define
class Policy:
    """A single per-agent policy version.

    Returned from `POST /v1/agents/{agent_id}/policies` (create),
    `GET /v1/agents/{agent_id}/policies` (paginated),
    `GET /v1/agents/{agent_id}/policies/active`,
    `GET /v1/agents/{agent_id}/policies/{version}`,
    `PATCH /v1/agents/{agent_id}/policies/{version}` (draft updates),
    `POST /v1/agents/{agent_id}/policies/{version}/activate`.

        Attributes:
            id (UUID):
            agent_id (UUID):
            version (int): Monotonic per-agent version number, assigned at create time.
            yaml_content (str): Raw policy YAML. Validated by the server before persisting.
            hash_ (str): SHA-256 of `yaml_content`, hex-encoded. Stable identifier for
                equality checks and trust-list pinning.
            is_active (bool): Exactly one row per agent has `is_active = true`. The activate
                endpoint maintains the invariant in a single transaction.
            created_at (datetime.datetime):
            description (None | str | Unset): Optional free-form description supplied at create time.
            created_by (None | Unset | UUID): `users.id` of the actor who created this version. `None` for
                system-created policies (rare; reserved for future automation).
    """

    id: UUID
    agent_id: UUID
    version: int
    yaml_content: str
    hash_: str
    is_active: bool
    created_at: datetime.datetime
    description: None | str | Unset = UNSET
    created_by: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        agent_id = str(self.agent_id)

        version = self.version

        yaml_content = self.yaml_content

        hash_ = self.hash_

        is_active = self.is_active

        created_at = self.created_at.isoformat()

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        created_by: None | str | Unset
        if isinstance(self.created_by, Unset):
            created_by = UNSET
        elif isinstance(self.created_by, UUID):
            created_by = str(self.created_by)
        else:
            created_by = self.created_by

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "agent_id": agent_id,
                "version": version,
                "yaml_content": yaml_content,
                "hash": hash_,
                "is_active": is_active,
                "created_at": created_at,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if created_by is not UNSET:
            field_dict["created_by"] = created_by

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        agent_id = UUID(d.pop("agent_id"))

        version = d.pop("version")

        yaml_content = d.pop("yaml_content")

        hash_ = d.pop("hash")

        is_active = d.pop("is_active")

        created_at = isoparse(d.pop("created_at"))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_created_by(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                created_by_type_0 = UUID(data)

                return created_by_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        created_by = _parse_created_by(d.pop("created_by", UNSET))

        policy = cls(
            id=id,
            agent_id=agent_id,
            version=version,
            yaml_content=yaml_content,
            hash_=hash_,
            is_active=is_active,
            created_at=created_at,
            description=description,
            created_by=created_by,
        )

        policy.additional_properties = d
        return policy

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
