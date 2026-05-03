from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

T = TypeVar("T", bound="TelemetryEventRow")


@_attrs_define
class TelemetryEventRow:
    """A single telemetry event row.

    Returned by `GET /v1/dashboard/events` (paginated list) and
    `GET /v1/dashboard/events/{request_id}` (single lookup). Mirrors
    the runtime row type in `checkrd_telemetry_store::TelemetryEventRow`.

        Attributes:
            agent_id (UUID):
            request_id (str): SDK-generated UUID for the intercepted request. Org-scoped —
                the same `request_id` from another org is never returned.
            timestamp (datetime.datetime):
            url_host (str):
            url_path (str): Parameterized path template (e.g., `/v1/charges/{id}`). PII is
                stripped client-side before the event leaves the SDK.
            method (str):
            source (str): `"sdk_signed"` (Ed25519-signed via RFC 9421 + DSSE) or
                `"otlp"` (bridged from OTLP/HTTP).
            api_key_id (None | Unset | UUID):
            sdk_version (None | str | Unset):
            status_code (int | None | Unset):
            latency_ms (int | None | Unset):
            policy_result (None | str | Unset): `"allowed"`, `"denied"`, or null (no policy was evaluated).
            deny_reason (None | str | Unset): Rule name that produced a deny decision, if any. Never carries
                body values — only user-authored rule identifiers.
            trace_id (None | str | Unset): 32-char lowercase hex (W3C Trace Context).
            span_id (None | str | Unset):
            parent_span_id (None | str | Unset):
            span_name (None | str | Unset):
            span_kind (None | str | Unset):
            span_status_code (None | str | Unset):
            matched_rule (None | str | Unset): Rule name that matched during evaluation, if any.
            matched_rule_kind (None | str | Unset): `"allow"`, `"deny"`, `"rate_limit"`, `"kill_switch"`, or
                `"default"`.
            policy_mode (None | str | Unset): `"enforce"` or `"dry_run"`.
            evaluation_path (None | str | Unset): JSON-serialized `Vec<EvaluationStep>`. Empty string = no path
                recorded.
    """

    agent_id: UUID
    request_id: str
    timestamp: datetime.datetime
    url_host: str
    url_path: str
    method: str
    source: str
    api_key_id: None | Unset | UUID = UNSET
    sdk_version: None | str | Unset = UNSET
    status_code: int | None | Unset = UNSET
    latency_ms: int | None | Unset = UNSET
    policy_result: None | str | Unset = UNSET
    deny_reason: None | str | Unset = UNSET
    trace_id: None | str | Unset = UNSET
    span_id: None | str | Unset = UNSET
    parent_span_id: None | str | Unset = UNSET
    span_name: None | str | Unset = UNSET
    span_kind: None | str | Unset = UNSET
    span_status_code: None | str | Unset = UNSET
    matched_rule: None | str | Unset = UNSET
    matched_rule_kind: None | str | Unset = UNSET
    policy_mode: None | str | Unset = UNSET
    evaluation_path: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = str(self.agent_id)

        request_id = self.request_id

        timestamp = self.timestamp.isoformat()

        url_host = self.url_host

        url_path = self.url_path

        method = self.method

        source = self.source

        api_key_id: None | str | Unset
        if isinstance(self.api_key_id, Unset):
            api_key_id = UNSET
        elif isinstance(self.api_key_id, UUID):
            api_key_id = str(self.api_key_id)
        else:
            api_key_id = self.api_key_id

        sdk_version: None | str | Unset
        if isinstance(self.sdk_version, Unset):
            sdk_version = UNSET
        else:
            sdk_version = self.sdk_version

        status_code: int | None | Unset
        if isinstance(self.status_code, Unset):
            status_code = UNSET
        else:
            status_code = self.status_code

        latency_ms: int | None | Unset
        if isinstance(self.latency_ms, Unset):
            latency_ms = UNSET
        else:
            latency_ms = self.latency_ms

        policy_result: None | str | Unset
        if isinstance(self.policy_result, Unset):
            policy_result = UNSET
        else:
            policy_result = self.policy_result

        deny_reason: None | str | Unset
        if isinstance(self.deny_reason, Unset):
            deny_reason = UNSET
        else:
            deny_reason = self.deny_reason

        trace_id: None | str | Unset
        if isinstance(self.trace_id, Unset):
            trace_id = UNSET
        else:
            trace_id = self.trace_id

        span_id: None | str | Unset
        if isinstance(self.span_id, Unset):
            span_id = UNSET
        else:
            span_id = self.span_id

        parent_span_id: None | str | Unset
        if isinstance(self.parent_span_id, Unset):
            parent_span_id = UNSET
        else:
            parent_span_id = self.parent_span_id

        span_name: None | str | Unset
        if isinstance(self.span_name, Unset):
            span_name = UNSET
        else:
            span_name = self.span_name

        span_kind: None | str | Unset
        if isinstance(self.span_kind, Unset):
            span_kind = UNSET
        else:
            span_kind = self.span_kind

        span_status_code: None | str | Unset
        if isinstance(self.span_status_code, Unset):
            span_status_code = UNSET
        else:
            span_status_code = self.span_status_code

        matched_rule: None | str | Unset
        if isinstance(self.matched_rule, Unset):
            matched_rule = UNSET
        else:
            matched_rule = self.matched_rule

        matched_rule_kind: None | str | Unset
        if isinstance(self.matched_rule_kind, Unset):
            matched_rule_kind = UNSET
        else:
            matched_rule_kind = self.matched_rule_kind

        policy_mode: None | str | Unset
        if isinstance(self.policy_mode, Unset):
            policy_mode = UNSET
        else:
            policy_mode = self.policy_mode

        evaluation_path: None | str | Unset
        if isinstance(self.evaluation_path, Unset):
            evaluation_path = UNSET
        else:
            evaluation_path = self.evaluation_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
                "request_id": request_id,
                "timestamp": timestamp,
                "url_host": url_host,
                "url_path": url_path,
                "method": method,
                "source": source,
            }
        )
        if api_key_id is not UNSET:
            field_dict["api_key_id"] = api_key_id
        if sdk_version is not UNSET:
            field_dict["sdk_version"] = sdk_version
        if status_code is not UNSET:
            field_dict["status_code"] = status_code
        if latency_ms is not UNSET:
            field_dict["latency_ms"] = latency_ms
        if policy_result is not UNSET:
            field_dict["policy_result"] = policy_result
        if deny_reason is not UNSET:
            field_dict["deny_reason"] = deny_reason
        if trace_id is not UNSET:
            field_dict["trace_id"] = trace_id
        if span_id is not UNSET:
            field_dict["span_id"] = span_id
        if parent_span_id is not UNSET:
            field_dict["parent_span_id"] = parent_span_id
        if span_name is not UNSET:
            field_dict["span_name"] = span_name
        if span_kind is not UNSET:
            field_dict["span_kind"] = span_kind
        if span_status_code is not UNSET:
            field_dict["span_status_code"] = span_status_code
        if matched_rule is not UNSET:
            field_dict["matched_rule"] = matched_rule
        if matched_rule_kind is not UNSET:
            field_dict["matched_rule_kind"] = matched_rule_kind
        if policy_mode is not UNSET:
            field_dict["policy_mode"] = policy_mode
        if evaluation_path is not UNSET:
            field_dict["evaluation_path"] = evaluation_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = UUID(d.pop("agent_id"))

        request_id = d.pop("request_id")

        timestamp = isoparse(d.pop("timestamp"))

        url_host = d.pop("url_host")

        url_path = d.pop("url_path")

        method = d.pop("method")

        source = d.pop("source")

        def _parse_api_key_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                api_key_id_type_0 = UUID(data)

                return api_key_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        api_key_id = _parse_api_key_id(d.pop("api_key_id", UNSET))

        def _parse_sdk_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sdk_version = _parse_sdk_version(d.pop("sdk_version", UNSET))

        def _parse_status_code(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        status_code = _parse_status_code(d.pop("status_code", UNSET))

        def _parse_latency_ms(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        latency_ms = _parse_latency_ms(d.pop("latency_ms", UNSET))

        def _parse_policy_result(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        policy_result = _parse_policy_result(d.pop("policy_result", UNSET))

        def _parse_deny_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        deny_reason = _parse_deny_reason(d.pop("deny_reason", UNSET))

        def _parse_trace_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        trace_id = _parse_trace_id(d.pop("trace_id", UNSET))

        def _parse_span_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        span_id = _parse_span_id(d.pop("span_id", UNSET))

        def _parse_parent_span_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_span_id = _parse_parent_span_id(d.pop("parent_span_id", UNSET))

        def _parse_span_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        span_name = _parse_span_name(d.pop("span_name", UNSET))

        def _parse_span_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        span_kind = _parse_span_kind(d.pop("span_kind", UNSET))

        def _parse_span_status_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        span_status_code = _parse_span_status_code(d.pop("span_status_code", UNSET))

        def _parse_matched_rule(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        matched_rule = _parse_matched_rule(d.pop("matched_rule", UNSET))

        def _parse_matched_rule_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        matched_rule_kind = _parse_matched_rule_kind(d.pop("matched_rule_kind", UNSET))

        def _parse_policy_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        policy_mode = _parse_policy_mode(d.pop("policy_mode", UNSET))

        def _parse_evaluation_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evaluation_path = _parse_evaluation_path(d.pop("evaluation_path", UNSET))

        telemetry_event_row = cls(
            agent_id=agent_id,
            request_id=request_id,
            timestamp=timestamp,
            url_host=url_host,
            url_path=url_path,
            method=method,
            source=source,
            api_key_id=api_key_id,
            sdk_version=sdk_version,
            status_code=status_code,
            latency_ms=latency_ms,
            policy_result=policy_result,
            deny_reason=deny_reason,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            span_name=span_name,
            span_kind=span_kind,
            span_status_code=span_status_code,
            matched_rule=matched_rule,
            matched_rule_kind=matched_rule_kind,
            policy_mode=policy_mode,
            evaluation_path=evaluation_path,
        )

        telemetry_event_row.additional_properties = d
        return telemetry_event_row

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
