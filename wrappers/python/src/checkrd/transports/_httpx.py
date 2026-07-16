"""httpx transport wrappers for Checkrd policy evaluation."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from checkrd._genai import attributes_for_url, detect_operation, detect_provider
from checkrd._genai_body import (
    extract_request_attrs,
    extract_response_attrs,
    settle_cost_into,
)
from checkrd._settings import DEFAULT_SECURITY_MODE, SecurityMode
from checkrd._version import __version__
from checkrd.engine import EvalResult, WasmEngine
from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.transports._stream_tap import STREAM_CAPTURE_PROVIDERS, install_stream_tap

logger = logging.getLogger("checkrd")

_USER_AGENT_SUFFIX = f"Checkrd-Python/{__version__}"

# Bodies larger than this are not passed to the WASM policy engine for inspection.
# Passing them would (a) risk memory pressure inside wasmtime, and (b) require
# buffering the entire request body in Python memory before the HTTP call.
#
# Behavior when a request exceeds this limit:
#
#   security_mode='strict'      — the request is DENIED with deny_reason
#                                 'body exceeds 1MB inspection limit'.
#                                 Rationale: a silent skip would let an attacker
#                                 pad a payload with 1KB of filler to evade
#                                 body-matcher policies (prompt-injection,
#                                 PII-exfiltration, etc.). Policy-as-defense
#                                 must fail closed, not fail silent.
#
#   security_mode='permissive'  — the request proceeds, body matchers do NOT
#                                 apply, and a WARNING is logged with the
#                                 request_id so the operator can triage.
#                                 This matches pre-1.0 behavior for backwards
#                                 compatibility during rollout.
MAX_BODY_SIZE = 1_048_576  # 1 MB

# Sentinel surfaced in telemetry + CheckrdPolicyDenied when a strict-mode
# request is blocked because the body is too large to inspect.
_OVERSIZE_BODY_DENY_REASON = "body exceeds 1MB inspection limit"

# Headers that MUST NOT be forwarded to hook callbacks. These contain third-party
# credentials (AI provider API keys, session tokens) that user-provided hooks should
# never see. A carelessly-written hook that logs the event would leak every customer's
# API keys. The WASM engine still receives all headers (sandboxed, no I/O) for policy
# matching — only hooks are sanitized.
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-checkrd-api-key",
    }
)


def _sanitize_headers_for_hooks(
    headers: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Strip credential-bearing headers before passing to user hook callbacks."""
    return [(k, v) for k, v in headers if k.lower() not in _SENSITIVE_HEADER_NAMES]


def _parse_traceparent(
    headers: list[tuple[str, str]],
) -> tuple[str | None, str | None]:
    """Extract trace_id and parent_span_id from W3C traceparent header.

    Format: {version}-{trace_id}-{parent_id}-{flags}
    Returns (trace_id, parent_span_id), or (None, None) if absent/invalid.
    """
    for name, value in headers:
        if name.lower() == "traceparent":
            parts = value.strip().split("-")
            if (
                len(parts) == 4
                and len(parts[1]) == 32
                and len(parts[2]) == 16
                and all(c in "0123456789abcdef" for c in parts[1])
                and all(c in "0123456789abcdef" for c in parts[2])
            ):
                return parts[1], parts[2]
    return None, None


def _check_oversized_body(
    request: httpx.Request,
    security_mode: SecurityMode,
    enforce: bool,
    agent_id: str,
    dashboard_url: str,
    batcher: Optional[Any],
    on_deny: Optional[Any],
) -> Optional[CheckrdPolicyDenied]:
    """Short-circuit requests whose body exceeds the WASM inspection limit.

    Returns a :class:`CheckrdPolicyDenied` to raise when ``security_mode`` is
    ``strict`` and enforcement is on; ``None`` otherwise. In permissive mode
    (or dry-run), logs a warning and returns ``None`` so the caller proceeds
    with ``body=None`` (current behavior).

    Why this exists: silently dropping body inspection for large payloads is
    the inverse of the policy engine's purpose. An attacker padding a prompt
    with 1 KB of filler could bypass body matchers entirely. Fail-closed.
    """
    content = request.content
    if not content or len(content) <= MAX_BODY_SIZE:
        return None

    size_kb = len(content) // 1024
    request_id = str(uuid.uuid4())

    if security_mode == "strict" and enforce:
        logger.warning(
            "checkrd: %s blocked — body size %d KB exceeds inspection limit "
            "(security_mode='strict'). Request denied to prevent body-matcher "
            "bypass. Lower the payload size or set security_mode='permissive' "
            "to pass through unchecked during rollout.",
            request_id,
            size_kb,
        )
        # Emit a synthetic telemetry event so the block is observable in
        # the dashboard just like a policy-rule deny.
        _emit_oversize_telemetry(
            request,
            request_id,
            batcher,
            size_kb,
        )
        if on_deny is not None:
            try:
                on_deny(_make_oversize_deny_event(request, request_id, size_kb))
            except Exception:
                logger.warning("checkrd: on_deny hook raised", exc_info=True)
        dash_url = _build_dashboard_url(dashboard_url, agent_id, request_id)
        return CheckrdPolicyDenied(
            reason=_OVERSIZE_BODY_DENY_REASON,
            request_id=request_id,
            rule_name=None,
            url=str(request.url),
            dashboard_url=dash_url,
            suggestion=(
                "Request bodies over 1 MB are not inspected by the policy "
                "engine. Lower the payload size, split the call, or set "
                "security_mode='permissive' if you accept pass-through."
            ),
        )

    logger.warning(
        "checkrd: request body size %d KB exceeds inspection limit — "
        "body matchers will NOT be applied (security_mode='permissive' or "
        "enforce=False). This is insecure for payload-sensitive rules.",
        size_kb,
    )
    return None


