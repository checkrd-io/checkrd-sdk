from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.plan_limits import PlanLimits
    from ..models.resource_usage import ResourceUsage


T = TypeVar("T", bound="BillingStatus")


@_attrs_define
class BillingStatus:
    """`GET /v1/billing/status` response body.

    Joins org-row billing state (plan tier, subscription status,
    period dates) with live resource counts and the Redis-backed
    monthly event counter. Powers the dashboard's `/settings/billing`
    page and the `checkrd billing status` CLI command.

        Attributes:
            plan_tier (str): `"free"`, `"team"`, or `"enterprise"`. Sourced from
                `organizations.plan_tier`.
            subscription_status (str): Stripe subscription status. Common values: `"active"`,
                `"canceling"` (synthesized when `cancel_at_period_end=true`),
                `"past_due"`, `"canceled"`, `"trialing"`, `"incomplete"`.
            has_stripe_customer (bool): `true` once the org has a Stripe Customer record. False until
                the first checkout/portal call (lazy customer creation).
            limits (PlanLimits): Resource counts and feature flags for a billing tier. Wire-shape
                mirror of `checkrd_shared::PlanLimits` (which can't derive
                `ToSchema` because it has to compile to `wasm32-wasip1`).

                Both structs serialize identically — the api crate converts at
                the boundary.
            usage (ResourceUsage): Resource counts for a workspace, joined to its plan limits.
            paid_until (datetime.datetime | None | Unset): End of the current paid period (Stripe `current_period_end`).
                Null when not subscribed.
            trial_ends_at (datetime.datetime | None | Unset): End of any active trial (Stripe `trial_end`). Null when not in
                trial.
    """

    plan_tier: str
    subscription_status: str
    has_stripe_customer: bool
    limits: PlanLimits
    usage: ResourceUsage
    paid_until: datetime.datetime | None | Unset = UNSET
    trial_ends_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        plan_tier = self.plan_tier

        subscription_status = self.subscription_status

        has_stripe_customer = self.has_stripe_customer

        limits = self.limits.to_dict()

        usage = self.usage.to_dict()

        paid_until: None | str | Unset
        if isinstance(self.paid_until, Unset):
            paid_until = UNSET
        elif isinstance(self.paid_until, datetime.datetime):
            paid_until = self.paid_until.isoformat()
        else:
            paid_until = self.paid_until

        trial_ends_at: None | str | Unset
        if isinstance(self.trial_ends_at, Unset):
            trial_ends_at = UNSET
        elif isinstance(self.trial_ends_at, datetime.datetime):
            trial_ends_at = self.trial_ends_at.isoformat()
        else:
            trial_ends_at = self.trial_ends_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "plan_tier": plan_tier,
                "subscription_status": subscription_status,
                "has_stripe_customer": has_stripe_customer,
                "limits": limits,
                "usage": usage,
            }
        )
        if paid_until is not UNSET:
            field_dict["paid_until"] = paid_until
        if trial_ends_at is not UNSET:
            field_dict["trial_ends_at"] = trial_ends_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_limits import PlanLimits
        from ..models.resource_usage import ResourceUsage

        d = dict(src_dict)
        plan_tier = d.pop("plan_tier")

        subscription_status = d.pop("subscription_status")

        has_stripe_customer = d.pop("has_stripe_customer")

        limits = PlanLimits.from_dict(d.pop("limits"))

        usage = ResourceUsage.from_dict(d.pop("usage"))

        def _parse_paid_until(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                paid_until_type_0 = isoparse(data)

                return paid_until_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        paid_until = _parse_paid_until(d.pop("paid_until", UNSET))

        def _parse_trial_ends_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                trial_ends_at_type_0 = isoparse(data)

                return trial_ends_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        trial_ends_at = _parse_trial_ends_at(d.pop("trial_ends_at", UNSET))

        billing_status = cls(
            plan_tier=plan_tier,
            subscription_status=subscription_status,
            has_stripe_customer=has_stripe_customer,
            limits=limits,
            usage=usage,
            paid_until=paid_until,
            trial_ends_at=trial_ends_at,
        )

        billing_status.additional_properties = d
        return billing_status

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
