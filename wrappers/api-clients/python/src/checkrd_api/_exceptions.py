"""Per-status error classes.

Mirrors the OpenAI / Anthropic / Stripe Python SDK shape exactly so
users who already know those libraries don't have to relearn anything.
``APIError`` is the base; everything is catchable via that single
``except`` clause when callers don't care about the status code.

Reference shape: ``openai-python/src/openai/_exceptions.py``.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import httpx


class CheckrdError(Exception):
    """Base class for every error raised by this SDK.

    Catch this if you want to handle anything the SDK might raise —
    network failures, signature mismatches, HTTP status errors. For
    finer-grained handling, catch the more specific subclasses
    below.
    """


class APIError(CheckrdError):
    """Base for any error returned by the Checkrd API.

    Carries the underlying ``request`` + the ``body`` parsed out of
    the Stripe-style error envelope (``{"error": {"type", "code",
    "message", "param"}}``). Most callers should catch the
    per-status subclasses (:class:`AuthenticationError`,
    :class:`RateLimitError`, …) rather than this base.
    """

    request: httpx.Request
    body: Optional[Mapping[str, Any]]
    code: Optional[str]
    param: Optional[str]
    type: Optional[str]
    message: str

    def __init__(
        self,
        message: str,
        request: httpx.Request,
        *,
        body: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.request = request
        self.message = message
        self.body = body
        if isinstance(body, Mapping):
            inner = body.get("error") if isinstance(body.get("error"), Mapping) else None
            if isinstance(inner, Mapping):
                self.code = inner.get("code") if isinstance(inner.get("code"), str) else None
                self.param = inner.get("param") if isinstance(inner.get("param"), str) else None
                self.type = inner.get("type") if isinstance(inner.get("type"), str) else None
            else:
                self.code = None
                self.param = None
                self.type = None
        else:
            self.code = None
            self.param = None
            self.type = None


class APIConnectionError(APIError):
    """Network reached the local resolver but never made it to the
    Checkrd control plane (DNS failure, TCP reset, TLS handshake
    failure, etc.)."""


class APITimeoutError(APIConnectionError):
    """The request was started but did not complete before the
    configured timeout. Distinct from a 408/504 returned by the
    server."""


class APIStatusError(APIError):
    """Base for any non-2xx response. Subclasses below cover each
    documented status code; callers usually catch one of those, not
    this one directly.

    Always exposes:

    - :attr:`status_code` — int, the HTTP status code.
    - :attr:`response`    — the underlying :class:`httpx.Response`.
    - :attr:`request_id`  — value of the ``checkrd-request-id``
      response header, if present. Useful for support tickets.
    """

    response: httpx.Response
    status_code: int
    request_id: Optional[str]

    def __init__(
        self,
        message: str,
        *,
        response: httpx.Response,
        body: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message, response.request, body=body)
        self.response = response
        self.status_code = response.status_code
        self.request_id = response.headers.get("checkrd-request-id") or response.headers.get(
            "x-request-id"
        )


class BadRequestError(APIStatusError):
    """400 — the request was syntactically invalid or violated a
    documented validation rule. ``body['error']['param']`` points at
    the offending field when applicable."""

    status_code = 400


class AuthenticationError(APIStatusError):
    """401 — missing, malformed, or rejected credentials. Re-issue
    the API key or refresh the JWT and retry."""

    status_code = 401


class PermissionDeniedError(APIStatusError):
    """403 — credentials parsed but the caller's role does not
    permit the operation. Promote the user's role or use a
    differently-scoped API key."""

    status_code = 403


class NotFoundError(APIStatusError):
    """404 — the resource referenced by the URL does not exist (or
    is in a different workspace)."""

    status_code = 404


class ConflictError(APIStatusError):
    """409 — the operation conflicts with current state, typically
    because of a uniqueness constraint or a concurrent
    modification."""

    status_code = 409


class UnprocessableEntityError(APIStatusError):
    """422 — the request body parsed but failed semantic validation
    (e.g., a referenced agent_id does not exist in this org)."""

    status_code = 422


class RateLimitError(APIStatusError):
    """429 — exceeded the org's rate limit or monthly event quota.
    Inspect the ``Retry-After`` header for the recommended wait."""

    status_code = 429


class InternalServerError(APIStatusError):
    """5xx — Checkrd-side failure. Usually transient; the SDK
    retries automatically up to ``max_retries``."""


def make_status_error(
    response: httpx.Response,
    body: Optional[Mapping[str, Any]] = None,
) -> APIStatusError:
    """Pick the right subclass based on ``response.status_code``.

    Used internally by :class:`Checkrd._request`. Mirrors the dispatch
    table OpenAI's SDK uses; each branch returns the specific subclass
    so callers can ``except RateLimitError`` instead of inspecting a
    generic error.
    """
    message = _extract_message(body) or f"HTTP {response.status_code}"
    if response.status_code == 400:
        return BadRequestError(message, response=response, body=body)
    if response.status_code == 401:
        return AuthenticationError(message, response=response, body=body)
    if response.status_code == 403:
        return PermissionDeniedError(message, response=response, body=body)
    if response.status_code == 404:
        return NotFoundError(message, response=response, body=body)
    if response.status_code == 409:
        return ConflictError(message, response=response, body=body)
    if response.status_code == 422:
        return UnprocessableEntityError(message, response=response, body=body)
    if response.status_code == 429:
        return RateLimitError(message, response=response, body=body)
    if response.status_code >= 500:
        return InternalServerError(message, response=response, body=body)
    return APIStatusError(message, response=response, body=body)


def _extract_message(body: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Pull ``body['error']['message']`` out of the Stripe-style
    envelope. Returns ``None`` if the envelope is missing or
    malformed — the caller falls back to ``HTTP <code>``."""
    if not isinstance(body, Mapping):
        return None
    err = body.get("error")
    if isinstance(err, Mapping):
        msg = err.get("message")
        if isinstance(msg, str):
            return msg
    return None
