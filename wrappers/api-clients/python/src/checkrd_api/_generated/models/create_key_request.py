from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.create_key_request_scope import CreateKeyRequestScope


T = TypeVar("T", bound="CreateKeyRequest")


@_attrs_define
class CreateKeyRequest:
    """`POST /v1/keys` request body.

    Attributes:
        name (str): Display name for the API key. Visible on the dashboard. Example: production-ingestion.
        scope (CreateKeyRequestScope): Scope of the key. Stripe-style: `"all"` for unrestricted (only
            minted by `checkrd login` device flow); `"read_only"` for the
            read-everything preset; `"restricted"` with a per-resource
            matrix for fine-grained access. Resources omitted from a
            `restricted` map default to no access.

            Wire shape:
            - `{"kind": "all"}`
            - `{"kind": "read_only"}`
            - `{"kind": "restricted", "resources": {"agents": "write", "policies": "read"}}`
        description (None | str | Unset): Optional free-form description.
    """

    name: str
    scope: CreateKeyRequestScope
    description: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        scope = self.scope.to_dict()

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "scope": scope,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_key_request_scope import CreateKeyRequestScope

        d = dict(src_dict)
        name = d.pop("name")

        scope = CreateKeyRequestScope.from_dict(d.pop("scope"))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        create_key_request = cls(
            name=name,
            scope=scope,
            description=description,
        )

        create_key_request.additional_properties = d
        return create_key_request

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
