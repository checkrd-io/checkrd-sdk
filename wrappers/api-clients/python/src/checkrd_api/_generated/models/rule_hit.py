from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="RuleHit")


@_attrs_define
class RuleHit:
    """`GET /v1/dashboard/rule-hits` response item.

    Aggregated hit count for a single policy rule across the queried
    window, plus a sparkline-ready hourly histogram. Powers the
    per-rule sparklines in the policy editor (Stripe Radar pattern).

        Attributes:
            rule_name (str):
            total (int):
            hourly_counts (list[int]): Histogram over the time window, oldest-to-newest. Always at
                hourly granularity (downsampled to fit when the window is
                wider).
    """

    rule_name: str
    total: int
    hourly_counts: list[int]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        rule_name = self.rule_name

        total = self.total

        hourly_counts = self.hourly_counts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "rule_name": rule_name,
                "total": total,
                "hourly_counts": hourly_counts,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        rule_name = d.pop("rule_name")

        total = d.pop("total")

        hourly_counts = cast(list[int], d.pop("hourly_counts"))

        rule_hit = cls(
            rule_name=rule_name,
            total=total,
            hourly_counts=hourly_counts,
        )

        rule_hit.additional_properties = d
        return rule_hit

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
