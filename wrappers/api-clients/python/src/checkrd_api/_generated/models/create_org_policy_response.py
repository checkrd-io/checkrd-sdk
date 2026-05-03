from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.create_org_policy_response_analysis import CreateOrgPolicyResponseAnalysis
    from ..models.org_policy import OrgPolicy


T = TypeVar("T", bound="CreateOrgPolicyResponse")


@_attrs_define
class CreateOrgPolicyResponse:
    """`POST /v1/org-policies` response body.

    The created policy version plus the static-analysis report. The
    analysis is the same shape returned by `POST /v1/org-policies/analyze`.

        Attributes:
            policy (OrgPolicy): A single org-policy version.

                Returned from `GET /v1/org-policies` (paginated),
                `GET /v1/org-policies/active`,
                `POST /v1/org-policies/{version}/activate`.
            analysis (CreateOrgPolicyResponseAnalysis): Raw `PolicyAnalysis` from the WASM core. Free-form because
                the upstream type lives in `crates/shared` (no utoipa deps).
    """

    policy: OrgPolicy
    analysis: CreateOrgPolicyResponseAnalysis
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        policy = self.policy.to_dict()

        analysis = self.analysis.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "policy": policy,
                "analysis": analysis,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_org_policy_response_analysis import CreateOrgPolicyResponseAnalysis
        from ..models.org_policy import OrgPolicy

        d = dict(src_dict)
        policy = OrgPolicy.from_dict(d.pop("policy"))

        analysis = CreateOrgPolicyResponseAnalysis.from_dict(d.pop("analysis"))

        create_org_policy_response = cls(
            policy=policy,
            analysis=analysis,
        )

        create_org_policy_response.additional_properties = d
        return create_org_policy_response

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
