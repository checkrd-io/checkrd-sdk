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
        return True
    except PolicySignatureError as exc:
        logger.warning(
            "checkrd: bootstrap policy install rejected "
            "(reason=%s, code=%s); engine remains on deny-all baseline",
            exc.reason,
            exc.code,
        )
        return False


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
