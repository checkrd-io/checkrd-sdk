from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ingest_request_events_item import IngestRequestEventsItem


T = TypeVar("T", bound="IngestRequest")


@_attrs_define
class IngestRequest:
    """`POST /v1/telemetry` request body.

    Sent by the SDK in batches of up to 1,000 events per request. The
    optional `sdk_version` is recorded on the row in ClickHouse so the
    dashboard can surface "events received from SDK X.Y.Z" — useful
    for spotting clients stuck on a buggy release.

        Attributes:
            events (list[IngestRequestEventsItem]): Up to 1,000 telemetry events per request. Each event is
                validated against `checkrd_shared::telemetry::validate_telemetry_event`
                before insertion — invalid events fail the whole batch with 400.
                Modeled as `Vec<Object>` because the runtime type lives in the
                WASM-safe `checkrd-shared` crate (no `utoipa` dep).
            sdk_version (None | str | Unset): Optional SDK version string (e.g., `"checkrd-py/0.4.1"`).
                Surfaced on the dashboard so operators can correlate ingest
                behavior with client versions.
    """

    events: list[IngestRequestEventsItem]
    sdk_version: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        events = []
        for events_item_data in self.events:
            events_item = events_item_data.to_dict()
            events.append(events_item)

        sdk_version: None | str | Unset
        if isinstance(self.sdk_version, Unset):
            sdk_version = UNSET
        else:
            sdk_version = self.sdk_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "events": events,
            }
        )
        if sdk_version is not UNSET:
            field_dict["sdk_version"] = sdk_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ingest_request_events_item import IngestRequestEventsItem

        d = dict(src_dict)
        events = []
        _events = d.pop("events")
        for events_item_data in _events:
            events_item = IngestRequestEventsItem.from_dict(events_item_data)

            events.append(events_item)

        def _parse_sdk_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sdk_version = _parse_sdk_version(d.pop("sdk_version", UNSET))

        ingest_request = cls(
            events=events,
            sdk_version=sdk_version,
        )

        ingest_request.additional_properties = d
        return ingest_request

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
