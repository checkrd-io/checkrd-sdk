"""Checkrd: policy enforcement for AI agent API calls.

The single recommended entry point is :class:`Checkrd`. It mirrors the
``OpenAI`` / ``Anthropic`` / ``Stripe`` SDK shape — one constructor,
keyword-only arguments, env-var fallback, and methods for every verb
(``wrap``, ``instrument``, ``instrument_openai``, ``with_options``,
``close``, ``healthy``).

Zero-config quickstart::

    from checkrd import Checkrd

    with Checkrd() as client:           # config from env
        client.instrument()             # patches every detected vendor
        # ... use openai / anthropic / cohere / groq / ... normally

Explicit per-client::

    import httpx, openai
    from checkrd import Checkrd

    checkrd = Checkrd(api_key="ck_live_xyz", policy="policy.yaml")
    http = checkrd.wrap(httpx.Client())
    client = openai.OpenAI(http_client=http)

Async parity::

    from checkrd import AsyncCheckrd

    async with AsyncCheckrd(api_key="ck_live_xyz") as client:
        http = client.wrap(httpx.AsyncClient())
        # ... await http.post(...)
"""

from __future__ import annotations

import functools
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
import asyncio
from typing import Any, Callable, Optional, TypeVar, Union, cast

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx

from checkrd._types import (
    DegradationReason,
    HealthCheck,
    HealthStatus,
    Policy,
    PolicyAction,
    PolicyDefault,
    PolicyRule,
    TelemetryDiagnostics,
)
from checkrd.transports._httpx import CHECKRD_REQUEST_ID_KEY
from checkrd._settings import (
    EnforceMode,
    SecurityMode,
    Settings,
    resolve,
)
from checkrd._state import (
    _GlobalContext,
    get_context,
    has_context,
    get_last_eval_at,
    is_degraded,
    set_context,
    set_degraded,
    with_lock,
)
from checkrd._logging import RateLimitFilter, SensitiveHeadersFilter
from checkrd._version import __version__
from checkrd.config import load_config
from checkrd.engine import WasmEngine
from checkrd._async_batcher import AsyncTelemetryBatcher
from checkrd._async_control import AsyncAuthError, AsyncControlReceiver
from checkrd._circuit_breaker import CircuitBreaker, CircuitBreakerDiagnostics
from checkrd._pagination import (
    AsyncCursorPage,
    AsyncOffsetPage,
    AsyncSinglePage,
    BaseAsyncPage,
    BasePage,
    CursorPage,
    OffsetPage,
    SinglePage,
)
from checkrd._response import APIResponse, StreamingAPIResponse
from checkrd.exceptions import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    APIUserAbortError,
    AuthenticationError,
    BadRequestError,
    CheckrdError,
    CheckrdInitError,
    CheckrdPolicyDenied,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    PolicySignatureError,
    RateLimitError,
    UnprocessableEntityError,
    make_api_error,
)
from checkrd.hooks import BeforeRequestHook, CheckrdEvent, OnAllowHook, OnDenyHook
from checkrd.identity import ExternalIdentity, IdentityProvider, LocalIdentity
from checkrd.integrations import (
    AnthropicInstrumentor,
    CohereInstrumentor,
    GoogleGenAIInstrumentor,
    GroqInstrumentor,
    HttpxClientInstrumentor,
    Instrumentor,
    MistralInstrumentor,
    OpenAIInstrumentor,
    TogetherInstrumentor,
)
from checkrd.sinks import (
    ControlPlaneSink,
    JsonFileSink,
    LoggingSink,
    OTelSpanSink,
    OtlpSink,
    TelemetrySink,
)
from checkrd.batcher import DropReason, OnDropCallback
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from checkrd.watchers import KillSwitchFileWatcher, PolicyFileWatcher
# `Checkrd` / `AsyncCheckrd` are the single public entry point — they
# match the OpenAI / Anthropic / Stripe SDK shape (one constructor,
# keyword-only arguments, methods for every verb). The lower-level
# ``wrap`` / ``wrap_async`` / ``init`` / ``instrument*`` module-level
# functions still exist below — the class methods delegate to them —
# but they are NOT part of the public surface (not in ``__all__``).
# Import them only from inside the SDK or from tests.
from checkrd.client import AsyncCheckrd, Checkrd

_F = TypeVar("_F", bound=Callable[..., Any])

__all__ = [
    # Unified client class — the only documented entry point
    "Checkrd",
    "AsyncCheckrd",
    # Hooks
    "CheckrdEvent",
    "BeforeRequestHook",
    "OnAllowHook",
    "OnDenyHook",
    "OnDropCallback",
    "DropReason",
    # Instrumentor classes
    "Instrumentor",
    "HttpxClientInstrumentor",
    "OpenAIInstrumentor",
    "AnthropicInstrumentor",
    "CohereInstrumentor",
    "MistralInstrumentor",
    "GroqInstrumentor",
    "TogetherInstrumentor",
    "GoogleGenAIInstrumentor",
    # Settings / types
    "SecurityMode",
    # Public type aliases — lift `dict[str, Any]` and free strings off
    # the function signatures users actually touch. See `_types.py`.
    "Policy",
    "PolicyRule",
    "PolicyAction",
    "PolicyDefault",
    "DegradationReason",
    "HealthCheck",
    "HealthStatus",
    "TelemetryDiagnostics",
    # Correlation key for the SDK's per-call request-id stamped on
    # every wrapped ``httpx.Response.extensions``. Read via:
    #   request_id = response.extensions.get(CHECKRD_REQUEST_ID_KEY)
    "CHECKRD_REQUEST_ID_KEY",
    # Errors — base
    "CheckrdError",
    # Errors — SDK-local
    "CheckrdInitError",
    "CheckrdPolicyDenied",
    "PolicySignatureError",
    # Errors — control-plane API
    "APIError",
    "APIStatusError",
    "APIConnectionError",
    "APITimeoutError",
    "APIResponseValidationError",
    "APIUserAbortError",
    # Errors — status-code subclasses
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    # Errors — dispatch
    "make_api_error",
    # Identity
    "IdentityProvider",
    "LocalIdentity",
    "ExternalIdentity",
    # Sinks
    "TelemetrySink",
    "JsonFileSink",
    "LoggingSink",
    "ControlPlaneSink",
    "OtlpSink",
    "OTelSpanSink",
    # Watchers
    "PolicyFileWatcher",
    "KillSwitchFileWatcher",
    # Settings
    "Settings",
    # Global-context accessors (post-init)
    "get_engine",
    "get_sink",
    "has_context",
    "__version__",
]