def _emit_oversize_telemetry(
    request: httpx.Request,
    request_id: str,
    batcher: Optional[Any],
    size_kb: int,
) -> None:
    """Emit a synthetic denied telemetry event for an oversize-body block."""
    if batcher is None:
        return
    now = datetime.now(timezone.utc)
    event: dict[str, Any] = {
        "request_id": request_id,
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url_host": request.url.host or "",
        "url_path": request.url.path or "",
        "method": request.method,
        "status_code": None,
        "latency_ms": None,
        "policy_result": "denied",
        "deny_reason": f"{_OVERSIZE_BODY_DENY_REASON} ({size_kb} KB)",
        "trace_id": None,
        "span_id": None,
        "parent_span_id": None,
        "span_name": f"{request.method} {request.url.host or ''}",
        "span_kind": "INTERNAL",
        "span_status_code": "UNSET",
        "span_status_message": _OVERSIZE_BODY_DENY_REASON,
    }
    try:
        batcher.enqueue(event)
    except Exception:
        logger.debug(
            "checkrd: oversize telemetry emit failed",
            exc_info=True,
        )


def _make_oversize_deny_event(
    request: httpx.Request,
    request_id: str,
    size_kb: int,
) -> dict[str, Any]:
    """Construct the hook event for an oversize-body block."""
    headers = [(k, v) for k, v in request.headers.items()]
    return {
        "type": "deny",
        "request_id": request_id,
        "method": request.method,
        "url": str(request.url),
        "headers": _sanitize_headers_for_hooks(headers),
        "policy_result": "denied",
        "deny_reason": f"{_OVERSIZE_BODY_DENY_REASON} ({size_kb} KB)",
        "rule_name": None,
        "dashboard_url": None,
        "suggestion": None,
    }


def _build_eval_kwargs(request: httpx.Request) -> dict[str, Any]:
    """Extract evaluation parameters from an httpx Request."""
    now = datetime.now(timezone.utc)
    if request.content and len(request.content) <= MAX_BODY_SIZE:
        try:
            body = request.content.decode("utf-8")
        except UnicodeDecodeError:
            body = ""  # Body exists but is not valid UTF-8; signals unparseable to engine
    else:
        body = None
    headers = [(k, v) for k, v in request.headers.items()]

    # OTEL trace context: extract from W3C traceparent or generate fresh
    extracted_trace_id, parent_span_id = _parse_traceparent(headers)
    trace_id = extracted_trace_id or uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]

    return {
        "request_id": str(uuid.uuid4()),
        "method": request.method,
        "url": str(request.url),
        "headers": headers,
        "body": body,
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp_ms": int(now.timestamp() * 1000),
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
    }


def _compute_span_status(
    allowed: bool,
    deny_reason: Optional[str],
    status_code: Optional[int],
) -> tuple[str, Optional[str]]:
    """Derive OTEL span status from policy result and HTTP response.

    Returns (status_code, status_message) following the OTEL StatusCode spec:
      OK    — request allowed, upstream returned 2xx-3xx
      ERROR — request allowed, upstream returned 5xx
      UNSET — request denied (policy working as designed), or 4xx, or no response
    """
    if not allowed:
        return ("UNSET", deny_reason)
    if status_code is None:
        return ("UNSET", None)
    if 200 <= status_code < 400:
        return ("OK", None)
    if status_code >= 500:
        return ("ERROR", f"upstream server error ({status_code})")
    return ("UNSET", None)


