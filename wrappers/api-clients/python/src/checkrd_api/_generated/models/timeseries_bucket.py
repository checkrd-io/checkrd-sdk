from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

T = TypeVar("T", bound="TimeseriesBucket")


@_attrs_define
class TimeseriesBucket:
    """`GET /v1/dashboard/timeseries` response item.

    One bucket of the dashboard chart. Bucket granularity is chosen
    server-side to fit the requested window (1m / 5m / 1h /...).

        Attributes:
            bucket (datetime.datetime):
            total (int):
            allowed (int):
            denied (int):
            error (int):
    """

    bucket: datetime.datetime
    total: int
    allowed: int
    denied: int
    error: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        bucket = self.bucket.isoformat()

        total = self.total

        allowed = self.allowed

        denied = self.denied

        error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "bucket": bucket,
                "total": total,
                "allowed": allowed,
                "denied": denied,
                "error": error,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        bucket = isoparse(d.pop("bucket"))

        total = d.pop("total")

        allowed = d.pop("allowed")

        denied = d.pop("denied")

        error = d.pop("error")

        timeseries_bucket = cls(
            bucket=bucket,
            total=total,
            allowed=allowed,
            denied=denied,
            error=error,
        )

        timeseries_bucket.additional_properties = d
        return timeseries_bucket

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
