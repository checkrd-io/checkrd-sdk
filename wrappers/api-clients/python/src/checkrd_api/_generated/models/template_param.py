from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TemplateParam")


@_attrs_define
class TemplateParam:
    """Definition of a single template parameter.

    Mirrors the server-side `crate::templates::TemplateParam` shape.
    The server's struct uses `&'static str` for in-binary catalogs; on
    the wire the values are owned `String`s.

        Attributes:
            name (str): Parameter name. Used as the substitution key (`{{name}}`) inside
                the template's YAML body.
            description (str): Human-readable description shown in the dashboard's parameter form.
            param_type (str): One of `"string"`, `"integer"`, `"boolean"`, `"string_array"`.
            required (bool): Whether the parameter must be provided at render time.
            default_value (None | str | Unset): JSON-encoded default value. Honored when the caller omits the
                parameter from the render request. `None` means there is no
                default — required parameters must be supplied.
    """

    name: str
    description: str
    param_type: str
    required: bool
    default_value: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        description = self.description

        param_type = self.param_type

        required = self.required

        default_value: None | str | Unset
        if isinstance(self.default_value, Unset):
            default_value = UNSET
        else:
            default_value = self.default_value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "description": description,
                "param_type": param_type,
                "required": required,
            }
        )
        if default_value is not UNSET:
            field_dict["default_value"] = default_value

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        description = d.pop("description")

        param_type = d.pop("param_type")

        required = d.pop("required")

        def _parse_default_value(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_value = _parse_default_value(d.pop("default_value", UNSET))

        template_param = cls(
            name=name,
            description=description,
            param_type=param_type,
            required=required,
            default_value=default_value,
        )

        template_param.additional_properties = d
        return template_param

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
