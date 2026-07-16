"""SECURITY: purpose-scoped trust separation for cost metering (M-12).

THE headline security gate for cost metering. Two structurally separate trust
roots — policy and pricing — must never merge. A pricing key must never be a
valid signer for a policy bundle, and a policy key must never be a valid signer
for a price table. This is TUF-style per-role key separation, defense-in-depth
BEYOND (and independent of) the DSSE PAE payload-type binding the WASM core
already enforces.

Three proofs:

1. **Cross-type replay, both directions (in-WASM)** — a PRICING-type-signed
   envelope fed to ``reload_policy_signed`` is rejected, and a POLICY-type-
   signed envelope fed to ``reload_pricing_signed`` is rejected. Proves a
   captured signature of one kind cannot install the other, regardless of trust
   list.

2. **The trust lists never merge (structural)** — ``trusted_pricing_keys()`` and
   ``trusted_policy_keys()`` are disjoint by construction (no shared keyid),
   read different override env vars, and are wired to different reload methods.

3. **Independent override gates** — the pricing override
   (``CHECKRD_PRICING_TRUST_OVERRIDE_JSON``) replaces ONLY the pricing list and
   leaves the policy list untouched, and vice versa.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from checkrd.engine import WasmEngine
from checkrd.exceptions import PolicySignatureError
from tests.conftest import requires_wasm

POLICY_PAYLOAD_TYPE = "application/vnd.checkrd.policy-bundle+json"
PRICING_PAYLOAD_TYPE = "application/vnd.checkrd.pricing-bundle+json"
TELEMETRY_PAYLOAD_TYPE = "application/vnd.checkrd.telemetry-batch+json"
_MAX_AGE_SECS = 86_400


def _require_cryptography() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        pytest.skip("PyCA cryptography not installed; skipping cross-purpose suite")


def _pae(payload_type: str, payload: bytes) -> bytes:
    return (
        b"DSSEv1 "
        + str(len(payload_type)).encode()
        + b" "
        + payload_type.encode()
        + b" "
        + str(len(payload)).encode()
        + b" "
        + payload
    )


def _envelope(sk_bytes: bytes, keyid: str, payload: bytes, payload_type: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sig = Ed25519PrivateKey.from_private_bytes(sk_bytes).sign(_pae(payload_type, payload))
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload).decode(),
        "signatures": [{"keyid": keyid, "sig": base64.b64encode(sig).decode()}],
    }


def _trust(pk_bytes: bytes, keyid: str) -> list[dict[str, Any]]:
    return [
        {"keyid": keyid, "public_key_hex": pk_bytes.hex(), "valid_from": 0, "valid_until": 2**63}
    ]


def _keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    return sk.private_bytes_raw(), sk.public_key().public_bytes_raw()


def _engine() -> WasmEngine:
    return WasmEngine(
        policy_json=json.dumps({"agent": "t", "mode": "enforce", "default": "allow", "rules": []}),
        agent_id="cross-purpose-agent",
    )


def _policy_bundle(version: int = 1) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "version": version,
            "signed_at": int(time.time()),
            "policy": {"agent": "t", "mode": "enforce", "default": "allow", "rules": []},
        }
    ).encode()


def _pricing_bundle(version: int = 1) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "version": version,
            "signed_at": int(time.time()),
            "rounding": "half_up",
            "skus": [
                {
                    "sku_id": "anthropic-default",
                    "provider": "anthropic",
                    "model_match": "**",
                    "unit": "per_1m_tokens",
                    "input_usd_micros_per_unit": 1_000_000,
                    "output_usd_micros_per_unit": 5_000_000,
                    "cache_read_usd_micros_per_unit": None,
                    "cache_write_usd_micros_per_unit": None,
                    "default_max_output_tokens": 4096,
                    "effective_from": 1_700_000_000,
                    "deprecated_after": None,
                    "source": "list",
                }
            ],
        }
    ).encode()


# ============================================================
# 1. Cross-type replay, both directions (in-WASM) — THE security gate
# ============================================================


@requires_wasm
class TestCrossTypeReplayRejection:
    def test_pricing_key_signed_policy_cannot_install_as_policy(self) -> None:
        """A *validly-signed* POLICY bundle, but signed under the PRICING
        payload type (as a pricing key would), fed to ``reload_policy_signed``
        with the signer trusted → REJECTED. A pricing signature cannot install
        a policy. The signer IS in the trust list, so the only thing wrong is
        the payload type — proving the type binding, not a trust short-circuit.
        """
        _require_cryptography()
        skb, pkb = _keypair()
        envelope = _envelope(skb, "cp", _policy_bundle(), PRICING_PAYLOAD_TYPE)
        engine = _engine()
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_policy_signed(
                json.dumps(envelope),
                json.dumps(_trust(pkb, "cp")),
                int(time.time()),
                _MAX_AGE_SECS,
            )
        # The policy verifier rejects a non-policy payload type with -4.
        assert exc.value.ffi_code == -4
        assert exc.value.reason == "payload_type_mismatch"

    def test_policy_key_signed_pricing_cannot_install_as_pricing(self) -> None:
        """The reverse and more dangerous direction: a *validly-signed* PRICING
        bundle signed under the POLICY payload type (as a captured policy
        signature would be), fed to ``reload_pricing_signed`` with the signer
        trusted → REJECTED -15. A policy signature cannot tamper with money.
        """
        _require_cryptography()
        skb, pkb = _keypair()
        envelope = _envelope(skb, "cp", _pricing_bundle(), POLICY_PAYLOAD_TYPE)
        engine = _engine()
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_pricing_signed(
                json.dumps(envelope),
                json.dumps(_trust(pkb, "cp")),
                int(time.time()),
                _MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -15
        assert exc.value.reason == "pricing_payload_type_mismatch"
        assert engine.get_active_pricing_version() == 0  # nothing installed

    def test_telemetry_signed_envelope_cannot_install_as_pricing(self) -> None:
        """A telemetry-batch-type envelope (the SDK's own outbound artifact an
        attacker could capture off the ingestion path) cannot install as a price
        table either → -15."""
        _require_cryptography()
        skb, pkb = _keypair()
        envelope = _envelope(skb, "cp", _pricing_bundle(), TELEMETRY_PAYLOAD_TYPE)
        engine = _engine()
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_pricing_signed(
                json.dumps(envelope),
                json.dumps(_trust(pkb, "cp")),
                int(time.time()),
                _MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -15

    def test_floor_a_real_pricing_bundle_from_same_key_installs(self) -> None:
        """Sanity floor: the SAME key, signing a real PRICING bundle under the
        PRICING type, DOES install — proving the rejections above are the
        payload-type gate, not an accidental key/parse failure."""
        _require_cryptography()
        skb, pkb = _keypair()
        engine = _engine()
        engine.reload_pricing_signed(
            json.dumps(_envelope(skb, "cp", _pricing_bundle(version=1), PRICING_PAYLOAD_TYPE)),
            json.dumps(_trust(pkb, "cp")),
            int(time.time()),
            _MAX_AGE_SECS,
        )
        assert engine.get_active_pricing_version() == 1


# ============================================================
# 2. The two trust lists never merge (structural — no WASM needed)
# ============================================================


class TestTrustListsAreDisjoint:
    def test_production_lists_share_no_keyid(self) -> None:
        """By construction the production policy and pricing lists are disjoint
        — no keyid appears in both. (Both are empty pre-1.0; this also guards
        against a future edit that pastes the same key into both.)"""
        from checkrd._trust import _PRICING_TRUSTED_KEYS, _PRODUCTION_TRUSTED_KEYS

        policy_ids = {k["keyid"] for k in _PRODUCTION_TRUSTED_KEYS}
        pricing_ids = {k["keyid"] for k in _PRICING_TRUSTED_KEYS}
        assert policy_ids.isdisjoint(pricing_ids), (
            "policy and pricing trust roots must never share a keyid — "
            "a key authorized for one role must not be authorized for the other"
        )

    def test_pricing_keys_are_a_separate_list_object(self) -> None:
        """The pricing list is a distinct object, not an alias of the policy
        list — mutating one must never affect the other."""
        from checkrd._trust import _PRICING_TRUSTED_KEYS, _PRODUCTION_TRUSTED_KEYS

        assert _PRICING_TRUSTED_KEYS is not _PRODUCTION_TRUSTED_KEYS

    def test_resolved_lists_share_no_keyid_under_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With BOTH overrides active (different keys), the resolved policy and
        pricing lists still share no keyid — the override env vars are separate
        and feed separate lists, so a pricing dev key is never trusted for
        policy and vice versa."""
        from checkrd._trust import trusted_policy_keys, trusted_pricing_keys

        policy_key = [
            {
                "keyid": "dev-policy",
                "public_key_hex": "a" * 64,
                "valid_from": 0,
                "valid_until": 2**63,
            }
        ]
        pricing_key = [
            {
                "keyid": "dev-pricing",
                "public_key_hex": "b" * 64,
                "valid_from": 0,
                "valid_until": 2**63,
            }
        ]
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", json.dumps(policy_key))
        monkeypatch.setenv("CHECKRD_PRICING_TRUST_OVERRIDE_JSON", json.dumps(pricing_key))

        policy_ids = {k["keyid"] for k in trusted_policy_keys()}
        pricing_ids = {k["keyid"] for k in trusted_pricing_keys()}
        assert policy_ids == {"dev-policy"}
        assert pricing_ids == {"dev-pricing"}
        assert policy_ids.isdisjoint(pricing_ids)


