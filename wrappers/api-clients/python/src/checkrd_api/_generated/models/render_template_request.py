from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.render_template_request_parameters import RenderTemplateRequestParameters


T = TypeVar("T", bound="RenderTemplateRequest")


@_attrs_define
class RenderTemplateRequest:
    """`POST /v1/policy-templates/{id}/render` request body.

    `parameters` is a free-form JSON object; keys must match the
    `TemplateParam.name` entries returned by the listing endpoint.
    Types are validated server-side against `TemplateParam.param_type`.

        Attributes:
            parameters (RenderTemplateRequestParameters): Parameter values keyed by parameter name. Required parameters
                without defaults must appear here or the call returns 400.
    """

    parameters: RenderTemplateRequestParameters
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        parameters = self.parameters.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "parameters": parameters,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.render_template_request_parameters import RenderTemplateRequestParameters

        d = dict(src_dict)
        parameters = RenderTemplateRequestParameters.from_dict(d.pop("parameters"))

        render_template_request = cls(
            parameters=parameters,
        )

        render_template_request.additional_properties = d
        return render_template_request

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
