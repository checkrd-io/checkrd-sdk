"""Tests for the ASGI middleware (``checkrd.asgi``).

We exercise the middleware against hand-rolled ASGI 3 callables —
no FastAPI / Starlette dependency required. The protocol shape is
stable since PEP 3333 era so structural tests are durable.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from checkrd.asgi import (
    CheckrdASGIMiddleware,
    instrument_app,
    uninstrument_app,
)
from checkrd.exceptions import CheckrdPolicyDenied


async def _hello_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI app that always returns 200 'hello'."""
    await receive()  # consume the request body
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"hello"})


async def _deny_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI app that always raises CheckrdPolicyDenied."""
    raise CheckrdPolicyDenied(
        reason="blocked by rule 'no-deletes'",
        request_id="req_abc",
        url="https://api.example.com/v1/charges/ch_123",
        dashboard_url="https://app.checkrd.io/events/req_abc",
    )


async def _drive_request(
    app: Any, scope: dict[str, Any] | None = None,
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Drive one HTTP request through an ASGI app and collect the response."""
    if scope is None:
        scope = {"type": "http", "method": "POST", "path": "/"}

    body_chunks: list[bytes] = []
    response_status: list[int] = []
    response_headers: list[tuple[bytes, bytes]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response_status.append(message["status"])
            response_headers.extend(message.get("headers", []))
        elif message["type"] == "http.response.body":
            body_chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    headers_dict = {k: v for k, v in response_headers}
    return response_status[0], headers_dict, b"".join(body_chunks)


class TestCheckrdASGIMiddleware:
    def test_passes_through_successful_request(self) -> None:
        wrapped = CheckrdASGIMiddleware(_hello_app)
        status, headers, body = asyncio.run(_drive_request(wrapped))
        assert status == 200
        assert body == b"hello"

    def test_converts_policy_deny_to_403_json(self) -> None:
        wrapped = CheckrdASGIMiddleware(_deny_app)
        status, headers, body = asyncio.run(_drive_request(wrapped))
        assert status == 403
        assert headers[b"content-type"] == b"application/json"
        payload = json.loads(body)
        assert payload["error"]["type"] == "policy_denied"
        assert payload["error"]["message"] == "blocked by rule 'no-deletes'"
        assert payload["error"]["request_id"] == "req_abc"
        assert payload["error"]["dashboard_url"] == "https://app.checkrd.io/events/req_abc"
        assert payload["error"]["docs_url"].startswith("https://checkrd.io/errors/")

    def test_preserves_dashboard_url_default(self) -> None:
        async def deny_no_dashboard(
            scope: dict[str, Any], receive: Any, send: Any,
        ) -> None:
            raise CheckrdPolicyDenied(
                reason="blocked", request_id="r",
            )

        wrapped = CheckrdASGIMiddleware(
            deny_no_dashboard,
            dashboard_url="https://my.checkrd.example/",
        )
        _status, _headers, body = asyncio.run(_drive_request(wrapped))
        payload = json.loads(body)
        assert payload["error"]["dashboard_url"] == "https://my.checkrd.example/"

    def test_skips_non_http_scopes(self) -> None:
        # Lifespan / websocket scopes pass straight through. We
        # verify by piping a lifespan scope and asserting the
        # downstream app got it unmodified.
        called = []

        async def lifespan_app(
            scope: dict[str, Any], receive: Any, send: Any,
        ) -> None:
            called.append(scope["type"])

        wrapped = CheckrdASGIMiddleware(lifespan_app)
        asyncio.run(
            wrapped(
                {"type": "lifespan"},
                lambda: asyncio.sleep(0),  # noqa: ARG005
                lambda msg: asyncio.sleep(0),  # noqa: ARG005
            ),
        )
        assert called == ["lifespan"]


class _FakeFastAPIApp:
    """Minimal stand-in for FastAPI's ``add_middleware`` surface."""

    def __init__(self) -> None:
        self.middlewares: list[tuple[type, dict[str, Any]]] = []

    def add_middleware(self, cls: type, **opts: Any) -> None:
        self.middlewares.append((cls, opts))


class TestInstrumentApp:
    def test_calls_add_middleware_on_first_invocation(self) -> None:
        app = _FakeFastAPIApp()
        instrument_app(app)
        assert len(app.middlewares) == 1
        cls, _opts = app.middlewares[0]
        assert cls is CheckrdASGIMiddleware

    def test_is_idempotent(self) -> None:
        app = _FakeFastAPIApp()
        instrument_app(app)
        instrument_app(app)
        instrument_app(app)
        # Only one middleware registered despite three calls.
        assert len(app.middlewares) == 1

    def test_propagates_dashboard_url(self) -> None:
        app = _FakeFastAPIApp()
        instrument_app(app, dashboard_url="https://dash.example/")
        cls, opts = app.middlewares[0]
        assert opts["dashboard_url"] == "https://dash.example/"

    def test_falls_back_to_wrapping_for_raw_asgi_apps(self) -> None:
        # A raw async callable doesn't have add_middleware; we wrap
        # and return the wrapper.
        wrapped = instrument_app(_hello_app)
        assert isinstance(wrapped, CheckrdASGIMiddleware)

    def test_uninstrument_clears_the_idempotency_flag(self) -> None:
        app = _FakeFastAPIApp()
        instrument_app(app)
        uninstrument_app(app)
        instrument_app(app)
        # Re-instrumented after uninstrument: two registrations total.
        assert len(app.middlewares) == 2
