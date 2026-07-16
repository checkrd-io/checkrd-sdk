"""Tests for the Python half of cost metering (M-12).

Covers the four pricing FFI bindings end-to-end against the rebuilt WASM
core, the ``cost_metering`` default-off switch, the extract→settle→cost-field
batcher path, pricing-version persistence, and the purpose-scoped pricing
trust roots.

The cross-purpose trust-rejection security gate lives in its own file
(``test_pricing_cross_purpose.py``) so the headline security test stands out.

# Standards anchored

- RFC 8032 (Ed25519) — PyCA ``cryptography`` signer vs ``ed25519-dalek`` verifier
- DSSE protocol.md — PAE construction
- ``crates/shared/src/dsse.rs::PRICING_BUNDLE_PAYLOAD_TYPE`` — domain separation
- ``crates/core/src/pricing.rs`` — SettleResult / UsageInput wire shapes
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from checkrd.engine import WasmEngine
from checkrd.exceptions import PolicySignatureError
from tests.conftest import requires_wasm, unique_id

# This module exercises the WASM core, so skip if .wasm isn't built.
pytestmark = requires_wasm


PRICING_PAYLOAD_TYPE = "application/vnd.checkrd.pricing-bundle+json"
_TEST_MAX_AGE_SECS = 86_400  # 24 hours


# ============================================================
# Builders — mirror crates/core/src/pricing.rs sample_skus / sample_bundle
# ============================================================


def _sample_skus() -> list[dict[str, Any]]:
    """Anthropic Sonnet (cache priced separately) + an anthropic catch-all.

    Byte-for-byte the same SKU prices the Rust ``sample_skus()`` uses, so the
    expected micro-USD figures below match the core's own unit tests.
    """
    return [
        {
            "sku_id": "anthropic-claude-sonnet",
            "provider": "anthropic",
            "model_match": "claude-sonnet-*",
            "unit": "per_1m_tokens",
            "input_usd_micros_per_unit": 3_000_000,
            "output_usd_micros_per_unit": 15_000_000,
            "cache_read_usd_micros_per_unit": 300_000,
            "cache_write_usd_micros_per_unit": 3_750_000,
            "default_max_output_tokens": 8192,
            "effective_from": 1_700_000_000,
            "deprecated_after": None,
            "source": "list",
        },
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
        },
    ]


def _build_pricing_bundle(
    version: int = 1,
    signed_at: int | None = None,
    skus: list[dict[str, Any]] | None = None,
) -> bytes:
    if signed_at is None:
        signed_at = int(time.time())
    bundle = {
        "schema_version": 1,
        "version": version,
        "signed_at": signed_at,
        "rounding": "half_up",
        "skus": _sample_skus() if skus is None else skus,
    }
    return json.dumps(bundle).encode()


def _pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE PAE, reconstructed in pure Python (secure-systems-lab/dsse)."""
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


def _build_dsse_envelope(
    private_key_bytes: bytes,
    keyid: str,
    payload_bytes: bytes,
    payload_type: str = PRICING_PAYLOAD_TYPE,
) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signing_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    sig = signing_key.sign(_pae(payload_type, payload_bytes))
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload_bytes).decode(),
        "signatures": [{"keyid": keyid, "sig": base64.b64encode(sig).decode()}],
    }


def _trust_list_for(public_key_bytes: bytes, keyid: str) -> list[dict[str, Any]]:
    return [
        {
            "keyid": keyid,
            "public_key_hex": public_key_bytes.hex(),
            "valid_from": 0,
            "valid_until": 2**63,
        }
    ]


def _make_engine() -> WasmEngine:
    return WasmEngine(
        policy_json=json.dumps({"agent": "t", "mode": "enforce", "default": "allow", "rules": []}),
        agent_id="pricing-test-agent",
    )


def _require_cryptography() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        pytest.skip("PyCA cryptography not installed; skipping pricing interop suite")


def _fresh_keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    return sk.private_bytes_raw(), sk.public_key().public_bytes_raw()


# ============================================================
# FFI round-trip — reload + version + settle
# ============================================================


