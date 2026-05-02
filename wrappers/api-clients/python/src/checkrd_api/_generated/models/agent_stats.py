from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AgentStats")


@_attrs_define
class AgentStats:
    """`GET /v1/dashboard/agents` response item.

    Per-agent rollup for the agents list view. Returned as
    `Vec<AgentStats>` ordered by `agent_name` ascending.

        Attributes:
            agent_id (UUID):
            agent_name (str):
            total_calls (int): Total telemetry events emitted by this agent in the window.
            denied_calls (int): Subset where the policy result was `denied`.
            error_calls (int): Subset with HTTP status >= 500.
            avg_latency_ms (float):
            p50_latency_ms (float):
            p95_latency_ms (float):
    """

    agent_id: UUID
    agent_name: str
    total_calls: int
    denied_calls: int
    error_calls: int
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = str(self.agent_id)

        agent_name = self.agent_name

        total_calls = self.total_calls

        denied_calls = self.denied_calls

        error_calls = self.error_calls

        avg_latency_ms = self.avg_latency_ms

        p50_latency_ms = self.p50_latency_ms

        p95_latency_ms = self.p95_latency_ms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "total_calls": total_calls,
                "denied_calls": denied_calls,
                "error_calls": error_calls,
                "avg_latency_ms": avg_latency_ms,
                "p50_latency_ms": p50_latency_ms,
                "p95_latency_ms": p95_latency_ms,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = UUID(d.pop("agent_id"))

        agent_name = d.pop("agent_name")

        total_calls = d.pop("total_calls")

        denied_calls = d.pop("denied_calls")

        error_calls = d.pop("error_calls")

        avg_latency_ms = d.pop("avg_latency_ms")

        p50_latency_ms = d.pop("p50_latency_ms")

        p95_latency_ms = d.pop("p95_latency_ms")

        agent_stats = cls(
            agent_id=agent_id,
            agent_name=agent_name,
            total_calls=total_calls,
            denied_calls=denied_calls,
            error_calls=error_calls,
            avg_latency_ms=avg_latency_ms,
            p50_latency_ms=p50_latency_ms,
            p95_latency_ms=p95_latency_ms,
        )

        agent_stats.additional_properties = d
        return agent_stats

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
