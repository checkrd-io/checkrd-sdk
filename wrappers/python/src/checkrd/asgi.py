"""ASGI middleware for FastAPI / Starlette / any other ASGI app.

This module exposes :class:`CheckrdASGIMiddleware` and the convenience
:func:`instrument_app` function. Both work with any ASGI 3 application,
which covers FastAPI, Starlette, Litestar, Quart, Sanic (with the ASGI
shim), and the Django ASGI server.

The middleware does **not** instrument outbound HTTP itself — that is
already covered by :func:`checkrd.instrument` which patches the vendor
SDKs (OpenAI, Anthropic, Cohere, …) at the ``httpx`` transport layer
and catches every framework-routed call transitively. What this
middleware adds is:

1. **Idempotent registration**. Following the OpenTelemetry convention,
   we set ``app.is_instrumented_by_checkrd = True`` after wrapping; a
   second call is a no-op. Mirrors
   ``opentelemetry.instrumentation.fastapi.FastAPIInstrumentor``.

2. **`CheckrdPolicyDenied` → 403 JSON**. When a denied request bubbles
   up from a handler that uses a Checkrd-wrapped HTTP client, we
   translate it into a Stripe-shaped error envelope with
   ``request_id``, ``dashboard_url``, and remediation deep link
   instead of letting the framework's default 500 page swallow the
   useful diagnostics.

3. **Per-request logger context**. The Checkrd request id and policy
   decision land in ``logging`` extras so any structured-logging
   pipeline downstream picks them up.

Example (FastAPI)::

    from fastapi import FastAPI
    import checkrd
    from checkrd.asgi import instrument_app
    from openai import OpenAI

    app = FastAPI()
    checkrd.init(api_key="ck_live_...", policy="policy.yaml")
    checkrd.instrument()  # patches OpenAI / Anthropic / etc.
    instrument_app(app)   # converts policy denials to 403 JSON

    client = OpenAI()

    @app.post("/chat")
    async def chat(prompt: str):
        return client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": prompt}],
        )

Example (Starlette / generic ASGI)::

    from starlette.applications import Starlette
    from checkrd.asgi import CheckrdASGIMiddleware

    app = Starlette()
    app.add_middleware(CheckrdASGIMiddleware)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from checkrd.exceptions import CheckrdPolicyDenied

logger = logging.getLogger("checkrd")

__all__ = [
    "CheckrdASGIMiddleware",
    "instrument_app",
    "uninstrument_app",
]

# Type aliases for the ASGI 3 protocol — kept loose so we don't
# require an asgiref dependency. ASGI 3's wire shape is stable.
ASGIScope = dict[str, Any]
ASGIMessage = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]

# Sentinel set on instrumented apps so a second call is a no-op.
# Mirrors OpenTelemetry's `is_instrumented_by_opentelemetry` flag.
_CHECKRD_INSTRUMENTED_FLAG = "is_instrumented_by_checkrd"


class CheckrdASGIMiddleware:
    """ASGI middleware that converts ``CheckrdPolicyDenied`` to 403 JSON.

    Conforms to the ASGI 3 protocol (the long-lived ``async def
    __call__(scope, receive, send)`` shape). Wraps any downstream ASGI
    app — pass it directly to ``add_middleware`` (Starlette/FastAPI)
    or use :func:`instrument_app` for the OpenTelemetry-style
    convenience.

    The middleware skips non-HTTP scopes (lifespan, websocket) so it
    composes cleanly with framework startup hooks.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        dashboard_url: Optional[str] = None,
    ) -> None:
        self._app = app
        self._dashboard_url = dashboard_url

    async def __call__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            # Lifespan, websocket, and any future ASGI scope types pass
            # straight through. Policy enforcement only applies to the
            # request/response path.
            await self._app(scope, receive, send)
            return

        try:
            await self._app(scope, receive, send)
        except CheckrdPolicyDenied as exc:
            await self._send_deny_response(send, exc)

    async def _send_deny_response(
        self, send: ASGISend, exc: CheckrdPolicyDenied,
    ) -> None:
        """Stripe-shaped 403 envelope when policy denies a downstream call.

        Matches the JS adapters' response shape so client code can rely
        on a consistent error format across SDKs.
        """
        body = json.dumps(
            {
                "error": {
                    "type": "policy_denied",
                    "message": exc.reason,
                    "code": exc.code,
                    "request_id": exc.request_id,
                    "dashboard_url": exc.dashboard_url
                    or self._dashboard_url,
                    "docs_url": exc.docs_url,
                },
            },
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            },
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            },
        )


def instrument_app(
    app: Any,
    *,
    dashboard_url: Optional[str] = None,
) -> Any:
    """Wrap an ASGI app with :class:`CheckrdASGIMiddleware`.

    Idempotent — a second call on the same app instance is a no-op. The
    ``is_instrumented_by_checkrd`` attribute is set on the app to
    record the wrap, matching OpenTelemetry's
    ``is_instrumented_by_opentelemetry`` convention.

    Works with **any framework that exposes an `add_middleware` method
    or accepts middleware in its constructor**: FastAPI, Starlette,
    Litestar, Quart, etc. For raw ASGI apps that don't have
    ``add_middleware``, instantiate :class:`CheckrdASGIMiddleware`
    directly:

        app = CheckrdASGIMiddleware(my_asgi_app)

    Args:
        app: The application instance to wrap.
        dashboard_url: Base URL embedded in deny-error deep links.

    Returns:
        The same app instance (mutated in place if it has
        ``add_middleware``; otherwise wrapped and returned).
    """
    if getattr(app, _CHECKRD_INSTRUMENTED_FLAG, False):
        logger.debug("checkrd: app already instrumented, skipping")
        return app

    if hasattr(app, "add_middleware") and callable(app.add_middleware):
        # FastAPI / Starlette / Litestar pattern.
        app.add_middleware(
            CheckrdASGIMiddleware,
            dashboard_url=dashboard_url,
        )
        setattr(app, _CHECKRD_INSTRUMENTED_FLAG, True)
        return app

    # Raw ASGI app — wrap it. Caller must use the return value.
    wrapped = CheckrdASGIMiddleware(app, dashboard_url=dashboard_url)
    # Best-effort: set the flag on the wrapper so ``uninstrument_app``
    # can find it later, and on the wrapped app for users who hold a
    # reference to the original.
    try:
        setattr(app, _CHECKRD_INSTRUMENTED_FLAG, True)
    except (AttributeError, TypeError):
        # Some ASGI callables (lambdas, slotted classes) don't accept
        # arbitrary attributes; not a real failure.
        pass
    setattr(wrapped, _CHECKRD_INSTRUMENTED_FLAG, True)
    return wrapped


def uninstrument_app(app: Any) -> Any:
    """Mark an app as un-instrumented for testing / hot-reload.

    Removes the ``is_instrumented_by_checkrd`` attribute so a follow-up
    call to :func:`instrument_app` registers fresh. Note: ASGI
    middleware can't be removed from a Starlette/FastAPI app once
    added — this only resets the flag so re-runs in tests work.
    """
    if hasattr(app, _CHECKRD_INSTRUMENTED_FLAG):
        try:
            delattr(app, _CHECKRD_INSTRUMENTED_FLAG)
        except AttributeError:
            pass
    return app