class TestPricingFfiRoundTrip:
    def test_reload_then_version_then_settle(self) -> None:
        """End-to-end: sign a PricingBundle, install it, settle a known call.

        1000 input @ $3/1M = 3000 micros; 500 output @ $15/1M = 7500 micros;
        total 10500 micro-USD. These are the exact rates the core's own
        ``sample_skus()`` uses, so the figure is verifiable against Rust.
        """
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        envelope = _build_dsse_envelope(skb, "test-cp", _build_pricing_bundle(version=3))
        engine = _make_engine()

        engine.reload_pricing_signed(
            json.dumps(envelope),
            json.dumps(_trust_list_for(pkb, "test-cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
        assert engine.get_active_pricing_version() == 3

        usage = json.dumps(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "input_tokens": 1000,
                "output_tokens": 500,
            }
        )
        result = engine.settle_usage("req-1", usage)
        assert result["cost_usd_micros"] == 10_500
        assert result["currency"] == "USD"
        assert result["pricing_bundle_version"] == 3
        assert result["pricing_status"] == "priced"
        assert result["overflow"] is False
        assert result["sku_id"] == "anthropic-claude-sonnet"

    def test_settle_cache_tokens_net_out_of_fresh_input(self) -> None:
        """Cache-read tokens are billed at the cache-read rate, netted out of
        fresh input — proving the wrapper passes the detail counters through and
        the core does the subtraction.

        input=1000 (200 cache-read), output=0:
          fresh_input = 1000 - 200 = 800 @ $3/1M     = 2400
          cache_read  = 200          @ $0.30/1M       =   60
          total = 2460 micro-USD.
        """
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        envelope = _build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=1))
        engine = _make_engine()
        engine.reload_pricing_signed(
            json.dumps(envelope),
            json.dumps(_trust_list_for(pkb, "cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
        usage = json.dumps(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "input_tokens": 1000,
                "output_tokens": 0,
                "cache_read_tokens": 200,
            }
        )
        result = engine.settle_usage("r", usage)
        assert result["cost_usd_micros"] == 2_460

    def test_settle_unknown_model_is_unpriced_not_an_error(self) -> None:
        """A model with no SKU under a provider that also has no catch-all is
        fail-open: cost 0, status ``unpriced_model``, version still the bundle's.
        """
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        envelope = _build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=4))
        engine = _make_engine()
        engine.reload_pricing_signed(
            json.dumps(envelope),
            json.dumps(_trust_list_for(pkb, "cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
        # openai has no SKU in the sample bundle (only anthropic does).
        usage = json.dumps(
            {"provider": "openai", "model": "gpt-4o", "input_tokens": 10, "output_tokens": 10}
        )
        result = engine.settle_usage("r", usage)
        assert result["pricing_status"] == "unpriced_model"
        assert result["cost_usd_micros"] == 0
        assert result["pricing_bundle_version"] == 4
        assert result["sku_id"] is None

    def test_settle_with_no_bundle_is_disabled(self) -> None:
        """Before any pricing reload, settle returns ``disabled`` / version 0 /
        cost 0 — never raises. This is the fail-open metering contract."""
        engine = _make_engine()
        assert engine.get_active_pricing_version() == 0
        result = engine.settle_usage(
            "r",
            json.dumps(
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-5",
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            ),
        )
        assert result["pricing_status"] == "disabled"
        assert result["pricing_bundle_version"] == 0
        assert result["cost_usd_micros"] == 0

    def test_rollback_lower_version_is_rejected_minus_21(self) -> None:
        """Install v3, then a v1 (rollback) is rejected -21; active stays 3."""
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        trusted = json.dumps(_trust_list_for(pkb, "cp"))
        engine = _make_engine()
        engine.reload_pricing_signed(
            json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=3))),
            trusted,
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_pricing_signed(
                json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=1))),
                trusted,
                int(time.time()),
                _TEST_MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -21
        assert exc.value.reason == "pricing_bundle_version_not_monotonic"
        assert engine.get_active_pricing_version() == 3

    def test_tampered_payload_is_rejected_minus_16(self) -> None:
        """Swap the payload after signing → -16 pricing_signature_invalid."""
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        envelope = _build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=1))
        # Re-sign nothing; just replace the payload with a different bundle.
        envelope["payload"] = base64.b64encode(_build_pricing_bundle(version=2)).decode()
        engine = _make_engine()
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_pricing_signed(
                json.dumps(envelope),
                json.dumps(_trust_list_for(pkb, "cp")),
                int(time.time()),
                _TEST_MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -16
        assert exc.value.reason == "pricing_signature_invalid"
        assert engine.get_active_pricing_version() == 0

    def test_unknown_signer_is_rejected_minus_17(self) -> None:
        _require_cryptography()
        skb, _pkb = _fresh_keypair()
        envelope = _build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=1))
        _other_sk, other_pk = _fresh_keypair()  # different key in the trust list
        engine = _make_engine()
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_pricing_signed(
                json.dumps(envelope),
                json.dumps(_trust_list_for(other_pk, "prod-cp")),
                int(time.time()),
                _TEST_MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -17

    def test_stale_bundle_is_rejected_minus_22(self) -> None:
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        now = int(time.time())
        stale = _build_pricing_bundle(version=1, signed_at=now - 25 * 3600)
        engine = _make_engine()
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_pricing_signed(
                json.dumps(_build_dsse_envelope(skb, "cp", stale)),
                json.dumps(_trust_list_for(pkb, "cp")),
                now,
                _TEST_MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -22

    def test_set_initial_pricing_version_one_shot(self) -> None:
        """Restore at 10, then a real install must be > 10; a second restore is
        rejected -24 (already set)."""
        from checkrd.exceptions import CheckrdInitError

        engine = _make_engine()
        engine.set_initial_pricing_version(10)
        assert engine.get_active_pricing_version() == 10
        with pytest.raises(CheckrdInitError) as exc:
            engine.set_initial_pricing_version(5)
        assert "pricing_version_already_set" in str(exc.value)
        assert engine.get_active_pricing_version() == 10

    def test_set_initial_then_reload_enforces_monotonic(self) -> None:
        """After restoring at 10, a v8 bundle is a rollback (-21); v11 installs."""
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        trusted = json.dumps(_trust_list_for(pkb, "cp"))
        engine = _make_engine()
        engine.set_initial_pricing_version(10)
        with pytest.raises(PolicySignatureError) as exc:
            engine.reload_pricing_signed(
                json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=8))),
                trusted,
                int(time.time()),
                _TEST_MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -21
        engine.reload_pricing_signed(
            json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=11))),
            trusted,
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
        assert engine.get_active_pricing_version() == 11