# ============================================================
# 3. Independent override gates
# ============================================================


class TestPricingTrustOverride:
    def test_double_gate_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The override JSON alone (no CHECKRD_ALLOW_TRUST_OVERRIDE) is ignored
        — same double-gate as the policy override."""
        from checkrd._trust import _PRICING_TRUSTED_KEYS, trusted_pricing_keys

        override = [{"keyid": "rogue", "public_key_hex": "a" * 64}]
        monkeypatch.setenv("CHECKRD_PRICING_TRUST_OVERRIDE_JSON", json.dumps(override))
        monkeypatch.delenv("CHECKRD_ALLOW_TRUST_OVERRIDE", raising=False)
        assert trusted_pricing_keys() == _PRICING_TRUSTED_KEYS

    def test_double_gate_applies_with_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from checkrd._trust import trusted_pricing_keys

        override = [
            {
                "keyid": "dev-pricing",
                "public_key_hex": "c" * 64,
                "valid_from": 0,
                "valid_until": 2**63,
            }
        ]
        monkeypatch.setenv("CHECKRD_PRICING_TRUST_OVERRIDE_JSON", json.dumps(override))
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        assert trusted_pricing_keys() == override

    def test_pricing_override_does_not_touch_policy_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting ONLY the pricing override leaves the policy list at its
        production value — the two roots resolve independently."""
        from checkrd._trust import _PRODUCTION_TRUSTED_KEYS, trusted_policy_keys

        override = [
            {
                "keyid": "dev-pricing",
                "public_key_hex": "c" * 64,
                "valid_from": 0,
                "valid_until": 2**63,
            }
        ]
        monkeypatch.setenv("CHECKRD_PRICING_TRUST_OVERRIDE_JSON", json.dumps(override))
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        monkeypatch.delenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", raising=False)
        assert trusted_policy_keys() == _PRODUCTION_TRUSTED_KEYS

    def test_policy_override_does_not_touch_pricing_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the reverse: setting ONLY the policy override leaves the pricing
        list untouched."""
        from checkrd._trust import _PRICING_TRUSTED_KEYS, trusted_pricing_keys

        override = [
            {
                "keyid": "dev-policy",
                "public_key_hex": "a" * 64,
                "valid_from": 0,
                "valid_until": 2**63,
            }
        ]
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", json.dumps(override))
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        monkeypatch.delenv("CHECKRD_PRICING_TRUST_OVERRIDE_JSON", raising=False)
        assert trusted_pricing_keys() == _PRICING_TRUSTED_KEYS

    def test_returns_copy_not_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mutating the returned pricing list must not affect future calls."""
        from checkrd._trust import trusted_pricing_keys

        monkeypatch.delenv("CHECKRD_PRICING_TRUST_OVERRIDE_JSON", raising=False)
        first = trusted_pricing_keys()
        first.append({"keyid": "mutant"})
        assert {"keyid": "mutant"} not in trusted_pricing_keys()


