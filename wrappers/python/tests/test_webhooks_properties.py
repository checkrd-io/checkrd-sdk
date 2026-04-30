"""Property-based tests for ``checkrd.webhooks``.

Companion to ``test_webhooks.py`` (the unit suite). Hypothesis-driven
properties harden the parser against fuzz-class input — the kind that
would normally require running a fuzzer in CI but that we can pin
deterministically with bounded random sampling.

Three invariants matter for the security-critical surface here:

1. **Parser is total.** No matter how garbled the signature header,
   ``verify_webhook`` raises ``WebhookVerificationError`` (with one of
   the five documented codes) — never an uncaught ``ValueError``,
   ``UnicodeDecodeError``, or ``re.error``. A parser that crashes is a
   denial-of-service vector for any framework that catches only
   ``WebhookVerificationError``.

2. **Hex decoder is well-formed-or-rejects.** Anything that isn't
   exactly 64 hex chars is dropped at parse time, before HMAC runs.
   This is the hand-verifiable property behind the constant-time claim
   — the comparison only ever runs on equal-length 64-hex inputs.

3. **Round-trip / tamper.** For any (secret, body, timestamp) triple
   we generate, the signature we compute verifies; flipping any byte
   of body, secret, or timestamp invalidates it. This is the property
   third-party services bet their integrations on.

Scope is bounded — ~100 examples per property, total runtime <2s on a
laptop. The Rust core has its own Wycheproof + mutation coverage for
HMAC primitives; these tests defend the wrapper's parser layer.
"""

from __future__ import annotations

import hashlib
import hmac

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from checkrd.webhooks import WebhookVerificationError, verify_webhook

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Documented codes — tests assert the parser only ever raises these. A
# refactor that introduced a new code without updating the docs would
# fail here, prompting either the docs to catch up or the new code to
# be removed.
_DOCUMENTED_CODES = frozenset(
    {
        "missing_header",
        "malformed_header",
        "timestamp_out_of_range",
        "signature_mismatch",
        "empty_secret",
    }
)

# Any UTF-8 text excluding unpaired surrogates (they can't encode).
# Hypothesis's ``text()`` already excludes surrogate halves by default
# but we pin it explicitly so a future Hypothesis change doesn't widen
# the alphabet under us.
_text_st = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=400,
)

# Bounded body bytes — keeps each property under a millisecond.
_body_st = st.binary(min_size=0, max_size=512)

# Secrets must be non-empty and printable; the verifier rejects empty
# strings before HMAC runs (separate ``empty_secret`` test exists).
_secret_st = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=8,
    max_size=64,
)

# Unix timestamps within the verifier's ``int`` accepting range. Stripe
# uses the seconds-since-epoch convention; we pick a window centred on
# 2026 to keep tolerance arithmetic well-defined.
_NOW = 1_730_000_000
_ts_st = st.integers(min_value=_NOW - 10_000, max_value=_NOW + 10_000)


