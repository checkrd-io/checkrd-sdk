from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.policy_template_param_schema import PolicyTemplateParamSchema
    from ..models.template_param import TemplateParam


T = TypeVar("T", bound="PolicyTemplate")


@_attrs_define
class PolicyTemplate:
    """`GET /v1/policy-templates` response row.

    Built-in templates only — no template body. Pair with a `render`
    call (`POST /v1/policy-templates/{id}/render`) to materialize a
    concrete YAML policy.

        Attributes:
            id (str): Stable template id (e.g. `"api-allowlist"`). Used as the path
                parameter on the render endpoint.
            name (str): Display name. Surface on the dashboard's template picker.
            description (str): One-line summary of what the template does.
            category (str): Coarse category (`"security"`, `"compliance"`, `"performance"`).
                Used for grouping in the picker UI.
            parameters (list[TemplateParam]): Ordered parameter list — same order the server renders them in.
            param_schema (PolicyTemplateParamSchema): JSON Schema describing the parameters accepted by this template.
                The dashboard auto-generates the parameter form from this schema.
    """

    id: str
    name: str
    description: str
    category: str
    parameters: list[TemplateParam]
    param_schema: PolicyTemplateParamSchema
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        description = self.description

        category = self.category

        parameters = []
        for parameters_item_data in self.parameters:
            parameters_item = parameters_item_data.to_dict()
            parameters.append(parameters_item)

        param_schema = self.param_schema.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "description": description,
                "category": category,
                "parameters": parameters,
                "param_schema": param_schema,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.policy_template_param_schema import PolicyTemplateParamSchema
        from ..models.template_param import TemplateParam

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        description = d.pop("description")

        category = d.pop("category")

        parameters = []
        _parameters = d.pop("parameters")
        for parameters_item_data in _parameters:
            parameters_item = TemplateParam.from_dict(parameters_item_data)

            parameters.append(parameters_item)

        param_schema = PolicyTemplateParamSchema.from_dict(d.pop("param_schema"))

        policy_template = cls(
            id=id,
            name=name,
            description=description,
            category=category,
            parameters=parameters,
            param_schema=param_schema,
        )

        policy_template.additional_properties = d
        return policy_template

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
