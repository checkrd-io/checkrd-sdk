from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.control_init_policy_envelope import ControlInitPolicyEnvelope


T = TypeVar("T", bound="ControlInit")


@_attrs_define
class ControlInit:
    """Initial event payload pushed on SSE stream open. Sent before any
    pubsub event so clients never need a separate poll to learn the
    current state of the agent.

    **`policy_envelope` is required for self-bootstrap.** Without it,
    a fresh SDK connection learns only the kill-switch state and waits
    for a `policy_updated` event that only fires on policy *change* —
    so an SDK that starts up against an agent with an existing active
    policy never enforces it. Mirroring the polling endpoint's payload
    here closes that gap: every `init` event carries the same DSSE-
    signed envelope the polling fallback returns, the SDK installs it
    the moment SSE handshakes, and the rest of the stream is just
    deltas.

        Attributes:
            kill_switch_active (bool): Whether the kill switch is currently engaged. `true` ⇒ deny all.
            active_policy_hash (None | str | Unset): SHA-256 of the active policy YAML, lowercase hex. `None` only
                when no active policy exists yet (brand-new agent before its
                first policy push). Kept alongside `policy_envelope` for clients
                that want to compare against a cached engine fingerprint and
                skip the install when nothing changed.
            policy_envelope (ControlInitPolicyEnvelope | Unset): DSSE-signed policy envelope, identical to the one returned
                by
                `GET /v1/agents/{agent_id}/control/state`. `None` when no
                active policy exists yet. Always populated after the first
                publish — strong-from-the-ground-up means there is no unsigned
                distribution path.
    """

    kill_switch_active: bool
    active_policy_hash: None | str | Unset = UNSET
    policy_envelope: ControlInitPolicyEnvelope | Unset = UNSET
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

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.control_init_policy_envelope import ControlInitPolicyEnvelope

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
        policy_envelope: ControlInitPolicyEnvelope | Unset
        if isinstance(_policy_envelope, Unset):
            policy_envelope = UNSET
        else:
            policy_envelope = ControlInitPolicyEnvelope.from_dict(_policy_envelope)

        control_init = cls(
            kill_switch_active=kill_switch_active,
            active_policy_hash=active_policy_hash,
            policy_envelope=policy_envelope,
        )

        control_init.additional_properties = d
        return control_init

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