# ============================================================
# Pricing trust-status diagnostic
# ============================================================


class TestProductionPricingTrustStatus:
    def test_empty_pricing_list_is_empty_dev_for_localhost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd._trust import production_pricing_trust_status

        monkeypatch.setattr("checkrd._trust._PRICING_TRUSTED_KEYS", [])
        level, _ = production_pricing_trust_status(base_url="http://localhost:8080", env={})
        assert level == "empty_dev"

    def test_empty_pricing_list_is_empty_production_for_prod_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd._trust import production_pricing_trust_status

        monkeypatch.setattr("checkrd._trust._PRICING_TRUSTED_KEYS", [])
        level, message = production_pricing_trust_status(base_url="https://api.checkrd.io", env={})
        assert level == "empty_production"
        assert "pricing trust list is empty" in message

    def test_populated_pricing_list_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from checkrd._trust import production_pricing_trust_status

        monkeypatch.setattr(
            "checkrd._trust._PRICING_TRUSTED_KEYS",
            [{"keyid": "x", "public_key_hex": "a" * 64, "valid_from": 0, "valid_until": 2**63}],
        )
        level, _ = production_pricing_trust_status(base_url="https://api.checkrd.io", env={})
        assert level == "ok"

    def test_pricing_override_reports_override(self) -> None:
        from checkrd._trust import production_pricing_trust_status

        env = {
            "CHECKRD_PRICING_TRUST_OVERRIDE_JSON": '[{"keyid":"x"}]',
            "CHECKRD_ALLOW_TRUST_OVERRIDE": "1",
        }
        level, _ = production_pricing_trust_status(base_url=None, env=env)
        assert level == "override"
