"""Checkrd exception hierarchy.

Modeled on the OpenAI / Anthropic / Stripe Python SDKs. Two families
share a single root so callers can write one ``except CheckrdError`` to
cover both::

    CheckrdError                            # root — every Checkrd error
    ├── CheckrdInitError                    # SDK init (WASM, policy load, key)
    ├── CheckrdPolicyDenied                 # WASM engine denied a request
    ├── PolicySignatureError                # DSSE bundle rejected
    └── APIError                            # control-plane HTTP error
        ├── APIStatusError                  # 4xx/5xx with a response body
        │   ├── BadRequestError             # 400
        │   ├── AuthenticationError         # 401
        │   ├── PermissionDeniedError       # 403
        │   ├── NotFoundError               # 404
        │   ├── ConflictError               # 409
        │   ├── UnprocessableEntityError    # 422
        │   ├── RateLimitError              # 429
        │   └── InternalServerError         # ≥ 500
        ├── APIConnectionError              # network failure (no response)
        │   └── APITimeoutError             # timeout (subclass of connection error)
        ├── APIResponseValidationError      # schema mismatch in 2xx body
        └── APIUserAbortError               # caller cancelled the request

Every error carries a stable ``.code`` string and a ``.docs_url``
property so downstream logs, dashboards, and pagers can route by code
without pattern-matching English text.

``APIStatusError`` exposes ``.response``, ``.status_code``, ``.headers``,
``.request_id`` for forensic logging. ``APIError`` exposes ``.request``
and ``.body``. Connection errors do not have a ``response`` because the
request never reached a server.

Use :func:`make_api_error` to dispatch a raw control-plane response to
the right subclass — that's the single place status-to-class mapping
lives, so callers never branch on an integer themselves.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


# Base URL for per-code error documentation. Matches Stripe's
# ``doc_url`` convention. Every exception below exposes a ``docs_url``
# property computed as ``f"{DOCS_BASE_URL}/{self.code}"``.
DOCS_BASE_URL = "https://checkrd.io/errors"


# ---------------------------------------------------------------------------
# Code-derivation helpers (kept for back-compat with existing call sites)
# ---------------------------------------------------------------------------


def _derive_deny_code(deny_reason: str) -> str:
    """Derive a stable error code from the WASM engine's deny reason."""
    if "kill switch" in deny_reason:
        return "kill_switch_active"
    if "rate limit" in deny_reason:
        return "rate_limit_exceeded"
    if "default policy" in deny_reason:
        return "default_policy_denied"
    return "policy_denied"


def _derive_init_code(message: str) -> str:
    """Derive a stable error code from a ``CheckrdInitError`` message."""
    lower = message.lower()
    if "wasm module not found" in lower or "not found" in lower and "wasm" in lower:
        return "wasm_not_found"
    if "failed to instantiate" in lower or "failed to load" in lower:
        return "wasm_load_failed"
    if "invalid policy" in lower or "policy" in lower and ("invalid" in lower or "malformed" in lower):
        return "invalid_policy"
    if "invalid key" in lower or "invalid utf-8" in lower:
        return "invalid_key"
    return "init_failed"


# ---------------------------------------------------------------------------
# FFI error code table (single source of truth, mirrored to JS SDK)
# ---------------------------------------------------------------------------

# Single source of truth for WASM FFI error codes.
#
# Mirrors the named constants in ``crates/core/src/interface.rs``
# (FFI_OK, FFI_PARSE_ERROR, FFI_INVALID_UTF8, FFI_INVALID_KEY,
# FFI_POLICY_*). The Python wrapper maps each code to a stable
# string label so logs, metrics, and exception messages never carry
# bare magic numbers.
#
# Codes -10..-14 cover the strong-from-the-ground-up tightening:
#   -10: PolicyBundle.schema_version mismatch
#   -11: bundle.version <= last_policy_version (rollback defense)
#   -12: bundle older than max_age_secs (replay defense)
#   -13: bundle.signed_at beyond clock-skew window (future-dated)
#   -14: set_initial_policy_version called when counter is non-zero
_FFI_ERROR_REASONS: dict[int, str] = {
    -1: "envelope_json_parse_error",
    -2: "invalid_utf8",
    -3: "trusted_keys_json_parse_error",
    -4: "payload_type_mismatch",
    -5: "signature_invalid",
    -6: "unknown_or_no_signer",
    -7: "key_not_in_validity_window",
    -8: "verified_payload_invalid",
    -9: "engine_not_initialized",
    -10: "schema_version_mismatch",
    -11: "bundle_version_not_monotonic",
    -12: "bundle_too_old",
    -13: "bundle_in_future",
    -14: "policy_version_already_set",
}

