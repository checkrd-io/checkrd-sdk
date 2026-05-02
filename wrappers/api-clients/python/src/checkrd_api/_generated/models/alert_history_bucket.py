from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

T = TypeVar("T", bound="AlertHistoryBucket")


@_attrs_define
class AlertHistoryBucket:
    """One time-bucketed aggregate over alert state transitions.

    Returned (as a sorted slice) from
    `GET /v1/alert-history/timeseries`.

        Attributes:
            bucket_start (datetime.datetime):
            firing (int):
            resolved (int):
            pending (int):
            ok (int):
            no_data (int):
            total (int):
    """

    bucket_start: datetime.datetime
    firing: int
    resolved: int
    pending: int
    ok: int
    no_data: int
    total: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        bucket_start = self.bucket_start.isoformat()

        firing = self.firing

        resolved = self.resolved

        pending = self.pending

        ok = self.ok

        no_data = self.no_data

        total = self.total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "bucket_start": bucket_start,
                "firing": firing,
                "resolved": resolved,
                "pending": pending,
                "ok": ok,
                "no_data": no_data,
                "total": total,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        bucket_start = isoparse(d.pop("bucket_start"))

        firing = d.pop("firing")

        resolved = d.pop("resolved")

        pending = d.pop("pending")

        ok = d.pop("ok")

        no_data = d.pop("no_data")

        total = d.pop("total")

        alert_history_bucket = cls(
            bucket_start=bucket_start,
            firing=firing,
            resolved=resolved,
            pending=pending,
            ok=ok,
            no_data=no_data,
            total=total,
        )

        alert_history_bucket.additional_properties = d
        return alert_history_bucket

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