# ============================================================
# Hypothesis property — settle_usage never raises on arbitrary usage
# ============================================================


class TestSettleUsageNeverRaises:
    def test_settle_arbitrary_usage_dicts(self) -> None:
        """``settle_usage`` is fail-open: for ANY usage dict (junk keys,
        negative counts, huge counts, missing fields) it returns a SettleResult
        and never raises. Property-style over a hand-built adversarial corpus.

        (A Hypothesis ``@given`` variant lives in ``test_ffi_properties.py``;
        this in-file version keeps the pricing-specific corpus next to the
        feature it guards.)
        """
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        engine = _make_engine()
        engine.reload_pricing_signed(
            json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=1))),
            json.dumps(_trust_list_for(pkb, "cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )

        corpus: list[Any] = [
            {},
            {"provider": "anthropic"},
            {"model": "claude-sonnet-4-5"},
            {"provider": "anthropic", "model": "claude-sonnet-4-5"},
            {"input_tokens": -5, "output_tokens": -100},
            {"input_tokens": 2**62, "output_tokens": 2**62},
            {"input_tokens": "not-a-number", "output_tokens": None},
            {"provider": 123, "model": ["weird"], "input_tokens": 1.5},
            {"cache_read_tokens": 2**63, "cache_creation_tokens": 2**63, "input_tokens": 1},
            {"reasoning_tokens": 999, "output_tokens": 1},
            {"unknown_field": "ignored", "provider": "anthropic", "model": "claude-sonnet-4-5"},
        ]
        for usage in corpus:
            try:
                payload = json.dumps(usage)
            except (TypeError, ValueError):
                payload = "{}"
            result = engine.settle_usage(f"r-{unique_id()}", payload)
            # Always a well-formed SettleResult.
            assert set(result) >= {
                "cost_usd_micros",
                "currency",
                "pricing_bundle_version",
                "pricing_status",
                "overflow",
                "sku_id",
            }
            assert isinstance(result["cost_usd_micros"], int)
            assert result["cost_usd_micros"] >= 0

    def test_settle_with_malformed_json_does_not_raise(self) -> None:
        """Even non-JSON usage degrades to all-zero usage (cost 0), never an
        exception — the FFI read is fail-open by contract."""
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        engine = _make_engine()
        engine.reload_pricing_signed(
            json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=1))),
            json.dumps(_trust_list_for(pkb, "cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
        result = engine.settle_usage("r", "{not valid json")
        assert isinstance(result["cost_usd_micros"], int)


# ============================================================
# build_usage_input — maps dotted gen_ai attrs → flat UsageInput
# ============================================================


class TestBuildUsageInput:
    def test_maps_all_dotted_keys(self) -> None:
        from checkrd._genai_body import build_usage_input

        # Transport / body / streaming path: dotted OTel keys.
        event = {
            "gen_ai.provider.name": "anthropic",
            "gen_ai.response.model": "claude-sonnet-4-5",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
            "gen_ai.usage.cache_read.input_tokens": 200,
            "gen_ai.usage.cache_creation.input_tokens": 100,
            "gen_ai.usage.reasoning.output_tokens": 50,
        }
        assert build_usage_input(event) == {
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_tokens": 200,
            "cache_creation_tokens": 100,
            "reasoning_tokens": 50,
        }

    def test_maps_flat_adapter_keys_to_parity_with_dotted(self) -> None:
        from checkrd._genai_body import build_usage_input

        # Framework-adapter path (LangChain / OpenAI-Agents) writes the FLAT
        # wire keys. These MUST price identically to the dotted form, or Python
        # would bill adapter LLM calls at $0 while JS billed them — the
        # cross-SDK money divergence this fix closes.
        flat_event = {
            "gen_ai_system": "openai",
            "gen_ai_model": "gpt-4o",
            "gen_ai_input_tokens": 1000,
            "gen_ai_output_tokens": 500,
        }
        assert build_usage_input(flat_event) == {
            "provider": "openai",
            "model": "gpt-4o",
            "input_tokens": 1000,
            "output_tokens": 500,
        }

    def test_no_token_counts_returns_none(self) -> None:
        from checkrd._genai_body import build_usage_input

        # No usage at all → None (mirrors JS `null`), so the caller skips the
        # settle FFI rather than stamping a spurious unpriced_model on a
        # non-GenAI event.
        assert build_usage_input({}) is None
        assert build_usage_input({"gen_ai.provider.name": "openai"}) is None

    def test_zero_tokens_is_a_real_value_not_absent(self) -> None:
        from checkrd._genai_body import build_usage_input

        # 0 is a real count, distinct from absent — it settles.
        usage = build_usage_input({"gen_ai.usage.input_tokens": 0})
        assert usage == {"input_tokens": 0}

    def test_bool_and_float_counts_are_skipped(self) -> None:
        from checkrd._genai_body import build_usage_input

        # bool (a JSON true) and float are skipped, never coerced; a present
        # real count keeps the event priceable.
        usage = build_usage_input(
            {
                "gen_ai.usage.input_tokens": 1000,
                "gen_ai.usage.output_tokens": True,  # bool must NOT count as 1
                "gen_ai.usage.cache_read.input_tokens": 3.5,  # float skipped
            }
        )
        assert usage == {"input_tokens": 1000}

    def test_response_model_preferred_over_request(self) -> None:
        from checkrd._genai_body import build_usage_input

        usage = build_usage_input(
            {
                "gen_ai.request.model": "req-model",
                "gen_ai.response.model": "resp-model",
                "gen_ai.usage.input_tokens": 1,
            }
        )
        assert usage is not None and usage["model"] == "resp-model"


# ============================================================
# cost_metering default-off switch
# ============================================================


class TestCostMeteringSwitch:
    def test_settings_default_off(self) -> None:
        from checkrd._settings import resolve

        assert resolve(env={}).cost_metering is False

    def test_env_truthy_turns_on(self) -> None:
        from checkrd._settings import resolve

        assert resolve(env={"CHECKRD_COST_METERING": "1"}).cost_metering is True
        assert resolve(env={"CHECKRD_COST_METERING": "true"}).cost_metering is True

    def test_env_falsy_or_garbage_stays_off(self) -> None:
        from checkrd._settings import resolve

        assert resolve(env={"CHECKRD_COST_METERING": "0"}).cost_metering is False
        assert resolve(env={"CHECKRD_COST_METERING": "nope"}).cost_metering is False

    def test_explicit_arg_wins_over_env(self) -> None:
        from checkrd._settings import resolve

        # Explicit False beats env truthy.
        assert (
            resolve(cost_metering=False, env={"CHECKRD_COST_METERING": "1"}).cost_metering is False
        )
        # Explicit True beats env falsy.
        assert resolve(cost_metering=True, env={"CHECKRD_COST_METERING": "0"}).cost_metering is True

    def test_enrich_off_does_not_call_settle(self) -> None:
        """With cost_metering off, the transport's enrich path must NOT touch
        the engine's settle/version FFI, and must leave the cost fields unset.
        """
        from checkrd.engine import EvalResult
        from checkrd.transports._httpx import _enrich_telemetry

        engine = Mock(spec=WasmEngine)
        result = EvalResult(
            allowed=True,
            deny_reason=None,
            telemetry_json=json.dumps(
                {
                    "request": {"url_host": "api.anthropic.com", "url_path": "/v1/messages"},
                    "gen_ai.usage.input_tokens": 1000,
                    "gen_ai.usage.output_tokens": 500,
                }
            ),
            request_id="req-1",
        )
        out = _enrich_telemetry(result, 200, 12, engine=engine, cost_metering=False)
        engine.settle_usage.assert_not_called()
        engine.get_active_pricing_version.assert_not_called()
        assert "cost_usd_micros" not in out
        assert "pricing_status" not in out

    def test_enrich_on_but_no_bundle_leaves_fields_unset(self) -> None:
        """cost_metering on but no price table (version 0): we check the
        version, see 0, and skip settle entirely — cost fields stay unset."""
        from checkrd.engine import EvalResult
        from checkrd.transports._httpx import _enrich_telemetry

        engine = Mock(spec=WasmEngine)
        engine.get_active_pricing_version.return_value = 0
        result = EvalResult(
            allowed=True,
            deny_reason=None,
            telemetry_json=json.dumps(
                {"request": {"url_host": "api.anthropic.com", "url_path": "/v1/messages"}}
            ),
            request_id="req-1",
        )
        out = _enrich_telemetry(result, 200, 12, engine=engine, cost_metering=True)
        engine.settle_usage.assert_not_called()
        assert "cost_usd_micros" not in out

    def test_enrich_on_with_bundle_stamps_cost_fields(self) -> None:
        """cost_metering on + bundle installed: the settle result is stamped
        onto the event as the flat cost fields."""
        from checkrd.engine import EvalResult
        from checkrd.transports._httpx import _enrich_telemetry

        engine = Mock(spec=WasmEngine)
        engine.get_active_pricing_version.return_value = 7
        engine.settle_usage.return_value = {
            "cost_usd_micros": 10_500,
            "currency": "USD",
            "pricing_bundle_version": 7,
            "pricing_status": "priced",
            "overflow": False,
            "sku_id": "anthropic-claude-sonnet",
        }
        result = EvalResult(
            allowed=True,
            deny_reason=None,
            telemetry_json=json.dumps(
                {
                    "request": {"url_host": "api.anthropic.com", "url_path": "/v1/messages"},
                    "gen_ai.response.model": "claude-sonnet-4-5",
                    "gen_ai.usage.input_tokens": 1000,
                    "gen_ai.usage.output_tokens": 500,
                }
            ),
            request_id="req-1",
        )
        out = _enrich_telemetry(result, 200, 12, engine=engine, cost_metering=True)
        engine.settle_usage.assert_called_once()
        # The UsageInput passed to settle reflects the event's gen_ai attrs.
        _rid, usage_json = engine.settle_usage.call_args.args
        usage = json.loads(usage_json)
        assert usage["provider"] == "anthropic"  # stamped by the URL extractor
        assert usage["model"] == "claude-sonnet-4-5"
        assert usage["input_tokens"] == 1000
        assert out["cost_usd_micros"] == 10_500
        assert out["currency"] == "USD"
        assert out["pricing_bundle_version"] == 7
        assert out["pricing_status"] == "priced"

    def test_enrich_swallows_settle_failure(self) -> None:
        """A settle_usage that raises must never break enrich — cost fields are
        simply left unset (a metering glitch can't break the host request)."""
        from checkrd.engine import EvalResult
        from checkrd.transports._httpx import _enrich_telemetry

        engine = Mock(spec=WasmEngine)
        engine.get_active_pricing_version.return_value = 3
        engine.settle_usage.side_effect = RuntimeError("boom")
        result = EvalResult(
            allowed=True,
            deny_reason=None,
            telemetry_json=json.dumps(
                {"request": {"url_host": "api.anthropic.com", "url_path": "/v1/messages"}}
            ),
            request_id="req-1",
        )
        out = _enrich_telemetry(result, 200, 12, engine=engine, cost_metering=True)
        assert "cost_usd_micros" not in out


# ============================================================
# Pricing-version persistence (_pricing_state)
# ============================================================


class TestPricingStatePersistence:
    def test_round_trip_version(self, tmp_path: Path) -> None:
        from checkrd._pricing_state import (
            load_persisted_pricing_version,
            persist_pricing_version,
        )

        state = tmp_path / "pricing_state.json"
        persist_pricing_version(42, path=state)
        assert load_persisted_pricing_version(state) == 42

    def test_missing_file_cold_starts_at_zero(self, tmp_path: Path) -> None:
        from checkrd._pricing_state import load_persisted_pricing_version

        assert load_persisted_pricing_version(tmp_path / "does_not_exist.json") == 0

    def test_corrupt_file_cold_starts_at_zero(self, tmp_path: Path) -> None:
        from checkrd._pricing_state import load_persisted_pricing_version

        state = tmp_path / "pricing_state.json"
        state.write_text("{this is not valid json", encoding="utf-8")
        assert load_persisted_pricing_version(state) == 0

    def test_unknown_schema_cold_starts_at_zero(self, tmp_path: Path) -> None:
        from checkrd._pricing_state import load_persisted_pricing_version

        state = tmp_path / "pricing_state.json"
        state.write_text(
            json.dumps({"schema_version": 999, "last_pricing_version": 5}),
            encoding="utf-8",
        )
        assert load_persisted_pricing_version(state) == 0

    def test_non_int_version_cold_starts_at_zero(self, tmp_path: Path) -> None:
        from checkrd._pricing_state import load_persisted_pricing_version

        state = tmp_path / "pricing_state.json"
        state.write_text(
            json.dumps({"schema_version": 1, "last_pricing_version": "five"}),
            encoding="utf-8",
        )
        assert load_persisted_pricing_version(state) == 0

    def test_bool_version_rejected(self, tmp_path: Path) -> None:
        # JSON true is a bool subclass of int — must NOT be accepted as version 1.
        from checkrd._pricing_state import load_persisted_pricing_version

        state = tmp_path / "pricing_state.json"
        state.write_text(
            json.dumps({"schema_version": 1, "last_pricing_version": True}),
            encoding="utf-8",
        )
        assert load_persisted_pricing_version(state) == 0

    def test_persist_failure_does_not_raise(self, tmp_path: Path) -> None:
        """A disk crash mid-persist (patched os.replace) is swallowed — the
        in-process rollback defense is unaffected, mirroring policy-state."""
        from unittest.mock import patch

        from checkrd._pricing_state import persist_pricing_version

        state = tmp_path / "pricing_state.json"
        with patch("checkrd._pricing_state.os.replace", side_effect=OSError("disk full")):
            persist_pricing_version(7, path=state)  # must not raise
        # File never materialized; loader cold-starts.
        from checkrd._pricing_state import load_persisted_pricing_version

        assert load_persisted_pricing_version(state) == 0

    def test_persist_invalid_version_is_skipped(self, tmp_path: Path) -> None:
        from checkrd._pricing_state import (
            load_persisted_pricing_version,
            persist_pricing_version,
        )

        state = tmp_path / "pricing_state.json"
        persist_pricing_version(-1, path=state)  # out of range → skipped
        assert load_persisted_pricing_version(state) == 0

    def test_restore_via_set_initial_across_simulated_restart(self) -> None:
        """The cross-restart property: a process installs v9, the version is
        persisted, and a *fresh* engine (simulated restart) re-seeded via
        ``set_initial_pricing_version`` then rejects a replayed v5.

        This is the end-to-end rollback-survives-restart proof at the engine
        seam, the pricing analogue of the policy-state restart test.
        """
        _require_cryptography()
        skb, pkb = _fresh_keypair()
        trusted = json.dumps(_trust_list_for(pkb, "cp"))

        # --- "run 1": install v9, read the persisted high-water mark.
        engine1 = _make_engine()
        engine1.reload_pricing_signed(
            json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=9))),
            trusted,
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
        persisted = engine1.get_active_pricing_version()
        assert persisted == 9

        # --- "run 2": brand-new engine (restart). Seed from persisted BEFORE
        #     any reload, exactly as ControlReceiver.start does on boot.
        engine2 = _make_engine()
        assert engine2.get_active_pricing_version() == 0  # in-memory reset
        engine2.set_initial_pricing_version(persisted)
        assert engine2.get_active_pricing_version() == 9

        # A replayed older (v5) signed bundle is now rejected post-restart.
        with pytest.raises(PolicySignatureError) as exc:
            engine2.reload_pricing_signed(
                json.dumps(_build_dsse_envelope(skb, "cp", _build_pricing_bundle(version=5))),
                trusted,
                int(time.time()),
                _TEST_MAX_AGE_SECS,
            )
        assert exc.value.ffi_code == -21
