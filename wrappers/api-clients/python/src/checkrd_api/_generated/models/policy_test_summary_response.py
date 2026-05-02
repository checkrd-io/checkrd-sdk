from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.policy_test_summary_response_results_item import PolicyTestSummaryResponseResultsItem


T = TypeVar("T", bound="PolicyTestSummaryResponse")


@_attrs_define
class PolicyTestSummaryResponse:
    """`POST /v1/agents/{agent_id}/policies/test` response body.

    Free-form JSON because the upstream `checkrd_shared::PolicyTestSummary`
    lives in the shared crate, which has no utoipa dependency.
    Documented inline in the OpenAPI spec; the server emits the
    canonical `PolicyTestSummary` shape verbatim. Fields below mirror
    that shape so the schema remains discoverable.

        Attributes:
            total (int): Total number of test cases evaluated.
            passed (int): Number that passed.
            failed (int): Number that failed.
            results (list[PolicyTestSummaryResponseResultsItem]): Per-test-case result rows. Each row carries `name`,
                `passed`,
                `expected_allowed`, `actual_allowed`, and (when set)
                `expected_rule`, `actual_rule`, and `error`.
    """

    total: int
    passed: int
    failed: int
    results: list[PolicyTestSummaryResponseResultsItem]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        passed = self.passed

        failed = self.failed

        results = []
        for results_item_data in self.results:
            results_item = results_item_data.to_dict()
            results.append(results_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "total": total,
                "passed": passed,
                "failed": failed,
                "results": results,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.policy_test_summary_response_results_item import PolicyTestSummaryResponseResultsItem

        d = dict(src_dict)
        total = d.pop("total")

        passed = d.pop("passed")

        failed = d.pop("failed")

        results = []
        _results = d.pop("results")
        for results_item_data in _results:
            results_item = PolicyTestSummaryResponseResultsItem.from_dict(results_item_data)

            results.append(results_item)

        policy_test_summary_response = cls(
            total=total,
            passed=passed,
            failed=failed,
            results=results,
        )

        policy_test_summary_response.additional_properties = d
        return policy_test_summary_response

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