# Backwards-compatible alias for the existing PolicySignatureError API.
# There is no separate "policy signature reasons" table — the same FFI
# error space is used by every code path.
_POLICY_SIGNATURE_REASONS = _FFI_ERROR_REASONS


# ===========================================================================
# Base
# ===========================================================================


class CheckrdError(Exception):
    """Base class for every error raised by the Checkrd SDK.

    A single ``except CheckrdError`` block is enough to catch any error
    the SDK can raise — both SDK-local failures (WASM init, policy
    decisions, signature verification) and control-plane HTTP errors.

    Attributes:
        message: Human-readable message. Same as ``str(exc)``.
        code: Stable machine-readable code suitable for metrics / log
            parsing. Always populated; subclasses derive it from context
            when callers don't supply one.

    Properties:
        docs_url: Deep-link to the docs page for this code, of the form
            ``https://checkrd.io/errors/{code}``. Matches Stripe's
            ``doc_url`` convention so log lines, dashboards, and 4xx
            envelopes can include a one-click remediation link.
    """

    message: str
    code: str

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code if code is not None else self._default_code()

    def _default_code(self) -> str:
        """Subclass hook for deriving a default code when none is supplied."""
        return "checkrd_error"

    @property
    def docs_url(self) -> str:
        """Deep link to the error-code documentation page."""
        return f"{DOCS_BASE_URL}/{self.code}"


# ===========================================================================
# SDK-local errors
# ===========================================================================


class CheckrdInitError(CheckrdError):
    """Raised when the WASM engine fails to initialize.

    Causes include a missing ``.wasm`` binary, a corrupted binary that
    fails the integrity check, an invalid policy file, or an invalid
    identity key. The ``.code`` attribute distinguishes the cases:

    - ``wasm_not_found``     — ``.wasm`` binary missing.
    - ``wasm_load_failed``   — binary corrupted or platform unsupported.
    - ``invalid_policy``     — policy file malformed or rejected by engine.
    - ``invalid_key``        — identity key malformed.
    - ``init_failed``        — catch-all for everything else.
    """

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message, code=code or _derive_init_code(message))


