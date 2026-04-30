"""WSGI middleware for Flask, Django (sync), Pyramid, Bottle.

Companion to :mod:`checkrd.asgi`. WSGI is the older sync request
protocol still used by Flask 2.x, Django (when not running ASGI),
Pyramid, and the Python web fossils. Django ≥3 supports both ASGI
and WSGI; for ASGI deployments use :class:`checkrd.asgi.CheckrdASGIMiddleware`
instead — it has lower latency.

The middleware traps ``CheckrdPolicyDenied`` raised from a downstream
handler that uses a Checkrd-wrapped HTTP client, and emits a
Stripe-shaped 403 JSON envelope identical to the ASGI middleware's.

Example (Flask)::

    from flask import Flask
    import checkrd
    from checkrd.wsgi import wrap_wsgi
    from openai import OpenAI

    app = Flask(__name__)
    checkrd.init(api_key="ck_live_...", policy="policy.yaml")
    checkrd.instrument()

    # Wrap the underlying WSGI app
    app.wsgi_app = wrap_wsgi(app.wsgi_app)

    client = OpenAI()

    @app.post("/chat")
    def chat():
        return client.chat.completions.create(
            model="gpt-4o", messages=[...],
        )

Example (Django)::

    # In wsgi.py
    from django.core.wsgi import get_wsgi_application
    from checkrd.wsgi import wrap_wsgi

    application = wrap_wsgi(get_wsgi_application())
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable, Optional

from checkrd.exceptions import CheckrdPolicyDenied

logger = logging.getLogger("checkrd")

__all__ = [
    "wrap_wsgi",
    "CheckrdWSGIMiddleware",
]

# WSGI 1.0.1 (PEP 3333) types — kept as plain callables to avoid an
# environ-types dependency.
WSGIEnviron = dict[str, Any]
StartResponse = Callable[[str, list[tuple[str, str]]], Callable[[bytes], Any]]
WSGIApp = Callable[[WSGIEnviron, StartResponse], Iterable[bytes]]


class CheckrdWSGIMiddleware:
    """WSGI middleware that translates ``CheckrdPolicyDenied`` to 403.

    Wraps a downstream WSGI app. The middleware's ``__call__``
    delegates the request, then catches a deny exception and emits
    a JSON error response with the same shape the ASGI middleware
    uses.

    Note that classic WSGI is sync; if your handler raises *during*
    response streaming (after ``start_response`` has been called), we
    can't safely rewrite the headers. Such cases re-raise the original
    exception so the WSGI server's error handler sees it. Pre-response
    exceptions get the JSON envelope.
    """

    def __init__(
        self,
        app: WSGIApp,
        *,
        dashboard_url: Optional[str] = None,
    ) -> None:
        self._app = app
        self._dashboard_url = dashboard_url

    def __call__(
        self, environ: WSGIEnviron, start_response: StartResponse,
    ) -> Iterable[bytes]:
        # Track whether start_response has fired so we know whether
        # it's safe to override headers in the deny path.
        response_started = [False]
        original_start = start_response

        def tracking_start(
            status: str, headers: list[tuple[str, str]],
            *args: Any, **kwargs: Any,
        ) -> Callable[[bytes], Any]:
            response_started[0] = True
            return original_start(status, headers, *args, **kwargs)

        try:
            return self._app(environ, tracking_start)
        except CheckrdPolicyDenied as exc:
            if response_started[0]:
                # Headers already sent; can't override. Surface the
                # original error to the WSGI server's logger.
                logger.warning(
                    "checkrd: policy deny after response started "
                    "(request_id=%s); WSGI server will handle as 500",
                    exc.request_id,
                )
                raise
            return self._send_deny(start_response, exc)

    def _send_deny(
        self,
        start_response: StartResponse,
        exc: CheckrdPolicyDenied,
    ) -> Iterable[bytes]:
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
        start_response(
            "403 Forbidden",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]


def wrap_wsgi(
    app: WSGIApp,
    *,
    dashboard_url: Optional[str] = None,
) -> WSGIApp:
    """Wrap a WSGI app with :class:`CheckrdWSGIMiddleware`.

    Idempotent — calling on an already-wrapped app returns the existing
    wrapper unchanged. Detects re-wrapping by checking for the
    ``_checkrd_wrapped`` attribute we set on the wrapper.

    Args:
        app: The WSGI app to wrap. For Flask, that's ``app.wsgi_app``.
            For Django, the result of ``get_wsgi_application()``.
        dashboard_url: Base URL embedded in deny-error deep links.

    Returns:
        A new WSGI callable. Caller must replace the original
        reference with this — WSGI middleware is composed by
        replacement, not mutation (unlike ASGI's ``add_middleware``).
    """
    if getattr(app, "_checkrd_wrapped", False):
        return app
    wrapped = CheckrdWSGIMiddleware(app, dashboard_url=dashboard_url)
    # Mark for idempotency on re-wrap.
    setattr(wrapped, "_checkrd_wrapped", True)
    return wrapped
