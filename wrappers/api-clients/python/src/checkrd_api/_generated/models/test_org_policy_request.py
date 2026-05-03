from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.test_org_policy_request_tests_type_0_item import TestOrgPolicyRequestTestsType0Item


T = TypeVar("T", bound="TestOrgPolicyRequest")


@_attrs_define
class TestOrgPolicyRequest:
    """`POST /v1/org-policies/test` request body.

    Either supply explicit `tests` in the request body, or omit and
    let the server extract a top-level `tests:` block from `yaml_content`.

        Attributes:
            yaml_content (str): Candidate YAML to test. Not persisted.
            tests (list[TestOrgPolicyRequestTestsType0Item] | None | Unset): Optional explicit test cases. When omitted, the
                server extracts
                the `tests:` block embedded in `yaml_content`.

                Free-form JSON because `PolicyTestCase.input.headers` is a
                `Vec<(String, String)>` which neither typeshare nor utoipa
                expresses cleanly. Schema is enforced server-side.
    """

    yaml_content: str
    tests: list[TestOrgPolicyRequestTestsType0Item] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        yaml_content = self.yaml_content

        tests: list[dict[str, Any]] | None | Unset
        if isinstance(self.tests, Unset):
            tests = UNSET
        elif isinstance(self.tests, list):
            tests = []
            for tests_type_0_item_data in self.tests:
                tests_type_0_item = tests_type_0_item_data.to_dict()
                tests.append(tests_type_0_item)

        else:
            tests = self.tests

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "yaml_content": yaml_content,
            }
        )
        if tests is not UNSET:
            field_dict["tests"] = tests

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_org_policy_request_tests_type_0_item import TestOrgPolicyRequestTestsType0Item

        d = dict(src_dict)
        yaml_content = d.pop("yaml_content")

        def _parse_tests(data: object) -> list[TestOrgPolicyRequestTestsType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                tests_type_0 = []
                _tests_type_0 = data
                for tests_type_0_item_data in _tests_type_0:
                    tests_type_0_item = TestOrgPolicyRequestTestsType0Item.from_dict(tests_type_0_item_data)

                    tests_type_0.append(tests_type_0_item)

                return tests_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[TestOrgPolicyRequestTestsType0Item] | None | Unset, data)

        tests = _parse_tests(d.pop("tests", UNSET))

        test_org_policy_request = cls(
            yaml_content=yaml_content,
            tests=tests,
        )

        test_org_policy_request.additional_properties = d
        return test_org_policy_request

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
