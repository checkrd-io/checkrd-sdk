"""Per-status error classes.

Mirrors the OpenAI / Anthropic / Stripe Python SDK shape exactly so
users who already know those libraries don't have to relearn anything.
``APIError`` is the base; everything is catchable via that single
``except`` clause when callers don't care about the status code.

The control plane speaks RFC 9457 ``application/problem+json`` (ADR-009v2,
superseding the former Stripe-style ``{"error":{…}}`` envelope). The body
is *flat*::

    {
        "type": "https://checkrd.io/errors/<code>",
        "title": "Invalid API key",
        "status": 401,
        "detail": "...",            # occurrence-specific, optional
        "instance": "/v1/agents",   # optional
        "code": "<machine_code>",   # extension member — clients branch on this
        "errors": [                 # extension member — per-field failures
            {"pointer": "/email", "detail": "must be a valid email address"}
        ],
        "request_id": "..."         # extension member, optional
    }

The standard RFC 9457 members are ``type``/``title``/``status``/
``detail``/``instance``; ``code``/``errors``/``request_id`` are extension
members (RFC 9457 §3.2). This SDK reads the flat top-level fields
directly — there is no nested ``error`` object.

Reference shape: ``openai-python/src/openai/_exceptions.py``.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Optional


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
    the RFC 9457 problem document. The standard problem members and the
    house extension members are lifted onto attributes for ergonomic
    access:

    - :attr:`code`       — stable, machine-readable code (top-level
      ``code``). Branch on this, not on English text.
    - :attr:`type`       — the dereferenceable problem-type URI.
    - :attr:`errors`     — list of per-field failures, each a mapping
      ``{"pointer": "/email", "detail": "..."}`` (RFC 6901 pointers).
      Empty list when the server reported no field-level detail.
    - :attr:`param`      — convenience accessor for the first
      ``errors[].pointer`` (or ``None``). Lets callers that only care
      about the single offending field skip indexing :attr:`errors`.
    - :attr:`request_id` — server correlation id from the body (the
      header value is preferred on :class:`APIStatusError`).

    Most callers should catch the per-status subclasses
    (:class:`AuthenticationError`, :class:`RateLimitError`, …) rather
    than this base.
    """

    request: Any
    body: Optional[Mapping[str, Any]]
    code: Optional[str]
    type: Optional[str]
    errors: List[Mapping[str, Any]]
    param: Optional[str]
    request_id: Optional[str]
    message: str

    def __init__(
        self,
        message: str,
        request: Any,
        *,
        body: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.request = request
        self.message = message
        self.body = body
        self.code = _extract_code(body)
        self.type = _extract_str(body, "type")
        self.errors = _extract_errors(body)
        # ``param`` is the first field pointer, when the server surfaced
        # per-field detail. Replaces the old Stripe ``error.param`` field.
        self.param = self.errors[0].get("pointer") if self.errors else None
        if not isinstance(self.param, str):
            self.param = None
        self.request_id = _extract_str(body, "request_id")


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
      response header, if present, falling back to the ``request_id``
      carried in the problem body. Useful for support tickets.
    """

    response: Any
    status_code: int

    def __init__(
        self,
        message: str,
        *,
        response: Any,
        body: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message, getattr(response, "request", None), body=body)
        self.response = response
        self.status_code = int(getattr(response, "status_code", 0))
        # Prefer the response header (set even when the body is non-JSON,
        # e.g. an LB 502); fall back to the request_id inside the problem
        # body that the base class already parsed.
        header_id = None
        try:
            header_id = response.headers.get("checkrd-request-id") or response.headers.get(
                "x-request-id"
            )
        except Exception:
            header_id = None
        if header_id is not None:
            self.request_id = header_id


class BadRequestError(APIStatusError):
    """400 — the request was syntactically invalid or violated a
    documented validation rule. The first :attr:`param` (or the full
    :attr:`errors` list) points at the offending field when
    applicable."""

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
    (e.g., a referenced agent_id does not exist in this org). The
    per-field reasons are in :attr:`errors`."""

    status_code = 422


class RateLimitError(APIStatusError):
    """429 — exceeded the org's rate limit or monthly event quota.

    The SDK retry loop honors the ``Retry-After`` response header
    automatically; callers see this only when retries are exhausted.
    The parsed value (seconds to wait) is surfaced on
    :attr:`retry_after` when the header was present and parseable, so
    callers can drive their own backoff.
    """

    status_code = 429

    retry_after: Optional[float]

    def __init__(
        self,
        message: str,
        *,
        response: Any,
        body: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message, response=response, body=body)
        self.retry_after = parse_retry_after(response)


class InternalServerError(APIStatusError):
    """5xx — Checkrd-side failure. Usually transient; the SDK
    retries automatically up to ``max_retries``."""


def make_status_error(
    response: Any,
    body: Optional[Mapping[str, Any]] = None,
) -> APIStatusError:
    """Pick the right subclass based on ``response.status_code``.

    Used internally by :class:`Checkrd._request`. Mirrors the dispatch
    table OpenAI's SDK uses; each branch returns the specific subclass
    so callers can ``except RateLimitError`` instead of inspecting a
    generic error.

    Degrades gracefully when the body is non-JSON or missing a ``code``
    (e.g. a load-balancer 502 with an HTML/empty body): the message
    falls back to ``HTTP <status>`` and ``.code``/``.errors`` are simply
    empty — building the exception never raises.
    """
    status = int(getattr(response, "status_code", 0))
    message = _extract_message(body) or f"HTTP {status}"
    if status == 400:
        return BadRequestError(message, response=response, body=body)
    if status == 401:
        return AuthenticationError(message, response=response, body=body)
    if status == 403:
        return PermissionDeniedError(message, response=response, body=body)
    if status == 404:
        return NotFoundError(message, response=response, body=body)
    if status == 409:
        return ConflictError(message, response=response, body=body)
    if status == 422:
        return UnprocessableEntityError(message, response=response, body=body)
    if status == 429:
        return RateLimitError(message, response=response, body=body)
    if status >= 500:
        return InternalServerError(message, response=response, body=body)
    return APIStatusError(message, response=response, body=body)


def _extract_message(body: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Pull a human-readable message from an RFC 9457 problem body.

    Prefers the occurrence-specific top-level ``detail``, falling back
    to the problem-type ``title``. Returns ``None`` if neither is a
    non-empty string (the caller then falls back to ``HTTP <status>``).
    """
    if not isinstance(body, Mapping):
        return None
    for key in ("detail", "title"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_code(body: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Pull the stable top-level ``code`` from an RFC 9457 problem body.

    Returns ``None`` when the body is non-JSON or lacks a string ``code``
    (e.g. an LB 502), leaving callers to fall back to status-based
    handling.
    """
    if not isinstance(body, Mapping):
        return None
    code = body.get("code")
    if isinstance(code, str) and code:
        return code
    return None


def _extract_str(body: Optional[Mapping[str, Any]], key: str) -> Optional[str]:
    """Read a top-level string member from the problem body, or ``None``."""
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _extract_errors(body: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Read the ``errors`` array (per-field failures) from the problem body.

    Each element is a mapping with ``pointer`` (RFC 6901) and ``detail``.
    Non-mapping or malformed entries are dropped; a missing/empty/invalid
    ``errors`` member yields an empty list.
    """
    if not isinstance(body, Mapping):
        return []
    raw = body.get("errors")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def parse_retry_after(response: Any) -> Optional[float]:
    """Parse the ``Retry-After`` response header into seconds-to-wait.

    Per RFC 9110 §10.2.3 the value is either a non-negative integer count
    of seconds or an HTTP-date. Returns the number of seconds to wait, or
    ``None`` when the header is absent or unparseable. Never raises.
    """
    try:
        headers = response.headers
    except Exception:
        return None
    raw = None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    # Integer seconds form.
    try:
        seconds = float(raw)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    # HTTP-date form (RFC 9110); compute the delta from now.
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(tz=when.tzinfo) if when.tzinfo else _dt.datetime.now()
    delta = (when - now).total_seconds()
    return delta if delta >= 0 else 0.0


__all__ = [
    # Base
    "CheckrdError",
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    # Status subclasses
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    # Dispatch + helpers
    "make_status_error",
    "parse_retry_after",
]
