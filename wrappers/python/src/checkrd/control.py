"""Real-time control signal receiver for Checkrd.

Connects to the control plane SSE endpoint to receive kill switch
and policy update signals. Falls back to polling if SSE is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any, Optional

import httpx
import httpx_sse
import yaml

from checkrd._circuit_breaker import CircuitBreaker
from checkrd._fork import register_fork_handler
from checkrd._policy_state import load_persisted_state, persist_state
from checkrd._trust import trusted_policy_keys, warn_if_misconfigured
from checkrd.exceptions import PolicySignatureError

if TYPE_CHECKING:
    from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")

_INITIAL_BACKOFF = 1.0  # seconds
_MAX_BACKOFF = 60.0  # seconds
# Maximum size of a single SSE event data payload before json.loads().
# A compromised or buggy control plane could send a multi-GB JSON blob that
# causes OOM. 10 MB is generous for any legitimate policy bundle.
_MAX_SSE_EVENT_BYTES = 10 * 1024 * 1024  # 10 MB

# Maximum age of a signed policy bundle, in seconds. Bundles signed more
# than this many seconds in the past are rejected as stale by the WASM
# core's reload_policy_signed. 24 hours is the production default — long
# enough to absorb operator activity gaps, short enough to bound replay
# attack windows.
_POLICY_BUNDLE_MAX_AGE_SECS = 86_400


# Fork-safety registry. The ``os.register_at_fork`` handler walks every
# live receiver in the forked child and calls ``_reinit_after_fork`` on
# each. Same pattern the telemetry batcher uses; see ``checkrd._fork``.
_LIVE_RECEIVERS: "weakref.WeakSet[ControlReceiver]" = weakref.WeakSet()


class AuthError(Exception):
    """Raised when the control plane rejects the API key. Not retryable."""


class ControlReceiver:
    """Background receiver for control signals from the Checkrd control plane.

    Manages an SSE connection to ``GET /v1/agents/{agent_id}/control``.
    On disconnect, reconnects with exponential backoff. Between reconnection
    attempts, polls ``GET /v1/agents/{agent_id}/control/state`` once as a
    fallback. If the control plane is unreachable, the wrapper keeps working
    with its last known state -- no crashes, no blocking.
    """

    def __init__(
        self,
        *,
        base_url: str,
        agent_id: str,
        api_key: str,
        engine: WasmEngine,
        api_version: str = "",
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._api_key = api_key
        self._engine = engine
        self._api_version = api_version
        # Hash of the policy bundle currently installed (or ``None``
        # before the first install). Used to short-circuit identical
        # re-installs at the wrapper layer — the OPA bundle / TUF
        # "don't re-apply unchanged" pattern. Without this, the WASM
        # core's strict-greater monotonic check would reject the
        # legitimate post-restart re-bootstrap of the same active
        # version. Loaded from disk in `_restore_persisted_policy_version`
        # so the cache survives restarts; updated on every successful
        # install in `_apply_policy_update`.
        self._last_installed_hash: Optional[str] = None
        # Shared CircuitBreaker — when the telemetry batcher trips it
        # because the control plane is hard-down, the receiver should
        # not waste a 90-second SSE read timeout on every reconnect.
        # ``None`` keeps legacy single-component behaviour (each
        # subsystem retries independently); callers that want unified
        # control-plane health detection pass the same breaker
        # instance the batcher uses. The reset window's jitter
        # (``CircuitBreaker.resetJitterMs``) prevents thundering-herd
        # reconnects across multiple agents.
        self._breaker = circuit_breaker
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pid = os.getpid()
        _LIVE_RECEIVERS.add(self)

    def start(self) -> None:
        """Start the background control receiver thread.

        Restores the persisted policy version high water mark from disk
        before launching the receiver. This is the cross-restart half of
        the rollback defense: without it, the in-memory monotonic check
        starts at 0 on every restart, and an attacker who can restart the
        SDK process can replay an old, signed-but-stale bundle exactly
        once. With it, the monotonic check spans the full life of the
        installation.

        Fork-safe via the module-level ``os.register_at_fork`` handler:
        if the host forks between ``__init__`` and ``start``, the
        handler runs in the child first and clears the inherited stale
        thread / stop-event references.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        # Loud one-shot warning when production trust roots are missing
        # AND we're pointed at a production control plane — every signed
        # policy update would silently be rejected. Fired here (not at
        # SDK import) because base_url is only known at receiver-construct
        # time, and only matters when we're about to start listening.
        warn_if_misconfigured(base_url=self._base_url, logger=logger)
        self._restore_persisted_policy_version()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"checkrd-control-{self._agent_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("checkrd: control receiver started for agent %s", self._agent_id)

    def _restore_persisted_policy_version(self) -> None:
        """Re-install the last verified bundle from disk on startup.

        OPA bundle / TUF client pattern: persist the verified envelope
        on every install, re-apply on the next process boot. The
        engine has rules from the first request (no "empty engine"
        window before SSE init lands) and the subsequent SSE init's
        identical bundle short-circuits as a cache hit instead of
        being rejected by the strict-greater monotonic check.

        Re-verification on load is mandatory:

          - Trust list may have rotated since the last write.
          - Bundle ``signed_at`` may have aged past ``max_age_secs``.
          - The file on disk could have been tampered with.

        ``reload_policy_signed`` enforces all three. On any failure we
        log and leave the engine empty — the SDK falls through to fresh
        SSE/poll fetch on the next event, which is the same posture as
        an initial install.

        Best-effort: never raises. If persistence is broken, defense
        degrades from "across restarts" to "within this process",
        never to "no defense at all".
        """
        version, bundle_hash, envelope_json = load_persisted_state()
        if envelope_json is None:
            # No persisted bundle (fresh install, legacy schema, or the
            # last write was version-only). Nothing to seed; the next
            # SSE/poll bundle installs into a clean engine.
            return
        try:
            self._engine.reload_policy_signed(
                envelope_json,
                json.dumps(trusted_policy_keys()),
                int(time.time()),
                _POLICY_BUNDLE_MAX_AGE_SECS,
            )
        except PolicySignatureError as exc:
            # Stale, sig-failed, or rejected by the trust list — drop
            # the persisted bundle and wait for SSE to re-deliver. We
            # log at warning because this is the operational case
            # (e.g., daily key rotation invalidates a 24h-old persist),
            # not a security incident.
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

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("checkrd: control receiver stopped for agent %s", self._agent_id)

    def _reinit_after_fork(self) -> None:
        """Re-initialize threading state in the forked child process.

        Called by the ``os.register_at_fork`` handler registered against
        ``_LIVE_RECEIVERS`` (see bottom of this module). Idempotent: a
        no-op when the recorded PID still matches the current one.

        After fork, the parent's daemon thread does not exist in the
        child. The inherited ``_stop`` Event and ``_thread`` reference
        are stale. Reset them so a subsequent ``start()`` in the child
        spawns a fresh thread.
        """
        pid = os.getpid()
        if pid == self._pid:
            return
        self._pid = pid
        self._stop = threading.Event()
        self._thread = None
        logger.debug("checkrd: control receiver reset after fork (pid=%d)", pid)

    # -- internal --

    def _run_loop(self) -> None:
        """Main loop: try SSE, fall back to polling on failure, reconnect with backoff."""
        backoff = _INITIAL_BACKOFF
        while not self._stop.is_set():
            # Short-circuit when the shared circuit breaker is open —
            # the batcher already discovered the control plane is down,
            # there is no point holding a 90-second SSE read open just
            # to confirm. Sleep for the breaker's jittered reset window
            # and try again. Without a shared breaker this is a no-op
            # (``allow()`` returns True) and the legacy independent-
            # backoff path runs unchanged.
            if self._breaker is not None and not self._breaker.allow():
                if self._stop.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            try:
                self._run_sse()
                # Clean disconnect → reset breaker (control plane was
                # reachable) and reset backoff for the next attempt.
                if self._breaker is not None:
                    self._breaker.record_success()
                backoff = _INITIAL_BACKOFF
            except AuthError as exc:
                # Auth errors are not retryable -- stop permanently
                logger.error("checkrd: %s", exc)
                return
            except Exception as exc:
                if self._breaker is not None:
                    self._breaker.record_failure()
                logger.warning(
                    "checkrd: SSE connection failed: %s, retrying in %.0fs", exc, backoff
                )

                # Poll once as fallback while waiting for reconnect
                try:
                    self._poll_once()
                    # The poll succeeded → control plane reachable
                    # (just SSE may have flapped). Reset the breaker
                    # so the batcher doesn't fast-fail unnecessarily.
                    if self._breaker is not None:
                        self._breaker.record_success()
                except Exception as poll_exc:
                    if self._breaker is not None:
                        self._breaker.record_failure()
                    logger.warning("checkrd: poll fallback failed: %s", poll_exc)

                # Wait with backoff (interruptible by stop())
                if self._stop.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def _control_headers(self) -> dict[str, str]:
        """GET-side header set for SSE subscribe + state poll.

        GET requests skip ``Content-Type`` / ``Idempotency-Key`` but
        still carry the ``X-Checkrd-SDK-*`` platform family and
        optional ``Checkrd-Version`` pin so operators can trace SSE
        connections through the same dashboards that watch POST
        traffic.
        """
        from checkrd._platform import default_control_headers

        return default_control_headers(
            self._api_key,
            api_version=self._api_version,
            content_type="",  # GET: no body, so no Content-Type
        )

    def _run_sse(self) -> None:
        """Connect to the SSE endpoint and process events until disconnect."""
        url = f"{self._base_url}/v1/agents/{self._agent_id}/control"
        headers = self._control_headers()

        with httpx.Client(timeout=httpx.Timeout(None, connect=10.0, read=90.0)) as client:
            with httpx_sse.connect_sse(client, "GET", url, headers=headers) as source:
                # Check for auth errors before iterating -- a 401/403 means
                # the API key is wrong and retrying won't help.
                resp = source.response
                if resp.status_code in (401, 403):
                    raise AuthError(
                        f"Control plane returned {resp.status_code} -- "
                        "check your API key. Stopping control receiver."
                    )
                for sse in source.iter_sse():
                    if self._stop.is_set():
                        return
                    self._handle_event(sse)

    def _handle_event(self, sse: httpx_sse.ServerSentEvent) -> None:
        """Dispatch a single SSE event to the appropriate engine method."""
        # Guard against oversized events from a compromised control plane.
        if len(sse.data) > _MAX_SSE_EVENT_BYTES:
            logger.warning(
                "checkrd: SSE event too large (%d bytes, limit %d); dropping",
                len(sse.data),
                _MAX_SSE_EVENT_BYTES,
            )
            return
        try:
            if sse.event == "init":
                data = json.loads(sse.data)
                active = data.get("kill_switch_active", False)
                self._engine.set_kill_switch(active)
                # Self-bootstrap: the init payload carries the full signed
                # envelope of the agent's active policy. Without this, an
                # SDK starting up against an existing-active-policy agent
                # would never enforce — the `policy_updated` SSE event
                # only fires on policy *change*. Reuse the exact same
                # apply-path used for `policy_updated` so verification +
                # rollback-protection + freshness all run identically.
                envelope = data.get("policy_envelope")
                logger.debug(
                    "checkrd: init state received (kill_switch=%s, policy=%s)",
                    active,
                    "present" if envelope else "absent",
                )
                if envelope is not None:
                    # Forward `active_policy_hash` so the wrapper's hash
                    # cache can short-circuit the FFI call when this is
                    # the same bundle the engine already had (post-
                    # restart bootstrap, SSE reconnect, etc.).
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

            # heartbeat / unknown events are silently ignored

        except (json.JSONDecodeError, KeyError, TypeError, yaml.YAMLError) as exc:
            logger.warning("checkrd: malformed SSE event data: %s", exc)
        except Exception as exc:
            logger.error("checkrd: error handling SSE event: %s", exc)

    def _poll_once(self) -> None:
        """Poll the control state endpoint once and apply changes."""
        url = f"{self._base_url}/v1/agents/{self._agent_id}/control/state"
        headers = self._control_headers()

        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            state = resp.json()

        self._engine.set_kill_switch(state.get("kill_switch_active", False))
        logger.debug(
            "checkrd: poll fallback applied (kill_switch=%s)", state.get("kill_switch_active")
        )
        # If the polling response carries a signed policy envelope, install
        # it. Forward `active_policy_hash` so the wrapper's idempotency
        # cache works on the poll path too — same code path as SSE.
        envelope = state.get("policy_envelope")
        if envelope is not None:
            self._apply_policy_update(
                {
                    "policy_envelope": envelope,
                    "active_policy_hash": state.get("active_policy_hash"),
                },
                source="poll",
            )

    def _apply_policy_update(self, data: dict[str, Any], source: str) -> None:
        """Install a signed policy update via the WASM core verifier.

        ``data`` is the JSON dict from an SSE ``init`` / ``policy_updated``
        event or a polling response. The dict MUST contain a
        ``policy_envelope`` field — strong from the ground up, there is no
        unsigned distribution path. Updates without an envelope are
        rejected with a structured warning.

        # Idempotency at the wrapper layer

        Before invoking the FFI, this method compares the bundle's
        content hash against ``self._last_installed_hash``. Match ⇒ skip
        the install entirely (the OPA bundle / TUF "don't re-apply
        unchanged" pattern). The hash is sourced from:

          1. The event's ``hash`` field (``policy_updated`` event,
             matches the server-computed SHA-256 of the YAML).
          2. The ``active_policy_hash`` co-field on ``init`` / poll
             responses — same value, different shape.
          3. As a last resort, ``hashlib.sha256`` over the verified
             payload bytes after the FFI call succeeds, so first-install
             after restart still fills the cache for next time.

        Without the hash cache, the WASM core's strict-greater monotonic
        check rejects the legitimate post-restart re-bootstrap of the
        same active version (the persisted high-water == incoming
        version). This wrapper layer is where idempotency lives; the
        FFI's strict-greater rule stays as the security-critical safety
        net for genuine rollback attempts.

        # FFI defenses (still apply when the install does run)

        - DSSE signature verification against the trusted key list
        - Strict-greater monotonic version check (rollback rejection)
        - Bundle freshness check (max age 24h, default)
        - Cross-type replay defense via DSSE payload type binding

        On any failure, the previous policy is left in place and a
        structured warning is logged. The SDK never silently installs an
        unverified policy.
        """
        envelope = data.get("policy_envelope")
        if envelope is None:
            logger.warning(
                "checkrd: policy update via %s missing required policy_envelope; "
                "keeping previous policy",
                source,
            )
            return

        # Source-of-truth ordering: explicit field on the event >
        # `active_policy_hash` on init/poll responses > computed-from-
        # payload after FFI call (handled below). Treat anything that
        # isn't a valid 64-char lowercase-hex string as "no hash known"
        # so we don't false-match on garbage.
        incoming_hash = data.get("hash") or data.get("active_policy_hash")
        if incoming_hash is not None and (
            not isinstance(incoming_hash, str)
            or len(incoming_hash) != 64
            or any(c not in "0123456789abcdef" for c in incoming_hash)
        ):
            incoming_hash = None

        # Idempotency short-circuit: the operator hasn't changed the
        # bundle since the last install in this process or the previous
        # one (persisted hash). Skip the FFI call entirely — the WASM
        # core's strict-greater check would reject it.
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
                source,
                data.get("version"),
            )
        except PolicySignatureError as exc:
            # The verifier rejected the envelope. Keep the old policy and
            # surface a structured warning. Production metrics label by
            # exc.reason for incident response.
            logger.warning(
                "checkrd: signed policy update rejected via %s "
                "(reason=%s, code=%s); keeping previous policy",
                source,
                exc.reason,
                exc.code,
            )
            return

        # Successful install: update both the in-memory hash cache and
        # Read the version from the engine (not from `data["version"]`)
        # so the persisted number matches what the WASM core actually
        # accepted. The hash field is server-canonical: it's
        # `SHA-256(yaml_content)` computed at publish time, the same
        # bytes the WASM core verified the signature over.
        if incoming_hash is None:
            # Server contract guarantees the hash is always present;
            # an absent field means a malformed event. Drop the cache
            # update + persistence: the engine accepted the install,
            # but without the hash we can't safely fill the cache, and
            # without a known hash the file we'd write would never
            # restore correctly. Next event hopefully carries the hash.
            logger.warning(
                "checkrd: signed policy install via %s missing hash field; "
                "cache + persistence skipped (next install will recover)",
                source,
            )
            return
        self._last_installed_hash = incoming_hash
        try:
            new_version = self._engine.get_active_policy_version()
            # OPA pattern: persist the verified envelope alongside the
            # version + hash so the next process boot can install the
            # policy from disk via ``_restore_persisted_policy_version``
            # — closing the bootstrap gap where the cache hits on init
            # but the engine has no rules.
            persist_state(
                new_version,
                bundle_hash=incoming_hash,
                bundle_envelope_json=envelope_json,
            )
        except Exception as exc:
            # Persistence is best-effort. The in-process monotonic check
            # still applies regardless of whether the disk write succeeds,
            # so we log and continue rather than letting the failure
            # propagate to the caller.
            logger.warning(
                "checkrd: failed to persist policy version high water mark "
                "(%s); rollback defense will not survive restart",
                exc,
            )


# Register the at-fork handler. Placed at module bottom (after the class
# definition) so the forward-reference inside ``_LIVE_RECEIVERS`` is
# already resolved by the time the handler walks the registry.
register_fork_handler(_LIVE_RECEIVERS, "_reinit_after_fork", "control receiver")
