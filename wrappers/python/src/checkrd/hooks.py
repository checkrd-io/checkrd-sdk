"""Request lifecycle hooks for the Checkrd SDK.

Hooks let users run custom logic at policy evaluation time without
modifying the SDK internals. Three hook points are available:

- ``before_request`` — fires before policy evaluation. Returning ``None``
  skips evaluation entirely (pass-through). Returning the event
  (possibly modified) proceeds normally. Use this for request-level
  overrides, sampling, or cost control.
- ``on_allow`` — fires after a request is allowed by the policy engine.
  Use this for allow-side observability (metrics, structured logging).
- ``on_deny`` — fires after a request is denied (both in enforce and
  dry-run modes). Use this for alerting (Slack, PagerDuty), audit
  trails, or custom deny behavior.

Hook exceptions are caught and logged at WARNING level. A crashing hook
never takes down the user's request. This follows the Sentry and
OpenTelemetry precedent.

Usage::

    import checkrd

    def alert_on_deny(event: checkrd.CheckrdEvent) -> None:
        slack.post(f"Blocked: {event.method} {event.url} — {event.deny_reason}")

    checkrd.init(policy="policy.yaml", on_deny=alert_on_deny)
    checkrd.instrument()

Or per-client::

    client = checkrd.wrap(
        httpx.Client(),
        on_deny=lambda e: print(f"Denied: {e.url}"),
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class CheckrdEvent:
    """Payload passed to lifecycle hooks.

    Populated progressively: ``before_request`` receives an event with
    only the request fields set (``allowed``, ``deny_reason``, etc. are
    ``None``). ``on_allow`` and ``on_deny`` receive a fully populated
    event after policy evaluation.

    Attributes:
        method: HTTP method (``GET``, ``POST``, etc.).
        url: The full request URL.
        headers: Request headers as a list of ``(name, value)`` tuples.
        body: Request body as a string, or ``None`` for empty/binary bodies.
        request_id: Unique correlation ID for this evaluation.
        allowed: ``True`` if the policy engine allowed the request,
            ``False`` if denied, ``None`` before evaluation.
        rule_name: Name of the matching rule, or ``None``.
        deny_reason: Human-readable deny reason, or ``None`` if allowed.
        suggestion: Actionable fix suggestion, or ``None``.
        dashboard_url: Deep link to the event in the dashboard, or ``None``.
    """

    method: str
    url: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: Optional[str] = None
    request_id: str = ""
    allowed: Optional[bool] = None
    rule_name: Optional[str] = None
    deny_reason: Optional[str] = None
    suggestion: Optional[str] = None
    dashboard_url: Optional[str] = None
    #: W3C Trace Context trace-id (32 lowercase hex chars), extracted
    #: from the request's ``traceparent`` header when present so
    #: hook callers can correlate the SDK's policy decision with
    #: the user's distributed-trace span. ``None`` when the request
    #: carries no ``traceparent``. Mirrors the value the telemetry
    #: batcher stamps on its ``POST /v1/telemetry`` so a single
    #: ``trace_id`` spans agent code → policy eval → telemetry
    #: ingestion → ClickHouse.
    trace_id: Optional[str] = None


#: Callback invoked before policy evaluation. Return ``None`` to skip
#: evaluation (pass-through), or return the event to proceed.
BeforeRequestHook = Callable[[CheckrdEvent], Optional[CheckrdEvent]]

#: Callback invoked after a request is allowed by the policy engine.
OnAllowHook = Callable[[CheckrdEvent], None]

#: Callback invoked after a request is denied (both enforce and dry-run).
OnDenyHook = Callable[[CheckrdEvent], None]


#: Hint metadata passed to :data:`BeforeSendHook`. Populated by the SDK;
#: users read but don't mutate. Mirrors Sentry's ``hint`` argument: gives
#: the hook context that's not stored in the event itself (e.g., what
#: agent the event came from, whether it's a stream completion or a
#: regular request evaluation). Stable keys are documented below; we
#: reserve the right to add more in minor releases (callers should
#: ``.get(key, default)`` rather than assume keys exist).
#:
#: Documented keys:
#:   - ``agent_id`` (str): the agent emitting the event.
#:   - ``event_kind`` (str): ``"request_evaluation"`` for the policy-
#:     decision event the transport enqueues, ``"stream_completion"``
#:     for the post-stream token-usage event, etc.
BeforeSendHint = "dict[str, object]"


#: Telemetry-event mutation/drop hook. Fires once per event right
#: before it's enqueued for batched delivery. Use it to:
#:
#: - **redact fields**: ``event.pop("body_hash", None); return event``
#: - **drop events**: ``return None`` (the event never ships; no
#:   ``dropped_*`` counter increments — operator-intended drops are
#:   not failures)
#: - **transform payloads**: rewrite endpoint URLs, normalize status
#:   codes, attach static labels, etc.
#:
#: Same name and same contract as Sentry's ``before_send``: returning
#: ``None`` drops, returning the (possibly mutated) event ships it.
#: Operators migrating from Sentry recognize the shape immediately.
#:
#: Hook exceptions are caught and logged; a crashing hook drops the
#: event but never takes down the calling thread or the user's
#: request critical path.
BeforeSendHook = Callable[
    ["dict[str, object]", "dict[str, object]"], Optional["dict[str, object]"],
]
