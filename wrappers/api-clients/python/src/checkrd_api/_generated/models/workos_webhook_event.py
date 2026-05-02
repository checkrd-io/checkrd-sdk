from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.workos_webhook_event_data import WorkosWebhookEventData


T = TypeVar("T", bound="WorkosWebhookEvent")


@_attrs_define
class WorkosWebhookEvent:
    """`POST /webhooks/workos` request body.

    Minimal envelope of an inbound WorkOS event. The handler reads
    only the `id` (for idempotency dedup), the `event` discriminator
    (for dispatch), and the opaque `data` blob (decoded per
    event-type). Everything else (organization_id, role, email)
    comes from our own `invitations` table — the payload is never
    trusted as a source of truth for security-relevant fields. See
    `routes/webhooks.rs` for the structural rationale.

        Attributes:
            id (str): Stable, unique WorkOS event identifier. Used as the
                idempotency key so duplicate deliveries are no-ops. Example: event_01JZ8DEK6MN4EQF5QPTMG8AVPC.
            event (str): Event discriminator, e.g., `invitation.accepted`,
                `invitation.created`, `invitation.revoked`. Example: invitation.accepted.
            data (WorkosWebhookEventData): Event-type-specific payload. Decoded per discriminator;
                callers should not depend on the shape across event types.
    """

    id: str
    event: str
    data: WorkosWebhookEventData
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        event = self.event

        data = self.data.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "event": event,
                "data": data,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workos_webhook_event_data import WorkosWebhookEventData

        d = dict(src_dict)
        id = d.pop("id")

        event = d.pop("event")

        data = WorkosWebhookEventData.from_dict(d.pop("data"))

        workos_webhook_event = cls(
            id=id,
            event=event,
            data=data,
        )

        workos_webhook_event.additional_properties = d
        return workos_webhook_event

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
