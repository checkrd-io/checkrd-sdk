"""Server-canonical policy bootstrap (sync path).

On wrap() / Checkrd construction the SDK fetches the agent's currently-
published DSSE-signed policy bundle from ``GET /v1/agents/:id/control/state``
and installs it via ``reload_policy_signed`` before returning. Until the
bundle lands the WASM engine runs the deny-all baseline configured at
boot, so every request fails closed -- matches OPA's bundle-bootstrap
pattern and Envoy xDS's initial-state delivery.

The bootstrap fetch is gated on the same hash cache the SSE
``ControlReceiver`` uses for ongoing updates, so a process restart
against an unchanged active bundle is a no-op at the WASM layer.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from checkrd._policy_state import persist_state
from checkrd._trust import trusted_policy_keys
from checkrd._version import __version__ as VERSION
from checkrd.exceptions import PolicySignatureError

logger = logging.getLogger("checkrd")

# Max age (seconds) accepted for a signed bundle. 24h matches the SSE
# receiver's freshness window; the WASM core enforces it.
_POLICY_BUNDLE_MAX_AGE_SECS = 86_400


def bootstrap_policy(
    *,
    engine: Any,
    control_plane_url: str,
    api_key: str,
    agent_id: str,
    api_version: Optional[str] = None,
    timeout_secs: float = 5.0,
) -> bool:
    """Fetch the agent's published signed bundle and install it.

    Mirrors the receiver's ``_poll_once()`` path so verification,
    freshness, and hash-cache invariants are identical across the
    bootstrap and the ongoing-update paths.

    Fail-closed contract: when the fetch fails, the bundle is
    malformed, or the server returns nothing, this function logs and
    returns ``False`` without installing a policy. The engine continues
    to run on whatever policy was supplied at boot -- typically the
    deny-all baseline, so every request denies until either a
    successful bootstrap arrives (next poll cycle) or the SSE receiver
    delivers a ``policy_updated`` event.

    Returns:
        True if a bundle was installed, False otherwise.
    """
    url = f"{control_plane_url.rstrip('/')}/v1/agents/{agent_id}/control/state"
    headers = {
        "X-API-Key": api_key,
        "User-Agent": f"checkrd-python/{VERSION}",
    }
    if api_version:
        headers["Checkrd-Version"] = api_version

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_secs)) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning(
            "checkrd: policy bootstrap fetch failed (%s); engine remains on "
            "deny-all baseline",
            exc,
        )
        return False

    if resp.status_code >= 400:
        logger.warning(
            "checkrd: policy bootstrap returned HTTP %s; engine remains on "
            "deny-all baseline",
            resp.status_code,
        )
        return False

    try:
        state: dict[str, Any] = resp.json()
    except ValueError as exc:
        logger.warning("checkrd: policy bootstrap response was not JSON: %s", exc)
        return False

    # Stamp the kill-switch first so the engine reflects the live
    # server state even when no policy is published yet.
    kill = state.get("kill_switch_active")
    if isinstance(kill, bool):
        engine.set_kill_switch(kill)

    envelope = state.get("policy_envelope")
    if envelope is None:
        logger.warning(
            "checkrd: control plane has no published policy for agent %s -- "
            "engine remains on deny-all baseline. Publish a policy in the "
            "dashboard to enable enforcement.",
            agent_id,
        )
        return False

    try:
        envelope_json = json.dumps(envelope)
        trusted_json = json.dumps(trusted_policy_keys())
        engine.reload_policy_signed(
            envelope_json,
            trusted_json,
            int(time.time()),
            _POLICY_BUNDLE_MAX_AGE_SECS,
        )
        logger.info(
            "checkrd: signed policy installed via bootstrap (version=%s)",
            state.get("active_policy_version"),
        )
    except PolicySignatureError as exc:
        logger.warning(
            "checkrd: bootstrap policy install rejected "
            "(reason=%s, code=%s); engine remains on deny-all baseline",
            exc.reason,
            exc.code,
        )
        return False

    # Persist the freshly-installed bundle so the next process boot
    # restores it from disk via ``_restore_persisted_policy_version``
    # — OPA bundle / TUF client pattern. Without this, the persisted
    # state on disk goes stale and every subsequent boot logs a
    # spurious ``persisted policy bundle rejected on restore
    # (reason=bundle_too_old)`` warning, followed by a
    # ``signed policy update rejected via SSE init`` warning when the
    # server re-delivers the bundle it just installed. The
    # ``SSE / poll`` install paths already persist on success; the
    # bootstrap path was the missing link.
    #
    # Best-effort: a failed persist still leaves the engine in a
    # working state (the in-process WASM core has the bundle); only
    # the cross-restart short-circuit degrades.
    try:
        # Hash + version sources of truth:
        #   * ``active_policy_hash`` — set on every response that
        #     carries an envelope; this is the SHA-256 the server
        #     and SSE receivers all agree on.
        #   * ``active_policy_version`` — currently optional on the
        #     control-state shape (the server may return ``None``).
        #     When it's missing we fall back to the engine's
        #     post-install version high-water-mark, which the WASM
        #     core just bumped to the bundle's inner ``version``
        #     field. Either path produces the same on-disk record
        #     a real ``policy_updated`` SSE install would write.
        bundle_hash = state.get("active_policy_hash")
        if not isinstance(bundle_hash, str):
            bundle_hash = None
        bundle_version = state.get("active_policy_version")
        if not isinstance(bundle_version, int):
            try:
                bundle_version = int(engine.get_active_policy_version())
            except Exception:
                bundle_version = None
        if bundle_hash is not None and isinstance(bundle_version, int):
            persist_state(
                bundle_version,
                bundle_hash=bundle_hash,
                bundle_envelope_json=envelope_json,
            )
    except Exception as exc:
        logger.warning(
            "checkrd: failed to persist bootstrap policy bundle (%s); "
            "next process boot will re-fetch from the control plane",
            exc,
        )

    return True


# Deny-all baseline policy installed at WASM boot when no local policy
# is provided. Every request fails closed until the bootstrap fetch
# installs the server-published bundle.
DENY_ALL_BASELINE_POLICY_JSON: str = json.dumps(
    {
        "agent": "",
        "mode": "enforce",
        "default": "deny",
        "rules": [],
    }
)
