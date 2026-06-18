from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.field_error import FieldError


T = TypeVar("T", bound="ProblemDetails")


@_attrs_define
class ProblemDetails:
    """RFC 9457 Problem Details object — the body of every non-2xx response.

    `type`/`title`/`status`/`detail`/`instance` are the RFC 9457 standard
    members; everything from `code` down is an extension member (RFC 9457
    §3.2). `code` is the stable, machine-readable identifier clients branch
    on; `errors` carries per-field validation failures; the remaining typed
    extensions are populated only by the specific problem types that need them.

    Built exclusively from the [`ErrorCode`] registry via [`ProblemDetails::from_code`],
    so `type`, `title`, `status`, and `code` can never drift from each other.

    ADR-009v2 (supersedes ADR-009): the wire format is RFC 9457
    `application/problem+json`, not the former Stripe-style `{"error":{…}}`
    envelope. The house `code` (and the `errors[]` field-pointer array) ride as
    extension members (RFC 9457 §3.2), so no client loses information. Adopted
    in the pre-1.0 / zero-user window where the breaking shape change cost
    nothing.

        Attributes:
            type_ (str): Dereferenceable URI identifying the problem type (RFC 9457 §3.1.1). Example:
                https://checkrd.io/errors/invalid_api_key.
            title (str): Stable, short summary of the problem type (RFC 9457 §3.1.2). Example: Invalid API key.
            status (int): HTTP status code, duplicated in-body for out-of-band use (RFC 9457 §3.1.3). Example: 401.
            code (str): Stable, fine-grained, machine-readable error code. Clients branch on this. Example: invalid_api_key.
            detail (None | str | Unset): Human-readable explanation specific to this occurrence (RFC 9457 §3.1.4).
            instance (None | str | Unset): URI reference identifying this specific occurrence (RFC 9457 §3.1.5).
                Stamped by the problem-normalization layer from the request path.
            errors (list[FieldError] | Unset): Per-field validation failures (RFC 6901 pointers). Omitted when empty.
            request_id (None | str | Unset): Correlation id for this request (mirrors the `x-request-id` header).
                Stamped by the problem-normalization layer.
            resource (None | str | Unset): Billing: the resource whose plan limit was hit (`agents`, `api_keys`, …).
            limit (int | None | Unset): Billing: the limit value for the current plan.
            current (int | None | Unset): Billing: the caller's current usage.
            feature (None | str | Unset): Billing: the gated feature name.
            required_tier (None | str | Unset): Billing: the tier required to unlock the feature or a higher limit.
            requested (None | str | Unset): Versioning: the raw `Checkrd-Version` value the client sent.
            minimum_supported (None | str | Unset): Versioning: the minimum supported API version.
            latest_supported (None | str | Unset): Versioning: the latest supported API version.
    """

    type_: str
    title: str
    status: int
    code: str
    detail: None | str | Unset = UNSET
    instance: None | str | Unset = UNSET
    errors: list[FieldError] | Unset = UNSET
    request_id: None | str | Unset = UNSET
    resource: None | str | Unset = UNSET
    limit: int | None | Unset = UNSET
    current: int | None | Unset = UNSET
    feature: None | str | Unset = UNSET
    required_tier: None | str | Unset = UNSET
    requested: None | str | Unset = UNSET
    minimum_supported: None | str | Unset = UNSET
    latest_supported: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        type_ = self.type_

        title = self.title

        status = self.status

        code = self.code

        detail: None | str | Unset
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        instance: None | str | Unset
        if isinstance(self.instance, Unset):
            instance = UNSET
        else:
            instance = self.instance

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        request_id: None | str | Unset
        if isinstance(self.request_id, Unset):
            request_id = UNSET
        else:
            request_id = self.request_id

        resource: None | str | Unset
        if isinstance(self.resource, Unset):
            resource = UNSET
        else:
            resource = self.resource

        limit: int | None | Unset
        if isinstance(self.limit, Unset):
            limit = UNSET
        else:
            limit = self.limit

        current: int | None | Unset
        if isinstance(self.current, Unset):
            current = UNSET
        else:
            current = self.current

        feature: None | str | Unset
        if isinstance(self.feature, Unset):
            feature = UNSET
        else:
            feature = self.feature

        required_tier: None | str | Unset
        if isinstance(self.required_tier, Unset):
            required_tier = UNSET
        else:
            required_tier = self.required_tier

        requested: None | str | Unset
        if isinstance(self.requested, Unset):
            requested = UNSET
        else:
            requested = self.requested

        minimum_supported: None | str | Unset
        if isinstance(self.minimum_supported, Unset):
            minimum_supported = UNSET
        else:
            minimum_supported = self.minimum_supported

        latest_supported: None | str | Unset
        if isinstance(self.latest_supported, Unset):
            latest_supported = UNSET
        else:
            latest_supported = self.latest_supported

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "type": type_,
                "title": title,
                "status": status,
                "code": code,
            }
        )
        if detail is not UNSET:
            field_dict["detail"] = detail
        if instance is not UNSET:
            field_dict["instance"] = instance
        if errors is not UNSET:
            field_dict["errors"] = errors
        if request_id is not UNSET:
            field_dict["request_id"] = request_id
        if resource is not UNSET:
            field_dict["resource"] = resource
        if limit is not UNSET:
            field_dict["limit"] = limit
        if current is not UNSET:
            field_dict["current"] = current
        if feature is not UNSET:
            field_dict["feature"] = feature
        if required_tier is not UNSET:
            field_dict["required_tier"] = required_tier
        if requested is not UNSET:
            field_dict["requested"] = requested
        if minimum_supported is not UNSET:
            field_dict["minimum_supported"] = minimum_supported
        if latest_supported is not UNSET:
            field_dict["latest_supported"] = latest_supported

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.field_error import FieldError

        d = dict(src_dict)
        type_ = d.pop("type")

        title = d.pop("title")

        status = d.pop("status")

        code = d.pop("code")

        def _parse_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        detail = _parse_detail(d.pop("detail", UNSET))

        def _parse_instance(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        instance = _parse_instance(d.pop("instance", UNSET))

        _errors = d.pop("errors", UNSET)
        errors: list[FieldError] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = FieldError.from_dict(errors_item_data)

                errors.append(errors_item)

        def _parse_request_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_id = _parse_request_id(d.pop("request_id", UNSET))

        def _parse_resource(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        resource = _parse_resource(d.pop("resource", UNSET))

        def _parse_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        limit = _parse_limit(d.pop("limit", UNSET))

        def _parse_current(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        current = _parse_current(d.pop("current", UNSET))

        def _parse_feature(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        feature = _parse_feature(d.pop("feature", UNSET))

        def _parse_required_tier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        required_tier = _parse_required_tier(d.pop("required_tier", UNSET))

        def _parse_requested(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        requested = _parse_requested(d.pop("requested", UNSET))

        def _parse_minimum_supported(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        minimum_supported = _parse_minimum_supported(d.pop("minimum_supported", UNSET))

        def _parse_latest_supported(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        latest_supported = _parse_latest_supported(d.pop("latest_supported", UNSET))

        problem_details = cls(
            type_=type_,
            title=title,
            status=status,
            code=code,
            detail=detail,
            instance=instance,
            errors=errors,
            request_id=request_id,
            resource=resource,
            limit=limit,
            current=current,
            feature=feature,
            required_tier=required_tier,
            requested=requested,
            minimum_supported=minimum_supported,
            latest_supported=latest_supported,
        )

        problem_details.additional_properties = d
        return problem_details

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
