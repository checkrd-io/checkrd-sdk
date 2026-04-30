"""Unified Checkrd client class — the recommended entry point.

Mirrors the ``OpenAI(api_key=...)`` / ``Anthropic(api_key=...)`` /
``Stripe(api_key=...)`` pattern. One object holds the resolved
configuration and lifecycle (engine, batcher, receiver, watchers);
``.wrap()`` attaches that configuration to an ``httpx.Client``.

Motivation for consolidation:
  Previously the SDK exposed :func:`checkrd.wrap` (per-client) and
  :func:`checkrd.init` + :func:`checkrd.instrument` (global) as two
  parallel entry points, each with ~15 keyword arguments. That was
  flexible but hard to pick up — new users asked "which one do I use?"
  The :class:`Checkrd` class bundles the options into a single
  constructor and exposes every verb as a method, so the "hello world"
  is one line.

Backwards compatibility:
  The top-level functions remain. ``Checkrd`` delegates to them via
  ``wrap()`` / ``wrap_async()`` / ``init()`` / ``instrument*()``, so
  existing integrations keep working unchanged. Over time,
  documentation will favor the class; the functions are not slated
  for removal in 1.x.

Typical usage::

    import httpx
    import openai
    from checkrd import Checkrd

    # One client per process. Config resolves from env when omitted.
    checkrd = Checkrd(api_key="ck_live_xyz", agent_id="my-agent")

    # Explicit per-client wrap (preferred for apps that want control).
    http = checkrd.wrap(httpx.Client())
    client = openai.OpenAI(http_client=http)

    # Or flip on global monkey-patching.
    checkrd.instrument_openai()
    client = openai.OpenAI()  # transparently goes through Checkrd

    # Immutable clone with overridden options (OpenAI pattern).
    strict = checkrd.with_options(security_mode="strict")

    # Clean shutdown (via context manager or explicit).
    checkrd.close()
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional, Union

import httpx

from checkrd._settings import EnforceMode, SecurityMode, Settings
from checkrd._types import Policy
from checkrd.batcher import OnDropCallback
from checkrd.hooks import BeforeRequestHook, OnAllowHook, OnDenyHook
from checkrd.identity import IdentityProvider
from checkrd.sinks import TelemetrySink


_SENTINEL: Any = object()
"""Three-valued sentinel for :meth:`Checkrd.with_options` / :meth:`AsyncCheckrd.with_options`.