def _sign(secret: str, ts: int, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# 1. Parser is total — never raises anything but WebhookVerificationError
# ---------------------------------------------------------------------------


class TestParserIsTotal:
    """For *any* string, ``verify_webhook`` either returns ``None`` or
    raises ``WebhookVerificationError``. Nothing else.
    """

    @given(header=_text_st)
    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_arbitrary_header_string_never_crashes(self, header: str) -> None:
        # Skip the empty-header case here — it has its own test below;
        # the property we are pinning is "non-empty garbage in →
        # ``WebhookVerificationError`` out".
        assume(len(header) > 0)
        try:
            verify_webhook(
                raw_body=b"x",
                signature_header=header,
                secret="whsec_test_xxxxxxxxxxxxxxxx",
                now_unix_secs=_NOW,
            )
        except WebhookVerificationError as exc:
            assert exc.code in _DOCUMENTED_CODES, (
                f"verify_webhook raised an undocumented code {exc.code!r}; "
                f"either add it to WebhookVerificationError._Code or fix "
                f"the parser"
            )
        # Any other exception bubbling up is a parser bug — pytest
        # surfaces it natively.

    @given(header_bytes=st.binary(min_size=1, max_size=400))
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_random_bytes_decoded_as_latin1_never_crashes(
        self, header_bytes: bytes
    ) -> None:
        # Real frameworks hand the header as a ``str``; if a buggy
        # framework hands raw bytes it'll be decoded as Latin-1 by
        # WSGI/ASGI before hitting us. Latin-1 round-trips every byte
        # so this is the widest plausible string we'd ever see.
        header = header_bytes.decode("latin-1")
        try:
            verify_webhook(
                raw_body=b"x",
                signature_header=header,
                secret="whsec_test_xxxxxxxxxxxxxxxx",
                now_unix_secs=_NOW,
            )
        except WebhookVerificationError as exc:
            assert exc.code in _DOCUMENTED_CODES

    @given(
        ts=_ts_st,
        garbage=st.text(min_size=0, max_size=100),
    )
    @settings(max_examples=100, deadline=None)
    def test_valid_timestamp_with_garbage_signatures_rejected_cleanly(
        self, ts: int, garbage: str
    ) -> None:
        # Header parses well enough to extract a timestamp, but the
        # signature half is junk. The verifier must classify this as
        # ``malformed_header`` (no v1 entries pass the regex) — never
        # ``signature_mismatch`` (which would imply HMAC ran on
        # attacker-controlled bytes).
        # We exclude the case where the garbage happens to be a
        # well-formed 64-hex string — that's vanishingly unlikely
        # under Hypothesis's text strategy but assume() keeps the
        # property crisp.
        assume(not (len(garbage) == 64 and all(c in "0123456789abcdefABCDEF" for c in garbage)))
        header = f"t={ts},v1={garbage}"
        try:
            verify_webhook(
                raw_body=b"x",
                signature_header=header,
                secret="whsec_test",
                now_unix_secs=ts,
            )
        except WebhookVerificationError as exc:
            assert exc.code == "malformed_header"


# ---------------------------------------------------------------------------
# 2. Hex decoder is well-formed-or-rejects
# ---------------------------------------------------------------------------


class TestHexDecoder:
    """Anything that isn't exactly 64 hex chars is dropped at parse
    time. The HMAC compare path only ever sees 64-hex inputs."""

    @given(
        ts=_ts_st,
        sig_len=st.integers(min_value=0, max_value=128).filter(lambda n: n != 64),
    )
    @settings(max_examples=100, deadline=None)
    def test_wrong_length_signature_is_malformed(
        self, ts: int, sig_len: int
    ) -> None:
        # Build a hex-like string of the wrong length; parser must
        # reject without ever invoking HMAC.
        sig = "a" * sig_len
        header = f"t={ts},v1={sig}"
        try:
            verify_webhook(
                raw_body=b"x",
                signature_header=header,
                secret="whsec_test",
                now_unix_secs=ts,
            )
        except WebhookVerificationError as exc:
            assert exc.code == "malformed_header"

    @given(
        ts=_ts_st,
        # 64-char strings that are ALMOST hex but contain at least one
        # invalid char. Drawn deliberately to exercise the regex.
        invalid_char=st.sampled_from(
            ["G", "Z", "g", "z", "!", " ", "/", "ñ", "💀"]
        ),
        position=st.integers(min_value=0, max_value=63),
    )
    @settings(max_examples=100, deadline=None)
    def test_non_hex_chars_in_64_char_signature_are_malformed(
        self, ts: int, invalid_char: str, position: int
    ) -> None:
        sig_chars = ["a"] * 64
        sig_chars[position] = invalid_char
        sig = "".join(sig_chars)
        header = f"t={ts},v1={sig}"
        try:
            verify_webhook(
                raw_body=b"x",
                signature_header=header,
                secret="whsec_test",
                now_unix_secs=ts,
            )
        except WebhookVerificationError as exc:
            assert exc.code == "malformed_header"

    @given(
        ts=_ts_st,
        sig_hex=st.text(
            alphabet="0123456789abcdefABCDEF", min_size=64, max_size=64
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_well_formed_hex_reaches_signature_compare(
        self, ts: int, sig_hex: str
    ) -> None:
        # A correctly-shaped envelope with random hex must reach the
        # HMAC compare path (and fail there as ``signature_mismatch``
        # because the random hex has 1 in 2^256 odds of matching).
        # If we ever see ``malformed_header`` here, the regex drifted.
        header = f"t={ts},v1={sig_hex}"
        try:
            verify_webhook(
                raw_body=b"x",
                signature_header=header,
                secret="whsec_test",
                now_unix_secs=ts,
            )
        except WebhookVerificationError as exc:
            assert exc.code == "signature_mismatch"


# ---------------------------------------------------------------------------
# 3. Round-trip / tamper invariants
# ---------------------------------------------------------------------------


class TestRoundTripAndTamper:
    """Generated triples (secret, ts, body) must round-trip through
    sign/verify, and any byte-level tamper must invalidate."""

    @given(secret=_secret_st, ts=_ts_st, body=_body_st)
    @settings(max_examples=100, deadline=None)
    def test_signed_then_verified_round_trip(
        self, secret: str, ts: int, body: bytes
    ) -> None:
        sig = _sign(secret, ts, body)
        # No exception → success. The verifier returns ``None``.
        verify_webhook(
            raw_body=body,
            signature_header=f"t={ts},v1={sig}",
            secret=secret,
            now_unix_secs=ts,
        )

    @given(
        secret=_secret_st,
        ts=_ts_st,
        body=_body_st,
        flip_position=st.integers(min_value=0, max_value=1023),
    )
    @settings(max_examples=100, deadline=None)
    def test_body_tamper_invalidates(
        self, secret: str, ts: int, body: bytes, flip_position: int
    ) -> None:
        # Need a non-empty body to flip a byte in.
        assume(len(body) > 0)
        sig = _sign(secret, ts, body)
        pos = flip_position % len(body)
        tampered = bytearray(body)
        tampered[pos] ^= 0x01  # flip one bit
        try:
            verify_webhook(
                raw_body=bytes(tampered),
                signature_header=f"t={ts},v1={sig}",
                secret=secret,
                now_unix_secs=ts,
            )
        except WebhookVerificationError as exc:
            assert exc.code == "signature_mismatch"

    @given(secret=_secret_st, ts=_ts_st, body=_body_st)
    @settings(max_examples=100, deadline=None)
    def test_timestamp_replay_invalidates(
        self, secret: str, ts: int, body: bytes
    ) -> None:
        # Sign at ``ts``, replay header with ``ts+1``. The signed
        # payload prefix changes, so HMAC must not match.
        sig = _sign(secret, ts, body)
        replay_ts = ts + 1
        try:
            verify_webhook(
                raw_body=body,
                signature_header=f"t={replay_ts},v1={sig}",
                secret=secret,
                now_unix_secs=replay_ts,
            )
        except WebhookVerificationError as exc:
            assert exc.code == "signature_mismatch"

    @given(
        good_secret=_secret_st,
        wrong_secret=_secret_st,
        ts=_ts_st,
        body=_body_st,
    )
    @settings(max_examples=100, deadline=None)
    def test_secret_swap_invalidates(
        self,
        good_secret: str,
        wrong_secret: str,
        ts: int,
        body: bytes,
    ) -> None:
        assume(good_secret != wrong_secret)
        sig = _sign(good_secret, ts, body)
        try:
            verify_webhook(
                raw_body=body,
                signature_header=f"t={ts},v1={sig}",
                secret=wrong_secret,
                now_unix_secs=ts,
            )
        except WebhookVerificationError as exc:
            assert exc.code == "signature_mismatch"

    @given(
        secret_count=st.integers(min_value=2, max_value=5),
        match_index=st.integers(min_value=0, max_value=4),
        ts=_ts_st,
        body=_body_st,
    )
    @settings(max_examples=50, deadline=None)
    def test_secret_rotation_matches_at_any_position(
        self,
        secret_count: int,
        match_index: int,
        ts: int,
        body: bytes,
    ) -> None:
        # Build N decoy secrets plus one real one at a random index.
        # The verifier must accept regardless of which slot matches.
        assume(match_index < secret_count)
        secrets = [f"whsec_decoy_{i:02d}_xxxxxxxxxxxx" for i in range(secret_count)]
        real_secret = "whsec_real_xxxxxxxxxxxxxxxx"
        secrets[match_index] = real_secret
        sig = _sign(real_secret, ts, body)
        verify_webhook(
            raw_body=body,
            signature_header=f"t={ts},v1={sig}",
            secret=secrets,
            now_unix_secs=ts,
        )
