from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="FieldError")


@_attrs_define
class FieldError:
    """One field-level validation failure — an extension member of
    [`ProblemDetails`]'s `errors` array.

    `pointer` is a JSON Pointer (RFC 6901) into the request body, e.g.
    `/email` or `/rules/0/action`. Returning an array of these lets a single
    response surface *every* invalid field at once.

        Attributes:
            pointer (str): JSON Pointer (RFC 6901) locating the offending member of the request body. Example: /email.
            detail (str): Human-readable reason this field was rejected. Example: must be a valid email address.
    """

    pointer: str
    detail: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        pointer = self.pointer

        detail = self.detail

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "pointer": pointer,
                "detail": detail,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        pointer = d.pop("pointer")

        detail = d.pop("detail")

        field_error = cls(
            pointer=pointer,
            detail=detail,
        )

        field_error.additional_properties = d
        return field_error

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