Callers that want to UNSET a field (``api_key=None``) need to pass
``None`` explicitly; omitting the kwarg entirely reuses the current
value. This mirrors the ``NotGiven`` / ``not_given`` pattern Stainless
ships in the OpenAI / Anthropic SDKs.
"""


@dataclass(frozen=True)
class _ClientConfig:
    """Immutable bundle of the construction-time options.

    Extracted so :meth:`Checkrd.with_options` can use ``dataclasses.replace``
    to produce a sibling client without duplicating every field.
    """

    agent_id: Optional[str] = None
    api_key: Optional[str] = None
    control_plane_url: Optional[str] = None
    policy: Union[str, Path, Policy, None] = None
    identity: Optional[IdentityProvider] = None
    enforce: EnforceMode = "auto"
    security_mode: Optional[SecurityMode] = None
    api_version: Optional[str] = None
    telemetry_sink: Optional[TelemetrySink] = None
    on_deny: Optional[OnDenyHook] = None
    on_allow: Optional[OnAllowHook] = None
    before_request: Optional[BeforeRequestHook] = None
    on_telemetry_drop: Optional[OnDropCallback] = None
    policy_watch: bool = False
    policy_watch_interval_secs: float = 5.0
    killswitch_file: Union[str, Path, None] = None
    killswitch_poll_interval_secs: float = 5.0
    default_headers: dict[str, str] = field(default_factory=dict)
    # Control-plane HTTP tuning (matches OpenAI / Anthropic shape — short-lived
    # POSTs to the batcher and key registrar; SSE receiver keeps its own
    # long-lived semantics).
    max_retries: int = 3
    timeout: float = 30.0
    connect_timeout: float = 5.0

    def merge(self, **overrides: Any) -> "_ClientConfig":
        """Produce a sibling config with the given overrides applied.

        Sentinel-aware: omitted kwargs reuse the current value, explicit
        ``None`` unsets. This is the ``with_options(...)`` engine.
        """
        applied: dict[str, Any] = {}
        for field_name in self.__dataclass_fields__:
            if field_name in overrides and overrides[field_name] is not _SENTINEL:
                applied[field_name] = overrides[field_name]
        return replace(self, **applied)


class Checkrd:
    """Synchronous Checkrd client — the recommended API entry point.

    One object per process. Holds the resolved configuration and
    (when a control-plane is configured) the background batcher,
    public-key registrar, and SSE control receiver. Lifetime is managed
    either manually (:meth:`close`) or via the context-manager protocol.

    All constructor arguments fall back to the environment when omitted:
    ``CHECKRD_API_KEY``, ``CHECKRD_BASE_URL``, ``CHECKRD_AGENT_ID``,
    ``CHECKRD_ENFORCE``, ``CHECKRD_SECURITY_MODE``, ``CHECKRD_API_VERSION``,
    and the PaaS service-name fallbacks for ``agent_id`` — see
    :mod:`checkrd._settings` for the full precedence chain.

    Example::

        with Checkrd(api_key="ck_live_xyz", agent_id="my-agent") as client:
            http = client.wrap(httpx.Client())
            response = http.post("https://api.openai.com/v1/...")
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        agent_id: Optional[str] = None,
        base_url: Optional[str] = None,
        policy: Union[str, Path, Policy, None] = None,
        identity: Optional[IdentityProvider] = None,
        enforce: EnforceMode = "auto",
        security_mode: Optional[SecurityMode] = None,
        api_version: Optional[str] = None,
        telemetry_sink: Optional[TelemetrySink] = None,
        on_deny: Optional[OnDenyHook] = None,
        on_allow: Optional[OnAllowHook] = None,
        before_request: Optional[BeforeRequestHook] = None,
        on_telemetry_drop: Optional[OnDropCallback] = None,
        policy_watch: bool = False,
        policy_watch_interval_secs: float = 5.0,
        killswitch_file: Union[str, Path, None] = None,
        killswitch_poll_interval_secs: float = 5.0,
        default_headers: Optional[dict[str, str]] = None,
        max_retries: int = 3,
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
    ) -> None:
        # ``base_url`` is the OpenAI/Anthropic name; the lower-level
        # machinery uses ``control_plane_url``. Keep the public API
        # aligned with the AI-SDK convention and translate internally.
        #
        # ``max_retries`` / ``timeout`` / ``connect_timeout`` apply to
        # short-lived control-plane POSTs (telemetry batcher, public-key
        # registration). The SSE control receiver has long-lived stream
        # semantics and uses its own internal idle/reconnect timeouts.
        self._config = _ClientConfig(
            agent_id=agent_id,
            api_key=api_key,
            control_plane_url=base_url,
            policy=policy,
            identity=identity,
            enforce=enforce,
            security_mode=security_mode,
            api_version=api_version,
            telemetry_sink=telemetry_sink,
            on_deny=on_deny,
            on_allow=on_allow,
            before_request=before_request,
            on_telemetry_drop=on_telemetry_drop,
            policy_watch=policy_watch,
            policy_watch_interval_secs=policy_watch_interval_secs,
            killswitch_file=killswitch_file,
            killswitch_poll_interval_secs=killswitch_poll_interval_secs,
            default_headers=dict(default_headers or {}),
            max_retries=max_retries,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )
        # Wrapped httpx.Client instances we've touched, kept so
        # :meth:`close` can drain their batchers / receivers.
        self._wrapped: list[httpx.Client] = []
        self._closed = False

    # ---------------------------------------------------------------
    # Introspection
    # ---------------------------------------------------------------

    @property
    def api_key(self) -> Optional[str]:
        """The API key that will be used (after env resolution).

        Reads from the resolved :class:`Settings`, not the raw
        constructor argument — so an operator who relied on
        ``CHECKRD_API_KEY`` can still see "yes, my client has a key"
        via this property.
        """
        return self.settings.api_key

    @property
    def agent_id(self) -> str:
        """The resolved agent ID (never ``None`` — derived if unset)."""
        return self.settings.agent_id

    @property
    def base_url(self) -> Optional[str]:
        """The resolved control-plane base URL."""
        return self.settings.control_plane_url

    @property
    def settings(self) -> Settings:
        """Snapshot of the fully-resolved settings.

        Useful for tests and health dashboards that want to assert on
        the effective configuration without reaching into private
        attributes.
        """
        from checkrd._settings import resolve

        return resolve(
            agent_id=self._config.agent_id,
            api_key=self._config.api_key,
            control_plane_url=self._config.control_plane_url,
            enforce=self._config.enforce,
            security_mode=self._config.security_mode,
            api_version=self._config.api_version,
        )

    # ---------------------------------------------------------------
    # Core verbs
    # ---------------------------------------------------------------

    def wrap(self, client: httpx.Client) -> httpx.Client:
        """Attach Checkrd policy enforcement to an ``httpx.Client``.

        The client is mutated in place: its transport is replaced with
        a Checkrd-aware transport that evaluates the policy engine on
        every request. Subsequent ``client.get()`` / ``.post()`` /
        etc. calls run through Checkrd transparently.

        Args:
            client: An ``httpx.Client`` to wrap. The client's existing
                transport is preserved as the inner transport — Checkrd
                evaluates each request, then forwards allowed ones to
                the original transport.

        Returns:
            The same client (for fluent-style chaining), now with
            Checkrd evaluation on every outbound HTTP call.

        Raises:
            CheckrdInitError: If the WASM engine fails to load and
                ``security_mode="strict"``. Pass
                ``security_mode="permissive"`` to degrade to
                pass-through on engine failure.

        Example:
            Wrap an httpx client and hand it to OpenAI::

                import httpx
                import openai
                from checkrd import Checkrd

                checkrd = Checkrd(api_key="ck_live_xyz", policy="policy.yaml")
                http = checkrd.wrap(httpx.Client(timeout=30.0))
                client = openai.OpenAI(http_client=http)
                client.chat.completions.create(...)  # routed through Checkrd
        """
        from checkrd import wrap as _wrap

        c = self._config
        result = _wrap(
            client,
            agent_id=c.agent_id,
            policy=c.policy,
            identity=c.identity,
            enforce=c.enforce,
            control_plane_url=c.control_plane_url,
            api_key=c.api_key,
            telemetry_sink=c.telemetry_sink,
            on_deny=c.on_deny,
            on_allow=c.on_allow,
            before_request=c.before_request,
            on_telemetry_drop=c.on_telemetry_drop,
            policy_watch=c.policy_watch,
            policy_watch_interval_secs=c.policy_watch_interval_secs,
            killswitch_file=c.killswitch_file,
            killswitch_poll_interval_secs=c.killswitch_poll_interval_secs,
            security_mode=c.security_mode,
            max_retries=c.max_retries,
            timeout=c.timeout,
            connect_timeout=c.connect_timeout,
        )
        self._wrapped.append(result)
        # Apply any default headers the caller supplied to the Checkrd
        # client constructor. httpx stores client-wide defaults on
        # ``client.headers`` — the merge is deliberate so callers can
        # still override per-request.
        if c.default_headers:
            for name, value in c.default_headers.items():
                result.headers[name] = value
        return result

    def with_options(
        self,
        *,
        api_key: Any = _SENTINEL,
        agent_id: Any = _SENTINEL,
        base_url: Any = _SENTINEL,
        policy: Any = _SENTINEL,
        identity: Any = _SENTINEL,
        enforce: Any = _SENTINEL,
        security_mode: Any = _SENTINEL,
        api_version: Any = _SENTINEL,
        telemetry_sink: Any = _SENTINEL,
        on_deny: Any = _SENTINEL,
        on_allow: Any = _SENTINEL,
        before_request: Any = _SENTINEL,
        on_telemetry_drop: Any = _SENTINEL,
        policy_watch: Any = _SENTINEL,
        policy_watch_interval_secs: Any = _SENTINEL,
        killswitch_file: Any = _SENTINEL,
        killswitch_poll_interval_secs: Any = _SENTINEL,
        default_headers: Any = _SENTINEL,
        max_retries: Any = _SENTINEL,
        timeout: Any = _SENTINEL,
        connect_timeout: Any = _SENTINEL,
    ) -> "Checkrd":
        """Return a new :class:`Checkrd` with the given options overridden.

        Immutable clone — the source client is unchanged. Mirrors
        ``OpenAI().with_options(...)`` in the official OpenAI SDK:
        useful for per-request overrides without mutating a shared
        singleton.

        Omitted kwargs reuse the current value; passing an explicit
        ``None`` unsets a field. This three-state semantics is what
        the ``_SENTINEL`` plumbing above encodes.

        Example::

            strict = client.with_options(security_mode="strict")
            # or set a different API version for a specific subtree
            new_client = client.with_options(api_version="2026-05-01")
        """
        renamed = {
            "api_key": api_key,
            "agent_id": agent_id,
            "control_plane_url": base_url,  # public alias → internal
            "policy": policy,
            "identity": identity,
            "enforce": enforce,
            "security_mode": security_mode,
            "api_version": api_version,
            "telemetry_sink": telemetry_sink,
            "on_deny": on_deny,
            "on_allow": on_allow,
            "before_request": before_request,
            "on_telemetry_drop": on_telemetry_drop,
            "policy_watch": policy_watch,
            "policy_watch_interval_secs": policy_watch_interval_secs,
            "killswitch_file": killswitch_file,
            "killswitch_poll_interval_secs": killswitch_poll_interval_secs,
            "default_headers": default_headers,
            "max_retries": max_retries,
            "timeout": timeout,
            "connect_timeout": connect_timeout,
        }
        new_config = self._config.merge(**renamed)
        # Bypass __init__ — we already have a validated config.
        sibling = Checkrd.__new__(Checkrd)
        sibling._config = new_config
        sibling._wrapped = []
        sibling._closed = False
        return sibling

    def instrument_openai(self) -> None:
        """Globally instrument the ``openai`` SDK through Checkrd.

        After this call, every ``openai.OpenAI()`` / ``openai.AsyncOpenAI()``
        instance routes requests through Checkrd. Idempotent — calling
        twice is a no-op.

        **Order matters**: call this BEFORE the first ``openai.OpenAI(...)``
        constructor. The patch wraps ``__init__``; clients constructed
        before ``instrument_openai()`` keep their pre-patch transport.

        Raises:
            CheckrdInitError: If the WASM engine fails to load
                (only when ``security_mode="strict"``, the default).

        Example:
            ::

                from checkrd import Checkrd
                from openai import OpenAI

                checkrd = Checkrd(api_key="ck_live_xyz")
                checkrd.instrument_openai()         # before constructing OpenAI()
                client = OpenAI()                   # automatically wrapped
                client.chat.completions.create(...) # routed through Checkrd
        """
        from checkrd import instrument_openai as _instrument

        self._ensure_global_context()
        _instrument()

    def instrument_anthropic(self) -> None:
        """Globally instrument the ``anthropic`` SDK through Checkrd."""
        from checkrd import instrument_anthropic as _instrument

        self._ensure_global_context()
        _instrument()

    def instrument(self) -> None:
        """Instrument every supported vendor SDK in one call.

        Patches the global constructors for OpenAI, Anthropic, Cohere,
        Mistral, Groq, Together, and Google GenAI. Idempotent.
        """
        from checkrd import instrument as _instrument

        self._ensure_global_context()
        _instrument()

    def healthy(self) -> Any:
        """Return the SDK health snapshot.

        Includes engine-loaded flag, agent ID, enforcement state, and
        per-batcher / per-receiver counters when available. See
        :func:`checkrd.healthy` for the full shape.

        Returns:
            A dict suitable for liveness / readiness probes::

                {
                    "status": "healthy" | "degraded" | "disabled",
                    "engine_loaded": bool,
                    "control_plane_connected": bool | None,
                    "agent_id": str | None,
                    "enforce": bool | None,
                    "last_eval_at": float | None,
                    "telemetry": {
                        "sent": int, "dropped_backpressure": int,
                        "dropped_signing_error": int,
                        "dropped_send_error": int, "pending": int,
                    } | None,
                }

        Example:
            FastAPI ``/healthz`` endpoint::

                @app.get("/healthz")
                def healthz():
                    return {"ok": True, "checkrd": checkrd.healthy()}
        """
        from checkrd import healthy as _healthy

        return _healthy()

    def close(self) -> None:
        """Release background resources (batcher, receiver, watchers).

        Safe to call multiple times. If any wrapped client carries its
        own batcher (created by :meth:`wrap`), it's flushed and stopped
        here — matches ``atexit`` behavior but is deterministic.

        Example:
            FastAPI lifespan handler — drain on shutdown::

                from contextlib import asynccontextmanager
                from fastapi import FastAPI
                from checkrd import Checkrd

                checkrd = Checkrd()

                @asynccontextmanager
                async def lifespan(app: FastAPI):
                    try:
                        yield
                    finally:
                        checkrd.close()   # flush telemetry, disconnect SSE

                app = FastAPI(lifespan=lifespan)
        """
        if self._closed:
            return
        self._closed = True
        # Drain any wrapped-client batchers we created. The attribute
        # was stashed on the client by `checkrd.wrap()`; silent on
        # clients we never wrapped (custom sinks own their lifecycle).
        for client in self._wrapped:
            batcher = getattr(client, "_checkrd_batcher", None)
            if batcher is not None:
                try:
                    batcher.stop()
                except Exception:
                    # `close()` must never raise. A failing batcher
                    # shutdown in test or production is a warning, not
                    # a new error to propagate.
                    pass
            receiver = getattr(client, "_checkrd_control", None)
            if receiver is not None:
                try:
                    receiver.stop()
                except Exception:
                    pass
        # If we also set up a global context (via `instrument*`), tear
        # it down too.
        from checkrd import has_context, shutdown

        if has_context():
            try:
                shutdown()
            except Exception:
                pass

    # ---------------------------------------------------------------
    # Context manager
    # ---------------------------------------------------------------

    def __enter__(self) -> "Checkrd":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        # API-key value is NEVER shown — this __repr__ ends up in logs,
        # error reports, and developer REPL sessions. Status-only
        # repr matches Stripe's "<Stripe object at 0x...>" discipline.
        has_key = self._config.api_key is not None or bool(
            __import__("os").environ.get("CHECKRD_API_KEY"),
        )
        return (
            f"Checkrd(agent_id={self.agent_id!r}, "
            f"base_url={self.base_url!r}, has_api_key={has_key})"
        )

    # ---------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------

    def _ensure_global_context(self) -> None:
        """Call :func:`checkrd.init` with our config if no context exists yet."""
        from checkrd import has_context, init

        if has_context():
            return
        c = self._config
        # `init()` returns a context manager; we don't enter it — the
        # caller holds the Checkrd object for lifetime, and `.close()`
        # drops the context when they're done.
        init(
            agent_id=c.agent_id,
            policy=c.policy,
            identity=c.identity,
            enforce=c.enforce,
            control_plane_url=c.control_plane_url,
            api_key=c.api_key,
            telemetry_sink=c.telemetry_sink,
            on_deny=c.on_deny,
            on_allow=c.on_allow,
            before_request=c.before_request,
            on_telemetry_drop=c.on_telemetry_drop,
            policy_watch=c.policy_watch,
            policy_watch_interval_secs=c.policy_watch_interval_secs,
            killswitch_file=c.killswitch_file,
            killswitch_poll_interval_secs=c.killswitch_poll_interval_secs,
            security_mode=c.security_mode,
            max_retries=c.max_retries,
            timeout=c.timeout,
            connect_timeout=c.connect_timeout,
        )


