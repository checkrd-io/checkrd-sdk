from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ErrorBody")


@_attrs_define
class ErrorBody:
    """The body inside the Stripe-style `{ "error": { ... } }` envelope.

    Defined separately from `ApiError` (which is the runtime sum type)
    because OpenAPI describes the *wire shape* clients see, not the
    internal Rust enum. Serializing `ApiError` always yields exactly
    this shape — see the `into_response` impl below.

        Attributes:
            type_ (str): Coarse-grained category. One of `authentication_error`,
                `permission_error`, `invalid_request_error`, `rate_limit_error`,
                `payment_required`, `not_found`, `idempotency_error`,
                `internal_error`, `bad_gateway`. Example: invalid_request_error.
            code (str): Machine-readable, fine-grained error code. See
                [`ErrorCode`] for the full enumeration. Example: missing_required_field.
            message (str): Human-readable message safe to surface to end users. Example: Missing required field.
            param (None | str | Unset): Optional pointer to the offending request parameter (for
                `invalid_request_error`). Example: name.
    """

    type_: str
    code: str
    message: str
    param: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        type_ = self.type_

        code = self.code

        message = self.message

        param: None | str | Unset
        if isinstance(self.param, Unset):
            param = UNSET
        else:
            param = self.param

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "type": type_,
                "code": code,
                "message": message,
            }
        )
        if param is not UNSET:
            field_dict["param"] = param

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        type_ = d.pop("type")

        code = d.pop("code")

        message = d.pop("message")

        def _parse_param(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        param = _parse_param(d.pop("param", UNSET))

        error_body = cls(
            type_=type_,
            code=code,
            message=message,
            param=param,
        )

        error_body.additional_properties = d
        return error_body

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
