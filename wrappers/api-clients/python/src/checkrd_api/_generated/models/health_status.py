from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="HealthStatus")


@_attrs_define
class HealthStatus:
    """`GET /health` response body.

    `status` is always `"ok"` while the process is alive — the ALB only
    uses HTTP status, not body content, for liveness. The other fields
    expose the live state of dependencies (Redis pubsub, control hub
    subscription) so operators can spot a degraded-but-up node.

        Attributes:
            status (str): Always `"ok"`. Present so older monitoring harnesses that
                pattern-match on the body keep working. Example: ok.
            redis (str): Redis publisher state: `"connected"`, `"disconnected"`, or
                `"not_configured"` (local dev / pre-Redis deployments). Example: connected.
            subscription_active (bool): `true` when the control-plane Redis pubsub subscription is
                active. When `false`, SSE clients fall back to the polling
                path — kill-switch propagation may be delayed by one poll
                interval.
    """

    status: str
    redis: str
    subscription_active: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        redis = self.redis

        subscription_active = self.subscription_active

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "status": status,
                "redis": redis,
                "subscription_active": subscription_active,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status")

        redis = d.pop("redis")

        subscription_active = d.pop("subscription_active")

        health_status = cls(
            status=status,
            redis=redis,
            subscription_active=subscription_active,
        )

        health_status.additional_properties = d
        return health_status

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
