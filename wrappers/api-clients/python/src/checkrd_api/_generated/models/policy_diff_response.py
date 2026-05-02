from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.policy_diff_response_default_action import PolicyDiffResponseDefaultAction
    from ..models.policy_diff_response_mode import PolicyDiffResponseMode
    from ..models.policy_diff_response_rules_item import PolicyDiffResponseRulesItem
    from ..models.policy_diff_response_summary import PolicyDiffResponseSummary


T = TypeVar("T", bound="PolicyDiffResponse")


@_attrs_define
class PolicyDiffResponse:
    """`POST /v1/agents/{agent_id}/policies/diff` response body.

    Free-form JSON because the upstream `checkrd_shared::PolicyDiff`
    lives in the shared crate, which has no utoipa dependency.
    Documented inline in the OpenAPI spec; the server emits the
    canonical `PolicyDiff` shape verbatim. Fields below mirror that
    shape so the schema remains discoverable.

        Attributes:
            summary (PolicyDiffResponseSummary): Aggregate counts (`added`, `modified`, `removed`, `unchanged`).
            default_action (PolicyDiffResponseDefaultAction): Diff action for the `default:` field (`add`, `modify`,
                `remove`,
                `unchanged`).
            mode (PolicyDiffResponseMode): Diff action for the `mode:` field.
            rules (list[PolicyDiffResponseRulesItem]): One entry per rule with the diff verdict.
    """

    summary: PolicyDiffResponseSummary
    default_action: PolicyDiffResponseDefaultAction
    mode: PolicyDiffResponseMode
    rules: list[PolicyDiffResponseRulesItem]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        summary = self.summary.to_dict()

        default_action = self.default_action.to_dict()

        mode = self.mode.to_dict()

        rules = []
        for rules_item_data in self.rules:
            rules_item = rules_item_data.to_dict()
            rules.append(rules_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "summary": summary,
                "default_action": default_action,
                "mode": mode,
                "rules": rules,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.policy_diff_response_default_action import PolicyDiffResponseDefaultAction
        from ..models.policy_diff_response_mode import PolicyDiffResponseMode
        from ..models.policy_diff_response_rules_item import PolicyDiffResponseRulesItem
        from ..models.policy_diff_response_summary import PolicyDiffResponseSummary

        d = dict(src_dict)
        summary = PolicyDiffResponseSummary.from_dict(d.pop("summary"))

        default_action = PolicyDiffResponseDefaultAction.from_dict(d.pop("default_action"))

        mode = PolicyDiffResponseMode.from_dict(d.pop("mode"))

        rules = []
        _rules = d.pop("rules")
        for rules_item_data in _rules:
            rules_item = PolicyDiffResponseRulesItem.from_dict(rules_item_data)

            rules.append(rules_item)

        policy_diff_response = cls(
            summary=summary,
            default_action=default_action,
            mode=mode,
            rules=rules,
        )

        policy_diff_response.additional_properties = d
        return policy_diff_response

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
