from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgReplayRequest")


@_attrs_define
class OrgReplayRequest:
    """`POST /v1/org-policies/replay` request body.

    Attributes:
        yaml_content (str): Candidate YAML to replay. Not persisted.
        range_ (None | str | Unset): Time range for event lookup: `"1h"`, `"24h"`, or `"7d"`.
            Defaults to `"24h"` when omitted.
        limit (int | None | Unset): Maximum events to evaluate. Defaults to 10_000. Caps at 10_000.
    """

    yaml_content: str
    range_: None | str | Unset = UNSET
    limit: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        yaml_content = self.yaml_content

        range_: None | str | Unset
        if isinstance(self.range_, Unset):
            range_ = UNSET
        else:
            range_ = self.range_

        limit: int | None | Unset
        if isinstance(self.limit, Unset):
            limit = UNSET
        else:
            limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "yaml_content": yaml_content,
            }
        )
        if range_ is not UNSET:
            field_dict["range"] = range_
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        yaml_content = d.pop("yaml_content")

        def _parse_range_(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        range_ = _parse_range_(d.pop("range", UNSET))

        def _parse_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        limit = _parse_limit(d.pop("limit", UNSET))

        org_replay_request = cls(
            yaml_content=yaml_content,
            range_=range_,
            limit=limit,
        )

        org_replay_request.additional_properties = d
        return org_replay_request

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
