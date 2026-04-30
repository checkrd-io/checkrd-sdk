"""Webhook signature verification helpers.

Mirrors the Stripe / OpenAI / Anthropic pattern: an HMAC over
``{timestamp}.{raw_body}`` with a shared secret, delivered as a header
the server computes and the client verifies before trusting the
payload.

The helpers here are transport-agnostic. Feed them the raw body as
received by your framework (Flask ``request.get_data()``, FastAPI
``await request.body()``, Django ``request.body``). Never a
re-serialized JSON object — any whitespace change invalidates the
HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Literal, Optional, Union

from checkrd.exceptions import CheckrdError

#: Default clock-skew tolerance, in seconds. Matches Stripe's default.
DEFAULT_TOLERANCE_SECS = 300

_V1_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class WebhookVerificationError(CheckrdError):
    """Raised when webhook signature verification fails.

    The ``.code`` attribute distinguishes failure reasons so callers
    can react programmatically:

    - ``missing_header`` — the signature header was absent or empty.
    - ``malformed_header`` — the envelope couldn't be parsed.
    - ``timestamp_out_of_range`` — outside the caller's tolerance
      window.
    - ``signature_mismatch`` — HMAC did not match any candidate
      secret.
    - ``empty_secret`` — caller passed an empty secret.
    """

    _Code = Literal[
        "missing_header",
        "malformed_header",
        "timestamp_out_of_range",
        "signature_mismatch",
        "empty_secret",
    ]

    def __init__(self, message: str, code: _Code) -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True)
class _SignatureEnvelope:
    timestamp: int
    signatures: tuple[str, ...]


def verify_webhook(
    raw_body: Union[bytes, str],
    signature_header: Optional[str],
    secret: Union[str, list[str], tuple[str, ...]],
    *,
    tolerance_secs: int = DEFAULT_TOLERANCE_SECS,
    now_unix_secs: Optional[int] = None,
) -> None:
    """Verify a webhook signature. Returns silently on success.

    :param raw_body: The exact body bytes received on the wire. Must
        not be re-serialized from a parsed object.
    :param signature_header: Value of the ``Checkrd-Signature`` header
        (or equivalent). Format:
        ``t=<unix_seconds>,v1=<hex_sha256>[,v1=<hex_sha256>]``.
    :param secret: Shared HMAC-SHA256 secret. Accepts a list/tuple for
        the rotation window — any valid secret authenticates.
    :param tolerance_secs: Clock-skew tolerance. Default 300.
    :param now_unix_secs: Override for the time source (test-only).
    :raises WebhookVerificationError: on any failure reason.

    Example::

        from flask import request
        from checkrd.webhooks import verify_webhook, WebhookVerificationError

        @app.post("/checkrd-webhook")
        def webhook() -> tuple[str, int]:
            try:
                verify_webhook(
                    raw_body=request.get_data(),
                    signature_header=request.headers.get("Checkrd-Signature"),
                    secret=os.environ["CHECKRD_WEBHOOK_SECRET"],
                )
            except WebhookVerificationError:
                return "invalid signature", 400
            return "", 204
    """
    secrets = (secret,) if isinstance(secret, str) else tuple(secret)
    if not secrets or any(len(s) == 0 for s in secrets):
        raise WebhookVerificationError("webhook secret is empty", "empty_secret")
    if not signature_header:
        raise WebhookVerificationError("signature header missing", "missing_header")

    envelope = _parse_signature_header(signature_header)

    now = now_unix_secs if now_unix_secs is not None else int(time.time())
    if abs(now - envelope.timestamp) > tolerance_secs:
        raise WebhookVerificationError(
            "timestamp outside tolerance window", "timestamp_out_of_range"
        )

    body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    signed_payload = f"{envelope.timestamp}.".encode("utf-8") + body_bytes

    # Constant-time comparison via hmac.compare_digest — never use
    # plain equality on secret-bearing values.
    for provided in envelope.signatures:
        for secret_candidate in secrets:
            expected = hmac.new(
                secret_candidate.encode("utf-8"),
                signed_payload,
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(provided.lower(), expected):
                return
    raise WebhookVerificationError(
        "no candidate signature matched", "signature_mismatch"
    )


def _parse_signature_header(header: str) -> _SignatureEnvelope:
    """Parse ``t=<unix_seconds>,v1=<hex>[,v1=<hex>...]``.

    The ``v1`` scheme tag lets us add forward-compatible algorithms
    (e.g. ``v2=`` for Ed25519) without breaking existing verifiers.
    """
    timestamp: Optional[int] = None
    sigs: list[str] = []
    for part in (p.strip() for p in header.split(",") if p.strip()):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                continue
        elif key == "v1" and _V1_HEX_RE.match(value):
            sigs.append(value.lower())
    if timestamp is None or not sigs:
        raise WebhookVerificationError(
            "signature header is malformed", "malformed_header"
        )
    return _SignatureEnvelope(timestamp=timestamp, signatures=tuple(sigs))


__all__ = [
    "verify_webhook",
    "WebhookVerificationError",
    "DEFAULT_TOLERANCE_SECS",
]
