from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OrgStats")


@_attrs_define
class OrgStats:
    """`GET /v1/dashboard/stats` response body.

    Org-wide aggregate counters and latency percentiles for the
    requested time window. Powers the dashboard overview cards.

        Attributes:
            total_calls (int): Total telemetry events ingested in the window.
            allowed_calls (int): Subset of events where the policy result was `allowed`.
            denied_calls (int): Subset of events where the policy result was `denied`.
            error_calls (int): Subset of events with HTTP status >= 500 (post-policy errors).
            avg_latency_ms (float): Mean latency across all events in the window, in milliseconds.
            p50_latency_ms (int): 50th-percentile latency in milliseconds.
            p95_latency_ms (int): 95th-percentile latency in milliseconds.
            p99_latency_ms (int): 99th-percentile latency in milliseconds.
            active_agents (int): Distinct agents that emitted at least one event in the window.
            total_agents (int): Total agents in the workspace (active + idle, excluding soft-deleted).
                Sourced from Aurora, not ClickHouse.
    """

    total_calls: int
    allowed_calls: int
    denied_calls: int
    error_calls: int
    avg_latency_ms: float
    p50_latency_ms: int
    p95_latency_ms: int
    p99_latency_ms: int
    active_agents: int
    total_agents: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total_calls = self.total_calls

        allowed_calls = self.allowed_calls

        denied_calls = self.denied_calls

        error_calls = self.error_calls

        avg_latency_ms = self.avg_latency_ms

        p50_latency_ms = self.p50_latency_ms

        p95_latency_ms = self.p95_latency_ms

        p99_latency_ms = self.p99_latency_ms

        active_agents = self.active_agents

        total_agents = self.total_agents

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "total_calls": total_calls,
                "allowed_calls": allowed_calls,
                "denied_calls": denied_calls,
                "error_calls": error_calls,
                "avg_latency_ms": avg_latency_ms,
                "p50_latency_ms": p50_latency_ms,
                "p95_latency_ms": p95_latency_ms,
                "p99_latency_ms": p99_latency_ms,
                "active_agents": active_agents,
                "total_agents": total_agents,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        total_calls = d.pop("total_calls")

        allowed_calls = d.pop("allowed_calls")

        denied_calls = d.pop("denied_calls")

        error_calls = d.pop("error_calls")

        avg_latency_ms = d.pop("avg_latency_ms")

        p50_latency_ms = d.pop("p50_latency_ms")

        p95_latency_ms = d.pop("p95_latency_ms")

        p99_latency_ms = d.pop("p99_latency_ms")

        active_agents = d.pop("active_agents")

        total_agents = d.pop("total_agents")

        org_stats = cls(
            total_calls=total_calls,
            allowed_calls=allowed_calls,
            denied_calls=denied_calls,
            error_calls=error_calls,
            avg_latency_ms=avg_latency_ms,
            p50_latency_ms=p50_latency_ms,
            p95_latency_ms=p95_latency_ms,
            p99_latency_ms=p99_latency_ms,
            active_agents=active_agents,
            total_agents=total_agents,
        )

        org_stats.additional_properties = d
        return org_stats

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
