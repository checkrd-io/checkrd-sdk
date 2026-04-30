"""WASM-free mocks for testing code that uses Checkrd.

Production Checkrd evaluates policy via a WebAssembly engine. That's
great for performance and sandboxing, but it's a heavy dependency for
unit tests — it requires the compiled ``.wasm`` binary, ``wasmtime``,
and a valid policy file. This module provides lightweight alternatives
that let you test your agent code without any of that.

Quick start::

    from checkrd.testing import mock_wrap
    import httpx

    # Always allow (default):
    client = mock_wrap(httpx.Client())

    # Always deny:
    client = mock_wrap(httpx.Client(), default="deny")

    # Rule-based (same format as your policy.yaml):
    client = mock_wrap(httpx.Client(), rules=[
        {"allow": {"method": ["GET"], "url": "api.stripe.com/*"}},
        {"deny": {"method": ["DELETE"], "url": "*"}},
    ])

    # Callback for full control:
    client = mock_wrap(
        httpx.Client(),
        policy_fn=lambda method, url, headers, body: method == "GET",
    )

The mock uses the real :class:`CheckrdTransport` under the hood, so your
tests exercise the same deny/allow branching, dry-run behavior,
User-Agent appending, and telemetry logging as production — only the
policy evaluation is different.

**No WASM dependency:** This module is importable and fully functional
without ``wasmtime`` installed. The only import is ``httpx``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

from checkrd.transports._httpx import (
    CheckrdAsyncTransport,
    CheckrdTransport,
)

__all__ = [
    "mock_wrap",
    "mock_wrap_async",
    "MockEngine",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: Callback signature for the ``policy_fn`` parameter. Receives the HTTP
#: method, URL, headers (as a list of tuples), and body (or None). Returns
#: ``True`` to allow the request, ``False`` to deny.
PolicyFn = Callable[[str, str, list[tuple[str, str]], Optional[str]], bool]


# ---------------------------------------------------------------------------
# MockEngine
# ---------------------------------------------------------------------------


class MockEngine:
    """A WASM-free policy engine for unit tests.

    Implements the same ``evaluate()`` signature as
    :class:`checkrd.engine.WasmEngine` so it can be plugged into the real
    :class:`CheckrdTransport`. Three evaluation modes are supported:

    1. **Default mode** (``default="allow"`` or ``default="deny"``): every
       request gets the same verdict. The simplest option for tests that
       don't care about policy logic.
    2. **Rule mode** (``rules=[...]``): URL/method glob matching, with deny
       rules checked first then allow rules, then the default. Matches the
       WASM engine's evaluation order. Good for testing specific policy
       behavior without the full engine.
    3. **Callback mode** (``policy_fn=...``): you decide per-request. Full
       control for edge-case tests.

    Modes are mutually exclusive: ``policy_fn`` wins over ``rules`` which
    wins over ``default``.

    Rate limiting and body matching are NOT implemented — they're too
    complex for a unit-test mock and rarely what you're testing. Use the
    real engine (via ``wrap()``) for integration tests that need them.
    """

    def __init__(
        self,
        *,
        default: str = "allow",
        rules: Optional[Sequence[Dict[str, Any]]] = None,
        policy_fn: Optional[PolicyFn] = None,
    ) -> None:
        if default not in ("allow", "deny"):
            raise ValueError(f"default must be 'allow' or 'deny'; got {default!r}")
        self._default = default
        self._rules: List[Dict[str, Any]] = list(rules) if rules else []
        self._policy_fn = policy_fn
        self._last_trace: List[str] = []

    @property
    def last_trace(self) -> List[str]:
        """The debug trace from the most recent evaluation. Useful for
        test assertions about which rules were checked."""
        return list(self._last_trace)

    def evaluate(
        self,
        request_id: str,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: Optional[str],
        timestamp: str,
        timestamp_ms: int,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> "_MockEvalResult":
        """Evaluate a request against the mock policy.

        Returns a :class:`_MockEvalResult` that duck-types as
        :class:`checkrd.engine.EvalResult`.
        """
        if self._policy_fn is not None:
            allowed = self._policy_fn(method, url, headers, body)
            return _MockEvalResult(
                allowed=allowed,
                deny_reason=None if allowed else "denied by policy_fn",
                request_id=request_id,
            )

        if self._rules:
            return self._evaluate_rules(method, url, request_id)

        allowed = self._default == "allow"
        return _MockEvalResult(
            allowed=allowed,
            deny_reason=None if allowed else "denied by default policy",
            request_id=request_id,
        )

    def _evaluate_rules(
        self,
        method: str,
        url: str,
        request_id: str,
    ) -> "_MockEvalResult":
        """Match request against rules, deny-first then allow-first."""
        url_for_match = url
        for prefix in ("https://", "http://"):
            if url_for_match.startswith(prefix):
                url_for_match = url_for_match[len(prefix):]
                break

        trace = [f"eval {method} {url}"]

        # Deny rules first.
        for rule in self._rules:
            deny = rule.get("deny")
            name = rule.get("name", "unnamed")
            if deny and self._matches(deny, method, url_for_match):
                trace.append(f"  rule '{name}' (deny) -> MATCH")
                self._last_trace = trace
                return _MockEvalResult(
                    allowed=False,
                    deny_reason=f"denied by rule '{name}'",
                    request_id=request_id,
                )
            elif deny:
                trace.append(f"  rule '{name}' (deny) -> skip")

        # Allow rules.
        for rule in self._rules:
            allow = rule.get("allow")
            name = rule.get("name", "unnamed")
            if allow and self._matches(allow, method, url_for_match):
                trace.append(f"  rule '{name}' (allow) -> MATCH")
                self._last_trace = trace
                return _MockEvalResult(
                    allowed=True,
                    deny_reason=None,
                    request_id=request_id,
                )
            elif allow:
                trace.append(f"  rule '{name}' (allow) -> skip")

        # Default.
        allowed = self._default == "allow"
        trace.append(f"  default -> {'ALLOW' if allowed else 'DENY'}")
        self._last_trace = trace
        return _MockEvalResult(
            allowed=allowed,
            deny_reason=None if allowed else "denied by default policy",
            request_id=request_id,
        )

    @staticmethod
    def _matches(clause: Dict[str, Any], method: str, url: str) -> bool:
        """Check if a rule clause matches the request method and URL."""
        methods = clause.get("method")
        if methods and method.upper() not in [m.upper() for m in methods]:
            return False
        pattern = clause.get("url", "*")
        return fnmatch(url, pattern)


class _MockEvalResult:
    """Duck-type of :class:`checkrd.engine.EvalResult` without importing it.

    Avoids pulling in the ``wasmtime`` dependency chain. The transport
    only reads ``.allowed``, ``.deny_reason``, ``.telemetry_json``, and
    ``.request_id``, so that's all we provide.
    """

    __slots__ = ("allowed", "deny_reason", "telemetry_json", "request_id")

    def __init__(
        self,
        allowed: bool,
        deny_reason: Optional[str],
        request_id: str,
    ) -> None:
        self.allowed = allowed
        self.deny_reason = deny_reason
        self.request_id = request_id
        self.telemetry_json = json.dumps(
            {
                "request_id": request_id,
                "policy_result": "allowed" if allowed else "denied",
                "deny_reason": deny_reason,
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "span_name": "mock",
                "span_kind": "INTERNAL",
                "span_status_code": "UNSET",
            }
        )


# ---------------------------------------------------------------------------
# mock_wrap / mock_wrap_async
# ---------------------------------------------------------------------------


def mock_wrap(
    client: httpx.Client,
    *,
    default: str = "allow",
    rules: Optional[Sequence[Dict[str, Any]]] = None,
    policy_fn: Optional[PolicyFn] = None,
    enforce: bool = True,
    on_deny: Optional[Callable[..., None]] = None,
    on_allow: Optional[Callable[..., None]] = None,
    before_request: Optional[Callable[..., Any]] = None,
) -> httpx.Client:
    """Replace ``client``'s transport with a Checkrd mock.

    Returns the same client instance (matching :func:`checkrd.wrap`
    semantics). The mock engine is configured by the keyword arguments.
    Hooks are supported to match the real ``wrap()`` interface.
    """
    engine = MockEngine(default=default, rules=rules, policy_fn=policy_fn)
    client._transport = CheckrdTransport(
        client._transport,
        engine,  # type: ignore[arg-type]  # duck-typed
        enforce=enforce,
        on_deny=on_deny,
        on_allow=on_allow,
        before_request=before_request,
    )
    return client


def mock_wrap_async(
    client: httpx.AsyncClient,
    *,
    default: str = "allow",
    rules: Optional[Sequence[Dict[str, Any]]] = None,
    policy_fn: Optional[PolicyFn] = None,
    enforce: bool = True,
    on_deny: Optional[Callable[..., None]] = None,
    on_allow: Optional[Callable[..., None]] = None,
    before_request: Optional[Callable[..., Any]] = None,
) -> httpx.AsyncClient:
    """Async variant of :func:`mock_wrap`. See that function for docs."""
    engine = MockEngine(default=default, rules=rules, policy_fn=policy_fn)
    client._transport = CheckrdAsyncTransport(
        client._transport,
        engine,  # type: ignore[arg-type]  # duck-typed
        enforce=enforce,
        on_deny=on_deny,
        on_allow=on_allow,
        before_request=before_request,
    )
    return client