class AsyncCheckrd(Checkrd):
    """Asynchronous Checkrd client — mirrors :class:`Checkrd`.

    Takes and returns ``httpx.AsyncClient`` from :meth:`wrap`. All other
    methods are identical — the engine, batcher, and receiver are the
    same sync machinery (the WASM core is synchronous, but async
    transports marshal data in and out without blocking the loop).

    The separate class exists so type checkers can distinguish sync
    from async callers — passing an ``httpx.Client`` to the async
    ``wrap()`` would be a type error, matching the
    ``OpenAI`` vs ``AsyncOpenAI`` split in the official SDK.

    Example::

        async with AsyncCheckrd(api_key="ck_live_xyz") as client:
            http = client.wrap(httpx.AsyncClient())
            response = await http.post("https://api.openai.com/v1/...")
    """

    def wrap(  # type: ignore[override]
        self, client: httpx.AsyncClient,
    ) -> httpx.AsyncClient:
        """Attach Checkrd enforcement to an ``httpx.AsyncClient``."""
        from checkrd import wrap_async as _wrap_async

        c = self._config
        result = _wrap_async(
            client,
            agent_id=c.agent_id,
            policy=c.policy,
            identity=c.identity,
            enforce=c.enforce,
            control_plane_url=c.control_plane_url,
            api_key=c.api_key,
            telemetry_sink=c.telemetry_sink,
            on_deny=c.on_deny,
            on_allow=c.on_allow,
            before_request=c.before_request,
            on_telemetry_drop=c.on_telemetry_drop,
            policy_watch=c.policy_watch,
            policy_watch_interval_secs=c.policy_watch_interval_secs,
            killswitch_file=c.killswitch_file,
            killswitch_poll_interval_secs=c.killswitch_poll_interval_secs,
            security_mode=c.security_mode,
            max_retries=c.max_retries,
            timeout=c.timeout,
            connect_timeout=c.connect_timeout,
        )
        # `self._wrapped` is typed ``list[httpx.Client]`` on the parent
        # class; the async variant stores async clients there for
        # shutdown purposes. The type punning is internal-only.
        self._wrapped.append(result)  # type: ignore[arg-type]
        if c.default_headers:
            for name, value in c.default_headers.items():
                result.headers[name] = value
        return result

    def with_options(self, **overrides: Any) -> "AsyncCheckrd":
        """Return a new :class:`AsyncCheckrd` with overridden options."""
        sibling_sync = super().with_options(**overrides)
        async_sibling = AsyncCheckrd.__new__(AsyncCheckrd)
        async_sibling._config = sibling_sync._config
        async_sibling._wrapped = []
        async_sibling._closed = False
        return async_sibling

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`. Safe to call multiple times.

        Drains the asyncio-native :class:`AsyncTelemetryBatcher` via
        ``await batcher.stop()`` for clean structured-concurrency
        shutdown — the worker task is cancelled, the final flush is
        awaited, and the owned ``httpx.AsyncClient`` is closed in one
        operation. Falls back to the sync ``close()`` path for any
        wrapped client that received the legacy thread-based batcher
        (e.g. when ``wrap_async(use_async_batcher=False)`` was used).
        """
        if self._closed:
            return
        self._closed = True

        for client in self._wrapped:
            batcher = getattr(client, "_checkrd_batcher", None)
            if batcher is not None:
                # AsyncTelemetryBatcher.stop is a coroutine; the
                # thread-based TelemetryBatcher.stop is sync. Inspect
                # the return value to decide whether to await — same
                # duck-typing used in the OpenAI / Anthropic SDKs for
                # mixed sync/async cleanup paths.
                try:
                    result = batcher.stop()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    # aclose() must never raise — failed batcher
                    # shutdown is a warning, not a new exception to
                    # propagate up the async stack.
                    pass
            receiver = getattr(client, "_checkrd_control", None)
            if receiver is not None:
                try:
                    result = receiver.stop()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    pass

        from checkrd import has_context, shutdown

        if has_context():
            try:
                shutdown()
            except Exception:
                pass

    async def __aenter__(self) -> "AsyncCheckrd":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