def _enrich_telemetry(
    result: EvalResult,
    status_code: Optional[int] = None,
    latency_ms: Optional[int] = None,
    *,
    engine: Optional[WasmEngine] = None,
    cost_metering: bool = False,
    extra_attrs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Parse the WASM telemetry JSON and enrich with response data + span status.

    The WASM core creates partial telemetry (no response, span_status_code=UNSET).
    This function completes the event with the actual HTTP response, derives
    the final OTEL span status for export to Datadog/Grafana/Honeycomb, and
    stamps the OpenTelemetry GenAI semantic-convention attributes
    (``gen_ai.provider.name``, ``gen_ai.operation.name``) when the request
    URL matches a known LLM endpoint. Stamping happens here (not in each
    vendor instrumentor) so the same enrichment fires whether the user
    wired the SDK via ``checkrd.instrument_openai()``,
    ``Checkrd().wrap(httpx.Client())``, or a hand-rolled httpx transport
    pointed at AWS Bedrock — every code path goes through this function.

    Cost metering (M-12): when ``cost_metering`` is on AND a signed pricing
    bundle is installed in ``engine``, the call's usage is settled against the
    in-WASM price table and the ``cost_usd_micros`` / ``currency`` /
    ``pricing_bundle_version`` / ``pricing_status`` fields are stamped onto the
    event. When off, or no bundle is installed, those fields are left unset.
    """
    import json

    from checkrd._genai import attributes_for_url

    telemetry: dict[str, Any] = json.loads(result.telemetry_json)
    if status_code is not None:
        telemetry["response"] = {"status_code": status_code, "latency_ms": latency_ms}
    code, message = _compute_span_status(result.allowed, result.deny_reason, status_code)
    telemetry["span_status_code"] = code
    telemetry["span_status_message"] = message

    # OTel GenAI semantic conventions. ``request`` is set by the WASM core
    # for every evaluated request — read host + path from there rather than
    # re-parsing the URL on the hot path.
    request: Optional[dict[str, Any]] = telemetry.get("request")
    if isinstance(request, dict):
        url_host = request.get("url_host", "") or ""
        url_path = request.get("url_path", "") or ""
        for attr_name, attr_value in attributes_for_url(url_host, url_path).items():
            telemetry[attr_name] = attr_value

    # Body-derived GenAI attributes (P1-15): the OTel ``gen_ai.usage.*`` token
    # counts + model the transport extracted from the (non-streaming) response
    # body, opt-in via ``extract_genai_body_attrs``. Merged BEFORE settle so the
    # cost metering below has real usage to price. Empty / absent when the opt-in
    # is off or the response carried no usage. (Streaming responses do NOT land
    # here — their usage arrives out-of-band and is settled by the stream tap.)
    if extra_attrs:
        telemetry.update(extra_attrs)

    # Cost metering (M-12): extract → settle → stamp cost fields. Inert unless
    # the feature is on AND a price table is installed. The settle helper lives
    # in ``checkrd._genai_body`` (no httpx dependency) so the framework adapters
    # — which bypass this transport — can reach the same code path. It runs
    # synchronously here on the request thread, sharing the engine with the
    # ``evaluate()`` call above (the wasmtime Store is not thread-safe).
    if cost_metering and engine is not None:
        settle_cost_into(telemetry, result.request_id, engine)

    return telemetry


def _log_telemetry(
    result: EvalResult,
    status_code: Optional[int] = None,
    latency_ms: Optional[int] = None,
    batcher: Optional[Any] = None,
    *,
    engine: Optional[WasmEngine] = None,
    cost_metering: bool = False,
    extra_attrs: Optional[dict[str, Any]] = None,
) -> None:
    """Log telemetry event and optionally enqueue for control plane delivery."""
    telemetry = _enrich_telemetry(
        result,
        status_code,
        latency_ms,
        engine=engine,
        cost_metering=cost_metering,
        extra_attrs=extra_attrs,
    )
    if result.allowed:
        logger.info(
            "checkrd: %s allowed (status=%s, latency=%sms)",
            result.request_id,
            status_code,
            latency_ms,
            extra={"checkrd_telemetry": telemetry},
        )
    else:
        logger.warning(
            "checkrd: %s denied: %s",
            result.request_id,
            result.deny_reason,
            extra={"checkrd_telemetry": telemetry},
        )

    if batcher is not None:
        batcher.enqueue(telemetry)


def _parse_rule_name(deny_reason: str) -> Optional[str]:
    """Extract the rule name from a WASM deny reason string."""
    if deny_reason.startswith("denied by rule '") and deny_reason.endswith("'"):
        return deny_reason[len("denied by rule '") : -1]
    if deny_reason.startswith("rate limit '") and "' exceeded" in deny_reason:
        return deny_reason[len("rate limit '") : deny_reason.index("' exceeded")]
    return None


def _build_suggestion(deny_reason: str, rule_name: Optional[str]) -> str:
    """Generate actionable guidance based on the deny category."""
    if "kill switch" in deny_reason:
        return (
            "The kill switch is active. Deactivate it via the dashboard "
            "or by removing the kill switch file."
        )
    if "rate limit" in deny_reason:
        if rule_name:
            return (
                f"Rate limit '{rule_name}' exceeded. Increase the limit in "
                "your policy or add request batching."
            )
        return "Rate limit exceeded. Increase the limit in your policy."
    if "default policy" in deny_reason:
        return (
            "No allow rule matched this request. Add an explicit allow "
            "rule for this URL pattern in your policy."
        )
    if rule_name:
        return (
            f"Blocked by rule '{rule_name}'. Edit the rule in your policy "
            "file or dashboard to allow this request."
        )
    return "Request denied by policy."


def _build_dashboard_url(
    dashboard_base: str,
    agent_id: str,
    request_id: str,
) -> Optional[str]:
    """Build a deep link to the denied event in the Checkrd dashboard."""
    if not dashboard_base or not agent_id:
        return None
    base = dashboard_base.rstrip("/")
    return f"{base}/agents/{agent_id}/events/{request_id}"


def _append_user_agent(request: httpx.Request) -> None:
    """Append SDK identifier to User-Agent header for incident tracing."""
    existing = request.headers.get("user-agent", "")
    if _USER_AGENT_SUFFIX not in existing:
        separator = " " if existing else ""
        request.headers["user-agent"] = f"{existing}{separator}{_USER_AGENT_SUFFIX}"


def _log_eval_debug(method: str, url: str, result: Any, eval_us: float) -> None:
    """Log a per-request evaluation trace at DEBUG level."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    verdict = "ALLOWED" if result.allowed else "DENIED"
    reason = f"\n  reason: {result.deny_reason}" if result.deny_reason else ""
    logger.debug(
        "checkrd: eval %s %s\n  verdict: %s in %.0fus%s",
        method,
        url,
        verdict,
        eval_us,
        reason,
    )


def _record_last_eval() -> None:
    """Record the timestamp of the last evaluation for health checks."""
    from checkrd._state import set_last_eval_at

    set_last_eval_at(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _handle_engine_error(
    request: httpx.Request,
    exc: BaseException,
    security_mode: SecurityMode,
) -> None:
    """Resolve a policy-engine runtime failure per the caller's ``security_mode``.

    ``engine.evaluate()`` runs a wasmtime instance on the hot path. A trap,
    an OOM inside the sandbox, or any other internal fault raises here — and
    that error must never be allowed to break the user's real API call in
    ``permissive`` mode, nor to silently let the call through in ``strict``.

    - ``permissive`` — fail **OPEN**. Log and return so the caller passes the
      original request through unmodified (the pre-1.0 pass-through contract:
      a broken security layer degrades to a no-op, it does not take the app
      down). This mirrors how engine *creation* failures degrade in
      permissive mode (``wrap()`` returns the client unwrapped).
    - ``strict`` — fail **CLOSED**. Raise :class:`CheckrdPolicyDenied` so the
      request is blocked exactly like a policy deny (the ASGI/WSGI middleware
      renders it as a 403). Never returns in this mode.

    The original exception is preserved in the log (``exc_info``) for
    debugging in both modes.
    """
    if security_mode == "strict":
        logger.error(
            "checkrd: policy engine evaluation failed (%s); failing CLOSED "
            "(security_mode='strict'). Request denied.",
            exc,
            exc_info=True,
        )
        raise CheckrdPolicyDenied(
            reason="policy engine evaluation error (failing closed)",
            request_id=str(uuid.uuid4()),
            code="policy_engine_error",
            url=str(request.url),
            suggestion=(
                "The policy engine raised an error while evaluating this "
                "request. In strict mode the SDK fails closed and denies the "
                "request. Inspect the logs for the underlying engine error, "
                "or set security_mode='permissive' to pass requests through "
                "unevaluated when the engine faults."
            ),
        )
    logger.warning(
        "checkrd: policy engine evaluation failed (%s); failing OPEN "
        "(security_mode='permissive'). Request passed through unevaluated.",
        exc,
        exc_info=True,
    )


def _make_pre_event(request: httpx.Request, request_id: str) -> Any:
    """Build a CheckrdEvent for the before_request hook (pre-evaluation)."""
    from checkrd.hooks import CheckrdEvent

    body_str: Optional[str] = None
    if request.content and len(request.content) <= MAX_BODY_SIZE:
        try:
            body_str = request.content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return CheckrdEvent(
        method=request.method,
        url=str(request.url),
        headers=_sanitize_headers_for_hooks([(k, v) for k, v in request.headers.items()]),
        body=body_str,
        request_id=request_id,
        trace_id=_extract_trace_id(request.headers),
    )


def _extract_trace_id(headers: httpx.Headers) -> Optional[str]:
    """Extract the trace-id (32 hex chars) from a W3C traceparent header.

    Format per W3C spec: ``{version}-{trace-id}-{parent-id}-{flags}``
    with ``version=00`` for the current spec. We accept the version
    field but drop traces from any other version — forward-compatible
    extraction means we can't reliably parse unknown versions.

    Returns ``None`` for any malformed input rather than raising;
    a bad traceparent in customer code must never break the hot
    path. Hook callers correlate when the value is present and fall
    back to ``request_id`` otherwise.
    """
    # ``httpx.Headers.get`` is annotated to return ``Any`` so the
    # ``isinstance`` narrows raw to ``str`` for the rest of the
    # function and propagates real types through tuple unpacking.
    raw = headers.get("traceparent")
    if not isinstance(raw, str):
        return None
    parts = raw.split("-")
    if len(parts) != 4:
        return None
    version, trace_id, _parent_id, _flags = parts
    if version != "00":
        return None
    if len(trace_id) != 32 or not all(c in "0123456789abcdef" for c in trace_id):
        return None
    if trace_id == "0" * 32:
        # All-zero trace-id is invalid per the W3C spec.
        return None
    return trace_id


def _make_post_event(
    request: httpx.Request,
    result: Any,
    rule_name: Optional[str],
    dashboard_url: Optional[str],
    suggestion: Optional[str],
) -> Any:
    """Build a CheckrdEvent for on_allow/on_deny hooks (post-evaluation)."""
    from checkrd.hooks import CheckrdEvent

    return CheckrdEvent(
        method=request.method,
        url=str(request.url),
        headers=_sanitize_headers_for_hooks([(k, v) for k, v in request.headers.items()]),
        request_id=result.request_id,
        allowed=result.allowed,
        rule_name=rule_name,
        deny_reason=result.deny_reason,
        suggestion=suggestion,
        trace_id=_extract_trace_id(request.headers),
        dashboard_url=dashboard_url,
    )


# ---------------------------------------------------------------------------
# Body-derived GenAI extraction (P1-15): non-streaming body + streaming tap
# ---------------------------------------------------------------------------


def _is_event_stream(response: httpx.Response) -> bool:
    """True if the response is an SSE stream (tap it) vs a buffered body (read it)."""
    ctype = response.headers.get("content-type", "")
    return "text/event-stream" in ctype.lower()


def _genai_request_model(provider: Optional[str], request: httpx.Request) -> Optional[str]:
    """The request's declared model (for pricing), from the request body. Never raises."""
    try:
        body = request.content
        attrs = extract_request_attrs(provider, body if body else None)
    except Exception:
        return None
    model = attrs.get("gen_ai.request.model")
    return model if isinstance(model, str) and model else None


def _stream_base_attrs(provider: str, request: httpx.Request) -> dict[str, Any]:
    """PII-safe base fields for the ``stream_completion`` event (no body/raw URL)."""
    host = request.url.host or ""
    path = request.url.path or ""
    attrs: dict[str, Any] = {"url_host": host, "url_path": path, "method": request.method}
    attrs.update(attributes_for_url(host, path))
    model = _genai_request_model(provider, request)
    if model is not None:
        attrs["gen_ai.request.model"] = model
    return attrs


def _merge_response_genai_attrs(
    provider: str,
    request: httpx.Request,
    body: Optional[bytes],
    headers: Any,
) -> dict[str, Any]:
    """Dotted ``gen_ai.*`` attrs (model + usage) for a non-streaming response.

    Merged with the request model so a provider whose response omits the model
    (Cohere) still has a priced SKU. Never raises.
    """
    attrs = dict(extract_response_attrs(provider, body, headers))
    model = _genai_request_model(provider, request)
    if model is not None:
        # The response model (what served the call) wins; request model backfills.
        attrs.setdefault("gen_ai.request.model", model)
    return attrs


def _enrich_or_tap_sync(
    request: httpx.Request,
    response: httpx.Response,
    result: EvalResult,
    *,
    engine: WasmEngine,
    cost_metering: bool,
    batcher: Optional[Any],
    agent_id: str,
    start_monotonic: float,
) -> dict[str, Any]:
    """Sync body-derived GenAI extraction.

    Returns dotted ``gen_ai.*`` attrs to merge into the base event
    (non-streaming), or ``{}`` after installing a lazy stream tap (streaming).
    Fail-open: any error is swallowed so the user's request is never broken.
    """
    try:
        provider = detect_provider(request.url.host or "")
        if provider is None:
            return {}
        if _is_event_stream(response):
            if provider in STREAM_CAPTURE_PROVIDERS:
                install_stream_tap(
                    response,
                    provider=provider,
                    base_attrs=_stream_base_attrs(provider, request),
                    request_id=result.request_id,
                    agent_id=agent_id,
                    engine=engine,
                    cost_metering=cost_metering,
                    batcher=batcher,
                    start_monotonic=start_monotonic,
                    is_async=False,
                )
            return {}
        # Only buffer the body for an actual inference endpoint (chat /
        # completions / embeddings / messages / generateContent). Gating on the
        # operation keeps us from reading a large non-inference response — a file
        # download or model list on the same provider host — into memory just to
        # find there is no usage to extract.
        if detect_operation(request.url.path or "") is None:
            return {}
        # Non-streaming: buffer the body once (httpx caches it; the caller's
        # response.json()/.content read the cache) and extract usage + model.
        try:
            body = response.read()
        except Exception:
            logger.debug(
                "checkrd: response.read() for genai extraction failed", exc_info=True
            )
            return {}
        return _merge_response_genai_attrs(provider, request, body, response.headers)
    except Exception:
        logger.debug("checkrd: genai body extraction failed (fail-open)", exc_info=True)
        return {}


async def _enrich_or_tap_async(
    request: httpx.Request,
    response: httpx.Response,
    result: EvalResult,
    *,
    engine: WasmEngine,
    cost_metering: bool,
    batcher: Optional[Any],
    agent_id: str,
    start_monotonic: float,
) -> dict[str, Any]:
    """Async analogue of :func:`_enrich_or_tap_sync` (awaits the body read)."""
    try:
        provider = detect_provider(request.url.host or "")
        if provider is None:
            return {}
        if _is_event_stream(response):
            if provider in STREAM_CAPTURE_PROVIDERS:
                install_stream_tap(
                    response,
                    provider=provider,
                    base_attrs=_stream_base_attrs(provider, request),
                    request_id=result.request_id,
                    agent_id=agent_id,
                    engine=engine,
                    cost_metering=cost_metering,
                    batcher=batcher,
                    start_monotonic=start_monotonic,
                    is_async=True,
                )
            return {}
        # See the sync path: only inference endpoints get their body buffered.
        if detect_operation(request.url.path or "") is None:
            return {}
        try:
            body = await response.aread()
        except Exception:
            logger.debug(
                "checkrd: response.aread() for genai extraction failed", exc_info=True
            )
            return {}
        return _merge_response_genai_attrs(provider, request, body, response.headers)
    except Exception:
        logger.debug("checkrd: genai body extraction failed (fail-open)", exc_info=True)
        return {}


class CheckrdTransport(httpx.BaseTransport):
    """Sync httpx transport that evaluates requests against the Checkrd policy engine."""

    _checkrd_instrumented = True

    def __init__(
        self,
        transport: httpx.BaseTransport,
        engine: WasmEngine,
        *,
        enforce: bool = True,
        batcher: Optional[Any] = None,
        agent_id: str = "",
        dashboard_url: str = "",
        on_deny: Optional[Any] = None,
        on_allow: Optional[Any] = None,
        before_request: Optional[Any] = None,
        security_mode: SecurityMode = DEFAULT_SECURITY_MODE,
        cost_metering: bool = False,
        extract_genai_body_attrs: bool = False,
    ) -> None:
        self._transport = transport
        self._engine = engine
        self._enforce = enforce
        self._batcher = batcher
        self._agent_id = agent_id
        self._dashboard_url = dashboard_url
        self._on_deny = on_deny
        self._on_allow = on_allow
        self._before_request = before_request
        self._security_mode: SecurityMode = security_mode
        self._cost_metering = cost_metering
        self._extract_genai_body_attrs = extract_genai_body_attrs

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Fail-closed: bodies over the WASM inspection limit must not silently
        # skip body-matcher rules. See _OVERSIZE_BODY_DENY_REASON + the
        # MAX_BODY_SIZE comment at the top of this file.
        oversized = _check_oversized_body(
            request,
            self._security_mode,
            self._enforce,
            self._agent_id,
            self._dashboard_url,
            self._batcher,
            self._on_deny,
        )
        if oversized is not None:
            raise oversized
        eval_kwargs = _build_eval_kwargs(request)

        # --- before_request hook ---
        if self._before_request is not None:
            event = _make_pre_event(request, eval_kwargs["request_id"])
            try:
                result_event = self._before_request(event)
            except Exception:
                logger.warning("checkrd: before_request hook raised; proceeding", exc_info=True)
                result_event = event
            if result_event is None:
                _append_user_agent(request)
                return self._transport.handle_request(request)

        eval_start = time.perf_counter_ns()
        try:
            result = self._engine.evaluate(**eval_kwargs)
        except Exception as exc:
            # Runtime engine fault (wasmtime trap, sandbox OOM, malformed core
            # output, etc.). strict → raises CheckrdPolicyDenied (fail closed);
            # permissive → logs and returns None so we pass the ORIGINAL
            # request through unmodified below (fail open — never break the
            # user's real API call).
            _handle_engine_error(request, exc, self._security_mode)
            return self._transport.handle_request(request)
        eval_us = (time.perf_counter_ns() - eval_start) / 1000
        _log_eval_debug(eval_kwargs["method"], str(request.url), result, eval_us)
        _record_last_eval()

        if not result.allowed:
            _log_telemetry(
                result,
                batcher=self._batcher,
                engine=self._engine,
                cost_metering=self._cost_metering,
            )
            deny_reason = result.deny_reason or "denied by policy"
            rule_name = _parse_rule_name(deny_reason)
            dash_url = _build_dashboard_url(
                self._dashboard_url,
                self._agent_id,
                result.request_id,
            )
            suggestion = _build_suggestion(deny_reason, rule_name)

            # --- on_deny hook (fires for both enforce and dry-run) ---
            if self._on_deny is not None:
                deny_event = _make_post_event(
                    request,
                    result,
                    rule_name,
                    dash_url,
                    suggestion,
                )
                try:
                    self._on_deny(deny_event)
                except Exception:
                    logger.warning("checkrd: on_deny hook raised", exc_info=True)

            if self._enforce:
                raise CheckrdPolicyDenied(
                    reason=deny_reason,
                    request_id=result.request_id,
                    rule_name=rule_name,
                    url=str(request.url),
                    dashboard_url=dash_url,
                    suggestion=suggestion,
                )
            logger.warning(
                "checkrd: %s would be denied (dry-run): %s",
                result.request_id,
                result.deny_reason,
            )

        # --- on_allow hook ---
        if result.allowed and self._on_allow is not None:
            allow_event = _make_post_event(request, result, None, None, None)
            try:
                self._on_allow(allow_event)
            except Exception:
                logger.warning("checkrd: on_allow hook raised", exc_info=True)

        _append_user_agent(request)

        start = time.monotonic()
        response = self._transport.handle_request(request)
        latency_ms = int((time.monotonic() - start) * 1000)

        # Body-derived GenAI extraction (P1-15, opt-in). Non-streaming responses
        # return the extracted usage/model to merge into the base event below
        # (feeding the cost settle in ``_enrich_telemetry``); streaming responses
        # get a lazy tap installed that emits a separate ``stream_completion``
        # event when the stream ends. Off by default ⇒ zero-overhead no-op.
        extra_attrs: dict[str, Any] = {}
        if self._extract_genai_body_attrs:
            extra_attrs = _enrich_or_tap_sync(
                request,
                response,
                result,
                engine=self._engine,
                cost_metering=self._cost_metering,
                batcher=self._batcher,
                agent_id=self._agent_id,
                start_monotonic=start,
            )

        _log_telemetry(
            result,
            status_code=response.status_code,
            latency_ms=latency_ms,
            batcher=self._batcher,
            engine=self._engine,
            cost_metering=self._cost_metering,
            extra_attrs=extra_attrs,
        )
        # Stamp the SDK's correlation request-id on the response's
        # extensions dict so callers can tie a specific call back to
        # a telemetry event without re-instrumenting:
        #   resp = client.get(...)
        #   request_id = resp.extensions.get("checkrd_request_id")
        # ``extensions`` is httpx's documented sidecar for transport-
        # supplied metadata; using it avoids polluting headers and is
        # the same channel httpx itself uses for ``http_version`` and
        # ``reason_phrase``.
        _attach_request_id(response, result.request_id)
        return response

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> CheckrdTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


#: Key under which the SDK stamps its correlation request-id on
#: every wrapped ``httpx.Response.extensions``. Callers read via
#: ``response.extensions.get(CHECKRD_REQUEST_ID_KEY)``.
CHECKRD_REQUEST_ID_KEY = "checkrd_request_id"


def _attach_request_id(response: httpx.Response, request_id: str) -> None:
    """Stamp the SDK's correlation request-id on ``response.extensions``.

    httpx documents ``extensions`` as a ``Mapping[str, Any]`` in the
    public type hint but always materializes it as a real dict on
    real responses. The defensive ``isinstance`` keeps a future shape
    change from cascading into the request path.
    """
    extensions = response.extensions
    if isinstance(extensions, dict):
        extensions[CHECKRD_REQUEST_ID_KEY] = request_id


class CheckrdAsyncTransport(httpx.AsyncBaseTransport):
    """Async httpx transport that evaluates requests against the Checkrd policy engine.

    WASM evaluation is synchronous and sub-millisecond -- running it in an executor
    would add more overhead than the evaluation itself.
    """

    _checkrd_instrumented = True

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        engine: WasmEngine,
        *,
        enforce: bool = True,
        batcher: Optional[Any] = None,
        agent_id: str = "",
        dashboard_url: str = "",
        on_deny: Optional[Any] = None,
        on_allow: Optional[Any] = None,
        before_request: Optional[Any] = None,
        security_mode: SecurityMode = DEFAULT_SECURITY_MODE,
        cost_metering: bool = False,
        extract_genai_body_attrs: bool = False,
    ) -> None:
        self._transport = transport
        self._engine = engine
        self._enforce = enforce
        self._batcher = batcher
        self._agent_id = agent_id
        self._dashboard_url = dashboard_url
        self._on_deny = on_deny
        self._on_allow = on_allow
        self._before_request = before_request
        self._security_mode: SecurityMode = security_mode
        self._cost_metering = cost_metering
        self._extract_genai_body_attrs = extract_genai_body_attrs

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        oversized = _check_oversized_body(
            request,
            self._security_mode,
            self._enforce,
            self._agent_id,
            self._dashboard_url,
            self._batcher,
            self._on_deny,
        )
        if oversized is not None:
            raise oversized
        eval_kwargs = _build_eval_kwargs(request)

        if self._before_request is not None:
            event = _make_pre_event(request, eval_kwargs["request_id"])
            try:
                result_event = self._before_request(event)
            except Exception:
                logger.warning("checkrd: before_request hook raised; proceeding", exc_info=True)
                result_event = event
            if result_event is None:
                _append_user_agent(request)
                return await self._transport.handle_async_request(request)

        eval_start = time.perf_counter_ns()
        try:
            result = self._engine.evaluate(**eval_kwargs)
        except Exception as exc:
            # Runtime engine fault. strict → raises CheckrdPolicyDenied (fail
            # closed); permissive → logs and returns None so we pass the
            # ORIGINAL request through unmodified below (fail open — never
            # break the user's real API call).
            _handle_engine_error(request, exc, self._security_mode)
            return await self._transport.handle_async_request(request)
        eval_us = (time.perf_counter_ns() - eval_start) / 1000
        _log_eval_debug(eval_kwargs["method"], str(request.url), result, eval_us)
        _record_last_eval()

        if not result.allowed:
            _log_telemetry(
                result,
                batcher=self._batcher,
                engine=self._engine,
                cost_metering=self._cost_metering,
            )
            deny_reason = result.deny_reason or "denied by policy"
            rule_name = _parse_rule_name(deny_reason)
            dash_url = _build_dashboard_url(
                self._dashboard_url,
                self._agent_id,
                result.request_id,
            )
            suggestion = _build_suggestion(deny_reason, rule_name)

            if self._on_deny is not None:
                deny_event = _make_post_event(
                    request,
                    result,
                    rule_name,
                    dash_url,
                    suggestion,
                )
                try:
                    self._on_deny(deny_event)
                except Exception:
                    logger.warning("checkrd: on_deny hook raised", exc_info=True)

            if self._enforce:
                raise CheckrdPolicyDenied(
                    reason=deny_reason,
                    request_id=result.request_id,
                    rule_name=rule_name,
                    url=str(request.url),
                    dashboard_url=dash_url,
                    suggestion=suggestion,
                )
            logger.warning(
                "checkrd: %s would be denied (dry-run): %s",
                result.request_id,
                result.deny_reason,
            )

        if result.allowed and self._on_allow is not None:
            allow_event = _make_post_event(request, result, None, None, None)
            try:
                self._on_allow(allow_event)
            except Exception:
                logger.warning("checkrd: on_allow hook raised", exc_info=True)

        _append_user_agent(request)

        start = time.monotonic()
        response = await self._transport.handle_async_request(request)
        latency_ms = int((time.monotonic() - start) * 1000)

        # Body-derived GenAI extraction (P1-15, opt-in). See the sync path.
        extra_attrs: dict[str, Any] = {}
        if self._extract_genai_body_attrs:
            extra_attrs = await _enrich_or_tap_async(
                request,
                response,
                result,
                engine=self._engine,
                cost_metering=self._cost_metering,
                batcher=self._batcher,
                agent_id=self._agent_id,
                start_monotonic=start,
            )

        _log_telemetry(
            result,
            status_code=response.status_code,
            latency_ms=latency_ms,
            batcher=self._batcher,
            engine=self._engine,
            cost_metering=self._cost_metering,
            extra_attrs=extra_attrs,
        )
        _attach_request_id(response, result.request_id)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> CheckrdAsyncTransport:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
