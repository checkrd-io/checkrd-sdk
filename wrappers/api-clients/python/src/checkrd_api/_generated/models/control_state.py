from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.control_state_policy_envelope import ControlStatePolicyEnvelope
    from ..models.control_state_pricing_envelope import ControlStatePricingEnvelope


T = TypeVar("T", bound="ControlState")


@_attrs_define
class ControlState:
    """`GET /v1/agents/{agent_id}/control/state` response body — the
    JSON polling fallback for SDKs that cannot hold an SSE
    connection (e.g., serverless runtimes with hard request budgets).

        Attributes:
            kill_switch_active (bool): Whether the kill switch is currently engaged.
            active_policy_hash (None | str | Unset): SHA-256 of the active policy YAML, lowercase hex. Same value as
                `ControlInit.active_policy_hash` so the SDK's hash-based
                idempotency cache (OPA bundle / TUF "don't re-apply unchanged"
                pattern) works identically across the SSE and poll paths.
                `None` when the agent has no active policy.
            policy_envelope (ControlStatePolicyEnvelope | Unset): DSSE-signed policy envelope. `None` only when the agent
                has
                no active policy at all (a brand-new agent before its first
                policy push). After the first policy is created, this field
                is always present — strong-from-the-ground-up means there is
                no unsigned distribution path.
            active_pricing_hash (None | str | Unset): SHA-256 of the active pricing bundle, lowercase hex. Same value as
                `ControlInit.active_pricing_hash` so the SDK's hash-based idempotency
                cache works identically across the SSE and poll paths. `None` until a
                pricing bundle is active (M-14 wires the catalog storage).
            pricing_envelope (ControlStatePricingEnvelope | Unset): DSSE-signed pricing envelope. `None` until the agent's
                org has an
                active pricing catalog (M-14). The price-table analogue of
                `policy_envelope`: after the first pricing bundle exists this field is
                always present — there is no unsigned pricing distribution path.
    """

    kill_switch_active: bool
    active_policy_hash: None | str | Unset = UNSET
    policy_envelope: ControlStatePolicyEnvelope | Unset = UNSET
    active_pricing_hash: None | str | Unset = UNSET
    pricing_envelope: ControlStatePricingEnvelope | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kill_switch_active = self.kill_switch_active

        active_policy_hash: None | str | Unset
        if isinstance(self.active_policy_hash, Unset):
            active_policy_hash = UNSET
        else:
            active_policy_hash = self.active_policy_hash

        policy_envelope: dict[str, Any] | Unset = UNSET
        if not isinstance(self.policy_envelope, Unset):
            policy_envelope = self.policy_envelope.to_dict()

        active_pricing_hash: None | str | Unset
        if isinstance(self.active_pricing_hash, Unset):
            active_pricing_hash = UNSET
        else:
            active_pricing_hash = self.active_pricing_hash

        pricing_envelope: dict[str, Any] | Unset = UNSET
        if not isinstance(self.pricing_envelope, Unset):
            pricing_envelope = self.pricing_envelope.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kill_switch_active": kill_switch_active,
            }
        )
        if active_policy_hash is not UNSET:
            field_dict["active_policy_hash"] = active_policy_hash
        if policy_envelope is not UNSET:
            field_dict["policy_envelope"] = policy_envelope
        if active_pricing_hash is not UNSET:
            field_dict["active_pricing_hash"] = active_pricing_hash
        if pricing_envelope is not UNSET:
            field_dict["pricing_envelope"] = pricing_envelope

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.control_state_policy_envelope import ControlStatePolicyEnvelope
        from ..models.control_state_pricing_envelope import ControlStatePricingEnvelope

        d = dict(src_dict)
        kill_switch_active = d.pop("kill_switch_active")

        def _parse_active_policy_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        active_policy_hash = _parse_active_policy_hash(d.pop("active_policy_hash", UNSET))

        _policy_envelope = d.pop("policy_envelope", UNSET)
        policy_envelope: ControlStatePolicyEnvelope | Unset
        if isinstance(_policy_envelope, Unset):
            policy_envelope = UNSET
        else:
            policy_envelope = ControlStatePolicyEnvelope.from_dict(_policy_envelope)

        def _parse_active_pricing_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        active_pricing_hash = _parse_active_pricing_hash(d.pop("active_pricing_hash", UNSET))

        _pricing_envelope = d.pop("pricing_envelope", UNSET)
        pricing_envelope: ControlStatePricingEnvelope | Unset
        if isinstance(_pricing_envelope, Unset):
            pricing_envelope = UNSET
        else:
            pricing_envelope = ControlStatePricingEnvelope.from_dict(_pricing_envelope)

        control_state = cls(
            kill_switch_active=kill_switch_active,
            active_policy_hash=active_policy_hash,
            policy_envelope=policy_envelope,
            active_pricing_hash=active_pricing_hash,
            pricing_envelope=pricing_envelope,
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
