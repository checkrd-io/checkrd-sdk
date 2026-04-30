"""Tests for ``checkrd.webhooks.verify_webhook``.

Closes the security-critical 0% coverage gap on a module that
implements the same Stripe-pattern HMAC scheme third-party services
use to authenticate inbound POSTs to customer endpoints. Every
verification path needs a test — a future maintainer who weakens
``hmac.compare_digest`` to ``==`` would otherwise ship silently.

The tests stand up a tiny "server side" using the same primitives
the public API expects (HMAC-SHA256 over ``{timestamp}.{body}``)
and feed the result through ``verify_webhook``. Each branch of the
verifier — happy path, every error code, multi-secret rotation,
str-vs-bytes body, default tolerance — has a dedicated assertion.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from checkrd.webhooks import (
    DEFAULT_TOLERANCE_SECS,
    WebhookVerificationError,
    verify_webhook,
)


# ---------------------------------------------------------------------------
# Helpers — synthesize the header the verifier expects.
# ---------------------------------------------------------------------------


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    """Compute the HMAC-SHA256 hex digest matching the verifier's input.

    The signed payload is exactly ``f"{timestamp}.".encode() + body``;
    any deviation (whitespace, trailing newline, JSON re-serialization)
    invalidates the result. Tests construct the digest with the same
    primitives so a verifier bug shows up here, not in cross-language
    interop.
    """
    msg = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _header(timestamp: int, *signatures: str) -> str:
    """Build the ``t=...,v1=...`` envelope. Multiple ``v1=`` entries
    are valid (the verifier walks them all)."""
    parts = [f"t={timestamp}"]
    for sig in signatures:
        parts.append(f"v1={sig}")
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestVerifyWebhookHappyPath:
    """A correctly-signed body verifies silently."""

    def test_round_trip_bytes_body(self) -> None:
        secret = "whsec_test_xxxxxxxxxxxxxxxxxxxxx"
        body = b'{"event":"policy.installed","agent":"sales-agent"}'
        ts = 1_730_000_000
        sig = _sign(secret, ts, body)
        # No exception → success. ``verify_webhook`` returns ``None``.
        verify_webhook(
            raw_body=body,
            signature_header=_header(ts, sig),
            secret=secret,
            now_unix_secs=ts,
        )

    def test_round_trip_str_body(self) -> None:
        # Frameworks like Django sometimes hand the body back as a
        # str. The verifier accepts both; behaviour must match.
        secret = "whsec_test_xxxxxxxxxxxxxxxxxxxxx"
        body_str = '{"event":"policy.installed"}'
        ts = 1_730_000_000
        sig = _sign(secret, ts, body_str.encode("utf-8"))
        verify_webhook(
            raw_body=body_str,
            signature_header=_header(ts, sig),
            secret=secret,
            now_unix_secs=ts,
        )

    def test_secret_list_first_match(self) -> None:
        # Rotation pattern: caller passes a list of acceptable
        # secrets and any one matching is enough. The first entry
        # matches here, the rest are decoys.
        good = "whsec_new_2026"
        decoy = "whsec_old_2025"
        body = b'{"x":1}'
        ts = 1_730_000_000
        sig = _sign(good, ts, body)
        verify_webhook(
            raw_body=body,
            signature_header=_header(ts, sig),
            secret=[good, decoy],
            now_unix_secs=ts,
        )

    def test_secret_list_second_match(self) -> None:
        # During the overlap window of a key rotation, traffic signed
        # with the OLD secret must still verify when the new one is
        # already deployed. The verifier walks all candidates.
        new = "whsec_new_2026"
        old = "whsec_old_2025"
        body = b'{"x":1}'
        ts = 1_730_000_000
        sig = _sign(old, ts, body)
        verify_webhook(
            raw_body=body,
            signature_header=_header(ts, sig),
            secret=[new, old],
            now_unix_secs=ts,
        )

    def test_secret_tuple_accepted(self) -> None:
        # API accepts tuple in addition to list — useful for
        # config-loaded immutable rotation lists.
        secret = ("whsec_a", "whsec_b")
        body = b"x"
        ts = 1_730_000_000
        sig = _sign("whsec_b", ts, body)
        verify_webhook(
            raw_body=body,
            signature_header=_header(ts, sig),
            secret=secret,
            now_unix_secs=ts,
        )

    def test_multi_signature_in_header(self) -> None:
        # The ``v1=`` envelope can carry several signatures (e.g.,
        # during the operator-facing portion of a key rotation
        # ceremony). The verifier accepts the body if ANY signature
        # validates against ANY secret.
        secret = "whsec_test"
        body = b"x"
        ts = 1_730_000_000
        right = _sign(secret, ts, body)
        wrong = "0" * 64
        verify_webhook(
            raw_body=body,
            signature_header=_header(ts, wrong, right),
            secret=secret,
            now_unix_secs=ts,
        )

    def test_now_at_tolerance_boundary_inclusive(self) -> None:
        # ``abs(now - timestamp) > tolerance`` means equality is
        # accepted. Boundary case must verify.
        secret = "whsec_test"
        body = b"x"
        ts = 1_730_000_000
        sig = _sign(secret, ts, body)
        verify_webhook(
            raw_body=body,
            signature_header=_header(ts, sig),
            secret=secret,
            tolerance_secs=300,
            now_unix_secs=ts + 300,
        )

    def test_default_tolerance_constant(self) -> None:
        # Documented contract — Stripe's default is 300; the SDK
        # mirrors it. Catch accidental changes.
        assert DEFAULT_TOLERANCE_SECS == 300


# ---------------------------------------------------------------------------
# Error paths — every ``WebhookVerificationError.code`` value
# ---------------------------------------------------------------------------


class TestVerifyWebhookFailures:
    def test_empty_secret_string(self) -> None:
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="t=1,v1=00",
                secret="",
                now_unix_secs=1,
            )
        assert exc.value.code == "empty_secret"

    def test_empty_secret_in_list(self) -> None:
        # If ANY entry in the rotation list is empty, treat as
        # operator misconfiguration and refuse rather than silently
        # accept the empty-key fallback.
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="t=1,v1=00",
                secret=["whsec_real", ""],
                now_unix_secs=1,
            )
        assert exc.value.code == "empty_secret"

    def test_missing_header_none(self) -> None:
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header=None,
                secret="whsec_test",
            )
        assert exc.value.code == "missing_header"

    def test_missing_header_empty(self) -> None:
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="",
                secret="whsec_test",
            )
        assert exc.value.code == "missing_header"

    def test_malformed_header_no_timestamp(self) -> None:
        # Header parses (it has ``v1=...``) but no ``t=``.
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="v1=" + ("a" * 64),
                secret="whsec_test",
                now_unix_secs=1,
            )
        assert exc.value.code == "malformed_header"

    def test_malformed_header_no_signature(self) -> None:
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="t=1730000000",
                secret="whsec_test",
                now_unix_secs=1_730_000_000,
            )
        assert exc.value.code == "malformed_header"

    def test_malformed_header_invalid_hex(self) -> None:
        # Length is 64 but contains non-hex chars — regex rejects.
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="t=1,v1=" + ("Z" * 64),
                secret="whsec_test",
                now_unix_secs=1,
            )
        assert exc.value.code == "malformed_header"

    def test_malformed_header_short_sig(self) -> None:
        # Signature must be exactly 64 hex chars (HMAC-SHA256 output).
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="t=1,v1=" + ("a" * 32),
                secret="whsec_test",
                now_unix_secs=1,
            )
        assert exc.value.code == "malformed_header"

    def test_malformed_header_non_integer_timestamp(self) -> None:
        # ``t=`` must parse as int. Non-integer values are dropped
        # silently by the parser, so only ``v1`` survives, then the
        # missing-timestamp branch fires.
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=b"x",
                signature_header="t=not-an-int,v1=" + ("a" * 64),
                secret="whsec_test",
                now_unix_secs=1,
            )
        assert exc.value.code == "malformed_header"

    def test_timestamp_too_old(self) -> None:
        secret = "whsec_test"
        body = b"x"
        ts = 1_730_000_000
        sig = _sign(secret, ts, body)
        # Default tolerance is 300; clock is 301 seconds ahead.
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=body,
                signature_header=_header(ts, sig),
                secret=secret,
                now_unix_secs=ts + 301,
            )
        assert exc.value.code == "timestamp_out_of_range"

    def test_timestamp_too_new(self) -> None:
        secret = "whsec_test"
        body = b"x"
        ts = 1_730_000_000
        sig = _sign(secret, ts, body)
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=body,
                signature_header=_header(ts, sig),
                secret=secret,
                now_unix_secs=ts - 301,
            )
        assert exc.value.code == "timestamp_out_of_range"

    def test_signature_mismatch(self) -> None:
        # Well-formed envelope, valid timestamp, but the signature
        # was computed under a different secret.
        wrong_secret = "whsec_attacker"
        right_secret = "whsec_real"
        body = b"x"
        ts = 1_730_000_000
        sig = _sign(wrong_secret, ts, body)
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=body,
                signature_header=_header(ts, sig),
                secret=right_secret,
                now_unix_secs=ts,
            )
        assert exc.value.code == "signature_mismatch"

    def test_signature_mismatch_after_body_tamper(self) -> None:
        # Caller verifies the body it received. If the body was
        # tampered with in transit, the HMAC must NOT match.
        secret = "whsec_test"
        original = b'{"amount":100}'
        tampered = b'{"amount":1000}'
        ts = 1_730_000_000
        sig = _sign(secret, ts, original)
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=tampered,
                signature_header=_header(ts, sig),
                secret=secret,
                now_unix_secs=ts,
            )
        assert exc.value.code == "signature_mismatch"

    def test_signature_mismatch_after_timestamp_replay(self) -> None:
        # The Stripe-pattern defense: even a captured valid
        # signature can't be replayed under a different timestamp.
        # Attacker rewrites ``t=`` to something fresh; HMAC fails
        # because the signed payload starts with the OLD timestamp.
        secret = "whsec_test"
        body = b"x"
        original_ts = 1_730_000_000
        sig = _sign(secret, original_ts, body)
        replay_ts = original_ts + 60
        with pytest.raises(WebhookVerificationError) as exc:
            verify_webhook(
                raw_body=body,
                signature_header=_header(replay_ts, sig),
                secret=secret,
                now_unix_secs=replay_ts,
            )
        assert exc.value.code == "signature_mismatch"


# ---------------------------------------------------------------------------
# Constant-time invariant
# ---------------------------------------------------------------------------


class TestConstantTimeCompare:
    """The verifier must use ``hmac.compare_digest`` (not ``==``).

    A direct test of constant-time behaviour requires statistical
    measurement and is flaky. Instead we pin the implementation
    detail: ``compare_digest`` is the only equality operator that
    appears next to the candidate signature in source. If a
    refactor introduces a plain ``==`` against secret-bearing input,
    this test fails loudly.
    """

    def test_source_uses_compare_digest_only(self) -> None:
        from pathlib import Path

        src = Path(__file__).parent.parent / "src" / "checkrd" / "webhooks.py"
        text = src.read_text(encoding="utf-8")
        # ``hmac.compare_digest`` is required.
        assert "hmac.compare_digest" in text, (
            "verify_webhook must use hmac.compare_digest for constant-time "
            "comparison; do not replace with `==` on secret-bearing input"
        )
        # No ``provided == expected`` style equality on candidate
        # signatures. The forbidden pattern is "==" appearing inside
        # the verify_webhook function body adjacent to ``provided`` /
        # ``expected`` / ``signature``. Cheap heuristic: scan every
        # line that mentions one of those names and assert no ``==``.
        sensitive_names = {"provided", "expected", "signature", "sig"}
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if " == " in stripped or stripped.endswith("=="):
                if any(name in stripped for name in sensitive_names):
                    pytest.fail(
                        f"webhooks.py:{lineno} appears to compare a "
                        f"secret-bearing value with `==`. Use "
                        f"``hmac.compare_digest`` instead.\n"
                        f"  {stripped}"
                    )


# ---------------------------------------------------------------------------
# Default time-source plumbing
# ---------------------------------------------------------------------------


class TestNowDefaulting:
    def test_defaults_to_real_clock_when_now_omitted(self) -> None:
        # Without ``now_unix_secs``, the verifier reads
        # ``time.time()``. We can't force the clock but we can pin
        # that the real-clock branch executes by signing with a
        # fresh-now timestamp — verification must succeed.
        import time

        secret = "whsec_test"
        body = b"x"
        ts = int(time.time())
        sig = _sign(secret, ts, body)
        verify_webhook(
            raw_body=body,
            signature_header=_header(ts, sig),
            secret=secret,
        )
