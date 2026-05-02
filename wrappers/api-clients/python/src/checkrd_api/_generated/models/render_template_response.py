from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="RenderTemplateResponse")


@_attrs_define
class RenderTemplateResponse:
    """`POST /v1/policy-templates/{id}/render` response body.

    Returns the rendered YAML alongside the template id for callers
    that batch-render. The YAML is validated server-side through the
    same pipeline as hand-authored policies — bad substitutions
    surface as 400 with a `validation_error`.

        Attributes:
            template_id (str): Echo of the path parameter — useful when batching renders.
            rendered_yaml (str): The rendered policy YAML, ready to send to
                `POST /v1/agents/{agent_id}/policies` or
                `POST /v1/org-policies`.
    """

    template_id: str
    rendered_yaml: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        template_id = self.template_id

        rendered_yaml = self.rendered_yaml

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "template_id": template_id,
                "rendered_yaml": rendered_yaml,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        template_id = d.pop("template_id")

        rendered_yaml = d.pop("rendered_yaml")

        render_template_response = cls(
            template_id=template_id,
            rendered_yaml=rendered_yaml,
        )

        render_template_response.additional_properties = d
        return render_template_response

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
