"""Global SDK state for ``checkrd.init()`` and integration instrumentors.

The :func:`checkrd.wrap` primitive is explicit and per-client. For users who
want the industry-standard "configure once, everything just works" pattern
(the model pioneered by ``sentry_sdk.init``, ``ddtrace.patch_all``, and
OpenTelemetry's auto-instrumentors), this module provides a complementary
global context::

    import checkrd
    checkrd.init(policy="policy.yaml")
    checkrd.instrument()  # patches detected AI libraries

    from openai import OpenAI
    client = OpenAI()   # traffic routes through the global Checkrd engine

The global is intentionally single-instance: one engine means one kill
switch, one rate-limit bucket, one telemetry sink across every library the
user instruments. That matches the mental model of "observe my agent",
not "observe my OpenAI calls and, separately, my Anthropic calls."

Thread safety: :func:`init` and :func:`shutdown` are guarded by a module
lock so concurrent initialization converges to a single context. The
global state is stored in plain module-level variables — **not**
``ContextVar`` — because SDK configuration is process-global, not
per-task. ``ContextVar`` has per-context semantics: any ``asyncio`` task
or thread running with a fresh ``contextvars.Context`` would see ``None``,
causing policy enforcement to silently vanish. This is the same design
Sentry uses: the *client* is a true global while only per-request state
(current span, scope) uses ``ContextVar``.

Reads are safe under both the GIL and PEP 703 free-threading because
Python reference assignment is atomic at the bytecode level (a single
``STORE_GLOBAL`` instruction). The ``_LOCK`` serializes compound
init/shutdown operations.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from checkrd._settings import Settings
from checkrd.engine import WasmEngine
from checkrd.exceptions import CheckrdInitError
from checkrd.identity import IdentityProvider
from checkrd.sinks import TelemetrySink

logger = logging.getLogger("checkrd")

_LOCK = threading.Lock()

# Module-level globals — process-wide, visible to every thread and async
# task regardless of their contextvars.Context. This is the correct
# semantic for SDK configuration (Sentry, Datadog, LaunchDarkly all use
# true globals for the client/config; only per-request state like the
# current span/scope uses ContextVar).
_GLOBAL_CONTEXT: Optional[_GlobalContext] = None
_DEGRADED: bool = False
_LAST_EVAL_AT: Optional[str] = None


@dataclass
class _GlobalContext:
    """Holds every runtime object produced by :func:`checkrd.init`.

    One context per process. Shared across every instrumented library and
    every call site that asks for it via :func:`get_context`. The fields
    match the arguments the ``CheckrdTransport`` needs, so an instrumentor
    can wrap a new httpx client with one attribute read per field.

    Attributes:
        engine: The initialized :class:`checkrd.engine.WasmEngine`. Policy
            evaluation, rate limits, kill switch, and telemetry all flow
            through this single instance.
        identity: The resolved :class:`checkrd.identity.IdentityProvider`.
            Used by the batcher to sign telemetry; bound to ``engine`` for
            ``LocalIdentity``.
        sink: The resolved :class:`TelemetrySink`, or ``None`` if the caller
            is running without a control plane and without a custom sink.
        enforce: The effective ``enforce`` boolean after resolving the
            ``"auto"`` default against the policy source.
        settings: The immutable :class:`checkrd.Settings` object from
            :func:`checkrd._settings.resolve`, exposed for debugging.
        watchers: Internal list of file watchers started by :func:`init`.
            Owned by the context so :func:`shutdown` can stop them cleanly.
        control_receiver: Internal SSE/polling control-plane receiver, or
            ``None`` if no control plane was configured.
    """

    engine: WasmEngine
    identity: IdentityProvider
    sink: Optional[TelemetrySink]
    enforce: bool
    settings: Settings
    on_deny: Optional[Any] = None
    on_allow: Optional[Any] = None
    before_request: Optional[Any] = None
    watchers: list[Any] = field(default_factory=list)
    control_receiver: Optional[Any] = None

    def shutdown(self) -> None:
        """Tear down every resource the context owns.

        Stops the control receiver (which joins its background thread),
        stops every file watcher, and closes the telemetry sink (if any).
        Errors from any single component are logged at warning level but
        do not prevent the remaining components from being stopped —
        shutdown must be robust to partial failure because users call it
        in atexit and finally blocks.
        """
        if self.control_receiver is not None:
            try:
                self.control_receiver.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkrd: control receiver shutdown failed: %s", exc)
            self.control_receiver = None

        for watcher in self.watchers:
            try:
                watcher.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkrd: watcher shutdown failed: %s", exc)
        self.watchers.clear()

        if self.sink is not None:
            try:
                self.sink.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkrd: sink shutdown failed: %s", exc)
            self.sink = None


def get_context() -> _GlobalContext:
    """Return the global Checkrd context.

    Called by integration instrumentors when patching target libraries.
    Raises :class:`CheckrdInitError` with a clear actionable message if
    :func:`checkrd.init` has not been called — this is nearly always a
    user error (``instrument()`` without ``init()``) and the error tells
    them exactly what to do.
    """
    ctx = _GLOBAL_CONTEXT
    if ctx is None:
        raise CheckrdInitError(
            "checkrd.init() must be called before checkrd.instrument() or "
            "before using integration classes. Call checkrd.init() once at "
            "program startup, or use checkrd.wrap(client) for per-client "
            "configuration."
        )
    return ctx


def has_context() -> bool:
    """Return ``True`` if :func:`checkrd.init` has been called successfully.

    Exposed so ``checkrd.instrument()`` can detect an uninitialized state
    and auto-initialize with defaults for the zero-config path.
    """
    return _GLOBAL_CONTEXT is not None


def set_context(ctx: Optional[_GlobalContext]) -> None:
    """Replace the global context. Internal — used by :func:`checkrd.init`.

    Thread-safe: callers must hold :data:`_LOCK`. The previous context's
    ``shutdown()`` is the caller's responsibility — this function only
    swaps the reference.
    """
    global _GLOBAL_CONTEXT  # noqa: PLW0603
    _GLOBAL_CONTEXT = ctx


def with_lock() -> threading.Lock:
    """Return the module-level lock for callers that need to synchronize
    around a compound init/shutdown operation. Exposed for :func:`checkrd.init`
    so the lock lives in one place."""
    return _LOCK


def is_degraded() -> bool:
    """Return ``True`` if init() ran but the WASM engine failed to load.

    In degraded mode, ``instrument()`` is a silent no-op rather than
    raising ``CheckrdInitError``. This distinguishes "user forgot
    ``init()``" (error) from "WASM failed" (graceful passthrough).
    """
    return _DEGRADED


def set_degraded(value: bool) -> None:
    """Set or clear the degraded flag. Internal."""
    global _DEGRADED  # noqa: PLW0603
    _DEGRADED = value


def get_last_eval_at() -> Optional[str]:
    """Return the ISO timestamp of the last policy evaluation, or ``None``."""
    return _LAST_EVAL_AT


def set_last_eval_at(timestamp: str) -> None:
    """Record the timestamp of the last policy evaluation. Internal."""
    global _LAST_EVAL_AT  # noqa: PLW0603
    _LAST_EVAL_AT = timestamp


__all__ = [
    "_GlobalContext",
    "get_context",
    "has_context",
    "set_context",
    "with_lock",
    "is_degraded",
    "set_degraded",
    "get_last_eval_at",
    "set_last_eval_at",
]
