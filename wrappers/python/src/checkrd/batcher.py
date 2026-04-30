"""Background telemetry batcher for the Checkrd SDK.

Collects enriched telemetry events from the transport layer and sends
them in batches to the control plane's ``POST /v1/telemetry`` endpoint.

Thread-safe: multiple transport instances can enqueue events concurrently.

Lifecycle:
  - Created by ``wrap()``/``wrap_async()`` when ``control_plane_url``
    and ``api_key`` are both provided.
  - Runs a daemon thread that flushes every ``flush_interval_secs``
    or when ``batch_size`` events have accumulated, whichever comes first.
  - On shutdown (``client.close()`` or ``atexit``), flushes remaining events.
  - Events are best-effort: if the HTTP request fails, events are logged
    and dropped. Telemetry is not mission-critical.

Signing:
  - When the batcher is constructed with a ``WasmEngine`` and ``agent_id``,
    every outbound request is signed using the agent's Ed25519 identity key
    via the WASM core's ``sign_telemetry_batch`` FFI export. The signature
    is delivered as an RFC 9421 ``Signature``/``Signature-Input`` header
    pair plus an RFC 9530 ``Content-Digest`` header. The DSSE envelope is
    not currently transmitted in the HTTP request body — that's a future
    extension; today the ingestion service stores the envelope on its side
    after rebuilding it from the verified RFC 9421 signature.
  - When the engine is in anonymous mode (no local key, e.g. KMS provider
    where signing happens elsewhere), batches are sent unsigned with a
    log warning. The ingestion service's ``warn`` mode accepts these
    during the rollout window.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import secrets
import ssl
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Union
from urllib.request import Request, urlopen

import weakref

from checkrd._circuit_breaker import CircuitBreaker
from checkrd._fork import register_fork_handler
from checkrd._platform import default_control_headers, new_idempotency_key
from checkrd._retry import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_SLEEP_SECS,
    next_backoff,
    should_retry_status,
)
from checkrd._version import __version__
from checkrd.exceptions import CheckrdInitError
from checkrd.hooks import BeforeSendHook

if TYPE_CHECKING:
    from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")

#: Reason tags passed to an ``on_drop`` callback. Kept as a ``Literal``
#: so callers can build exhaustive ``match`` statements without sentinel
#: strings floating around the codebase. New reasons may be added in
#: minor versions; callers should fall through to a default arm.
DropReason = Literal["backpressure", "signing_error", "send_error"]

#: Callback signature for ``TelemetryBatcher``'s ``on_drop`` hook.
#: The ``count`` is the number of events rolled up into this drop — a
#: single 100-event batch that hits a signing error fires one callback
#: with ``count=100``, not 100 callbacks with ``count=1``. Keeps the hot
#: path cheap and the callback aligned with the SDK's own counters.
OnDropCallback = Callable[[DropReason, int], None]


def _generate_traceparent() -> str:
    """Generate a W3C Trace Context traceparent header value.

    Format: ``00-{trace_id}-{parent_id}-{flags}`` where trace_id is 32
    lowercase hex chars and parent_id is 16 lowercase hex chars. Uses
    :mod:`secrets` for cryptographic randomness (not :mod:`random`) so
    trace IDs are unpredictable — W3C spec requires this for privacy.

    Emitted on every telemetry batch POST to enable end-to-end request
    correlation across Python SDK → ingestion → writer → ClickHouse.
    Operators can query Loki with
    ``{service=~".+"} | json | trace_id="abc"`` to see the full pipeline
    for a single batch.
    """
    trace_id = secrets.token_hex(16)  # 32 hex chars
    parent_id = secrets.token_hex(8)  # 16 hex chars
    return f"00-{trace_id}-{parent_id}-01"

def _approx_event_bytes(event: dict[str, Any]) -> int:
    """Cheap byte-cost estimate for queue accounting.

    JSON-serializing every enqueue() would be exact but ~10× more
    expensive than the dict insert it accompanies. ``len(repr(event))``
    overshoots actual JSON by a constant factor (Python's repr quotes
    + escapes more aggressively), which makes the byte bound err on
    the conservative side — exactly what an OOM protection wants.
    """
    return len(repr(event))


# Signature validity window in seconds. The verifier rejects signatures whose
# (now - created) exceeds this value plus its own clock-skew tolerance.
SIGNATURE_WINDOW_SECS = 300

# Fork-safety registry. Every active batcher is held here weakly; the
# at-fork handler registered below walks the set in the forked child
# and calls each instance's ``_reinit_after_fork`` method. Replaces the
# old per-``enqueue`` PID check, which added a syscall to the hot path
# and only caught the inconsistency on the next hot-path operation.
_LIVE_BATCHERS: "weakref.WeakSet[TelemetryBatcher]" = weakref.WeakSet()
register_fork_handler(_LIVE_BATCHERS, "_reinit_after_fork", "telemetry batcher")

# Retry configuration is centralised in :mod:`checkrd._retry`. The
# batcher consumes ``DEFAULT_MAX_ATTEMPTS``, ``DEFAULT_MAX_SLEEP_SECS``,
# and the helper functions ``next_backoff`` / ``should_retry_status`` —
# every retry loop in this SDK MUST go through that module so the
# server hints (``retry-after-ms``, ``x-should-retry``) are honored
# uniformly.


class TelemetryBatcher:
    """Background batcher that sends telemetry events to the control plane."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        engine: "WasmEngine",
        signer_agent_id: str,
        batch_size: int = 100,
        flush_interval_secs: float = 5.0,
        max_queue_size: int = 10_000,
        max_queue_bytes: int = 100 * 1024 * 1024,
        on_drop: Optional[OnDropCallback] = None,
        api_version: str = "",
        circuit_breaker: Optional[CircuitBreaker] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        request_timeout_secs: float = 30.0,
        before_send: Optional[BeforeSendHook] = None,
        verify: Union[bool, str, "ssl.SSLContext"] = True,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/telemetry"
        self._api_key = api_key
        self._batch_size = batch_size
        self._flush_interval = flush_interval_secs
        self._max_queue_size = max_queue_size
        # OOM protection. Count alone is insufficient — a 10K-event
        # queue of 100KB events is 1GB resident, OOM territory under
        # sustained backpressure. 100MB default mirrors Sentry's
        # transport bound; tune via `max_queue_bytes` for high-throughput
        # deployments. See `_approx_event_bytes` for the accounting.
        self._max_queue_bytes = max_queue_bytes
        self._queue_bytes = 0
        # `verify` follows httpx's contract: True (system trust store,
        # default — recommended for prod), False (insecure — local-dev
        # only), str (CA bundle path, e.g., self-hosted with custom
        # CA), or `ssl.SSLContext` (full control, e.g., cert pinning).
        self._verify = verify
        self._sdk_version = __version__
        self._engine = engine
        self._signer_agent_id = signer_agent_id
        self._on_drop = on_drop
        self._api_version = api_version
        self._max_attempts = max_attempts
        self._request_timeout_secs = request_timeout_secs
        self._before_send = before_send
        # Default the breaker ON. A control-plane outage during a
        # deploy would otherwise let every batcher flush burn its full
        # 24-second retry budget on every attempt; the breaker collapses
        # that to a fast-fail after the failure threshold (5 by default).
        # Callers wiring a shared breaker across batcher + key registrar
        # pass an explicit instance to coordinate.
        self._breaker = circuit_breaker if circuit_breaker is not None else CircuitBreaker()

        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._flush_event = threading.Event()
        self._stopped = False

        # Fork safety (Sentry BackgroundWorker pattern). After os.fork(),
        # the child process inherits the parent's thread objects but the
        # actual OS threads do not exist in the child. Without this check,
        # enqueue() would silently buffer events that are never flushed
        # (dead thread), and the lock could be in an inconsistent state.
        # Gunicorn with preload_app=True is the canonical trigger.
        self._pid = os.getpid()

        # Loss tracking (Sentry client-reports pattern). Counters are
        # monotonically increasing and never reset — consumers read them
        # via properties and diff against previous reads.
        #
        # ``signing_error`` is broken out from ``send_error`` because the
        # two have completely different root causes and remediations:
        #   - ``signing_error`` = WASM engine has no signing key, keep
        #     firing until config is fixed. Policy for most operators:
        #     page.
        #   - ``send_error`` = control plane unreachable, usually
        #     transient. Policy: dashboard card, not a page.
        # Collapsing them hides the distinction from the dashboard.
        self._events_dropped_backpressure: int = 0
        self._events_dropped_signing_error: int = 0
        self._events_dropped_send_error: int = 0
        self._events_sent: int = 0
        # Most recent ``Checkrd-Request-Id`` (or ``X-Request-Id``) seen
        # on a control-plane response. Surfaced via ``diagnostics()``
        # for support-ticket correlation — the same Stripe / OpenAI
        # convention every observability dashboard expects. ``None``
        # before the first batch ships in this process.
        self._last_request_id: Optional[str] = None

        self._thread = threading.Thread(
            target=self._run, name="checkrd-telemetry-batcher", daemon=True
        )
        self._thread.start()
        atexit.register(self.stop)

        # Plug into the module-level fork-safety registry. Held weakly
        # so a closed batcher doesn't keep itself alive past its useful
        # lifetime — the at-fork handler is a non-issue once GC reaps it.
        _LIVE_BATCHERS.add(self)

    def _reinit_after_fork(self) -> None:
        """Re-initialize threading state in the forked child process.

        Called by the module-level ``os.register_at_fork`` handler in
        the child immediately after fork. Idempotent: if the recorded
        PID still matches the current one (handler ran in the parent
        for some reason, or test invoked manually), the call is a no-op.

        After fork, the parent's daemon thread does not exist in the
        child process. The old buffer, counters, lock, and stop event
        may be in an inconsistent state because the parent's threads
        were holding them mid-write at the moment of fork. Reset
        everything and spawn a fresh thread under the new PID.

        Same pattern as Sentry's ``BackgroundWorker`` after they
        switched from per-op PID checks to ``register_at_fork``.
        """
        pid = os.getpid()
        if pid == self._pid:
            return
        self._pid = pid
        # Re-create synchronization primitives — the inherited ones may
        # be held by a thread that no longer exists in this process.
        self._lock = threading.Lock()
        self._flush_event = threading.Event()
        self._buffer = []
        self._stopped = False
        self._events_dropped_backpressure = 0
        self._events_dropped_signing_error = 0
        self._events_dropped_send_error = 0
        self._events_sent = 0
        self._last_request_id = None
        self._thread = threading.Thread(
            target=self._run, name="checkrd-telemetry-batcher", daemon=True
        )
        self._thread.start()
        logger.debug("checkrd: telemetry batcher restarted after fork (pid=%d)", pid)

    def enqueue(self, event: dict[str, Any]) -> None:
        """Add a telemetry event to the buffer. Thread-safe.

        If a ``before_send`` hook is configured, it runs first: the
        hook can mutate the event, drop it (return ``None``), or
        leave it unchanged. Hook exceptions are logged and treated
        as a drop — a crashing hook never takes the calling thread
        down. Sentry's ``before_send`` semantic; the event is
        operator-controlled at this point and counted only as a
        successful enqueue (no ``dropped_*`` counter).

        If the buffer exceeds ``max_queue_size``, the event is dropped
        with a warning (backpressure).
        """
        if self._before_send is not None:
            try:
                hint: dict[str, object] = {
                    "agent_id": self._signer_agent_id,
                    "event_kind": event.get("event_type", "request_evaluation"),
                }
                mutated = self._before_send(event, hint)
            except Exception:
                # Hook exceptions never crash the caller. The
                # alternative — re-raising — would let a buggy
                # before_send take down the user's request path,
                # which is exactly the failure mode hooks are
                # supposed to insulate against.
                logger.exception(
                    "checkrd: before_send hook raised; dropping event",
                )
                return
            if mutated is None:
                # Operator chose to drop. Not a failure → don't
                # increment the drop counters.
                return
            event = mutated
        # Bound the queue by BOTH event count AND total bytes. Count
        # alone is insufficient — a 10K-event queue of 100KB events is
        # 1GB resident, OOM territory under sustained backpressure.
        # Sentry's transport uses the same dual bound for the same
        # reason. ``_approx_event_bytes`` is conservative (overshoots
        # JSON), which is what an OOM guard wants.
        approx_event_bytes = _approx_event_bytes(event)
        dropped = False
        with self._lock:
            if len(self._buffer) >= self._max_queue_size:
                self._events_dropped_backpressure += 1
                logger.warning(
                    "checkrd: telemetry buffer full (%d events), dropping event",
                    self._max_queue_size,
                )
                dropped = True
            elif self._queue_bytes + approx_event_bytes > self._max_queue_bytes:
                self._events_dropped_backpressure += 1
                logger.warning(
                    "checkrd: telemetry buffer full (%d bytes), dropping event",
                    self._max_queue_bytes,
                )
                dropped = True
            else:
                self._buffer.append(event)
                self._queue_bytes += approx_event_bytes
                if len(self._buffer) >= self._batch_size:
                    self._flush_event.set()
        # Fire the on_drop callback OUTSIDE the lock — a slow or
        # crashing user callback must not block other producers. If the
        # callback itself raises, the drop still happened and the
        # counter is already updated; we log and move on.
        if dropped:
            self._notify_drop("backpressure", 1)

    def _notify_drop(self, reason: DropReason, count: int) -> None:
        """Invoke the user-supplied on_drop callback, if any.

        Runs without the batcher lock so a slow callback cannot stall
        other producers. Exceptions from the callback are swallowed
        and logged at WARNING — drop telemetry is already a failure
        mode, we don't compound it with an uncaught exception from
        the user's crash path.
        """
        if self._on_drop is None:
            return
        try:
            self._on_drop(reason, count)
        except Exception:
            # Never let on_drop failures re-raise into the caller — the
            # caller is already on a failure path (the drop itself).
            logger.exception(
                "checkrd: on_drop callback raised (reason=%s, count=%d)",
                reason,
                count,
            )

    def flush(self) -> None:
        """Flush buffered events immediately. Safe to call from any thread."""
        events = self._drain()
        if events:
            self._send(events)

    def stop(self) -> None:
        """Stop the batcher thread and flush remaining events.

        Called automatically via ``atexit`` and from ``client.close()``.
        Safe to call multiple times.
        """
        if self._stopped:
            return
        self._stopped = True
        self._flush_event.set()
        self._thread.join(timeout=5.0)
        # Final flush in case the thread didn't drain everything
        self.flush()

    @property
    def pending_count(self) -> int:
        """Number of events currently buffered. For testing/monitoring."""
        with self._lock:
            return len(self._buffer)

    @property
    def events_sent(self) -> int:
        """Total events successfully sent since creation. Monotonically increasing."""
        return self._events_sent

    @property
    def events_dropped(self) -> int:
        """Total events dropped since creation (all reasons)."""
        return (
            self._events_dropped_backpressure
            + self._events_dropped_signing_error
            + self._events_dropped_send_error
        )

    def diagnostics(self) -> dict[str, Any]:
        """Telemetry pipeline self-diagnostics (Sentry client-reports pattern).

        Returns counters for monitoring dashboards and ``healthy()``::

            {
                "sent": 4500,
                "dropped_backpressure": 0,
                "dropped_signing_error": 0,
                "dropped_send_error": 12,
                "pending": 42,
                "last_request_id": "req_01HZX...",
            }

        ``dropped_signing_error`` is tracked separately from
        ``dropped_send_error`` because the two mean different things:
        signing errors are configuration problems (page), send errors
        are transient network hiccups (warn). Callers that want the
        old combined number can sum the two.

        ``last_request_id`` carries the most recent
        ``Checkrd-Request-Id`` (or fallback ``X-Request-Id``) returned
        by the control plane. Stripe/OpenAI/Anthropic convention —
        operators paste it into a support ticket and the on-call can
        look up the exact server-side request. ``None`` before the
        first batch ships.
        """
        with self._lock:
            pending = len(self._buffer)
        return {
            "sent": self._events_sent,
            "dropped_backpressure": self._events_dropped_backpressure,
            "dropped_signing_error": self._events_dropped_signing_error,
            "dropped_send_error": self._events_dropped_send_error,
            "pending": pending,
            "last_request_id": self._last_request_id,
        }

    def _run(self) -> None:
        """Background thread loop: wait for flush trigger or timeout."""
        while not self._stopped:
            triggered = self._flush_event.wait(timeout=self._flush_interval)
            if triggered:
                self._flush_event.clear()
            events = self._drain()
            if events:
                self._send(events)

    def _drain(self) -> list[dict[str, Any]]:
        """Drain the buffer under the lock. Returns the events."""
        with self._lock:
            if not self._buffer:
                return []
            events = self._buffer[:]
            self._buffer.clear()
            self._queue_bytes = 0
            return events

    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Materialize ``self._verify`` into an ``ssl.SSLContext``.

        Mirrors httpx's contract on top of urllib so the sync and async
        batchers honor the same ``verify=`` argument:

          * ``True``  → ``None`` (urlopen uses the system trust store).
          * ``False`` → unverified context (insecure; emits a one-shot
                        log warning so disabled-in-prod is loud).
          * ``str``   → context loaded from the given CA bundle path.
          * ``ssl.SSLContext`` → returned as-is.
        """
        v = self._verify
        if v is True:
            return None
        if v is False:
            logger.warning(
                "checkrd: TLS verification disabled (verify=False). "
                "This is intended for local development only; never ship "
                "verify=False to production."
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if isinstance(v, str):
            return ssl.create_default_context(cafile=v)
        if isinstance(v, ssl.SSLContext):
            return v
        # Defensive: log + degrade to default rather than crash. The
        # type annotation already rejects this at type-check time, so
        # reaching here means a runtime override slipped past mypy.
        logger.warning(
            "checkrd: ignoring unknown verify=%r; using system trust store",
            v,
        )
        return None

    def _send(self, events: list[dict[str, Any]]) -> None:
        """POST events to the control plane. Failures are logged and dropped.

        Telemetry signing is mandatory: every batch must be signed before
        being sent. If the WASM core can't produce a signature (e.g. the
        engine is in anonymous KMS mode without a local key), the batch is
        dropped with a structured error log. There is no unsigned fallback —
        strong from the ground up.
        """
        # Flatten the nested TelemetryEvent dicts into the flat API format
        flat_events = [self._flatten_event(e) for e in events]

        # Canonicalize the body so the signer and verifier hash identical bytes.
        # sort_keys + compact separators is a stable serialization.
        body = json.dumps(
            {"events": flat_events, "sdk_version": self._sdk_version},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        # Sign the request. If signing fails for any reason, drop the batch
        # with a structured warning rather than send unsigned.
        try:
            sig_headers = self._build_signature_headers(body)
        except CheckrdInitError as exc:
            # Signing errors are CONFIGURATION errors, not network
            # errors. Tracked on a dedicated counter so dashboards can
            # page on sustained signing_error > 0 (rare, deterministic,
            # demands fix) without the noise of occasional send_error
            # from transient ingestion hiccups. See also diagnostics().
            dropped = len(events)
            self._events_dropped_signing_error += dropped
            logger.error(
                "checkrd: telemetry signing unavailable (%s); dropping %d events",
                exc,
                dropped,
            )
            self._notify_drop("signing_error", dropped)
            return

        # Stripe-style idempotency: one ``Idempotency-Key`` generated
        # ONCE before the retry loop and reused across every attempt so
        # the control plane can dedupe a retry of an already-accepted
        # batch. A fresh key per attempt would defeat the whole point.
        # Matches the JS SDK's behavior — both wrappers send the same
        # header shape so dashboards can correlate retries across
        # languages.
        idempotency_key = new_idempotency_key()
        # Consolidated header set: Content-Type, X-API-Key, User-Agent,
        # Idempotency-Key, the X-Checkrd-SDK-* platform family, and
        # optional Checkrd-Version pin — identical to the JS wrapper's
        # ``defaultControlHeaders`` so operators watching the control
        # plane see matching telemetry shapes across SDKs. Traceparent
        # is stamped here (not in the helper) because only telemetry
        # carries an end-to-end trace; key registration and SSE reuse
        # the control plane's own span.
        headers: dict[str, str] = {
            **default_control_headers(
                self._api_key,
                api_version=self._api_version,
                idempotency_key=idempotency_key,
            ),
            "traceparent": _generate_traceparent(),
            **sig_headers,
        }

        # Fast-fail when the breaker is open — no point eating retry
        # budget against a control plane that has been down long
        # enough for the breaker to trip. One request per
        # ``reset_after_secs`` window will still get through (half-open
        # probe) so we recover automatically.
        if not self._breaker.allow():
            dropped = len(events)
            self._events_dropped_send_error += dropped
            logger.debug(
                "checkrd: telemetry send fast-failed (circuit open), "
                "dropping %d events",
                dropped,
            )
            self._notify_drop("send_error", dropped)
            return

        # Centralised retry contract — same table, same hints, same
        # backoff formula as the JS SDK and OpenAI / Stripe. The
        # ``_retry`` helpers honor ``retry-after-ms`` and the
        # ``x-should-retry`` server hint, neither of which the legacy
        # in-line loop respected.
        for attempt in range(self._max_attempts):
            # Stamp X-Checkrd-Retry-Count on retry attempts so the
            # control plane can correlate "this is retry N" in its
            # access logs. Mirrors OpenAI's X-Stainless-Retry-Count.
            attempt_headers = dict(headers)
            if attempt > 0:
                attempt_headers["X-Checkrd-Retry-Count"] = str(attempt)
            req = Request(
                self._url, data=body, headers=attempt_headers, method="POST",
            )
            try:
                # `_verify` follows httpx's contract; map to urllib's
                # `context` parameter. None → urlopen uses the default
                # CA bundle (current behavior). Custom contexts pin or
                # disable verification per the caller's intent.
                ctx = self._build_ssl_context()
                with urlopen(
                    req,
                    timeout=self._request_timeout_secs,
                    context=ctx,
                ) as resp:
                    if resp.status < 400:
                        self._events_sent += len(events)
                        # Capture the server-assigned request-id for
                        # support-ticket correlation. The control plane
                        # echoes ``Checkrd-Request-Id``; we accept the
                        # conventional ``X-Request-Id`` form for
                        # cross-tooling reach.
                        self._last_request_id = (
                            resp.headers.get("Checkrd-Request-Id")
                            or resp.headers.get("X-Request-Id")
                        )
                        self._breaker.record_success()
                        return  # success

                    response_headers = (
                        dict(resp.headers) if hasattr(resp, "headers") else {}
                    )
                    if (
                        should_retry_status(resp.status, response_headers)
                        and attempt < self._max_attempts - 1
                    ):
                        delay = next_backoff(
                            attempt, response_headers,
                            max_sleep_secs=DEFAULT_MAX_SLEEP_SECS,
                        )
                        logger.debug(
                            "checkrd: telemetry send HTTP %d, retry %d/%d in %.2fs",
                            resp.status, attempt + 1, self._max_attempts, delay,
                        )
                        time.sleep(delay)
                        continue

                    # Non-retryable or exhausted retries.
                    dropped = len(events)
                    self._events_dropped_send_error += dropped
                    self._breaker.record_failure()
                    logger.warning(
                        "checkrd: telemetry send failed (HTTP %d), dropping %d events",
                        resp.status, dropped,
                    )
                    self._notify_drop("send_error", dropped)
                    return
            except Exception as e:
                # Network-level failure — no headers to inspect, fall
                # back to local exponential backoff.
                if attempt < self._max_attempts - 1:
                    delay = next_backoff(
                        attempt, {},
                        max_sleep_secs=DEFAULT_MAX_SLEEP_SECS,
                    )
                    logger.debug(
                        "checkrd: telemetry send failed (%s), retry %d/%d in %.2fs",
                        e, attempt + 1, self._max_attempts, delay,
                    )
                    time.sleep(delay)
                    continue
                dropped = len(events)
                self._events_dropped_send_error += dropped
                self._breaker.record_failure()
                logger.warning(
                    "checkrd: telemetry send failed (%s), dropping %d events",
                    e, dropped,
                )
                self._notify_drop("send_error", dropped)

    def _build_signature_headers(self, body: bytes) -> dict[str, str]:
        """Sign the body via the WASM core and return the headers to attach.

        Raises ``CheckrdInitError`` if the engine has no signing key
        (anonymous KMS mode). Production wrap() configures a LocalIdentity
        by default, so this only fires when the caller explicitly opts into
        ExternalIdentity without wiring up KMS-side signing.
        """
        created = int(time.time())
        nonce = secrets.token_hex(16)
        result = self._engine.sign_telemetry_batch(
            batch_json=body,
            target_uri=self._url,
            signer_agent=self._signer_agent_id,
            nonce=nonce,
            created=created,
            expires=created + SIGNATURE_WINDOW_SECS,
        )
        if result is None:
            raise CheckrdInitError(
                "telemetry signing is mandatory but the engine has no signing key. "
                "Use a LocalIdentity (default) or wire up an ExternalIdentity with a "
                "working sign() method."
            )

        return {
            "Content-Digest": result["content_digest"],
            "Signature-Input": result["signature_input"],
            "Signature": result["signature"],
            "X-Checkrd-Signer-Agent": self._signer_agent_id,
        }

    @staticmethod
    def _flatten_event(event: dict[str, Any]) -> dict[str, Any]:
        """Flatten nested TelemetryEvent (WASM output) to flat API input format.

        The WASM core produces ``{request: {url_host, ...}, response: {...}, ...}``.
        The API expects flat fields: ``{url_host, url_path, status_code, ...}``.
        """
        flat: dict[str, Any] = {}

        # Copy top-level scalars
        for key in (
            "event_id",
            "agent_id",
            "instance_id",
            "timestamp",
            "policy_result",
            "deny_reason",
            "trace_id",
            "span_id",
            "parent_span_id",
            "span_name",
            "span_kind",
            "span_status_code",
            "span_status_message",
            # B1: evaluation metadata from WASM core
            "matched_rule",
            "matched_rule_kind",
            "evaluation_path",
        ):
            if key in event:
                flat[key] = event[key]

        # Rename event_id -> request_id (WASM uses event_id, API expects request_id)
        if "event_id" in flat:
            flat["request_id"] = flat.pop("event_id")

        # WASM emits `mode`; API field is `policy_mode`.
        if "mode" in event:
            flat["policy_mode"] = event["mode"]

        # Flatten request sub-object
        request: Optional[dict[str, Any]] = event.get("request")
        if request:
            flat["url_host"] = request.get("url_host", "")
            flat["url_path"] = request.get("url_path", "")
            flat["method"] = request.get("method", "")
            flat["body_hash"] = request.get("body_hash")

        # Flatten response sub-object
        response: Optional[dict[str, Any]] = event.get("response")
        if response:
            flat["status_code"] = response.get("status_code")
            flat["latency_ms"] = response.get("latency_ms")

        return flat
