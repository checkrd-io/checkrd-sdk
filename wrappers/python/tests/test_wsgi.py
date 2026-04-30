"""Tests for the WSGI middleware (``checkrd.wsgi``)."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Iterable

import pytest

from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.wsgi import CheckrdWSGIMiddleware, wrap_wsgi


def _make_environ() -> dict[str, Any]:
    """Minimal WSGI environ for a POST /."""
    return {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/",
        "wsgi.input": BytesIO(b""),
        "wsgi.errors": BytesIO(),
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
    }


def _hello_app(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"hello"]


def _deny_app(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:  # noqa: ARG001
    raise CheckrdPolicyDenied(
        reason="blocked",
        request_id="req_xyz",
        url="https://api.example/",
        dashboard_url="https://dash/e/xyz",
    )


def _drive(app: Any) -> tuple[str, list[tuple[str, str]], bytes]:
    """Drive one request through a WSGI app and collect the response."""
    captured_status: list[str] = []
    captured_headers: list[list[tuple[str, str]]] = []

    def start_response(
        status: str, headers: list[tuple[str, str]],
        *args: Any, **kwargs: Any,
    ) -> Any:
        captured_status.append(status)
        captured_headers.append(headers)
        return lambda chunk: None

    body = b"".join(app(_make_environ(), start_response))
    return captured_status[0], captured_headers[0], body


class TestCheckrdWSGIMiddleware:
    def test_passes_through_successful_request(self) -> None:
        wrapped = CheckrdWSGIMiddleware(_hello_app)
        status, _headers, body = _drive(wrapped)
        assert status == "200 OK"
        assert body == b"hello"

    def test_translates_policy_deny_to_403_json(self) -> None:
        wrapped = CheckrdWSGIMiddleware(_deny_app)
        status, headers, body = _drive(wrapped)
        assert status == "403 Forbidden"
        header_dict = dict(headers)
        assert header_dict["Content-Type"] == "application/json"
        payload = json.loads(body)
        assert payload["error"]["type"] == "policy_denied"
        assert payload["error"]["request_id"] == "req_xyz"
        assert payload["error"]["dashboard_url"] == "https://dash/e/xyz"
        assert payload["error"]["docs_url"].startswith("https://checkrd.io/errors/")

    def test_re_raises_when_response_already_started(self) -> None:
        # If a handler raises AFTER calling start_response (rare but
        # possible with streaming responses), we can't override the
        # headers. Surface the original exception so the WSGI server
        # logs it correctly.
        def app(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
            start_response("200 OK", [("Content-Type", "text/plain")])
            raise CheckrdPolicyDenied(
                reason="late deny", request_id="r",
            )

        wrapped = CheckrdWSGIMiddleware(app)
        with pytest.raises(CheckrdPolicyDenied):
            _drive(wrapped)


class TestWrapWsgi:
    def test_wraps_idempotently(self) -> None:
        once = wrap_wsgi(_hello_app)
        twice = wrap_wsgi(once)
        assert once is twice

    def test_propagates_dashboard_url(self) -> None:
        wrapped = wrap_wsgi(_deny_app, dashboard_url="https://dash/")
        _status, _headers, body = _drive(wrapped)
        payload = json.loads(body)
        # When the deny exception's own dashboard_url is set, that
        # wins; the override only fills in when the exception didn't
        # carry one. Verify with a separate app that omits it.
        assert payload["error"]["dashboard_url"] == "https://dash/e/xyz"

        def deny_no_dashboard(
            environ: dict[str, Any], start_response: Any,
        ) -> Iterable[bytes]:  # noqa: ARG001
            raise CheckrdPolicyDenied(reason="b", request_id="r")

        wrapped2 = wrap_wsgi(deny_no_dashboard, dashboard_url="https://dash/")
        _s, _h, body2 = _drive(wrapped2)
        payload2 = json.loads(body2)
        assert payload2["error"]["dashboard_url"] == "https://dash/"
