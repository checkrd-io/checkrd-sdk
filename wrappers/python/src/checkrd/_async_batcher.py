"""Async-native telemetry batcher.

Mirrors :class:`checkrd.batcher.TelemetryBatcher` interface so the
:class:`checkrd.AsyncCheckrd` client can drop in an asyncio-based
batcher instead of the thread-based one. Same retry contract, same
circuit breaker, same Idempotency-Key, same signing —
the only thing that changes is the I/O substrate.

Why a separate class:
  - Threads-in-asyncio works but creates context-switch overhead on
    every ``enqueue()`` from a coroutine. For an async-first app
    making ~10K LLM calls/min the overhead adds up.
  - Async cancellation is cleaner. ``await client.aclose()`` cancels
    the worker task and awaits the final flush in one structured
    concurrency operation; the threaded version requires
    ``join(timeout=...)`` which can swallow drops.
  - Errors surface to the calling event loop instead of being trapped
    inside a daemon thread that nobody is awaiting.

The thread-based :class:`TelemetryBatcher` is kept as the default for
sync callers; nothing changes for users of the synchronous
:class:`checkrd.Checkrd` client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import time
from typing import TYPE_CHECKING, Any, Optional, Union

import httpx

from checkrd._circuit_breaker import CircuitBreaker
from checkrd._platform import default_control_headers, new_idempotency_key
from checkrd._retry import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_SLEEP_SECS,
    next_backoff,
    should_retry_status,
)
from checkrd._version import __version__
from checkrd.batcher import (
    SIGNATURE_WINDOW_SECS,
    DropReason,
    OnDropCallback,
    TelemetryBatcher,
)
from checkrd.exceptions import CheckrdInitError
from checkrd.hooks import BeforeSendHook

if TYPE_CHECKING:
    from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")


def _generate_traceparent() -> str:
    """W3C Trace Context value — see :func:`checkrd.batcher._generate_traceparent`."""
    trace_id = secrets.token_hex(16)
    parent_id = secrets.token_hex(8)
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


class AsyncTelemetryBatcher:
    """Asyncio-based telemetry batcher.

    Behavioral parity with :class:`TelemetryBatcher`:
      - Same default ``batch_size`` / ``flush_interval_secs`` /
        ``max_queue_size``.
      - Same backpressure (drop on full queue) + ``on_drop`` callback.
      - Same signing requirement (no unsigned fallback) — drops a
        batch with ``signing_error`` when the engine has no signing
        key.
      - Same retry contract via :mod:`checkrd._retry`.
      - Same circuit breaker hooks.
      - Same diagnostics counters.

    The interface is intentionally a strict superset (constructor
    takes the same kwargs plus an optional pre-existing
    ``httpx.AsyncClient``) so future code can transparently swap from
    sync to async without changing call sites.
    """

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
        http_client: Optional[httpx.AsyncClient] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        request_timeout_secs: float = 30.0,
        connect_timeout_secs: float = 5.0,
        before_send: Optional["BeforeSendHook"] = None,
        verify: Union[bool, str, "ssl.SSLContext"] = True,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/telemetry"
        self._api_key = api_key
        self._batch_size = batch_size
        self._flush_interval = flush_interval_secs
        self._max_queue_size = max_queue_size
        self._max_queue_bytes = max_queue_bytes
        self._queue_bytes = 0
        self._sdk_version = __version__
        self._engine = engine
        self._signer_agent_id = signer_agent_id
        self._on_drop = on_drop
        self._api_version = api_version
        self._max_attempts = max_attempts
        self._request_timeout_secs = request_timeout_secs
        self._connect_timeout_secs = connect_timeout_secs
        self._before_send = before_send
        self._breaker = (
            circuit_breaker if circuit_breaker is not None else CircuitBreaker()
        )

        self._buffer: list[dict[str, Any]] = []
        # asyncio.Lock not strictly required (single event loop) but
        # used for parity with TelemetryBatcher's protection of the
        # shared buffer. Cheap when uncontended.
        self._lock = asyncio.Lock()
        self._flush_event = asyncio.Event()
        self._stopped = False

        # Counters (Sentry client-reports pattern).
        self._events_dropped_backpressure = 0
        self._events_dropped_signing_error = 0
        self._events_dropped_send_error = 0
        self._events_sent = 0

        # If the caller didn't bring an httpx.AsyncClient, build one
        # we own — closed on ``stop()``. Customer-supplied clients are
        # never closed by the batcher; the customer keeps lifecycle.
        # `verify` follows httpx's contract: True (system trust store,
        # default), False (insecure — local-dev only), str (CA bundle
        # path), or `ssl.SSLContext` (full control, e.g., cert pinning).
        if http_client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self._request_timeout_secs,
                    connect=self._connect_timeout_secs,
                ),
                verify=verify,
            )
            self._owns_client = True
        else:
            self._client = http_client
            self._owns_client = False

        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        """Spawn the background worker task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(
            self._run(), name="checkrd-telemetry-batcher",
        )

    def enqueue(self, event: dict[str, Any]) -> None:
        """Append an event to the buffer.

        Synchronous (no ``await``) so the same call site works from
        both sync and async code paths in the transport layer. Thread
        safety is implicit — asyncio is single-threaded per event loop.

        ``before_send`` runs first when configured. See
        :class:`TelemetryBatcher.enqueue` for the contract — same
        semantics, same Sentry-pattern hook.
        """
        if self._before_send is not None:
            try:
                hint: dict[str, object] = {
                    "agent_id": self._signer_agent_id,
                    "event_kind": event.get("event_type", "request_evaluation"),
                }
                mutated = self._before_send(event, hint)
            except Exception:
                logger.exception(
                    "checkrd: before_send hook raised; dropping event",
                )
                return
            if mutated is None:
                return
            event = mutated
        # Bound the queue by BOTH event count AND total bytes. Count
        # alone is insufficient — a 10K-event queue of 100KB events is
        # 1GB of resident memory, OOM territory under sustained
        # backpressure (a stalled control plane). Sentry's transport
        # uses the same dual bound for the same reason.
        approx_event_bytes = _approx_event_bytes(event)
        if len(self._buffer) >= self._max_queue_size:
            self._events_dropped_backpressure += 1
            logger.warning(
                "checkrd: telemetry buffer full (%d events), dropping event",
                self._max_queue_size,
            )
            self._notify_drop("backpressure", 1)
            return
        if self._queue_bytes + approx_event_bytes > self._max_queue_bytes:
            self._events_dropped_backpressure += 1
            logger.warning(
                "checkrd: telemetry buffer full (%d bytes), dropping event",
                self._max_queue_bytes,
            )
            self._notify_drop("backpressure", 1)
            return
        self._buffer.append(event)
        self._queue_bytes += approx_event_bytes
        if len(self._buffer) >= self._batch_size:
            self._flush_event.set()

    def _notify_drop(self, reason: DropReason, count: int) -> None:
        if self._on_drop is None:
            return
        try:
            self._on_drop(reason, count)
        except Exception:
            logger.exception(
                "checkrd: on_drop callback raised (reason=%s, count=%d)",
                reason, count,
            )

    async def flush(self) -> None:
        """Flush the current buffer immediately."""
        events = self._drain()
        if events:
            await self._send(events)

    async def stop(self) -> None:
        """Stop the worker task and flush remaining events.

        Safe to call multiple times. ``await``-able for clean
        structured-concurrency shutdown::

            async with AsyncCheckrd(...) as client:
                ...
            # await client.aclose() called automatically; awaits flush
        """
        if self._stopped:
            return
        self._stopped = True
        self._flush_event.set()
        if self._task is not None:
            try:
                # Give the worker a chance to drain on its own.
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        # Final flush in case the worker didn't drain everything.
        await self.flush()
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    @property
    def events_sent(self) -> int:
        return self._events_sent

    @property
    def events_dropped(self) -> int:
        return (
            self._events_dropped_backpressure
            + self._events_dropped_signing_error
            + self._events_dropped_send_error
        )

    def diagnostics(self) -> dict[str, int]:
        return {
            "sent": self._events_sent,
            "dropped_backpressure": self._events_dropped_backpressure,
            "dropped_signing_error": self._events_dropped_signing_error,
            "dropped_send_error": self._events_dropped_send_error,
            "pending": len(self._buffer),
        }

    async def _run(self) -> None:
        """Background loop: wait for flush trigger or interval, then drain."""
        while not self._stopped:
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(), timeout=self._flush_interval,
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            events = self._drain()
            if events:
                await self._send(events)

    def _drain(self) -> list[dict[str, Any]]:
        if not self._buffer:
            return []
        events = self._buffer[:]
        self._buffer.clear()
        # Reset byte counter — the buffer is empty. Approximate
        # accounting is fine here; the goal is OOM protection, not
        # exact memory bookkeeping.
        self._queue_bytes = 0
        return events

    async def _send(self, events: list[dict[str, Any]]) -> None:
        """POST a batch to the control plane with full retry contract."""
        flat_events = [TelemetryBatcher._flatten_event(e) for e in events]
        body = json.dumps(
            {"events": flat_events, "sdk_version": self._sdk_version},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        try:
            sig_headers = self._build_signature_headers(body)
        except CheckrdInitError as exc:
            dropped = len(events)
            self._events_dropped_signing_error += dropped
            logger.error(
                "checkrd: telemetry signing unavailable (%s); dropping %d events",
                exc, dropped,
            )
            self._notify_drop("signing_error", dropped)
            return

        idempotency_key = new_idempotency_key()
        headers: dict[str, str] = {
            **default_control_headers(
                self._api_key,
                api_version=self._api_version,
                idempotency_key=idempotency_key,
            ),
            "traceparent": _generate_traceparent(),
            **sig_headers,
        }

        if not self._breaker.allow():
            dropped = len(events)
            self._events_dropped_send_error += dropped
            logger.debug(
                "checkrd: async telemetry send fast-failed (circuit open), "
                "dropping %d events",
                dropped,
            )
            self._notify_drop("send_error", dropped)
            return

        for attempt in range(self._max_attempts):
            # Stamp X-Checkrd-Retry-Count on retry attempts (mirrors
            # OpenAI's X-Stainless-Retry-Count) so the control plane
            # can correlate retry waves in its access logs.
            attempt_headers = dict(headers)
            if attempt > 0:
                attempt_headers["X-Checkrd-Retry-Count"] = str(attempt)
            try:
                response = await self._client.post(
                    self._url, content=body, headers=attempt_headers,
                )
                if response.status_code < 400:
                    self._events_sent += len(events)
                    self._breaker.record_success()
                    return

                response_headers = dict(response.headers)
                if (
                    should_retry_status(response.status_code, response_headers)
                    and attempt < self._max_attempts - 1
                ):
                    delay = next_backoff(
                        attempt, response_headers,
                        max_sleep_secs=DEFAULT_MAX_SLEEP_SECS,
                    )
                    logger.debug(
                        "checkrd: async telemetry send HTTP %d, retry "
                        "%d/%d in %.2fs",
                        response.status_code, attempt + 1,
                        self._max_attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                dropped = len(events)
                self._events_dropped_send_error += dropped
                self._breaker.record_failure()
                logger.warning(
                    "checkrd: async telemetry send failed (HTTP %d), dropping "
                    "%d events",
                    response.status_code, dropped,
                )
                self._notify_drop("send_error", dropped)
                return
            except Exception as exc:
                if attempt < self._max_attempts - 1:
                    delay = next_backoff(
                        attempt, {},
                        max_sleep_secs=DEFAULT_MAX_SLEEP_SECS,
                    )
                    logger.debug(
                        "checkrd: async telemetry send failed (%s), retry "
                        "%d/%d in %.2fs",
                        exc, attempt + 1, self._max_attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                dropped = len(events)
                self._events_dropped_send_error += dropped
                self._breaker.record_failure()
                logger.warning(
                    "checkrd: async telemetry send failed (%s), dropping "
                    "%d events",
                    exc, dropped,
                )
                self._notify_drop("send_error", dropped)

    def _build_signature_headers(self, body: bytes) -> dict[str, str]:
        """Sign the body via the WASM core. Identical to ``TelemetryBatcher``."""
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
                "telemetry signing is mandatory but the engine has no "
                "signing key. Use a LocalIdentity (default) or wire up an "
                "ExternalIdentity with a working sign() method."
            )
        return {
            "Content-Digest": result["content_digest"],
            "Signature-Input": result["signature_input"],
            "Signature": result["signature"],
            "X-Checkrd-Signer-Agent": self._signer_agent_id,
        }


__all__ = ["AsyncTelemetryBatcher"]
