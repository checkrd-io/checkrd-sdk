"""Trusted public keys for verifying signed policy bundles from the control plane.

The Checkrd control plane signs every policy update with an Ed25519 key held
in AWS Secrets Manager. The SDK ships with a list of trusted public keys —
each with a key ID and a validity window — and refuses to install any policy
update whose signature can't be verified against an in-window trusted key.

This file is the trust root for the entire policy distribution path. Compile-
time pinning means an attacker who compromises the network or DNS can't
substitute their own signing key, even on first use; the same threat model
that browser TLS root CAs and OPA bundle signing use.

# Format

Each entry is a dict with four fields:

- ``keyid`` — stable identifier matching the ``keyid`` in DSSE signatures
  emitted by the control plane.
- ``public_key_hex`` — 64 lowercase hex characters encoding a 32-byte Ed25519
  public key.
- ``valid_from`` — Unix seconds when this key starts being trusted (inclusive).
- ``valid_until`` — Unix seconds when this key stops being trusted (exclusive).

# Key rotation

Add a new entry with ``valid_from = now`` and ``valid_until = now + 10 years``,
then cut a new SDK release. Wait for adoption (typically 30-60 days) and
switch the control plane to sign with the new key. Old SDKs continue working
because the old key is still in their list during the overlap window. After
all SDKs upgrade past the old ``valid_until``, retire the old private key
from AWS Secrets Manager.

This is the same overlap window pattern used by every long-lived signing
system (Certificate Transparency logs, OPA bundles, browser update.googleapis.com).

# Test override

The ``CHECKRD_POLICY_TRUST_OVERRIDE_JSON`` environment variable accepts a
JSON array in the same format as the constant below. When set, it REPLACES
the production list — used by tests, dev environments, and the cross-
implementation interop test that signs with a fresh ephemeral key per run.

NEVER set this in production. The dev fallback is gated on the env var so
production deployments fail closed if the var is accidentally set to an
empty list.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Literal, Mapping, Optional

_logger = logging.getLogger("checkrd")

# Production trusted keys. Populated by the bootstrap script
# `scripts/generate-policy-signing-key.py` before the first signed release;
# empty during pre-1.0 development. The CI publish workflow MUST verify this
# list is non-empty (see `production_trust_status`) — shipping an empty list
# silently disables every signed policy update at the SDK side.
#
# Format: list of dicts matching crates/core/src/dsse_verify.rs::TrustedKey.
_PRODUCTION_TRUSTED_KEYS: list[dict[str, Any]] = [
    # Bootstrap key for the production control plane. Private half lives in
    # AWS Secrets Manager `checkrd/prod/policy-signing-key`; this entry is
    # the public-key half pinned into the SDK so DSSE-signed policy bundles
    # from api.checkrd.io verify on every install.
    #
    # Validity window: 10 years. Trust *roots* (this entry — the SDK's
    # compile-time pin) follow the Sigstore Fulcio / Apple WWDR / TLS root
    # CA convention of long-lived roots so SDK versions in the field keep
    # verifying for years without forced upgrades. The control plane can
    # rotate the underlying key any time inside that window via the overlap
    # pattern; rotation does not require shortening this entry's window.
    #
    # Rotating: append a new entry above this one with `valid_from = now`,
    # ship a new SDK release containing both, then switch the control plane
    # to sign with the new key. Old SDKs continue verifying because their
    # original entry is still in-window. Retire the old entry only after all
    # deployed SDKs have aged past its `valid_until`. See KEY-CUSTODY.md for
    # the full runbook.
    {
        "keyid": "checkrd-control-plane",
        "public_key_hex": "5b6bd586744b59f28b2ff02aac7817447175610deb973db253030e8abee5ae01",
        "valid_from": 1777329219,   # 2026-04-27T22:33:39Z
        "valid_until": 2092689219,  # 2036-04-24T22:33:39Z (10 years)
    },
]

# Substring identifying a production-shaped control plane URL. Used by
# `production_trust_status` to decide whether an empty trust list is
# benign (dev/test) or a release-blocker (production target).
_PRODUCTION_HOST_MARKER = "checkrd.io"

#: Distinct states the trust configuration can be in. Returned by
#: :func:`production_trust_status` so callers (CI guard, startup warning,
#: dashboards) can branch on a stable label rather than parse free text.
TrustStatusLevel = Literal["ok", "override", "empty_dev", "empty_production"]


def trusted_policy_keys() -> list[dict[str, Any]]:
    """Return the list of trusted policy signing keys.

    Reads ``CHECKRD_POLICY_TRUST_OVERRIDE_JSON`` first; if set AND the
    double-gate ``CHECKRD_ALLOW_TRUST_OVERRIDE=1`` is also set, parses it
    as JSON and returns the parsed list. Otherwise returns the production list.

    The double-gate prevents accidental or malicious trust override in
    production. A single compromised env var is not enough — both must be
    set. This closes the attack vector where a container escape or CI
    injection sets the override to inject a rogue signing key.

    The override is used by tests and dev environments where the control
    plane runs with an ephemeral signing key whose public key is logged at
    startup. The dev workflow is: set both env vars, then the SDK trusts the
    ephemeral key for the dev session.
    """
    override = os.environ.get("CHECKRD_POLICY_TRUST_OVERRIDE_JSON")
    if override:
        gate = os.environ.get("CHECKRD_ALLOW_TRUST_OVERRIDE", "")
        if gate not in ("1", "true", "yes"):
            _logger.warning(
                "checkrd: CHECKRD_POLICY_TRUST_OVERRIDE_JSON is set but "
                "CHECKRD_ALLOW_TRUST_OVERRIDE is not '1'. Ignoring override. "
                "Both env vars must be set to override trusted keys."
            )
            return list(_PRODUCTION_TRUSTED_KEYS)
        try:
            parsed = json.loads(override)
            if isinstance(parsed, list):
                if not parsed:
                    _logger.warning(
                        "checkrd: trust override is an empty list — all "
                        "signed policy updates will be rejected."
                    )
                _logger.warning(
                    "checkrd: using %d trust-override key(s) instead of "
                    "production keys. DO NOT use this in production.",
                    len(parsed),
                )
                return parsed
        except json.JSONDecodeError:
            _logger.warning(
                "checkrd: CHECKRD_POLICY_TRUST_OVERRIDE_JSON is not valid "
                "JSON. Falling back to production keys."
            )
    return list(_PRODUCTION_TRUSTED_KEYS)


# Module-level guard so :func:`warn_if_misconfigured` fires at most once per
# process. Operators see one critical line at startup, not one per reconnect.
_misconfigured_warning_fired_lock = threading.Lock()
_misconfigured_warning_fired = False


def production_trust_status(
    *,
    base_url: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[TrustStatusLevel, str]:
    """Diagnose the trust list configuration for the current environment.

    Pure function — takes the relevant inputs explicitly so tests don't
    have to monkeypatch ``os.environ`` or the production constant. The
    runtime callers (startup warning, ``checkrd policy trust-status``)
    pass ``base_url`` and the live env.

    Levels:

    - ``ok`` — production keys present and no override. Steady state.
    - ``override`` — ``CHECKRD_POLICY_TRUST_OVERRIDE_JSON`` is in effect
      (matches the double-gate in :func:`trusted_policy_keys`). Expected
      in dev/test; unexpected in production.
    - ``empty_dev`` — production list is empty AND the URL is not
      production-shaped. Benign for local development.
    - ``empty_production`` — production list is empty AND the URL points
      at a production control plane. Signed policy distribution is
      effectively disabled; ship-blocker.
    """
    env = os.environ if env is None else env
    override = env.get("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", "")
    gate = env.get("CHECKRD_ALLOW_TRUST_OVERRIDE", "")
    override_active = bool(override) and gate in ("1", "true", "yes")

    if override_active:
        return (
            "override",
            "trust list override active via CHECKRD_POLICY_TRUST_OVERRIDE_JSON. "
            "Acceptable for dev/test; never set in production.",
        )

    if _PRODUCTION_TRUSTED_KEYS:
        return (
            "ok",
            f"production trust list contains {len(_PRODUCTION_TRUSTED_KEYS)} "
            "key(s). Signed policy updates will be verified against this list.",
        )

    if base_url and _PRODUCTION_HOST_MARKER in base_url:
        return (
            "empty_production",
            "production trust list is empty AND the control plane URL "
            f"({base_url!r}) targets a production endpoint. Every signed "
            "policy update will be rejected. Run "
            "scripts/generate-policy-signing-key.py and update "
            "_PRODUCTION_TRUSTED_KEYS before shipping.",
        )

    return (
        "empty_dev",
        "production trust list is empty. Acceptable for local development; "
        "set CHECKRD_POLICY_TRUST_OVERRIDE_JSON to a dev key to verify "
        "signed bundles, or run scripts/generate-policy-signing-key.py "
        "to bootstrap a production key.",
    )


def warn_if_misconfigured(
    *,
    base_url: Optional[str],
    logger: Optional[logging.Logger] = None,
) -> None:
    """One-shot startup warning for misconfigured trust roots.

    Called from :class:`checkrd.control.ControlReceiver.start` so the
    warning fires at the moment the SDK begins listening for signed
    bundles — not at import time, where ``base_url`` isn't known. Safe
    to call from multiple threads or repeatedly; the underlying log
    fires at most once per process.
    """
    global _misconfigured_warning_fired
    level, message = production_trust_status(base_url=base_url)
    if level != "empty_production":
        return
    with _misconfigured_warning_fired_lock:
        if _misconfigured_warning_fired:
            return
        _misconfigured_warning_fired = True
    (logger or _logger).critical("checkrd: %s", message)


def _reset_warning_state_for_tests() -> None:
    """Reset the one-shot warning flag. Tests only.

    Public-but-underscored helper because pytest captures need to assert
    the warning fires, then re-arm it for the next case.
    """
    global _misconfigured_warning_fired
    with _misconfigured_warning_fired_lock:
        _misconfigured_warning_fired = False
