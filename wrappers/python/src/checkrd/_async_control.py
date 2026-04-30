"""Async-native SSE control receiver.

Mirrors :class:`checkrd.control.ControlReceiver` interface exactly,
swapping the daemon-thread + sync httpx loop for an asyncio Task +
``httpx.AsyncClient.stream()``. Same DSSE bundle install, same auth
short-circuit, same exponential backoff, same diagnostics counters
— async apps just don't pay the threads-vs-event-loop overhead.

Lifecycle:
  - Construct with the same kwargs as ``ControlReceiver``.
  - Call :meth:`start` to spawn the asyncio.Task.
  - Call :meth:`stop` (await it) to cancel and unwind cleanly.
  - The task aborts the in-flight SSE stream via the AbortController
    on the AsyncClient, then awaits the final disconnect — no
    thread-join timeout to coordinate.

The thread-based :class:`ControlReceiver` is kept as the default for
sync callers; nothing changes for users of :class:`checkrd.Checkrd`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

import httpx
import httpx_sse
import yaml

from checkrd._circuit_breaker import CircuitBreaker
from checkrd._policy_state import load_persisted_state, persist_state
from checkrd._trust import trusted_policy_keys, warn_if_misconfigured
from checkrd.exceptions import PolicySignatureError

if TYPE_CHECKING:
    from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")

# Mirrored from ``control.py`` so async + sync use identical defaults.
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_MAX_SSE_EVENT_BYTES = 10 * 1024 * 1024
_POLICY_BUNDLE_MAX_AGE_SECS = 86_400


class AsyncAuthError(Exception):
    """Raised when the control plane rejects the API key. Not retryable."""


class AsyncControlReceiver:
    """Asyncio-native control receiver. See module docstring for rationale.

    Public surface matches :class:`ControlReceiver`:

      - ``start()`` — schedules the asyncio.Task. Idempotent.
      - ``stop()`` (coroutine) — cancels the task and awaits unwind.
      - ``diagnostics()`` — same shape.
    """

    def __init__(
        self,
        *,
        base_url: str,
        agent_id: str,
        api_key: str,
        engine: "WasmEngine",
        api_version: str = "",
        # Caller-supplied async client lets tests inject mocks; default
        # owns its lifecycle and closes on stop().
        http_client: Optional[httpx.AsyncClient] = None,
        # See ``ControlReceiver.__init__`` for the rationale — when the
        # shared breaker is open, skip the SSE reconnect entirely.
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._api_key = api_key
        self._engine = engine
        self._api_version = api_version
        self._breaker = circuit_breaker
        # Hash of the bundle currently installed (or ``None`` until first
        # install). Mirrors the sync receiver's hash cache — see
        # `ControlReceiver` for the OPA / TUF "don't re-apply unchanged"
        # rationale.
        self._last_installed_hash: Optional[str] = None

        if http_client is None:
            # Same timeout posture as the sync receiver:
            #   * connect=10s — fail fast on a wedged DNS / TCP handshake
            #   * read=90s — comfortably above AWS ALB / Cloudflare /
            #     Nginx idle ceilings so a single missed heartbeat
            #     doesn't thrash the connection
            #   * default=None — no overall timeout; SSE streams are
            #     long-lived
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=10.0, read=90.0),
            )
            self._owns_client = True
        else:
            self._client = http_client
            self._owns_client = False

        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._reconnects = 0
        self._events_received = 0
        self._last_event_at: Optional[float] = None

    def start(self) -> None:
        """Schedule the background receiver task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        # Mirror the sync receiver: fire the one-shot trust-roots
        # warning at the moment we begin listening for signed bundles,
        # not at SDK import (where ``base_url`` isn't yet known).
        warn_if_misconfigured(base_url=self._base_url, logger=logger)
        self._restore_persisted_policy_version()
        self._stop_event.clear()
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(
            self._run_loop(), name=f"checkrd-async-control-{self._agent_id}",
        )
        logger.info(
            "checkrd: async control receiver started for agent %s",
            self._agent_id,
        )

    def _restore_persisted_policy_version(self) -> None:
        """Re-install the persisted bundle on startup (OPA pattern).

        Mirror of :meth:`ControlReceiver._restore_persisted_policy_version`
        — see that method for the rationale on why we install the
        envelope rather than just seeding the version counter.
        """
        version, bundle_hash, envelope_json = load_persisted_state()
        if envelope_json is None:
            return
        try:
            self._engine.reload_policy_signed(
                envelope_json,
                json.dumps(trusted_policy_keys()),
                int(time.time()),
                _POLICY_BUNDLE_MAX_AGE_SECS,
            )
        except PolicySignatureError as exc:
            logger.warning(
                "checkrd: persisted policy bundle rejected on restore "
                "(reason=%s, code=%s); will reinitialize from server",
                exc.reason, exc.code,
            )
            return
        self._last_installed_hash = bundle_hash
        logger.info(
            "checkrd: restored persisted policy version=%d, hash=%s… "
            "for agent %s",
            version,
            bundle_hash[:16] if bundle_hash else "<none>",
            self._agent_id,
        )

    async def stop(self) -> None:
        """Cancel the receiver task and await its unwind.

        ``await``-able for clean structured-concurrency shutdown::

            async with AsyncCheckrd(...) as client:
                ...
            # await client.aclose() called automatically; awaits stop()
        """
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass
        logger.info(
            "checkrd: async control receiver stopped for agent %s",
            self._agent_id,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Snapshot of receiver counters, parity with the sync version."""
        return {
            "running": self._task is not None and not self._task.done(),
            "connected": self._task is not None and not self._task.done(),
            "reconnects": self._reconnects,
            "events_received": self._events_received,
            "last_event_at": self._last_event_at,
        }

    # --- internal ---

    def _control_headers(self) -> dict[str, str]:
        """GET-side header set — same as ``ControlReceiver._control_headers``."""
        from checkrd._platform import default_control_headers

        return default_control_headers(
            self._api_key,
            api_version=self._api_version,
            content_type="",  # GET: no body
        )

    async def _run_loop(self) -> None:
        """Reconnect loop. Mirrors ``ControlReceiver._run_loop``."""
        backoff = _INITIAL_BACKOFF
        while not self._stop_event.is_set():
            # Short-circuit when the shared circuit breaker says the
            # control plane is down. See the sync receiver for the
            # full rationale.
            if self._breaker is not None and not self._breaker.allow():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            try:
                await self._run_sse()
                if self._breaker is not None:
                    self._breaker.record_success()
                backoff = _INITIAL_BACKOFF
            except AsyncAuthError as exc:
                # Auth errors are permanent — stop forever.
                logger.error("checkrd: %s", exc)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._reconnects += 1
                if self._breaker is not None:
                    self._breaker.record_failure()
                logger.warning(
                    "checkrd: SSE connection failed: %s, retrying in %.0fs",
                    exc, backoff,
                )
                # Polling fallback while waiting — same as sync receiver.
                try:
                    await self._poll_once()
                    if self._breaker is not None:
                        self._breaker.record_success()
                except Exception as poll_exc:
                    if self._breaker is not None:
                        self._breaker.record_failure()
                    logger.warning("checkrd: poll fallback failed: %s", poll_exc)

                # Sleep with stop-event interrupt.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff,
                    )
                    return  # stop event was set
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _run_sse(self) -> None:
        """Connect and process events until disconnect."""
        url = f"{self._base_url}/v1/agents/{self._agent_id}/control"
        headers = self._control_headers()

        async with httpx_sse.aconnect_sse(
            self._client, "GET", url, headers=headers,
        ) as source:
            resp = source.response
            if resp.status_code in (401, 403):
                raise AsyncAuthError(
                    f"Control plane returned {resp.status_code} -- "
                    "check your API key. Stopping control receiver."
                )
            async for sse in source.aiter_sse():
                if self._stop_event.is_set():
                    return
                self._handle_event(sse)
                self._events_received += 1
                self._last_event_at = time.time()

    def _handle_event(self, sse: httpx_sse.ServerSentEvent) -> None:
        """Dispatch a single SSE event. Identical to sync version."""
        if len(sse.data) > _MAX_SSE_EVENT_BYTES:
            logger.warning(
                "checkrd: SSE event too large (%d bytes, limit %d); dropping",
                len(sse.data), _MAX_SSE_EVENT_BYTES,
            )
            return
        try:
            if sse.event == "init":
                data = json.loads(sse.data)
                active = data.get("kill_switch_active", False)
                self._engine.set_kill_switch(active)
                # Self-bootstrap: install the active policy from the same
                # envelope shape `policy_updated` uses. See sync receiver
                # in `control.py` for the rationale.
                envelope = data.get("policy_envelope")
                logger.debug(
                    "checkrd: init state received (kill_switch=%s, policy=%s)",
                    active,
                    "present" if envelope else "absent",
                )
                if envelope is not None:
                    # Forward `active_policy_hash` so the hash cache can
                    # short-circuit the FFI call on idempotent replay.
                    self._apply_policy_update(
                        {
                            "policy_envelope": envelope,
                            "active_policy_hash": data.get("active_policy_hash"),
                        },
                        source="SSE init",
                    )
            elif sse.event == "kill_switch":
                data = json.loads(sse.data)
                active = data["active"]
                self._engine.set_kill_switch(active)
                logger.info(
                    "checkrd: kill switch %s via SSE",
                    "activated" if active else "deactivated",
                )
            elif sse.event == "policy_updated":
                data = json.loads(sse.data)
                self._apply_policy_update(data, source="SSE")
        except (json.JSONDecodeError, KeyError, TypeError, yaml.YAMLError) as exc:
            logger.warning("checkrd: malformed SSE event data: %s", exc)
        except Exception as exc:
            logger.error("checkrd: error handling SSE event: %s", exc)

    async def _poll_once(self) -> None:
        """Poll the control state endpoint once via the async client."""
        url = f"{self._base_url}/v1/agents/{self._agent_id}/control/state"
        headers = self._control_headers()
        # Per-call timeout so a hung poll doesn't extend the outer
        # backoff window beyond what the SSE reconnect promises.
        resp = await self._client.get(
            url, headers=headers, timeout=10.0,
        )
        resp.raise_for_status()
        state = resp.json()
        self._engine.set_kill_switch(state.get("kill_switch_active", False))
        envelope = state.get("policy_envelope")
        if envelope is not None:
            # Forward `active_policy_hash` so the hash cache works on the
            # poll path identically to SSE. Mirrors the sync receiver.
            self._apply_policy_update(
                {
                    "policy_envelope": envelope,
                    "active_policy_hash": state.get("active_policy_hash"),
                },
                source="poll",
            )

    def _apply_policy_update(self, data: dict[str, Any], source: str) -> None:
        """Install a signed policy update — identical to the sync path.

        Synchronous because :meth:`WasmEngine.reload_policy_signed` is a
        sync FFI call into wasmtime; there is no async I/O to await.
        Persistence to disk afterwards is also sync (filesystem ops on
        local fast disk are not worth the asyncio overhead).

        See :meth:`ControlReceiver._apply_policy_update` for the full
        rationale on the hash-cache idempotency layer.
        """
        envelope = data.get("policy_envelope")
        if envelope is None:
            logger.warning(
                "checkrd: policy update via %s missing required policy_envelope; "
                "keeping previous policy",
                source,
            )
            return

        incoming_hash = data.get("hash") or data.get("active_policy_hash")
        if incoming_hash is not None and (
            not isinstance(incoming_hash, str)
            or len(incoming_hash) != 64
            or any(c not in "0123456789abcdef" for c in incoming_hash)
        ):
            incoming_hash = None

        if (
            incoming_hash is not None
            and self._last_installed_hash is not None
            and incoming_hash == self._last_installed_hash
        ):
            logger.debug(
                "checkrd: signed policy update via %s already installed "
                "(hash=%s…); skipping no-op re-apply",
                source,
                incoming_hash[:16],
            )
            return

        try:
            envelope_json = json.dumps(envelope)
            trusted_json = json.dumps(trusted_policy_keys())
            self._engine.reload_policy_signed(
                envelope_json,
                trusted_json,
                int(time.time()),
                _POLICY_BUNDLE_MAX_AGE_SECS,
            )
            logger.info(
                "checkrd: signed policy installed via %s (version=%s)",
                source, data.get("version"),
            )
        except PolicySignatureError as exc:
            logger.warning(
                "checkrd: signed policy update rejected via %s "
                "(reason=%s, code=%s); keeping previous policy",
                source, exc.reason, exc.code,
            )
            return

        # Trust the server's `active_policy_hash` / `hash` field
        # exclusively — it's `SHA-256(yaml_content)`, computed at publish
        # time. The SDK does NOT compute a fallback because the only
        # bytes available locally are the DSSE payload (JSON-wrapped
        # PolicyBundle), and SHA-256 of those bytes ≠ SHA-256 of the
        # source YAML. Any "fallback" hash would silently mismatch the
        # server's hash forever, defeating the cache. See the sync
        # receiver in ``control.py`` for the full rationale.
        if incoming_hash is None:
            logger.warning(
                "checkrd: signed policy install via %s missing hash field; "
                "cache + persistence skipped (next install will recover)",
                source,
            )
            return
        self._last_installed_hash = incoming_hash
        try:
            new_version = self._engine.get_active_policy_version()
            # Persist the verified envelope alongside (version, hash) so
            # the next process boot can install via the OPA-pattern
            # restore path. Without the envelope, the cache-hit on init
            # would leave the engine empty.
            persist_state(
                new_version,
                bundle_hash=incoming_hash,
                bundle_envelope_json=envelope_json,
            )
        except Exception as exc:
            logger.warning(
                "checkrd: failed to persist policy version high water mark "
                "(%s); rollback defense will not survive restart",
                exc,
            )


__all__ = ["AsyncControlReceiver", "AsyncAuthError"]
