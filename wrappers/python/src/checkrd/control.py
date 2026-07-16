"""Real-time control signal receiver for Checkrd.

Connects to the control plane SSE endpoint to receive kill switch,
policy update, and pricing update signals. Falls back to polling if SSE
is unavailable.

Policy bundles delivered on ``init`` and ``policy_updated`` are the
*effective* policy: the control plane has already merged the org-level
deny rules with the agent-level allow rules before signing. This module
installs the bytes verbatim through ``reload_policy_signed`` and never
merges -- the merge logic lives in one place on the server, matching
the Envoy xDS / OPA Bundles industry pattern.

Pricing bundles delivered on ``init`` and ``pricing_updated`` (M-14) are
installed through ``reload_pricing_signed`` under the STRUCTURALLY-SEPARATE
pricing trust root (:func:`checkrd._trust.trusted_pricing_keys`), never the
policy root. Once a pricing bundle installs,
``engine.get_active_pricing_version()`` becomes ``> 0`` and the per-call
settle path meters cost. Pricing install is FAIL-OPEN: a rejected/stale/
missing price table leaves the previous table in place and never blocks the
host — a missing price table just means cost isn't metered, in contrast to
the fail-closed policy path.
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
from checkrd._pricing_state import (
    load_persisted_pricing_version,
    persist_pricing_version,
)
from checkrd._trust import (
    trusted_policy_keys,
    trusted_pricing_keys,
    warn_if_misconfigured,
)
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

# Maximum age of a signed *pricing* bundle, in seconds. The cost-metering
# analogue of ``_POLICY_BUNDLE_MAX_AGE_SECS``; passed to the WASM core's
# ``reload_pricing_signed`` freshness check, which rejects any price table
# signed more than this many seconds in the past (FFI ``-22``). Same 24-hour
# production default as the policy window: long enough to absorb operator
# activity gaps, short enough to bound the replay window in which a captured
# older-but-signed price table could be re-presented. Kept a SEPARATE constant
# (not an alias of the policy one) so the two windows can be tuned
# independently — pricing tolerates a longer staleness than policy if we ever
# want it, because a stale price table only means cost isn't metered, never
# that requests are blocked.
_PRICING_BUNDLE_MAX_AGE_SECS = 86_400


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
        # Hash of the pricing bundle currently installed (or ``None`` before
        # the first pricing install). The cost-metering analogue of
        # ``_last_installed_hash`` — same OPA-bundle / TUF "don't re-apply
        # unchanged" idempotency, so a same-bundle SSE ``init`` / poll after a
        # reconnect short-circuits the ``reload_pricing_signed`` FFI instead of
        # tripping the WASM core's strict-greater monotonic check. Unlike the
        # policy hash, this is NOT seeded from disk: the pricing state file
        # persists only the version (see ``_pricing_state``), because a missing
        # price table is fail-open, so there is no persisted envelope to
        # re-install and thus no persisted hash to seed. It starts ``None`` and
        # fills on the first successful in-process pricing install.
        self._last_installed_pricing_hash: Optional[str] = None
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
        # Cost-metering rollback defense (M-12): restore the persisted pricing
        # version high water mark BEFORE the receiver can install any signed
        # price table, so a process restart cannot reset the monotonic check to
        # 0 and let an old price table be replayed. Symmetric with the policy
        # restore above; best-effort and never raises.
        self._restore_persisted_pricing_version()
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

        # Skip the restore when the engine already holds a bundle at
        # or above the persisted version. ``init()`` runs
        # ``bootstrap_policy`` before ``ControlReceiver.start()``, so
        # by the time we get here a fresher bundle is usually already
        # installed. Re-applying the older persisted bundle would
        # trip the WASM core's strict-greater monotonic check
        # (``bundle_version_not_monotonic``) and surface as a
        # spurious WARNING on every SDK boot — exactly the behaviour
        # operators reported as a "false crash" log.
        try:
            current_version = int(self._engine.get_active_policy_version())
        except Exception:
            # Engine FFI not ready or the test seam returned a non-int
            # (mock engines do this) — fall back to "attempt the
            # restore". `reload_policy_signed` is the authoritative
            # gate and rejects if it must.
            current_version = -1
        if current_version >= version:
            logger.debug(
                "checkrd: skipping persisted policy restore "
                "(engine already at version %d, persisted version=%d)",
                current_version,
                version,
            )
            # Still seed the hash cache so a same-bundle SSE init
            # short-circuits the FFI call.
            self._last_installed_hash = bundle_hash
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
                exc.reason,
                exc.code,
            )
            return
        self._last_installed_hash = bundle_hash
        logger.info(
            "checkrd: restored persisted policy version=%d, hash=%s… for agent %s",
            version,
            bundle_hash[:16] if bundle_hash else "<none>",
            self._agent_id,
        )

    def _restore_persisted_pricing_version(self) -> None:
        """Seed the engine's pricing version high water mark from disk (M-12).

        The cost-metering analogue of :meth:`_restore_persisted_policy_version`,
        but version-only: the price table is fail-open (a missing table just
        means cost 0), so unlike policy we do NOT re-install a persisted
        envelope — we only feed the persisted version back through
        ``set_initial_pricing_version`` so the WASM core's monotonic rollback
        check spans restarts. The fresh price table arrives via the control
        plane after connect.

        Best-effort: never raises. A version of 0 (no prior install, or a
        corrupt state file) is a no-op — ``set_initial_pricing_version(0)`` is
        skipped because it carries no information and would needlessly burn the
        engine's one-shot restore window.
        """
        version = load_persisted_pricing_version()
        if version <= 0:
            # Nothing persisted (fresh install / corrupt file). Leave the
            # engine's high water mark at its default 0.
            return
        try:
            self._engine.set_initial_pricing_version(version)
        except Exception as exc:  # noqa: BLE001
            # A real signed install already happened this process (one-shot
            # lockout, FFI -24), the engine FFI isn't ready, or a mock engine
            # is in play. None of these are security-relevant: the in-process
            # monotonic check still holds from whatever the engine loaded.
            logger.debug(
                "checkrd: could not restore persisted pricing version=%d (%s); "
                "in-process rollback defense unaffected",
                version,
                exc,
            )
            return
        logger.info(
            "checkrd: restored persisted pricing version=%d for agent %s",
            version,
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

                # Cost-metering self-bootstrap (M-14): the init payload also
                # carries the agent's active *pricing* envelope when the org
                # has cost metering configured. Without installing it here,
                # `get_active_pricing_version()` would stay 0 and the settle
                # path would never run — the `pricing_updated` event only
                # fires on a price-table *change*. Install via the same
                # fail-open apply-path used for `pricing_updated`, forwarding
                # `active_pricing_hash` for the idempotency short-circuit.
                pricing_envelope = data.get("pricing_envelope")
                if pricing_envelope is not None:
                    self._apply_pricing_update(
                        {
                            "pricing_envelope": pricing_envelope,
                            "active_pricing_hash": data.get("active_pricing_hash"),
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

            elif sse.event == "pricing_updated":
                # Cost-metering live-update (M-14): the control plane published
                # a new signed price table. Install it fail-open — the money
                # path, mirroring `policy_updated` but never blocking the host.
                data = json.loads(sse.data)
                self._apply_pricing_update(data, source="SSE")

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
        # Poll fallback must install pricing too — edge/browser SDKs that never
        # hold an SSE stream open (Cloudflare Workers, Vercel Edge) rely
        # entirely on this path to bring cost metering live. Same shape as the
        # policy poll install: forward `active_pricing_hash` for idempotency.
        pricing_envelope = state.get("pricing_envelope")
        if pricing_envelope is not None:
            self._apply_pricing_update(
                {
                    "pricing_envelope": pricing_envelope,
                    "active_pricing_hash": state.get("active_pricing_hash"),
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

    def _apply_pricing_update(self, data: dict[str, Any], source: str) -> None:
        """Install a signed *pricing* update via the WASM core verifier (M-14).

        The cost-metering analogue of :meth:`_apply_policy_update`, and the
        method that brings cost metering *live*: after a successful install
        ``engine.get_active_pricing_version()`` becomes ``> 0`` and the
        per-call settle path (``settle_usage``) starts stamping cost onto
        telemetry events. Until an install lands, metering is dormant
        (version 0 ⇒ ``pricing_status = "disabled"``).

        ``data`` is the JSON dict from an SSE ``init`` / ``pricing_updated``
        event or a polling response, carrying a ``pricing_envelope`` field —
        strong from the ground up, there is no unsigned distribution path.

        # Two ways this differs from the policy path

        1. **Trust root (SECURITY-CRITICAL).** The envelope is verified ONLY
           against :func:`checkrd._trust.trusted_pricing_keys` — the
           structurally-separate pricing trust anchor — never
           :func:`~checkrd._trust.trusted_policy_keys`. A pricing bundle is
           valid only under pricing roots; a key trusted for policy but not
           pricing MUST NOT be able to install a price table (proven by the
           trust-isolation test). This is TUF-style per-role key separation,
           defense-in-depth beyond the DSSE payload-type binding the core
           already enforces.

        2. **Fail-open posture.** Policy is fail-closed (a missing policy
           denies everything, so its restore path re-installs the persisted
           envelope). Pricing is fail-OPEN metering: if the install fails —
           bad signature, stale/replayed bundle, rollback, or an empty trust
           list — we log a structured WARNING, LEAVE the previous price table
           in place, and NEVER raise or block the host. A missing/stale price
           table just means cost isn't metered for a while, never that a
           request is blocked. (This is also why only the *version* is
           persisted, not the envelope — see ``_pricing_state``.)

        # Idempotency at the wrapper layer

        Mirrors :meth:`_apply_policy_update`: before invoking the FFI, compare
        the bundle's content hash against ``self._last_installed_pricing_hash``.
        Match ⇒ skip the install (OPA-bundle / TUF "don't re-apply unchanged").
        The hash is sourced from the event's ``hash`` field
        (``pricing_updated``) or the ``active_pricing_hash`` co-field
        (``init`` / poll). Without this, a same-bundle re-bootstrap after an
        SSE reconnect would trip the core's strict-greater monotonic check.

        # FFI defenses (still apply when the install does run)

        - DSSE signature verification against the trusted *pricing* key list
        - Strict-greater monotonic version check (price-table rollback
          rejection, FFI ``-21``)
        - Bundle freshness check (max age ``_PRICING_BUNDLE_MAX_AGE_SECS``,
          FFI ``-22``)
        - Cross-type replay defense via the pricing DSSE payload-type binding
          (a policy- or telemetry-signed envelope can never install as a price
          table, FFI ``-15``)
        """
        envelope = data.get("pricing_envelope")
        if envelope is None:
            logger.warning(
                "checkrd: pricing update via %s missing required pricing_envelope; "
                "keeping previous price table",
                source,
            )
            return

        # Source-of-truth ordering identical to the policy path: explicit
        # `hash` on the event > `active_pricing_hash` on init/poll responses >
        # (there is no compute-from-payload fallback because pricing persists
        # only the version, so a missing hash just means "no cache seed"). Treat
        # anything that isn't a 64-char lowercase-hex string as "no hash known"
        # so we don't false-match on garbage.
        incoming_hash = data.get("hash") or data.get("active_pricing_hash")
        if incoming_hash is not None and (
            not isinstance(incoming_hash, str)
            or len(incoming_hash) != 64
            or any(c not in "0123456789abcdef" for c in incoming_hash)
        ):
            incoming_hash = None

        # Idempotency short-circuit: the operator hasn't changed the price table
        # since the last install in this process. Skip the FFI call entirely —
        # the WASM core's strict-greater check would otherwise reject it.
        if (
            incoming_hash is not None
            and self._last_installed_pricing_hash is not None
            and incoming_hash == self._last_installed_pricing_hash
        ):
            logger.debug(
                "checkrd: signed pricing update via %s already installed "
                "(hash=%s…); skipping no-op re-apply",
                source,
                incoming_hash[:16],
            )
            return

        try:
            envelope_json = json.dumps(envelope)
            # SECURITY: pricing bundles verify ONLY against the pricing trust
            # root — never the policy root. See the class-level note above.
            trusted_json = json.dumps(trusted_pricing_keys())
            self._engine.reload_pricing_signed(
                envelope_json,
                trusted_json,
                int(time.time()),
                _PRICING_BUNDLE_MAX_AGE_SECS,
            )
            logger.info(
                "checkrd: signed pricing table installed via %s (version=%s)",
                source,
                data.get("version"),
            )
        except PolicySignatureError as exc:
            # FAIL-OPEN: the verifier rejected the price table (bad signature,
            # stale, rollback -21, cross-type -15, empty trust list, …). Unlike
            # policy, we do NOT treat this as security-critical enforcement —
            # cost simply isn't metered. Keep the old price table, surface a
            # structured warning for the metering dashboard, and return without
            # raising. The ``reload_pricing_signed`` FFI raises
            # ``PolicySignatureError`` (carrying the pricing codes -15..-24)
            # in the Python SDK; catch it exactly as the policy path does.
            logger.warning(
                "checkrd: signed pricing update rejected via %s "
                "(reason=%s, code=%s); keeping previous price table "
                "(cost metering unaffected — fail-open)",
                source,
                exc.reason,
                exc.code,
            )
            return

        # Successful install. Fill the hash cache so a same-bundle re-apply
        # short-circuits, then persist the VERSION (not the envelope — pricing
        # is version-only on disk) read from the engine so the rollback defense
        # survives restarts via ``_restore_persisted_pricing_version``.
        if incoming_hash is None:
            # No usable hash on the event — the install still took (metering is
            # live), but we can't seed the idempotency cache. Persist the
            # version anyway so the cross-restart rollback defense holds; the
            # cache fills on the next event that does carry a hash.
            logger.warning(
                "checkrd: signed pricing install via %s missing hash field; "
                "idempotency cache not seeded (next install will recover)",
                source,
            )
        else:
            self._last_installed_pricing_hash = incoming_hash
        try:
            new_version = self._engine.get_active_pricing_version()
            persist_pricing_version(new_version)
        except Exception as exc:
            # Persistence is best-effort, same as the policy path: the
            # in-process monotonic check still applies whether or not the disk
            # write succeeds, so log and continue rather than propagate.
            logger.warning(
                "checkrd: failed to persist pricing version high water mark "
                "(%s); rollback defense will not survive restart",
                exc,
            )


# Register the at-fork handler. Placed at module bottom (after the class
# definition) so the forward-reference inside ``_LIVE_RECEIVERS`` is
# already resolved by the time the handler walks the registry.
register_fork_handler(_LIVE_RECEIVERS, "_reinit_after_fork", "control receiver")