logger = logging.getLogger("checkrd")
# Rate-limit internal warnings (Datadog DDLogger pattern). Prevents log
# flooding when control plane is down or telemetry sends are failing.
# 1 message per 60s per unique call site, with "[N skipped]" suffix.
logger.addFilter(RateLimitFilter(rate_limit_secs=60))

# Redact sensitive headers from log output (OpenAI SensitiveHeadersFilter
# pattern). Applies to checkrd, httpx, and httpcore loggers so that
# enabling DEBUG logging never leaks API keys or auth tokens to log
# files, stdout, or monitoring systems.
_sensitive_filter = SensitiveHeadersFilter()
logger.addFilter(_sensitive_filter)
for _http_logger_name in ("httpx", "httpcore"):
    logging.getLogger(_http_logger_name).addFilter(_sensitive_filter)

def _no_throw(default: Any = None) -> Callable[[_F], _F]:
    """Decorator that catches unexpected exceptions from public SDK methods.

    Follows the PostHog ``@no_throw()`` pattern: observability/proxy SDKs
    must never crash the host application. Exceptions are logged at WARNING
    level and the ``default`` value is returned.

    Does NOT swallow ``CheckrdPolicyDenied`` or ``CheckrdInitError`` (those
    are deliberate user-facing signals) or ``KeyboardInterrupt``/``SystemExit``.
    """
    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except (CheckrdPolicyDenied, CheckrdInitError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                logger.warning(
                    "checkrd: %s() raised unexpectedly; returning default",
                    fn.__name__,
                    exc_info=True,
                )
                return default
        return wrapper  # type: ignore[return-value]
    return decorator


_OBSERVATION_MODE_POLICY: dict[str, Any] = {
    "agent": "checkrd-observation",
    "default": "allow",
    "rules": [],
}


# ============================================================================
# wrap() / wrap_async()
# ============================================================================


def wrap(
    client: httpx.Client,
    *,
    agent_id: Union[str, None] = None,
    policy: Union[str, Path, "Policy", None] = None,
    identity: Union[IdentityProvider, None] = None,
    enforce: EnforceMode = "auto",
    control_plane_url: Union[str, None] = None,
    api_key: Union[str, None] = None,
    telemetry_sink: Union[TelemetrySink, None] = None,
    on_deny: Optional[OnDenyHook] = None,
    on_allow: Optional[OnAllowHook] = None,
    before_request: Optional[BeforeRequestHook] = None,
    on_telemetry_drop: Optional[OnDropCallback] = None,
    policy_watch: bool = False,
    policy_watch_interval_secs: float = 5.0,
    killswitch_file: Union[str, Path, None] = None,
    killswitch_poll_interval_secs: float = 5.0,
    security_mode: Optional[SecurityMode] = None,
    max_retries: int = 3,
    timeout: float = 30.0,
    connect_timeout: float = 5.0,
) -> httpx.Client:
    """Wrap an httpx.Client with Checkrd policy enforcement.

    All keyword arguments fall back to environment variables when omitted.

    Args:
        security_mode: ``"strict"`` (default, fail-closed) raises
            :class:`CheckrdInitError` if the WASM engine cannot load.
            ``"permissive"`` logs a warning and returns the client
            unwrapped. Env-var override: ``CHECKRD_SECURITY_MODE``.
        on_telemetry_drop: Optional ``(reason, count) -> None`` callback
            fired when the batcher drops events. ``reason`` is one of
            ``"backpressure"``, ``"signing_error"``, ``"send_error"``.
            Exceptions from the callback are swallowed (and logged) so a
            buggy callback cannot crash the hot path. Ignored when
            ``telemetry_sink`` is explicitly set — the caller's sink
            owns its error handling.
    """
    runtime = _build_runtime(
        agent_id=agent_id, policy=policy, identity=identity, enforce=enforce,
        control_plane_url=control_plane_url, api_key=api_key,
        telemetry_sink=telemetry_sink, security_mode=security_mode,
        on_telemetry_drop=on_telemetry_drop,
        max_retries=max_retries, timeout=timeout, connect_timeout=connect_timeout,
    )
    if runtime is None:
        return client

    client._transport = CheckrdTransport(
        client._transport, runtime.engine,
        enforce=runtime.effective_enforce, batcher=runtime.sink,
        agent_id=runtime.settings.agent_id,
        dashboard_url=runtime.settings.dashboard_url or "",
        on_deny=on_deny, on_allow=on_allow, before_request=before_request,
        security_mode=runtime.settings.security_mode,
    )
    _maybe_start_control(
        runtime.engine, runtime.settings.agent_id,
        runtime.settings.control_plane_url, runtime.settings.api_key, client,
        api_version=runtime.settings.api_version,
        circuit_breaker=runtime.breaker,
    )
    _maybe_register_public_key(
        runtime.settings.control_plane_url, runtime.settings.api_key,
        runtime.settings.agent_id, runtime.identity,
        api_version=runtime.settings.api_version,
        max_retries=max_retries, timeout=timeout,
    )
    _maybe_start_watchers(
        client, runtime.engine, policy if policy_watch else None,
        policy_watch_interval_secs, killswitch_file, killswitch_poll_interval_secs,
    )
    if runtime.sink is not None:
        client._checkrd_batcher = runtime.sink  # type: ignore[attr-defined]
    return client


def wrap_async(
    client: httpx.AsyncClient,
    *,
    agent_id: Union[str, None] = None,
    policy: Union[str, Path, "Policy", None] = None,
    identity: Union[IdentityProvider, None] = None,
    enforce: EnforceMode = "auto",
    control_plane_url: Union[str, None] = None,
    api_key: Union[str, None] = None,
    telemetry_sink: Union[TelemetrySink, None] = None,
    on_deny: Optional[OnDenyHook] = None,
    on_allow: Optional[OnAllowHook] = None,
    before_request: Optional[BeforeRequestHook] = None,
    on_telemetry_drop: Optional[OnDropCallback] = None,
    policy_watch: bool = False,
    policy_watch_interval_secs: float = 5.0,
    killswitch_file: Union[str, Path, None] = None,
    killswitch_poll_interval_secs: float = 5.0,
    security_mode: Optional[SecurityMode] = None,
    use_async_batcher: bool = True,
    max_retries: int = 3,
    timeout: float = 30.0,
    connect_timeout: float = 5.0,
) -> httpx.AsyncClient:
    """Wrap an httpx.AsyncClient. See :func:`wrap` for docs.

    :param use_async_batcher: When True (the default for async callers),
        the runtime uses :class:`AsyncTelemetryBatcher` instead of the
        thread-based ``TelemetryBatcher``. Async apps avoid
        thread-context-switch overhead per enqueue, and shutdown is
        coordinated via ``await aclose()`` instead of ``thread.join()``.
        Set to ``False`` for the legacy thread-based behavior — useful
        when running under uvloop or trio where structured-concurrency
        cancellation differs from CPython asyncio's.
    """
    runtime = _build_runtime(
        agent_id=agent_id, policy=policy, identity=identity, enforce=enforce,
        control_plane_url=control_plane_url, api_key=api_key,
        telemetry_sink=telemetry_sink, security_mode=security_mode,
        on_telemetry_drop=on_telemetry_drop,
        use_async_batcher=use_async_batcher,
        max_retries=max_retries, timeout=timeout, connect_timeout=connect_timeout,
    )
    if runtime is None:
        return client

    client._transport = CheckrdAsyncTransport(
        client._transport, runtime.engine,
        enforce=runtime.effective_enforce, batcher=runtime.sink,
        agent_id=runtime.settings.agent_id,
        dashboard_url=runtime.settings.dashboard_url or "",
        on_deny=on_deny, on_allow=on_allow, before_request=before_request,
        security_mode=runtime.settings.security_mode,
    )
    # Async wrap path: use ``AsyncControlReceiver`` so the SSE
    # connection runs as an asyncio Task instead of paying a
    # thread-context-switch on every event. Falls back to the sync
    # receiver when no event loop is present (callers that wrap an
    # ``httpx.AsyncClient`` outside an event loop are unusual but
    # legal — that path keeps working).
    _maybe_start_async_control(
        runtime.engine, runtime.settings.agent_id,
        runtime.settings.control_plane_url, runtime.settings.api_key, client,
        api_version=runtime.settings.api_version,
        circuit_breaker=runtime.breaker,
    )
    _maybe_register_public_key(
        runtime.settings.control_plane_url, runtime.settings.api_key,
        runtime.settings.agent_id, runtime.identity,
        api_version=runtime.settings.api_version,
        max_retries=max_retries, timeout=timeout,
    )
    _maybe_start_watchers(
        client, runtime.engine, policy if policy_watch else None,
        policy_watch_interval_secs, killswitch_file, killswitch_poll_interval_secs,
    )
    if runtime.sink is not None:
        client._checkrd_batcher = runtime.sink  # type: ignore[attr-defined]
    return client


# ============================================================================
# Internal: policy, engine, runtime
# ============================================================================


def _resolve_policy(
    policy: Union[str, Path, "Policy", None], agent_id: str,
) -> tuple[str, bool]:
    if policy is not None:
        return load_config(policy=policy), True
    try:
        return load_config(policy=None), True
    except CheckrdInitError:
        obs = dict(_OBSERVATION_MODE_POLICY)
        obs["agent"] = agent_id
        logger.info(
            "checkrd: no policy configured — running in observation mode. "
            "Pass policy=... to wrap() or create ~/.checkrd/policy.yaml."
        )
        return json.dumps(obs), False


def _resolve_effective_enforce(settings: Settings, policy_was_explicit: bool) -> bool:
    """Resolve the transport's per-request enforce decision.

    The semantic mirrors what every comparable enforcement-point does
    (OPA-PEPs, Envoy ext_authz, Stripe Radar, AWS Config, Cloudflare
    WAF): **the engine is the authority on enforce-vs-dry-run, the
    transport just acts on the verdict.** Our policy schema's `mode`
    field is honored *inside* the WASM engine — `mode: dry_run` makes
    `evaluate_request` return `allowed=true` even when a deny rule
    matches (see crates/core PolicyMode::DryRun). So when the engine
    returns `allowed=false`, the operator's documented intent was to
    block; if the transport second-guesses by observing-anyway it
    silently breaks the user's published policy.

    Resolution rules:
      - `enforce_override = True/False` (operator explicit) → wins.
      - `enforce_override = None` (default `enforce="auto"`) → trust
        the engine: return `True`. Engines without a policy still
        return `allowed=true` (default-allow), so blanket-enforce is
        safe at boot. Once a `mode: dry_run` policy is installed, the
        engine returns `allowed=true` for matched-deny rules anyway,
        so the transport's "block on deny" never fires — same outcome,
        just routed through the engine instead of the transport.

    Why this used to be `policy_was_explicit`:
        Before signed policies arrived via SSE, "no constructor policy"
        meant "this SDK never sees policy at all" — so observation was
        a safe default. Now that the control plane delivers policies
        post-construction, that proxy is wrong: every customer using
        the dashboard would see denies-without-blocking, breaking the
        "I authored a deny rule, expect denials to actually deny"
        contract. The fix here is the architectural one — see
        scripts/demo-sdk-telemetry.py for an explicit-`enforce=True`
        override if you want to be belt-and-suspenders.
    """
    if settings.enforce_override is not None:
        return settings.enforce_override
    # Default to enforce. The engine respects the policy's `mode`
    # (dry_run returns allowed=true), and an unloaded engine returns
    # allowed=true via its default-allow boot policy — so this default
    # is safe whether or not a policy is ever installed.
    _ = policy_was_explicit  # kept for signature stability + tests
    return True


def _create_engine_from_json(
    policy_json: str, agent_id: str, identity: IdentityProvider,
) -> WasmEngine:
    # For LocalIdentity, use _private_key_ref() to get the mutable bytearray
    # directly. This avoids creating an immutable bytes() copy that cannot be
    # zeroized — the bytearray is the same object that bind_engine() will
    # later zero out. WasmEngine.__init__ accepts Union[bytes, bytearray].
    if isinstance(identity, LocalIdentity):
        private_key = identity._private_key_ref() or b""
        engine = WasmEngine(policy_json, agent_id, private_key_bytes=private_key, instance_id="")
        identity.bind_engine(engine)
    else:
        # ExternalIdentity: private_key_bytes is None (KMS mode), pass
        # instance_id so the WASM core can identify the signer.
        private_key = identity.private_key_bytes or b""
        instance_id = identity.instance_id if identity.private_key_bytes is None else ""
        engine = WasmEngine(policy_json, agent_id, private_key_bytes=private_key, instance_id=instance_id)
    return engine


@dataclass
class _Runtime:
    engine: WasmEngine
    identity: IdentityProvider
    sink: Any
    effective_enforce: bool
    settings: Settings
    # Single CircuitBreaker shared between the telemetry batcher and
    # the control-plane SSE receiver. When the batcher trips it (5xx /
    # network failure on telemetry POSTs), the receiver's reconnect
    # loop short-circuits — no point burning a 90-second SSE read
    # timeout to confirm what we already know. One source of truth for
    # control-plane health, mirroring the AWS SDK / OkHttp pattern.
    breaker: CircuitBreaker


def _build_runtime(
    *,
    agent_id: Union[str, None],
    policy: Union[str, Path, "Policy", None],
    identity: Union[IdentityProvider, None],
    enforce: EnforceMode,
    control_plane_url: Union[str, None],
    api_key: Union[str, None],
    telemetry_sink: Union[TelemetrySink, None],
    debug: bool = False,
    security_mode: Optional[SecurityMode] = None,
    on_telemetry_drop: Optional[OnDropCallback] = None,
    api_version: Optional[str] = None,
    use_async_batcher: bool = False,
    max_retries: int = 3,
    timeout: float = 30.0,
    connect_timeout: float = 5.0,
) -> Union[_Runtime, None]:
    """Resolve settings and build runtime.

    Returns ``None`` only when the SDK is disabled via ``CHECKRD_DISABLED``
    or when ``security_mode="permissive"`` and the engine failed to load.
    In strict mode (the default) engine failures propagate as
    :class:`CheckrdInitError` — the security layer must not silently
    disable itself.
    """
    settings = resolve(
        agent_id=agent_id, api_key=api_key,
        control_plane_url=control_plane_url, enforce=enforce,
        debug=debug, security_mode=security_mode,
        api_version=api_version,
    )
    # Operator-facing PII banner fires BEFORE the disabled short-circuit.
    # Case we're guarding: operator turned on CHECKRD_DEBUG=1 AND has
    # CHECKRD_DISABLED=1 in some rollback scenario — they still want
    # to know that re-enabling Checkrd would route prompt payloads
    # through debug logs. Fires on stderr once per process when any
    # Checkrd entry point observes ``debug=True`` or ``CHECKRD_DEBUG=1``.
    if settings.debug:
        from checkrd._logging import warn_debug_pii_risk
        warn_debug_pii_risk()

    if settings.disabled:
        logger.info("checkrd: disabled via CHECKRD_DISABLED")
        return None

    # Policy resolution is separate from engine construction. A bad
    # explicit policy is a user error and must raise. Engine failures
    # are gated by security_mode.
    policy_json, policy_was_explicit = _resolve_policy(policy, settings.agent_id)
    effective_enforce = _resolve_effective_enforce(settings, policy_was_explicit)
    identity_provider: IdentityProvider = identity if identity is not None else LocalIdentity()

    try:
        engine = _create_engine_from_json(policy_json, settings.agent_id, identity_provider)
    except CheckrdInitError:
        # A user-supplied policy that the engine rejects is always a loud
        # failure, regardless of security_mode — it's caller error, not an
        # infrastructure outage.
        if policy is not None:
            raise
        if settings.security_mode == "strict":
            # Fail-closed: the engine is the security layer. If it won't
            # start, we do NOT silently pass traffic through.
            raise
        logger.warning(
            "checkrd: engine failed to load; running in pass-through mode "
            "(security_mode='permissive'). Policy enforcement is DISABLED "
            "but your application works normally.",
        )
        set_degraded(True)
        return None
    except Exception as exc:
        if settings.security_mode == "strict":
            raise CheckrdInitError(
                f"engine failed to load: {exc}. Set security_mode='permissive' "
                f"or CHECKRD_SECURITY_MODE=permissive to opt in to pass-through "
                f"degradation during rollout."
            ) from exc
        logger.warning(
            "checkrd: engine failed to load (%s); running in pass-through mode "
            "(security_mode='permissive'). Policy enforcement is DISABLED "
            "but your application works normally.",
            exc,
        )
        set_degraded(True)
        return None

    # One breaker per process, shared by the batcher and the
    # control-plane SSE receiver. Default thresholds (5 failures →
    # open, 30s ± 5s reset window) match the JS SDK so cross-language
    # operator dashboards see consistent behaviour.
    breaker = CircuitBreaker()
    sink = _resolve_sink(
        telemetry_sink, settings.control_plane_url, settings.api_key,
        engine, settings.agent_id,
        on_drop=on_telemetry_drop,
        api_version=settings.api_version,
        use_async=use_async_batcher,
        max_retries=max_retries,
        timeout=timeout,
        connect_timeout=connect_timeout,
        circuit_breaker=breaker,
    )
    return _Runtime(
        engine=engine, identity=identity_provider, sink=sink,
        effective_enforce=effective_enforce, settings=settings,
        breaker=breaker,
    )


# ============================================================================
# init() / shutdown()
# ============================================================================


def init(
    *,
    agent_id: Union[str, None] = None,
    policy: Union[str, Path, "Policy", None] = None,
    identity: Union[IdentityProvider, None] = None,
    enforce: EnforceMode = "auto",
    control_plane_url: Union[str, None] = None,
    api_key: Union[str, None] = None,
    telemetry_sink: Union[TelemetrySink, None] = None,
    on_deny: Optional[OnDenyHook] = None,
    on_allow: Optional[OnAllowHook] = None,
    before_request: Optional[BeforeRequestHook] = None,
    on_telemetry_drop: Optional[OnDropCallback] = None,
    debug: bool = False,
    policy_watch: bool = False,
    policy_watch_interval_secs: float = 5.0,
    killswitch_file: Union[str, Path, None] = None,
    killswitch_poll_interval_secs: float = 5.0,
    security_mode: Optional[SecurityMode] = None,
    max_retries: int = 3,
    timeout: float = 30.0,
    connect_timeout: float = 5.0,
) -> _InitContextManager:
    """Initialize the global Checkrd runtime for :func:`instrument`.

    Pass ``debug=True`` (or set ``CHECKRD_DEBUG=1``) to enable per-request
    evaluation traces at DEBUG level.

    ``security_mode`` defaults to ``"strict"`` (fail-closed). Engine init
    failures raise :class:`CheckrdInitError` rather than silently disabling
    the security layer. Set ``security_mode="permissive"`` (or the env var
    ``CHECKRD_SECURITY_MODE=permissive``) to opt in to pass-through on
    failure during initial rollout.

    Returns a context manager for automatic cleanup::

        with checkrd.init(policy="policy.yaml"):
            checkrd.instrument()
            # ... use instrumented clients ...
        # shutdown() called automatically
    """
    set_degraded(False)  # reset on re-init

    runtime = _build_runtime(
        agent_id=agent_id, policy=policy, identity=identity, enforce=enforce,
        control_plane_url=control_plane_url, api_key=api_key,
        telemetry_sink=telemetry_sink, debug=debug,
        security_mode=security_mode,
        on_telemetry_drop=on_telemetry_drop,
        max_retries=max_retries, timeout=timeout, connect_timeout=connect_timeout,
    )
    if runtime is None:
        return _InitContextManager()  # disabled or degraded

    # Configure debug logging when requested.
    if runtime.settings.debug:
        checkrd_logger = logging.getLogger("checkrd")
        if not checkrd_logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            checkrd_logger.addHandler(handler)
        checkrd_logger.setLevel(logging.DEBUG)

    with with_lock():
        if has_context():
            try:
                get_context().shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkrd: previous context cleanup failed: %s", exc)

        ctx = _GlobalContext(
            engine=runtime.engine, identity=runtime.identity,
            sink=runtime.sink, enforce=runtime.effective_enforce,
            settings=runtime.settings,
            on_deny=on_deny, on_allow=on_allow, before_request=before_request,
        )
        set_context(ctx)

    _maybe_register_public_key(
        runtime.settings.control_plane_url, runtime.settings.api_key,
        runtime.settings.agent_id, runtime.identity,
        api_version=runtime.settings.api_version,
        max_retries=max_retries, timeout=timeout,
    )
    _global_maybe_start_control(ctx, runtime)
    _global_maybe_start_watchers(
        ctx, runtime, policy if policy_watch else None,
        policy_watch_interval_secs, killswitch_file, killswitch_poll_interval_secs,
    )
    return _InitContextManager()


class _InitContextManager:
    """Context manager returned by ``init()`` for automatic cleanup.

    Supports the LaunchDarkly ``with LDClient(config) as client:`` pattern::

        with checkrd.init(policy="policy.yaml"):
            checkrd.instrument()
            # ... use instrumented clients ...
        # shutdown() called automatically on exit

    Also works as a plain call (returns are ignored)::

        checkrd.init(policy="policy.yaml")
    """

    def __enter__(self) -> "_InitContextManager":
        return self

    def __exit__(self, *args: object) -> None:
        shutdown()


@_no_throw()
def shutdown() -> None:
    """Tear down the global runtime. Safe for atexit/finally."""
    with with_lock():
        if has_context():
            try:
                get_context().shutdown()
            finally:
                set_context(None)
        set_degraded(False)


def get_engine() -> WasmEngine:
    """Return the live WASM engine installed by :func:`init`.

    Framework adapters (LangChain, OpenAI Agents, Claude Agent SDK,
    MCP) take an ``engine`` argument — this is the canonical way to
    fetch it after a global :func:`init`. Mirrors the JS SDK's
    ``getEngine()`` so cross-language docs share one pattern.

    Raises:
        CheckrdInitError: If :func:`init` has not been called yet.
    """
    if not has_context():
        raise CheckrdInitError(
            "checkrd.get_engine(): call checkrd.init(...) first.",
        )
    return get_context().engine


def get_sink() -> Optional[TelemetrySink]:
    """Return the live telemetry sink, or ``None`` when no control plane is configured."""
    if not has_context():
        return None
    return get_context().sink


@_no_throw(default={"status": "error", "engine_loaded": False})
def healthy() -> HealthCheck:
    """Return a health check dict for monitoring and K8s readiness probes.

    Returns a dict with keys: ``status`` (``"healthy"``, ``"degraded"``,
    or ``"disabled"``), ``engine_loaded``, ``control_plane_connected``,
    ``agent_id``, ``enforce``, ``last_eval_at``.
    """
    if is_degraded():
        # ``HealthCheck(...)`` is the TypedDict-as-callable form
        # (PEP 589) — pyright narrows each kwarg to the declared
        # field type, including the ``status`` Literal. Cleaner than
        # ``cast(HealthCheck, {...})`` and catches typos at the call
        # site (a misnamed kwarg fails the type check).
        #
        # ``set_degraded(True)`` is set by ``_build_runtime`` only when
        # the WASM engine refuses to load AND we're in permissive mode
        # (strict mode raises). So the reason at this layer is always
        # ``wasm_failed``; other degradation modes (control plane
        # unreachable, signing unavailable, etc.) keep ``status="healthy"``
        # and surface via the post-context branch below.
        return HealthCheck(
            status="degraded",
            degradation_reason="wasm_failed",
            engine_loaded=False,
            control_plane_connected=None,
            agent_id=None,
            enforce=None,
            last_eval_at=get_last_eval_at(),
        )
    if not has_context():
        return HealthCheck(
            status="disabled",
            degradation_reason=None,
            engine_loaded=False,
            control_plane_connected=None,
            agent_id=None,
            enforce=None,
            last_eval_at=get_last_eval_at(),
        )
    ctx = get_context()
    cp_connected: Optional[bool] = None
    if ctx.settings.has_control_plane:
        cp_connected = ctx.control_receiver is not None
    # Telemetry pipeline self-diagnostics (Sentry client-reports pattern).
    # ``TelemetrySink`` is a Protocol that does not declare ``diagnostics``
    # — it's an optional extension implemented by the batcher's sink.
    # The hasattr guard pins the dynamic check; the cast tells pyright
    # the call is safe.
    telemetry_stats: Optional[TelemetryDiagnostics] = None
    sink = ctx.sink
    # ``TelemetrySink`` is a Protocol covering only ``enqueue``;
    # ``diagnostics`` is an optional extension some sinks expose.
    # ``getattr`` is the right idiom for an optional attribute: mypy
    # and pyright disagree on whether ``hasattr`` narrows attribute
    # access (mypy yes, pyright no), but both treat
    # ``getattr(x, name, default)`` uniformly.
    diag_fn: Optional[Callable[[], TelemetryDiagnostics]] = (
        getattr(sink, "diagnostics", None) if sink is not None else None
    )
    if diag_fn is not None:
        telemetry_stats = diag_fn()
    # Classify post-init degradation. The engine loaded fine and a
    # context exists; what's potentially wrong now is the runtime
    # plumbing (control plane reachability, signing key availability,
    # telemetry backpressure). Status stays ``"healthy"`` unless one
    # of these crosses a threshold; the reason populates either way
    # so dashboards can warn before the threshold trips.
    degradation = _classify_degradation(ctx, cp_connected, telemetry_stats)
    return HealthCheck(
        status=degradation[0],
        degradation_reason=degradation[1],
        engine_loaded=True,
        control_plane_connected=cp_connected,
        agent_id=ctx.settings.agent_id,
        enforce=ctx.enforce,
        last_eval_at=get_last_eval_at(),
        telemetry=telemetry_stats,
    )


def _classify_degradation(
    ctx: Any,
    cp_connected: Optional[bool],
    telemetry: Optional[TelemetryDiagnostics],
) -> tuple[HealthStatus, Optional[DegradationReason]]:
    """Map runtime state to a (status, reason) pair.

    Order matters: more-severe degradations win. Circuit breaker open
    is the strongest signal because it means the SDK is actively
    fast-failing telemetry; telemetry-dropping is weaker because it
    can be transient backpressure under load.
    """
    # Shared circuit breaker (batcher + receiver) tripped — fast-fail
    # mode. Highest-severity degradation we surface today.
    breaker = getattr(ctx, "breaker", None)
    if breaker is not None:
        diag = breaker.diagnostics()
        if diag.get("state") == "open":
            return ("degraded", "control_plane_circuit_open")
    # Telemetry diagnostics — sustained signing or send errors mean
    # something is misconfigured (signing) or unreachable (send).
    if telemetry is not None:
        if telemetry["dropped_signing_error"] > 0 and telemetry["sent"] == 0:
            return ("degraded", "signing_unavailable")
        # Active control plane configured but no successful sends and
        # we have unsent events accumulating → unreachable.
        if (
            cp_connected is False
            and telemetry["sent"] == 0
            and (telemetry["pending"] > 0 or telemetry["dropped_send_error"] > 0)
        ):
            return ("degraded", "control_plane_unreachable")
        # Backpressure passed a heuristic threshold (more drops than
        # successful sends).
        if (
            telemetry["dropped_backpressure"] > 0
            and telemetry["dropped_backpressure"] > telemetry["sent"]
        ):
            return ("degraded", "telemetry_dropping")
    return ("healthy", None)


def _global_maybe_start_control(ctx: _GlobalContext, runtime: _Runtime) -> None:
    if not (runtime.settings.control_plane_url and runtime.settings.api_key):
        return
    from checkrd.control import ControlReceiver
    receiver = ControlReceiver(
        base_url=runtime.settings.control_plane_url,
        agent_id=runtime.settings.agent_id,
        api_key=runtime.settings.api_key,
        engine=runtime.engine,
        api_version=runtime.settings.api_version,
        circuit_breaker=runtime.breaker,
    )
    receiver.start()
    ctx.control_receiver = receiver


def _global_maybe_start_watchers(
    ctx: _GlobalContext, runtime: _Runtime,
    policy: Union[str, Path, "Policy", None],
    policy_interval: float,
    killswitch_file: Union[str, Path, None],
    killswitch_interval: float,
) -> None:
    if policy is not None and isinstance(policy, (str, Path)):
        try:
            w = PolicyFileWatcher(runtime.engine, policy, interval_secs=policy_interval)
            w.start()
            ctx.watchers.append(w)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkrd: policy watcher failed: %s", exc)
    if killswitch_file is not None:
        try:
            ks = KillSwitchFileWatcher(
                runtime.engine, killswitch_file, interval_secs=killswitch_interval,
            )
            ks.start()
            ctx.watchers.append(ks)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkrd: kill switch watcher failed: %s", exc)


# ============================================================================
# instrument() / uninstrument()
# ============================================================================

_OPENAI_INSTRUMENTOR: Union[OpenAIInstrumentor, None] = None
_ANTHROPIC_INSTRUMENTOR: Union[AnthropicInstrumentor, None] = None
_COHERE_INSTRUMENTOR: Union[CohereInstrumentor, None] = None
_MISTRAL_INSTRUMENTOR: Union[MistralInstrumentor, None] = None
_GROQ_INSTRUMENTOR: Union[GroqInstrumentor, None] = None
_TOGETHER_INSTRUMENTOR: Union[TogetherInstrumentor, None] = None
_GOOGLE_GENAI_INSTRUMENTOR: Union[GoogleGenAIInstrumentor, None] = None


def _get(attr: str, cls: type) -> Any:
    """Lazy singleton for instrumentors."""
    g = globals()
    if g[attr] is None:
        g[attr] = cls()
    return g[attr]


@_no_throw()
def instrument() -> None:
    """Auto-instrument every detected AI library. Requires :func:`init`."""
    if is_degraded():
        logger.debug("checkrd: degraded mode; instrument() is a no-op")
        return
    get_context()
    for attr, cls in _ALL_INSTRUMENTORS:
        _get(attr, cls).instrument()


@_no_throw()
def uninstrument() -> None:
    """Revert all patches."""
    for attr, cls in _ALL_INSTRUMENTORS:
        _get(attr, cls).uninstrument()


_ALL_INSTRUMENTORS: list[tuple[str, type]] = [
    ("_OPENAI_INSTRUMENTOR", OpenAIInstrumentor),
    ("_ANTHROPIC_INSTRUMENTOR", AnthropicInstrumentor),
    ("_COHERE_INSTRUMENTOR", CohereInstrumentor),
    ("_MISTRAL_INSTRUMENTOR", MistralInstrumentor),
    ("_GROQ_INSTRUMENTOR", GroqInstrumentor),
    ("_TOGETHER_INSTRUMENTOR", TogetherInstrumentor),
    ("_GOOGLE_GENAI_INSTRUMENTOR", GoogleGenAIInstrumentor),
]


def _instrument_one(attr: str, cls: type) -> None:
    if is_degraded():
        return
    get_context()
    _get(attr, cls).instrument()


def _uninstrument_one(attr: str, cls: type) -> None:
    _get(attr, cls).uninstrument()


def instrument_openai() -> None:
    _instrument_one("_OPENAI_INSTRUMENTOR", OpenAIInstrumentor)

def uninstrument_openai() -> None:
    _uninstrument_one("_OPENAI_INSTRUMENTOR", OpenAIInstrumentor)

def instrument_anthropic() -> None:
    _instrument_one("_ANTHROPIC_INSTRUMENTOR", AnthropicInstrumentor)

def uninstrument_anthropic() -> None:
    _uninstrument_one("_ANTHROPIC_INSTRUMENTOR", AnthropicInstrumentor)

def instrument_cohere() -> None:
    _instrument_one("_COHERE_INSTRUMENTOR", CohereInstrumentor)

def uninstrument_cohere() -> None:
    _uninstrument_one("_COHERE_INSTRUMENTOR", CohereInstrumentor)

def instrument_mistral() -> None:
    _instrument_one("_MISTRAL_INSTRUMENTOR", MistralInstrumentor)

def uninstrument_mistral() -> None:
    _uninstrument_one("_MISTRAL_INSTRUMENTOR", MistralInstrumentor)

def instrument_groq() -> None:
    _instrument_one("_GROQ_INSTRUMENTOR", GroqInstrumentor)

def uninstrument_groq() -> None:
    _uninstrument_one("_GROQ_INSTRUMENTOR", GroqInstrumentor)

def instrument_together() -> None:
    _instrument_one("_TOGETHER_INSTRUMENTOR", TogetherInstrumentor)

def uninstrument_together() -> None:
    _uninstrument_one("_TOGETHER_INSTRUMENTOR", TogetherInstrumentor)

def instrument_google_genai() -> None:
    _instrument_one("_GOOGLE_GENAI_INSTRUMENTOR", GoogleGenAIInstrumentor)

def uninstrument_google_genai() -> None:
    _uninstrument_one("_GOOGLE_GENAI_INSTRUMENTOR", GoogleGenAIInstrumentor)


# ============================================================================
# Per-client helpers
# ============================================================================


def _maybe_create_batcher(
    control_plane_url: Union[str, None], api_key: Union[str, None],
    engine: WasmEngine, agent_id: str,
    on_drop: Optional[OnDropCallback] = None,
    api_version: str = "",
    *,
    use_async: bool = False,
    max_retries: int = 3,
    timeout: float = 30.0,
    connect_timeout: float = 5.0,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> Any:
    """Build the right telemetry batcher for the caller's runtime.

    When ``use_async=True`` (set by :func:`wrap_async` and the
    :class:`AsyncCheckrd` class), constructs an
    :class:`AsyncTelemetryBatcher` that runs on the asyncio event
    loop instead of a daemon thread. The async variant has the same
    contract (``enqueue`` is sync, ``flush`` and ``stop`` are async)
    and the same diagnostics / circuit-breaker hooks — async apps
    just don't pay the thread-context-switch overhead per enqueue.

    ``circuit_breaker`` is optional; when supplied, the batcher
    *and* the SSE receiver share it so a single control-plane outage
    trips the breaker once for both subsystems.
    """
    if not (control_plane_url and api_key):
        return None
    if use_async:
        from checkrd._async_batcher import AsyncTelemetryBatcher

        return AsyncTelemetryBatcher(
            base_url=control_plane_url, api_key=api_key,
            engine=engine, signer_agent_id=agent_id,
            on_drop=on_drop,
            api_version=api_version,
            max_attempts=max_retries,
            request_timeout_secs=timeout,
            connect_timeout_secs=connect_timeout,
            circuit_breaker=circuit_breaker,
        )
    from checkrd.batcher import TelemetryBatcher
    return TelemetryBatcher(
        base_url=control_plane_url, api_key=api_key,
        engine=engine, signer_agent_id=agent_id,
        on_drop=on_drop,
        api_version=api_version,
        max_attempts=max_retries,
        request_timeout_secs=timeout,
        circuit_breaker=circuit_breaker,
    )


def _resolve_sink(
    explicit: Union[TelemetrySink, None], control_plane_url: Union[str, None],
    api_key: Union[str, None], engine: WasmEngine, agent_id: str,
    on_drop: Optional[OnDropCallback] = None,
    api_version: str = "",
    *,
    use_async: bool = False,
    max_retries: int = 3,
    timeout: float = 30.0,
    connect_timeout: float = 5.0,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> Any:
    if explicit is not None:
        # Explicit sink wins — if the caller brought their own
        # TelemetrySink they also own its error handling, and our
        # on_drop has no plausible hookup target.
        return explicit
    return _maybe_create_batcher(
        control_plane_url, api_key, engine, agent_id,
        on_drop=on_drop, api_version=api_version,
        use_async=use_async,
        max_retries=max_retries, timeout=timeout, connect_timeout=connect_timeout,
        circuit_breaker=circuit_breaker,
    )


def _maybe_start_watchers(
    client: object, engine: WasmEngine,
    policy: Union[str, Path, "Policy", None], policy_interval: float,
    killswitch_file: Union[str, Path, None], killswitch_interval: float,
) -> None:
    watchers: list[Any] = []
    if policy is not None and isinstance(policy, (str, Path)):
        try:
            pw = PolicyFileWatcher(engine, policy, interval_secs=policy_interval)
            pw.start()
            watchers.append(pw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkrd: policy watcher failed: %s", exc)
    if killswitch_file is not None:
        try:
            ks = KillSwitchFileWatcher(engine, killswitch_file, interval_secs=killswitch_interval)
            ks.start()
            watchers.append(ks)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkrd: kill switch watcher failed: %s", exc)
    if watchers:
        client._checkrd_watchers = watchers  # type: ignore[attr-defined]


# Public key registration retry config (Stripe-style exponential backoff).
# ``_PK_REGISTER_MAX_RETRIES`` is the default for the optional
# ``max_retries`` arg to :func:`_maybe_register_public_key`. Kept as a
# module-level constant so tests and observability code can reference
# the documented default without recreating it.
_PK_REGISTER_MAX_RETRIES = 3
_PK_REGISTER_INITIAL_DELAY = 1.0  # seconds
_PK_REGISTER_MAX_DELAY = 10.0  # seconds


def _maybe_register_public_key(
    control_plane_url: Union[str, None], api_key: Union[str, None],
    agent_id: str, identity: IdentityProvider,
    api_version: str = "",
    *,
    max_retries: int = _PK_REGISTER_MAX_RETRIES,
    timeout: float = 5.0,
) -> None:
    if not control_plane_url or not api_key:
        return
    try:
        public_key = identity.public_key
    except Exception:
        return
    if not public_key:
        return

    def _register() -> None:
        import secrets
        import time

        from checkrd._platform import default_control_headers, new_idempotency_key

        url = f"{control_plane_url.rstrip('/')}/v1/agents/{agent_id}/public-key"
        body = json.dumps({"public_key": public_key.hex()}).encode("utf-8")
        # Stripe-style idempotency: same key across every retry so the
        # control plane can dedupe. Generated ONCE before the retry
        # loop — the full consolidated header set (platform family,
        # User-Agent, optional Checkrd-Version) is reused on every
        # attempt.
        idempotency_key = new_idempotency_key()
        headers = default_control_headers(
            api_key,
            api_version=api_version,
            idempotency_key=idempotency_key,
        )

        delay = _PK_REGISTER_INITIAL_DELAY
        for attempt in range(max_retries):
            # Stamp X-Checkrd-Retry-Count on retry attempts (mirrors
            # OpenAI's X-Stainless-Retry-Count). Same idempotency key
            # is reused across all attempts so the control plane can
            # dedupe; only the retry-count header changes per attempt.
            attempt_headers = dict(headers)
            if attempt > 0:
                attempt_headers["X-Checkrd-Retry-Count"] = str(attempt)
            req = Request(
                url, data=body, headers=attempt_headers, method="POST",
            )
            try:
                with urlopen(req, timeout=timeout) as resp:
                    if resp.status < 400:
                        logger.debug(
                            "checkrd: public key registration ok (HTTP %d)", resp.status,
                        )
                        return  # success
            except HTTPError as e:
                if e.code == 409:
                    # Key mismatch is permanent — retrying won't help.
                    logger.warning(
                        "checkrd: public key for agent %s differs from the key "
                        "already registered with the control plane. If you "
                        "rotated keys, revoke the old key in the dashboard first.",
                        agent_id,
                    )
                    return
                if e.code in (401, 403):
                    # Auth errors are permanent — retrying won't help.
                    logger.warning(
                        "checkrd: public key registration failed "
                        "(HTTP %d — check your API key). Telemetry signature "
                        "verification may fail on the server side.",
                        e.code,
                    )
                    return
                # Transient server error — retry if attempts remain.
                if attempt < max_retries - 1:
                    logger.debug(
                        "checkrd: public key registration HTTP %d, "
                        "retry %d/%d in %.1fs",
                        e.code, attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)
                    # Jitter: uniform(delay/2, delay) — prevents thundering herd.
                    jitter_range = int(delay * 500)
                    jitter = delay / 2 + (secrets.randbelow(max(jitter_range, 1)) / 1000)
                    delay = min(jitter * 2, _PK_REGISTER_MAX_DELAY)
                    continue
            except (URLError, TimeoutError, OSError):
                # Network-level failure — retry if attempts remain.
                if attempt < max_retries - 1:
                    logger.debug(
                        "checkrd: public key registration failed (network), "
                        "retry %d/%d in %.1fs",
                        attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, _PK_REGISTER_MAX_DELAY)
                    continue

        # All retries exhausted.
        logger.warning(
            "checkrd: public key registration failed after %d attempts for "
            "agent %s. The control plane does not have this agent's public "
            "key — telemetry signature verification will fail server-side. "
            "Check network connectivity to %s and verify your API key.",
            max_retries,
            agent_id,
            control_plane_url,
        )

    threading.Thread(target=_register, name="checkrd-pk-register", daemon=True).start()


def _maybe_start_control(
    engine: WasmEngine, agent_id: str,
    control_plane_url: Union[str, None], api_key: Union[str, None],
    client: object,
    api_version: str = "",
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> None:
    if control_plane_url and api_key:
        from checkrd.control import ControlReceiver
        receiver = ControlReceiver(
            base_url=control_plane_url, agent_id=agent_id,
            api_key=api_key, engine=engine,
            api_version=api_version,
            circuit_breaker=circuit_breaker,
        )
        receiver.start()
        client._checkrd_control = receiver  # type: ignore[attr-defined]


def _maybe_start_async_control(
    engine: WasmEngine, agent_id: str,
    control_plane_url: Union[str, None], api_key: Union[str, None],
    client: object,
    api_version: str = "",
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> None:
    """Start the ``AsyncControlReceiver`` for ``wrap_async``.

    Falls back to the sync receiver when there is no running event
    loop — :class:`AsyncControlReceiver.start` schedules an
    ``asyncio.Task``, which requires a loop. A caller wrapping an
    ``httpx.AsyncClient`` outside a loop (rare; but legal: e.g.,
    ``asyncio.run(main())`` constructs the client before the loop
    fully spins up) gets the sync receiver and the original
    thread-based behaviour. Inside a normal asyncio app the async
    receiver runs cooperatively on the event loop.
    """
    if not (control_plane_url and api_key):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop → fall back to the sync receiver. The
        # daemon thread cooperates fine with an httpx.AsyncClient.
        _maybe_start_control(
            engine, agent_id, control_plane_url, api_key, client,
            api_version=api_version, circuit_breaker=circuit_breaker,
        )
        return
    receiver = AsyncControlReceiver(
        base_url=control_plane_url, agent_id=agent_id,
        api_key=api_key, engine=engine,
        api_version=api_version,
        circuit_breaker=circuit_breaker,
    )
    receiver.start()
    client._checkrd_control = receiver  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Public-API guard (PEP 562)
# ---------------------------------------------------------------------------

# Names already on the package's namespace at this point — captured
# once at import time so the guard below doesn't have to walk
# ``globals()`` on every lookup. Anything in this set was an
# intentional internal cross-module re-export at the moment the
# package was first imported; the guard only fires on *new* private
# names that some future code path tries to surface as
# ``checkrd._foo`` or ``from checkrd import _foo``.
_GLOBALS_SNAPSHOT = frozenset(globals())
_PUBLIC_NAMES = frozenset(__all__)


def __getattr__(name: str) -> Any:
    """Module-level ``__getattr__`` — PEP 562 hook.

    Two effects:

    1. Forward-looking guard. Any *new* underscore-prefixed name that
       isn't already wired into ``checkrd.__dict__`` will, on first
       access via ``checkrd._something`` or ``from checkrd import
       _something``, raise :class:`AttributeError` with a message that
       points the caller at the supported public surface.

    2. Better diagnostics. Default ``module 'checkrd' has no attribute
       'X'`` is replaced with a hint that lists the public ``__all__``
       names — useful when a typo lands somewhere autocomplete can't
       reach (e.g., a string passed to ``getattr``).

    Limitations (documented for the test suite to pin):

    - Names already imported into ``checkrd.__dict__`` at module load
      time bypass this hook entirely (PEP 562 only invokes ``__getattr__``
      on misses). Pre-existing internal helpers like ``_no_throw`` or
      ``_GlobalContext`` therefore remain reachable. They are *not*
      part of the public API contract — they may be renamed or removed
      in any release.
    - Submodule imports (``from checkrd._state import _GlobalContext``)
      use Python's standard ``importlib`` machinery, which never goes
      through the parent package's ``__getattr__``. Treat anything
      under a ``_*``-prefixed submodule as private.
    """
    if name.startswith("_") and not name.startswith("__"):
        raise AttributeError(
            f"checkrd.{name!r} is a private name and not part of the "
            f"public SDK surface. Use the names in checkrd.__all__ "
            f"instead. If you need an internal hook for a legitimate "
            f"integration, open an issue at "
            f"https://github.com/checkrd-io/checkrd-sdk/issues."
        )
    raise AttributeError(
        f"module 'checkrd' has no attribute {name!r}. "
        f"Public API: see checkrd.__all__."
    )


def __dir__() -> list[str]:
    """Restrict ``dir(checkrd)`` to documented names.

    Mirrors the ``__getattr__`` contract: the public surface is
    ``__all__``. Hides the internal helpers the package re-exports for
    cross-module use, so REPL autocompletion and ``help(checkrd)``
    don't tempt callers into private territory.
    """
    return sorted(_PUBLIC_NAMES)
