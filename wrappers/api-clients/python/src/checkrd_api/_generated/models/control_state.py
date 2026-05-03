from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.control_state_policy_envelope import ControlStatePolicyEnvelope


T = TypeVar("T", bound="ControlState")


@_attrs_define
class ControlState:
    """`GET /v1/agents/{agent_id}/control/state` response body — the
    JSON polling fallback for SDKs that cannot hold an SSE
    connection (e.g., serverless runtimes with hard request budgets).

        Attributes:
            kill_switch_active (bool): Whether the kill switch is currently engaged.
            policy_envelope (ControlStatePolicyEnvelope | Unset): DSSE-signed policy envelope. `None` only when the agent
                has
                no active policy at all (a brand-new agent before its first
                policy push). After the first policy is created, this field
                is always present — strong-from-the-ground-up means there is
                no unsigned distribution path.
    """

    kill_switch_active: bool
    policy_envelope: ControlStatePolicyEnvelope | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kill_switch_active = self.kill_switch_active

        policy_envelope: dict[str, Any] | Unset = UNSET
        if not isinstance(self.policy_envelope, Unset):
            policy_envelope = self.policy_envelope.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kill_switch_active": kill_switch_active,
            }
        )
        if policy_envelope is not UNSET:
            field_dict["policy_envelope"] = policy_envelope

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.control_state_policy_envelope import ControlStatePolicyEnvelope

        d = dict(src_dict)
        kill_switch_active = d.pop("kill_switch_active")

        _policy_envelope = d.pop("policy_envelope", UNSET)
        policy_envelope: ControlStatePolicyEnvelope | Unset
        if isinstance(_policy_envelope, Unset):
            policy_envelope = UNSET
        else:
            policy_envelope = ControlStatePolicyEnvelope.from_dict(_policy_envelope)

        control_state = cls(
            kill_switch_active=kill_switch_active,
            policy_envelope=policy_envelope,
        )

        control_state.additional_properties = d
        return control_state

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
