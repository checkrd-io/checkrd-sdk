from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.control_policy_updated_event_policy_envelope import ControlPolicyUpdatedEventPolicyEnvelope


T = TypeVar("T", bound="ControlPolicyUpdatedEvent")


@_attrs_define
class ControlPolicyUpdatedEvent:
    """`policy_updated` SSE event payload — emitted whenever a new
    policy version is activated for the agent.

    `policy_envelope` is a DSSE envelope (`PolicyBundle` payload type)
    that the SDK verifies in-WASM against its pinned trust list before
    installing the policy. Strong-from-the-ground-up: there is no
    unsigned distribution path.

        Attributes:
            version (int): Monotonic policy version. Used by the SDK to enforce rollback
                protection: a bundle with `version <= last_policy_version` is
                rejected.
            hash_ (str): SHA-256 of the policy YAML, lowercase hex.
            policy_envelope (ControlPolicyUpdatedEventPolicyEnvelope): DSSE envelope wrapping the canonical `PolicyBundle`
                JSON.
                Verified in-WASM by the SDK.
    """

    version: int
    hash_: str
    policy_envelope: ControlPolicyUpdatedEventPolicyEnvelope
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        version = self.version

        hash_ = self.hash_

        policy_envelope = self.policy_envelope.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "version": version,
                "hash": hash_,
                "policy_envelope": policy_envelope,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.control_policy_updated_event_policy_envelope import ControlPolicyUpdatedEventPolicyEnvelope

        d = dict(src_dict)
        version = d.pop("version")

        hash_ = d.pop("hash")

        policy_envelope = ControlPolicyUpdatedEventPolicyEnvelope.from_dict(d.pop("policy_envelope"))

        control_policy_updated_event = cls(
            version=version,
            hash_=hash_,
            policy_envelope=policy_envelope,
        )

        control_policy_updated_event.additional_properties = d
        return control_policy_updated_event

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
