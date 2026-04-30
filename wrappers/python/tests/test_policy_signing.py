"""End-to-end tests for signed policy bundle distribution.

The tests in this file use the **PyCA `cryptography`** Ed25519 implementation
as the signer side, and the **WASM core** ``reload_policy_signed`` FFI as the
verifier side. The two implementations are completely independent (different
codebases, different languages, different crypto backends), so passing this
test proves the wire format is interoperable with any RFC 8032 / RFC 9421 /
DSSE-conformant Ed25519 library.

Mirrors the cross-implementation interop pattern from ``test_batcher.py``
that we used for telemetry signing.

# Standards anchored

- RFC 8032 (Ed25519) — both PyCA `cryptography` and `ed25519-dalek` implement
- DSSE protocol.md — PAE construction
- ``crates/shared/src/dsse.rs::POLICY_BUNDLE_PAYLOAD_TYPE`` — domain separation
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from checkrd.engine import WasmEngine
from checkrd.exceptions import PolicySignatureError
from tests.conftest import requires_wasm

# This test module exercises the WASM core, so skip if .wasm isn't built.
pytestmark = requires_wasm


# ============================================================
# Test helpers
# ============================================================


def _make_engine() -> WasmEngine:
    """Construct a WasmEngine with a default-deny policy and an anonymous
    identity (signing isn't needed for the verifier-side tests)."""
    return WasmEngine(
        policy_json=json.dumps(
            {
                "agent": "test-agent",
                "default": "deny",
                "rules": [],
            }
        ),
        agent_id="test-agent",
    )


_PERMISSIVE_POLICY = {
    "agent": "test-agent",
    "default": "allow",
    "rules": [],
}

# Test constants matching the WASM core's reload_policy_signed expectations.
_TEST_MAX_AGE_SECS = 86_400  # 24 hours


def _build_policy_bundle(
    policy: dict, version: int = 1, signed_at: int | None = None
) -> bytes:
    """Wrap a policy in a versioned PolicyBundle and serialize to JSON bytes.

    Strong-from-the-ground-up: every signed payload is a versioned bundle,
    never a bare policy.
    """
    if signed_at is None:
        signed_at = int(time.time())
    bundle = {
        "schema_version": 1,
        "version": version,
        "signed_at": signed_at,
        "policy": policy,
    }
    return json.dumps(bundle).encode()


def _build_dsse_envelope(
    private_key_bytes: bytes,
    keyid: str,
    payload_bytes: bytes,
    payload_type: str = "application/vnd.checkrd.policy-bundle+yaml",
) -> dict:
    """Construct a DSSE envelope by signing with PyCA cryptography.

    This is the gold-standard cross-implementation test: PyCA cryptography
    is a completely independent Ed25519 implementation (Rust ``cryptography_rust``
    backed by OpenSSL/BoringSSL), and the verifier inside the WASM core uses
    ``ed25519-dalek`` (pure Rust). If the two agree on the signature, the
    wire format is interoperable.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signing_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

    # Reconstruct DSSE PAE in pure Python so the test doesn't depend on any
    # of our own DSSE code on the signer side. This is the spec text from
    # secure-systems-lab/dsse/protocol.md.
    pae = (
        b"DSSEv1 "
        + str(len(payload_type)).encode()
        + b" "
        + payload_type.encode()
        + b" "
        + str(len(payload_bytes)).encode()
        + b" "
        + payload_bytes
    )
    sig = signing_key.sign(pae)

    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload_bytes).decode(),
        "signatures": [
            {
                "keyid": keyid,
                "sig": base64.b64encode(sig).decode(),
            }
        ],
    }


def _trust_list_for(public_key_bytes: bytes, keyid: str) -> list[dict]:
    return [
        {
            "keyid": keyid,
            "public_key_hex": public_key_bytes.hex(),
            "valid_from": 0,
            "valid_until": 2**63,
        }
    ]


# ============================================================
# Cross-implementation round-trip
# ============================================================


def _check_cryptography_available() -> None:
    """Skip the suite cleanly if PyCA cryptography isn't installed."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        pytest.skip("PyCA cryptography not installed; skipping interop suite")


def test_pyca_cryptography_signs_wasm_core_verifies_policy() -> None:
    """The end-to-end interop test.

    1. Generate an Ed25519 keypair via PyCA cryptography.
    2. Sign a permissive policy via DSSE PAE (reconstructed in pure Python).
    3. Call ``engine.reload_policy_signed`` with the envelope and a trust
       list containing only the test public key.
    4. Assert the policy installs (no exception raised).

    Passing this test proves: (a) the WASM core's signature base / PAE
    construction matches the DSSE spec, (b) ed25519-dalek and PyCA
    cryptography agree on Ed25519 signatures over the same bytes, (c) the
    JSON envelope wire format is unambiguous.
    """
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()

    payload = _build_policy_bundle(_PERMISSIVE_POLICY)
    envelope = _build_dsse_envelope(sk_bytes, "test-cp-2026", payload)
    trusted = _trust_list_for(pk_bytes, "test-cp-2026")

    engine = _make_engine()
    engine.reload_policy_signed(
        json.dumps(envelope),
        json.dumps(trusted),
        int(time.time()),
        _TEST_MAX_AGE_SECS,
    )
    # No exception → success.


def test_tampered_envelope_is_rejected() -> None:
    """Flip one byte of the payload after signing → verification must fail."""
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()

    payload = _build_policy_bundle(_PERMISSIVE_POLICY)
    envelope = _build_dsse_envelope(sk_bytes, "test-cp", payload)
    # Tamper: replace the payload with a different policy AFTER signing.
    tampered_payload = json.dumps(
        {"agent": "evil", "default": "allow", "rules": []}
    ).encode()
    envelope["payload"] = base64.b64encode(tampered_payload).decode()

    engine = _make_engine()
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(envelope),
            json.dumps(_trust_list_for(pk_bytes, "test-cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -5
    assert exc_info.value.reason == "signature_invalid"
    assert exc_info.value.code == "signature_invalid"


def test_unknown_signer_is_rejected() -> None:
    """Signature is valid but the keyid is not in the trust list."""
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()

    payload = _build_policy_bundle(_PERMISSIVE_POLICY)
    envelope = _build_dsse_envelope(sk_bytes, "unknown-cp", payload)

    # Trust list contains a different keyid (with a different public key).
    other_sk = Ed25519PrivateKey.generate()
    trusted = _trust_list_for(other_sk.public_key().public_bytes_raw(), "production-cp")

    engine = _make_engine()
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(envelope),
            json.dumps(trusted),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -6
    assert exc_info.value.reason == "unknown_or_no_signer"
    assert exc_info.value.code == "unknown_or_no_signer"


def test_cross_type_replay_attack_is_rejected() -> None:
    """The most important test: a signature on the same bytes under the
    TELEMETRY payload type cannot be installed as a policy.

    This proves the DSSE payload-type binding gives real domain separation.
    Without it, an attacker who captured a valid telemetry signature could
    try to install a malicious policy.
    """
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()

    payload = _build_policy_bundle(_PERMISSIVE_POLICY)
    # Sign under the TELEMETRY payload type — wrong type for the policy verifier.
    envelope = _build_dsse_envelope(
        sk_bytes,
        "test-cp",
        payload,
        payload_type="application/vnd.checkrd.telemetry-batch+json",
    )

    engine = _make_engine()
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(envelope),
            json.dumps(_trust_list_for(pk_bytes, "test-cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -4
    assert exc_info.value.reason == "payload_type_mismatch"
    assert exc_info.value.code == "payload_type_mismatch"


def test_expired_key_is_rejected() -> None:
    """Trusted key with valid_until in the past must be rejected."""
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()

    payload = _build_policy_bundle(_PERMISSIVE_POLICY)
    envelope = _build_dsse_envelope(sk_bytes, "test-cp", payload)
    expired_trusted = [
        {
            "keyid": "test-cp",
            "public_key_hex": pk_bytes.hex(),
            "valid_from": 0,
            "valid_until": 1,  # ~Jan 1 1970
        }
    ]

    engine = _make_engine()
    # Use a "now" much later than valid_until=1 so the key-window check fires.
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(envelope),
            json.dumps(expired_trusted),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -7
    assert exc_info.value.reason == "key_not_in_validity_window"
    assert exc_info.value.code == "key_not_in_validity_window"


def test_stale_bundle_is_rejected() -> None:
    """A bundle whose ``signed_at`` is older than ``max_age_secs`` must be
    rejected with ``bundle_too_old``. Defends against an attacker who
    captured a valid envelope long ago and is replaying it now.

    The freshness check is `now - signed_at > max_age_secs` (strict
    greater-than), so we sign 25 hours in the past against a 24-hour
    window to land just outside the boundary.
    """
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()

    now = int(time.time())
    stale_signed_at = now - 25 * 3600  # 25 hours ago
    payload = _build_policy_bundle(_PERMISSIVE_POLICY, signed_at=stale_signed_at)
    envelope = _build_dsse_envelope(sk_bytes, "test-cp", payload)

    engine = _make_engine()
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(envelope),
            json.dumps(_trust_list_for(pk_bytes, "test-cp")),
            now,
            _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -12
    assert exc_info.value.reason == "bundle_too_old"


def test_future_dated_bundle_is_rejected() -> None:
    """A bundle whose ``signed_at`` is far in the future is rejected with
    ``bundle_in_future``. Defends against a control plane with a wildly
    wrong clock or a malicious one trying to lock SDKs into a never-
    expiring envelope.
    """
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()

    now = int(time.time())
    future_signed_at = now + 24 * 3600  # 24 hours in the future
    payload = _build_policy_bundle(_PERMISSIVE_POLICY, signed_at=future_signed_at)
    envelope = _build_dsse_envelope(sk_bytes, "test-cp", payload)

    engine = _make_engine()
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(envelope),
            json.dumps(_trust_list_for(pk_bytes, "test-cp")),
            now,
            _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -13
    assert exc_info.value.reason == "bundle_in_future"


def test_monotonic_rollback_is_rejected() -> None:
    """Installing a bundle with version <= last_policy_version is rejected
    with ``bundle_version_not_monotonic``. This is the rollback defense:
    once an SDK has installed v5, an attacker cannot trick it back to v3
    (which might be a more permissive historical policy).

    The check uses ``<=`` not ``<`` so even installing the SAME version
    again is rejected — the wire-level dedupe is the SDK's job, the WASM
    core enforces strict monotonicity.
    """
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()
    trusted = _trust_list_for(pk_bytes, "test-cp")

    engine = _make_engine()

    # First install v5 — succeeds.
    v5_payload = _build_policy_bundle(_PERMISSIVE_POLICY, version=5)
    v5_envelope = _build_dsse_envelope(sk_bytes, "test-cp", v5_payload)
    engine.reload_policy_signed(
        json.dumps(v5_envelope), json.dumps(trusted), int(time.time()), _TEST_MAX_AGE_SECS,
    )
    assert engine.get_active_policy_version() == 5

    # Now try v3 (rollback) — must be rejected.
    v3_payload = _build_policy_bundle(_PERMISSIVE_POLICY, version=3)
    v3_envelope = _build_dsse_envelope(sk_bytes, "test-cp", v3_payload)
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(v3_envelope), json.dumps(trusted), int(time.time()), _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -11
    assert exc_info.value.reason == "bundle_version_not_monotonic"
    # Active version unchanged after the rejected install.
    assert engine.get_active_policy_version() == 5

    # Same version twice is also rejected (strict `<=`).
    with pytest.raises(PolicySignatureError) as exc2:
        engine.reload_policy_signed(
            json.dumps(v5_envelope), json.dumps(trusted), int(time.time()), _TEST_MAX_AGE_SECS,
        )
    assert exc2.value.ffi_code == -11


def test_key_not_yet_valid_is_rejected() -> None:
    """Trusted key whose ``valid_from`` is in the future must be rejected.
    Mirrors the existing expired-key test but on the lower boundary."""
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()

    now = int(time.time())
    payload = _build_policy_bundle(_PERMISSIVE_POLICY)
    envelope = _build_dsse_envelope(sk_bytes, "test-cp", payload)
    not_yet_trusted = [
        {
            "keyid": "test-cp",
            "public_key_hex": pk_bytes.hex(),
            "valid_from": now + 24 * 3600,  # not valid for another day
            "valid_until": now + 365 * 24 * 3600,
        }
    ]

    engine = _make_engine()
    with pytest.raises(PolicySignatureError) as exc_info:
        engine.reload_policy_signed(
            json.dumps(envelope),
            json.dumps(not_yet_trusted),
            now,
            _TEST_MAX_AGE_SECS,
        )
    assert exc_info.value.ffi_code == -7
    assert exc_info.value.reason == "key_not_in_validity_window"


def test_envelope_with_no_signatures_is_rejected() -> None:
    """A wire envelope carrying an empty ``signatures`` array must be
    rejected. The DSSE spec requires at least one signature; the verifier
    surfaces this as a structured error (not a panic) so the receiver can
    log + continue with the previous policy."""
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    pk_bytes = sk.public_key().public_bytes_raw()

    payload = _build_policy_bundle(_PERMISSIVE_POLICY)
    envelope_no_sigs = {
        "payloadType": "application/vnd.checkrd.policy-bundle+yaml",
        "payload": base64.b64encode(payload).decode(),
        "signatures": [],
    }

    engine = _make_engine()
    with pytest.raises(PolicySignatureError):
        engine.reload_policy_signed(
            json.dumps(envelope_no_sigs),
            json.dumps(_trust_list_for(pk_bytes, "test-cp")),
            int(time.time()),
            _TEST_MAX_AGE_SECS,
        )


def test_signed_policy_actually_replaces_old_policy() -> None:
    """End-to-end: install a default-deny policy, then sign and install a
    permissive one. Evaluate a request that the new policy must allow but
    the old one would have denied. Proves the install is actually applied
    after verification (not just verified-then-discarded).
    """
    _check_cryptography_available()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    engine = _make_engine()
    # Sanity: default-deny rejects an unknown URL.
    pre = engine.evaluate(
        request_id="r-pre",
        method="GET",
        url="https://example.com/api",
        headers=[],
        body=None,
        timestamp="2026-04-08T10:00:00Z",
        timestamp_ms=int(time.time() * 1000),
    )
    assert not pre.allowed

    sk = Ed25519PrivateKey.generate()
    sk_bytes = sk.private_bytes_raw()
    pk_bytes = sk.public_key().public_bytes_raw()
    envelope = _build_dsse_envelope(
        sk_bytes, "test-cp", _build_policy_bundle(_PERMISSIVE_POLICY)
    )
    engine.reload_policy_signed(
        json.dumps(envelope),
        json.dumps(_trust_list_for(pk_bytes, "test-cp")),
        int(time.time()),
        _TEST_MAX_AGE_SECS,
    )

    # After the signed reload, the same request must be allowed.
    post = engine.evaluate(
        request_id="r-post",
        method="GET",
        url="https://example.com/api",
        headers=[],
        body=None,
        timestamp="2026-04-08T10:00:00Z",
        timestamp_ms=int(time.time() * 1000),
    )
    assert post.allowed, "permissive policy should allow this request"
