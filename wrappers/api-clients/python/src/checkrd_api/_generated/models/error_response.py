from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.error_body import ErrorBody


T = TypeVar("T", bound="ErrorResponse")


@_attrs_define
class ErrorResponse:
    """Stripe-style error envelope returned for any non-2xx response.

    Wire shape: `{ "error": { "type", "code", "message", "param?" } }`.
    Every error response in the API conforms to this — clients can
    type-narrow against it once and reuse across every endpoint.

        Attributes:
            error (ErrorBody): The body inside the Stripe-style `{ "error": { ... } }` envelope.

                Defined separately from `ApiError` (which is the runtime sum type)
                because OpenAPI describes the *wire shape* clients see, not the
                internal Rust enum. Serializing `ApiError` always yields exactly
                this shape — see the `into_response` impl below.
    """

    error: ErrorBody
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        error = self.error.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error": error,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.error_body import ErrorBody

        d = dict(src_dict)
        error = ErrorBody.from_dict(d.pop("error"))

        error_response = cls(
            error=error,
        )

        error_response.additional_properties = d
        return error_response

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
