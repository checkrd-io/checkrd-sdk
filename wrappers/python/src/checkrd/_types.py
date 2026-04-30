"""Public type aliases for the Checkrd Python SDK.

Replaces the ``dict[str, Any]`` and free-string parameters that
previously leaked through the public surface. Each type here is one
of:

  - A :class:`typing.TypedDict` — for a structured object whose top-
    level keys are known. ``total=False`` plus ``typing_extensions.Required``
    is the cross-version-compatible spelling for "some keys mandatory,
    some optional"; we accept the typing_extensions backport because
    PEP 655 (``Required``/``NotRequired``) only landed in stdlib in
    3.11 and the SDK supports 3.9+.
  - A :class:`typing.Literal` — for closed enums (action verbs,
    severity levels, mode flags). Lifts magic strings out of the
    public API so static checkers can refuse the wrong literal.

Why a dedicated module: keeping these in ``_settings.py`` (which
already houses :data:`SecurityMode` / :data:`EnforceMode`) would mix
configuration types with payload types, and would force consumers
who only want :class:`Policy` to import a module that touches env
vars at import time. Splitting them mirrors the pattern Stripe and
Anthropic use for their typed API surface.

Stability: every name re-exported from :mod:`checkrd` is part of the
public API and follows SemVer. New optional keys may be added in
minor releases (TypedDict with ``total=False`` makes that
backward-compatible); removing or repurposing a key is a major
release.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Optional, TypedDict

# typing.Required / NotRequired only landed in stdlib in 3.11. We
# support 3.9+, so instead of pulling in `typing_extensions` (extra
# install for the backport), we use the cross-version-compatible
# TypedDict inheritance pattern: split each shape into a ``total=False``
# parent (optional fields) and a ``total=True`` subclass (required
# fields). mypy + pyright understand this idiom natively.


# ===========================================================================
# Closed enums — reject magic strings on the public surface.
# ===========================================================================

#: Outcome of a single :class:`PolicyRule`. ``allow`` and ``deny`` are
#: terminal; ``rate_limit`` falls through to the rule's
#: ``rate_limit`` block before deciding.
PolicyAction = Literal["allow", "deny", "rate_limit"]

#: The whole-policy default applied when no rule matches.
PolicyDefault = Literal["allow", "deny"]

#: Status returned from :func:`checkrd.healthy`. ``healthy`` means the
#: WASM engine is loaded and the SDK is fully wired; ``degraded``
#: means a non-fatal failure happened (engine missing in permissive
#: mode); ``disabled`` means ``CHECKRD_DISABLED`` is set or no
#: ``init()`` was performed.
HealthStatus = Literal["healthy", "degraded", "disabled", "error"]

#: Specific cause when :data:`HealthStatus` is ``"degraded"``. K8s
#: readiness probes only need the high-level ``status``, but
#: dashboards and runbooks want a stable token they can pivot on.
#: Each value maps to a documented remediation:
#:
#:   - ``wasm_failed`` — WASM engine refused to load. Permissive mode
#:     is letting traffic through unevaluated. Check the WASM
#:     integrity hash and the ``CHECKRD_SKIP_WASM_INTEGRITY`` env var.
#:   - ``control_plane_unreachable`` — telemetry POSTs and SSE
#:     receiver both failing. Verify the URL + API key + network
#:     reachability.
#:   - ``control_plane_circuit_open`` — shared :class:`CircuitBreaker`
#:     tripped. Wait for the jittered reset window or check the
#:     control plane's status page.
#:   - ``signing_unavailable`` — engine has no Ed25519 private key
#:     (anonymous mode). Telemetry batches drop with
#:     ``signing_error``. Configure ``LocalIdentity`` or wire up
#:     ``ExternalIdentity`` with a working KMS-backed signer.
#:   - ``telemetry_dropping`` — backpressure or send errors past a
#:     threshold. Inspect ``HealthCheck.telemetry`` for the specific
#:     drop counter.
#:
#: ``None`` whenever ``status != "degraded"``.
DegradationReason = Literal[
    "wasm_failed",
    "control_plane_unreachable",
    "control_plane_circuit_open",
    "signing_unavailable",
    "telemetry_dropping",
]


# ===========================================================================
# Policy shape — TypedDict mirror of the schema validated server-side
# and inside the WASM core. We keep nested `match` / `rate_limit`
# objects open (Mapping[str, Any]) because they have many optional
# fields and a generated TypedDict per shape would obscure what users
# actually need to write.
# ===========================================================================


class PolicyRule(TypedDict, total=False):
    """One rule inside a :class:`Policy`.

    A rule is a (matcher, action) pair. The matcher describes which
    requests apply; the action describes the verdict. ``rate_limit``
    rules carry an extra ``rate_limit`` object — rules that are
    purely allow/deny may omit it.

    All fields are optional in the type system because a rule with
    only ``match`` and ``action`` is the common case; the WASM core
    enforces structural validity at install time.
    """

    #: HTTP method, URL, body, header matchers. See the policy schema.
    match: Mapping[str, Any]
    #: Verdict when the matcher fires.
    action: PolicyAction
    #: Per-rule rate limit (only meaningful when ``action == "rate_limit"``).
    rate_limit: Mapping[str, Any]
    #: Free-text label surfaced in deny reasons / logs.
    name: str


class _PolicyOptional(TypedDict, total=False):
    """Optional metadata on a :class:`Policy`. Split out because
    TypedDict's ``total=False`` is class-wide; inheriting it as the
    base of :class:`Policy` and re-declaring required fields with
    ``total=True`` is the cross-version-compatible spelling for
    "some keys mandatory, some optional"."""

    #: Optional schema version for the policy document itself.
    #: Independent of ``checkrd``'s SemVer; bump when introducing
    #: incompatible new keys.
    schema_version: int
    #: Optional human-readable description for dashboards.
    description: str


class Policy(_PolicyOptional, total=True):
    """Top-level Checkrd policy document — the structured form of
    what would otherwise live in a YAML / JSON file.

    Required fields (``agent``, ``default``, ``rules``) match the
    server-side schema. Optional fields (inherited from
    :class:`_PolicyOptional`) cover metadata that the WASM core
    ignores but operators reach for in dashboards / audit logs.

    Pass either a :class:`Policy` dict, a YAML/JSON string, or a
    :class:`pathlib.Path` to ``policy=...`` on
    :func:`checkrd.wrap`, :func:`checkrd.init`, and
    :class:`checkrd.Checkrd`.
    """

    #: Stable identifier for the agent the policy applies to. Must
    #: match the ``agent_id`` passed to ``init()`` / ``wrap()``;
    #: mismatch is a configuration error caught at engine startup.
    agent: str
    #: Whole-policy default verdict applied when no rule matches.
    default: PolicyDefault
    #: Ordered list of rules. The WASM core evaluates them in the
    #: order given — first matching wins.
    rules: list[PolicyRule]


# ===========================================================================
# Health status — return shape of :func:`checkrd.healthy`. K8s
# readiness probes / dashboards parse this; locking it down with a
# TypedDict makes silent breakage from added/removed keys impossible.
# ===========================================================================


class TelemetryDiagnostics(TypedDict):
    """Snapshot of the telemetry batcher's loss counters.

    Mirrors :meth:`checkrd.batcher.TelemetryBatcher.diagnostics`.
    Embedded in :class:`HealthCheck` under the ``telemetry`` key so
    one health probe surfaces both ``ok`` / ``degraded`` AND the
    drop-counter shape monitoring expects.
    """

    sent: int
    dropped_backpressure: int
    dropped_signing_error: int
    dropped_send_error: int
    pending: int
    last_request_id: Optional[str]


class _HealthCheckOptional(TypedDict, total=False):
    """Optional fields of :class:`HealthCheck` — see :class:`Policy`
    for the rationale on splitting required vs optional via
    inheritance."""

    telemetry: Optional[TelemetryDiagnostics]
    #: Stable token identifying which subsystem caused a ``degraded``
    #: status. ``None`` when status is not degraded; populated to
    #: exactly one of :data:`DegradationReason`'s values when it is.
    #: Dashboards pivot on this; K8s probes ignore it.
    degradation_reason: Optional[DegradationReason]


class HealthCheck(_HealthCheckOptional, total=True):
    """Return value of :func:`checkrd.healthy`.

    Designed for two consumers: K8s readiness probes (``status`` is
    the only field they care about) and observability dashboards
    (``degradation_reason`` + ``telemetry`` for the failure mode).
    Keeping the shape a TypedDict means a typo in a downstream
    dashboard query fails the type checker instead of silently
    rendering blank panels.
    """

    status: HealthStatus
    engine_loaded: bool
    control_plane_connected: Optional[bool]
    agent_id: Optional[str]
    enforce: Optional[bool]
    #: ISO-8601 timestamp string of the most recent policy evaluation,
    #: or ``None`` before the first request runs through the SDK. Stored
    #: as a string (not a :class:`float` Unix timestamp) because the
    #: value lands in K8s probe responses and Grafana panels that
    #: render strings directly without timezone-format inference.
    last_eval_at: Optional[str]


__all__ = [
    "DegradationReason",
    "HealthCheck",
    "HealthStatus",
    "Policy",
    "PolicyAction",
    "PolicyDefault",
    "PolicyRule",
    "TelemetryDiagnostics",
]