class CheckrdPolicyDenied(CheckrdError):
    """Raised when the policy engine denies a request.

    Carries the full denial context so callers can build framework-aware
    responses (HTTP 403 envelope, MCP error message, agent retry logic).
    The class is intentionally rich — logging just the message loses the
    rule name, the dashboard link, and the actionable suggestion.

    Attributes:
        reason:        Human-readable deny reason from the WASM engine.
        request_id:    Correlation ID shared with control-plane telemetry.
        rule_name:     Name of the rule that fired (None for default-deny).
        url:           The request URL that was denied.
        dashboard_url: Deep link to the event in the Checkrd dashboard.
        suggestion:    Actionable remediation hint.
        code:          One of ``policy_denied``, ``rate_limit_exceeded``,
                       ``default_policy_denied``, ``kill_switch_active``.
    """

    reason: str
    request_id: str
    rule_name: Optional[str]
    url: Optional[str]
    dashboard_url: Optional[str]
    suggestion: Optional[str]

    def __init__(
        self,
        reason: str,
        request_id: str,
        *,
        code: Optional[str] = None,
        rule_name: Optional[str] = None,
        url: Optional[str] = None,
        dashboard_url: Optional[str] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        self.reason = reason
        self.request_id = request_id
        self.rule_name = rule_name
        self.url = url
        self.dashboard_url = dashboard_url
        self.suggestion = suggestion
        derived_code = code if code is not None else _derive_deny_code(reason)
        super().__init__(
            self._format_message(reason, request_id, url, suggestion, dashboard_url, derived_code),
            code=derived_code,
        )

    @staticmethod
    def _format_message(
        reason: str,
        request_id: str,
        url: Optional[str],
        suggestion: Optional[str],
        dashboard_url: Optional[str],
        code: str,
    ) -> str:
        lines = [f"Request {request_id} denied: {reason}"]
        if url:
            lines.append(f"  Request: {url}")
        if suggestion:
            lines.append(f"  Fix: {suggestion}")
        if dashboard_url:
            lines.append(f"  Dashboard: {dashboard_url}")
        lines.append(f"  Docs: {DOCS_BASE_URL}/{code}")
        return "\n".join(lines)


class PolicySignatureError(CheckrdError):
    """Raised when a signed policy bundle fails verification.

    The previous policy is left in place — the engine never silently
    installs an unverified policy.

    Attributes:
        ffi_code: The raw FFI error code from ``reload_policy_signed``
            (see ``_FFI_ERROR_REASONS``). Negative integer.
        reason:   Human-readable label for the FFI code, suitable for
            metrics and structured logging. Identical to ``.code``.
        code:     Inherited from :class:`CheckrdError`. Equals ``reason``
            so dashboards can group on the same field across error types.
    """

    ffi_code: int
    reason: str

    def __init__(self, ffi_code: int) -> None:
        self.ffi_code = ffi_code
        self.reason = _POLICY_SIGNATURE_REASONS.get(ffi_code, f"unknown_{ffi_code}")
        super().__init__(
            f"Signed policy bundle rejected (ffi_code={ffi_code}, reason={self.reason})",
            code=self.reason,
        )


# ===========================================================================
# Control-plane API errors
# ===========================================================================


class APIError(CheckrdError):
    """Base class for every control-plane HTTP error.

    Catch this to handle any failure from a Checkrd API call uniformly.
    The two main subtrees are :class:`APIStatusError` (the server
    answered with 4xx/5xx) and :class:`APIConnectionError` (the request
    never got a response).

    Attributes:
        request: The ``httpx.Request`` that failed (or ``None`` if not
            available). Useful for retries and forensic logging.
        body:    Parsed error response body, when one was returned.
            Typically a dict matching the Stripe-style envelope
            ``{"error": {"type", "code", "message", "param"}}``.
    """

    request: Any
    body: Any

    def __init__(
        self,
        message: str,
        request: Any = None,
        *,
        body: Any = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message, code=code)
        self.request = request
        self.body = body


class APIStatusError(APIError):
    """Control-plane responded with a 4xx or 5xx status.

    Most callers should catch a more specific subclass
    (:class:`RateLimitError`, :class:`AuthenticationError`, etc.); this
    is the supertype for "any HTTP-level failure" branches.

    Attributes:
        response:    The ``httpx.Response`` object.
        status_code: HTTP status code (e.g. 429).
        headers:     Lower-cased header dict for forensic logging.
        request_id:  Server-generated request ID, from
            ``Checkrd-Request-Id`` / ``X-Request-Id``. Quote this in
            support tickets.
    """

    response: Any
    status_code: int
    headers: Mapping[str, str]
    request_id: Optional[str]

    def __init__(
        self,
        message: str,
        *,
        response: Any,
        body: Any = None,
        code: Optional[str] = None,
    ) -> None:
        # Populate response-derived fields BEFORE calling super().__init__
        # so the base class's ``_default_code()`` can read ``status_code``
        # when the caller didn't supply an explicit code.
        self.response = response
        self.status_code = int(getattr(response, "status_code", 0))
        try:
            raw_headers = response.headers
            self.headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        except Exception:
            self.headers = {}
        self.request_id = (
            self.headers.get("checkrd-request-id")
            or self.headers.get("x-request-id")
        )
        request = getattr(response, "request", None)
        super().__init__(message, request=request, body=body, code=code)

    def _default_code(self) -> str:
        # Fallback when the body has no ``error.code``: include the status
        # so dashboards can still differentiate, but in a stable shape.
        status = getattr(self, "status_code", 0)
        if status:
            return f"http_{status}"
        return "api_status_error"


class APIConnectionError(APIError):
    """Network-level failure — DNS, TCP, TLS — before getting a response.

    These are typically transient. The retry loop in the SDK handles
    them automatically; callers see this only when retries are
    exhausted or disabled.
    """

    def __init__(
        self,
        *,
        message: str = "Connection error.",
        request: Any = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message, request=request, code=code or "api_connection_error")


class APITimeoutError(APIConnectionError):
    """Request timed out before a response was received.

    Subclass of :class:`APIConnectionError` because there is no response
    body — distinguishing timeout from generic connection error lets
    callers apply different retry policies (timeouts often warrant a
    longer backoff than DNS failures).
    """

    def __init__(self, *, request: Any = None) -> None:
        super().__init__(
            message="Request timed out.",
            request=request,
            code="api_timeout",
        )


class APIResponseValidationError(APIError):
    """Server returned 2xx but the body did not match the expected schema.

    Symptom of a control-plane / SDK version skew. Callers may want to
    log and continue rather than crash — the request itself succeeded.
    """

    def __init__(
        self,
        message: str = "Data returned by API invalid for expected schema.",
        *,
        request: Any = None,
        body: Any = None,
    ) -> None:
        super().__init__(
            message, request=request, body=body, code="response_validation_error"
        )


class APIUserAbortError(APIError):
    """Request was cancelled by the caller.

    Distinct from :class:`APITimeoutError` because the cause is
    user-initiated — do not retry. Mirrors OpenAI's ``APIUserAbortError``.
    """

    def __init__(self, *, request: Any = None) -> None:
        super().__init__("Request was aborted.", request=request, code="user_abort")


# ---------------------------------------------------------------------------
# Status-code subclasses
# ---------------------------------------------------------------------------


class BadRequestError(APIStatusError):
    """400 Bad Request — the SDK sent a malformed request."""


class AuthenticationError(APIStatusError):
    """401 Unauthorized — invalid or missing API key."""


class PermissionDeniedError(APIStatusError):
    """403 Forbidden — API key lacks permission for the resource."""


class NotFoundError(APIStatusError):
    """404 Not Found — typically an unknown agent / org / resource id."""


class ConflictError(APIStatusError):
    """409 Conflict — Idempotency-Key reuse, duplicate resource."""


class UnprocessableEntityError(APIStatusError):
    """422 Unprocessable Entity — schema validation failed."""


class RateLimitError(APIStatusError):
    """429 Too Many Requests.

    Inspect ``self.headers["retry-after"]`` /
    ``self.headers["retry-after-ms"]`` to decide how long to back off.
    The SDK retry loop already honors these — callers see this only
    when retries are exhausted.
    """


class InternalServerError(APIStatusError):
    """5xx — control-plane internal error. Generally transient."""


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def make_api_error(*, response: Any, body: Any = None) -> APIError:
    """Map an HTTP response to the most specific :class:`APIError` subclass.

    Mirrors OpenAI's ``APIError._make_status_error`` dispatch table.
    Callers issuing control-plane requests should funnel every non-2xx
    response through this single function so the status-to-class
    mapping has exactly one home.

    Args:
        response: An ``httpx.Response`` (or anything quack-typed with
            ``.status_code``, ``.request``, ``.headers``).
        body:     Pre-parsed response body (dict). When omitted, callers
            should at minimum pass the raw text so log messages aren't
            empty; subclasses don't try to re-parse.

    Returns:
        The narrowest applicable :class:`APIError` subclass instance.
    """
    status = int(getattr(response, "status_code", 0))
    message = _extract_message(body) or f"HTTP {status} error"
    # If the server's error envelope includes a stable code, prefer it
    # over the SDK's status-based fallback. Lets dashboards group on
    # ``error.code`` (e.g. ``cannot_delete_last_org``) instead of the
    # less-specific ``http_409``.
    code = _extract_code(body)
    if status == 400:
        return BadRequestError(message, response=response, body=body, code=code)
    if status == 401:
        return AuthenticationError(message, response=response, body=body, code=code)
    if status == 403:
        return PermissionDeniedError(message, response=response, body=body, code=code)
    if status == 404:
        return NotFoundError(message, response=response, body=body, code=code)
    if status == 409:
        return ConflictError(message, response=response, body=body, code=code)
    if status == 422:
        return UnprocessableEntityError(message, response=response, body=body, code=code)
    if status == 429:
        return RateLimitError(message, response=response, body=body, code=code)
    if status >= 500:
        return InternalServerError(message, response=response, body=body, code=code)
    return APIStatusError(message, response=response, body=body, code=code)


def _extract_code(body: Any) -> Optional[str]:
    """Pull the stable error code from a Stripe-style envelope.

    Returns ``error.code`` if present (the value control planes use for
    dashboard grouping and runbook routing) or ``None`` so the caller
    falls through to the status-derived default.
    """
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        c = err.get("code")
        if isinstance(c, str) and c:
            return c
    c = body.get("code")
    if isinstance(c, str) and c:
        return c
    return None


def _extract_message(body: Any) -> Optional[str]:
    """Pull a human-readable message from a Stripe-style error envelope.

    Handles two shapes:

    - ``{"error": {"message": "..."}}`` — Stripe's nested envelope, also
      what ``crates/api/src/errors.rs`` emits.
    - ``{"message": "..."}`` — plain top-level message.

    Returns ``None`` if neither shape applies, leaving the caller to
    pick a sensible default.
    """
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            return msg
    msg = body.get("message")
    if isinstance(msg, str):
        return msg
    return None


__all__ = [
    "DOCS_BASE_URL",
    # Base
    "CheckrdError",
    # SDK-local
    "CheckrdInitError",
    "CheckrdPolicyDenied",
    "PolicySignatureError",
    # API errors
    "APIError",
    "APIStatusError",
    "APIConnectionError",
    "APITimeoutError",
    "APIResponseValidationError",
    "APIUserAbortError",
    # Status subclasses
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    # Dispatch
    "make_api_error",
]
