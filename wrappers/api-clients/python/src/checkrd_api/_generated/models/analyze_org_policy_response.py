from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.analyze_org_policy_response_findings_item import AnalyzeOrgPolicyResponseFindingsItem
    from ..models.analyze_org_policy_response_summary import AnalyzeOrgPolicyResponseSummary
    from ..models.analyze_org_policy_response_warnings_item import AnalyzeOrgPolicyResponseWarningsItem


T = TypeVar("T", bound="AnalyzeOrgPolicyResponse")


@_attrs_define
class AnalyzeOrgPolicyResponse:
    """`POST /v1/org-policies/analyze` response body.

    Carries enriched findings (with line numbers + auto-fix
    suggestions where available) plus the legacy `warnings` /
    `summary` fields kept for forward-compat with older callers.

        Attributes:
            findings (list[AnalyzeOrgPolicyResponseFindingsItem]): Enriched findings — same shape as the per-agent analyze
                endpoint.
                Each finding carries severity, code, message, optional rule
                name, optional 1-based line number, and optional auto-fix.
            warnings (list[AnalyzeOrgPolicyResponseWarningsItem]): Legacy warnings array from
                `checkrd_shared::PolicyAnalysis`.
            summary (AnalyzeOrgPolicyResponseSummary): Legacy summary block from `checkrd_shared::PolicyAnalysis`.
    """

    findings: list[AnalyzeOrgPolicyResponseFindingsItem]
    warnings: list[AnalyzeOrgPolicyResponseWarningsItem]
    summary: AnalyzeOrgPolicyResponseSummary
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        findings = []
        for findings_item_data in self.findings:
            findings_item = findings_item_data.to_dict()
            findings.append(findings_item)

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)

        summary = self.summary.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "findings": findings,
                "warnings": warnings,
                "summary": summary,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.analyze_org_policy_response_findings_item import AnalyzeOrgPolicyResponseFindingsItem
        from ..models.analyze_org_policy_response_summary import AnalyzeOrgPolicyResponseSummary
        from ..models.analyze_org_policy_response_warnings_item import AnalyzeOrgPolicyResponseWarningsItem

        d = dict(src_dict)
        findings = []
        _findings = d.pop("findings")
        for findings_item_data in _findings:
            findings_item = AnalyzeOrgPolicyResponseFindingsItem.from_dict(findings_item_data)

            findings.append(findings_item)

        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in _warnings:
            warnings_item = AnalyzeOrgPolicyResponseWarningsItem.from_dict(warnings_item_data)

            warnings.append(warnings_item)

        summary = AnalyzeOrgPolicyResponseSummary.from_dict(d.pop("summary"))

        analyze_org_policy_response = cls(
            findings=findings,
            warnings=warnings,
            summary=summary,
        )

        analyze_org_policy_response.additional_properties = d
        return analyze_org_policy_response

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
