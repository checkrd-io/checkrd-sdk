"""Smoke tests for the resource-based facade.

The facade pattern only earns the "100% industry standard" claim if
the surface is actually exercised: client construction, options
chaining, error class hierarchy, pagination iterator. We use
``respx`` to mock httpx without standing up a full server, mirroring
how OpenAI and Anthropic test their own SDKs.
"""
from __future__ import annotations

import httpx
import pytest
import respx

import checkrd_api
from checkrd_api import (
    AuthenticationError,
    Checkrd,
    ConflictError,
    NotFoundError,
    RateLimitError,
)

# ---------------------------------------------------------------------------
# Construction + with_options
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_defaults(self) -> None:
        client = Checkrd(api_key="ck_test_x")
        assert client.api_version == checkrd_api.DEFAULT_API_VERSION
        assert client.max_retries == checkrd_api.DEFAULT_MAX_RETRIES
        assert client.base_url == checkrd_api.DEFAULT_BASE_URL
        assert client.timeout == checkrd_api.DEFAULT_TIMEOUT_SECS

    def test_with_options_layers_overrides(self) -> None:
        client = Checkrd(api_key="ck_test_x", max_retries=2, timeout=60)
        layered = client.with_options(max_retries=5, timeout=10)
        assert layered.max_retries == 5
        assert layered.timeout == 10
        # Original is unchanged.
        assert client.max_retries == 2
        assert client.timeout == 60

    def test_resource_attached_lazily(self) -> None:
        client = Checkrd(api_key="ck_test_x")
        # Touching the property the first time triggers import.
        assert client.agents is not None
        assert client.agents is client.agents  # cached_property memoizes

    def test_context_manager_closes_http(self) -> None:
        with Checkrd(api_key="ck_test_x") as client:
            assert client._http is not None
        # No exception on exit; the underlying httpx client was closed.

    def test_env_fallback_for_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_API_KEY", "ck_env_value")
        client = Checkrd()
        assert client.api_key == "ck_env_value"


# ---------------------------------------------------------------------------
# Happy-path agents.list with mocked httpx
# ---------------------------------------------------------------------------


class TestListAgents:
    @respx.mock
    def test_returns_paginated_iterator(self) -> None:
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test")
        first_page = {
            "data": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "org_id": "00000000-0000-0000-0000-000000000000",
                    "name": "agent-a",
                    "slug": "agent-a",
                    "description": None,
                    "status": "active",
                    "public_key": None,
                    "kill_switch_active": False,
                    "active_policy_mode": None,
                    "created_at": "2026-04-15T10:00:00Z",
                },
            ],
            "has_more": True,
            "next_cursor": "11111111-1111-1111-1111-111111111111",
        }
        second_page = {
            "data": [
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "org_id": "00000000-0000-0000-0000-000000000000",
                    "name": "agent-b",
                    "slug": "agent-b",
                    "description": None,
                    "status": "active",
                    "public_key": None,
                    "kill_switch_active": True,
                    "active_policy_mode": None,
                    "created_at": "2026-04-15T10:01:00Z",
                },
            ],
            "has_more": False,
            "next_cursor": None,
        }
        # Match either of the two GET calls — first sends limit=20,
        # second appends ?cursor=…. respx matches the path; we
        # respond with the first or second page based on the cursor
        # query param.
        route = respx.get("https://api.example.test/v1/agents").mock(
            side_effect=[
                httpx.Response(200, json=first_page),
                httpx.Response(200, json=second_page),
            ],
        )

        names = [agent.name for agent in client.agents.list()]
        assert names == ["agent-a", "agent-b"]
        assert route.call_count == 2

    @respx.mock
    def test_with_options_max_retries_overrides(self) -> None:
        # max_retries=0 → exactly one attempt, no retry on 500.
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test")
        respx.get("https://api.example.test/v1/agents").respond(
            500,
            json={
                "type": "https://checkrd.io/errors/internal_error",
                "title": "Internal error",
                "status": 500,
                "detail": "boom",
                "code": "internal_error",
            },
        )
        with pytest.raises(checkrd_api.InternalServerError):
            client.with_options(max_retries=0).agents.list()
        client.close()


# ---------------------------------------------------------------------------
# Error class hierarchy
# ---------------------------------------------------------------------------


class TestErrorMapping:
    @respx.mock
    @pytest.mark.parametrize(
        "status, exc",
        [
            (400, checkrd_api.BadRequestError),
            (401, AuthenticationError),
            (403, checkrd_api.PermissionDeniedError),
            (404, NotFoundError),
            (409, ConflictError),
            (422, checkrd_api.UnprocessableEntityError),
            (429, RateLimitError),
            (500, checkrd_api.InternalServerError),
            (503, checkrd_api.InternalServerError),
        ],
    )
    def test_status_code_maps_to_subclass(self, status: int, exc: type) -> None:
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test", max_retries=0)
        respx.get("https://api.example.test/v1/agents/abc").respond(
            status,
            # Flat RFC 9457 application/problem+json — no nested "error".
            json={
                "type": "https://checkrd.io/errors/test_code",
                "title": "Test error",
                "status": status,
                "detail": f"oops {status}",
                "code": "test_code",
            },
        )
        with pytest.raises(exc) as raised:
            client.agents.retrieve("abc")
        # Every subclass exposes status_code, code, type, message —
        # all read from the flat top-level problem members.
        assert raised.value.status_code == status
        assert raised.value.code == "test_code"
        assert raised.value.type == "https://checkrd.io/errors/test_code"
        assert "oops" in raised.value.message
        client.close()

    @respx.mock
    def test_field_errors_mapped_to_errors_and_param(self) -> None:
        # 422 with per-field pointers: errors[] is exposed verbatim and
        # .param surfaces the first pointer (the old Stripe error.param).
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test", max_retries=0)
        respx.get("https://api.example.test/v1/agents/abc").respond(
            422,
            json={
                "type": "https://checkrd.io/errors/validation_failed",
                "title": "Validation failed",
                "status": 422,
                "detail": "request body failed validation",
                "code": "validation_failed",
                "errors": [
                    {"pointer": "/email", "detail": "must be a valid email address"},
                    {"pointer": "/name", "detail": "must not be empty"},
                ],
                "request_id": "req_body123",
            },
        )
        with pytest.raises(checkrd_api.UnprocessableEntityError) as raised:
            client.agents.retrieve("abc")
        err = raised.value
        assert err.code == "validation_failed"
        assert err.param == "/email"
        assert [e["pointer"] for e in err.errors] == ["/email", "/name"]
        assert err.errors[0]["detail"] == "must be a valid email address"
        # No response header → request_id falls back to the body value.
        assert err.request_id == "req_body123"
        client.close()

    @respx.mock
    def test_request_id_header_preferred_over_body(self) -> None:
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test", max_retries=0)
        respx.get("https://api.example.test/v1/agents/abc").respond(
            404,
            json={
                "type": "https://checkrd.io/errors/agent_not_found",
                "title": "Not found",
                "status": 404,
                "detail": "no such agent",
                "code": "agent_not_found",
                "request_id": "req_in_body",
            },
            headers={"checkrd-request-id": "req_abc123"},
        )
        with pytest.raises(NotFoundError) as raised:
            client.agents.retrieve("abc")
        # The response header wins over the body's request_id.
        assert raised.value.request_id == "req_abc123"
        client.close()

    @respx.mock
    @pytest.mark.parametrize("body", ["<html>502 Bad Gateway</html>", ""])
    def test_non_json_error_body_degrades_gracefully(self, body: str) -> None:
        # A load-balancer 502 with an HTML or empty body must still map to
        # a sane exception: message falls back to HTTP <status>, and the
        # flat accessors are empty rather than raising while building.
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test", max_retries=0)
        respx.get("https://api.example.test/v1/agents/abc").respond(
            502,
            text=body,
            headers={"content-type": "text/html"},
        )
        with pytest.raises(checkrd_api.InternalServerError) as raised:
            client.agents.retrieve("abc")
        err = raised.value
        assert err.status_code == 502
        assert err.code is None
        assert err.type is None
        assert err.param is None
        assert err.errors == []
        assert err.message == "HTTP 502"
        client.close()

    @respx.mock
    def test_rate_limit_surfaces_retry_after(self) -> None:
        # 429 with Retry-After (integer seconds) is parsed onto the error.
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test", max_retries=0)
        respx.get("https://api.example.test/v1/agents/abc").respond(
            429,
            json={
                "type": "https://checkrd.io/errors/rate_limit_exceeded",
                "title": "Too many requests",
                "status": 429,
                "detail": "slow down",
                "code": "rate_limit_exceeded",
            },
            headers={"retry-after": "7"},
        )
        with pytest.raises(RateLimitError) as raised:
            client.agents.retrieve("abc")
        assert raised.value.retry_after == 7.0
        client.close()


# ---------------------------------------------------------------------------
# Headers + auth injection
# ---------------------------------------------------------------------------


class TestHeaders:
    @respx.mock
    def test_x_api_key_injected(self) -> None:
        client = Checkrd(api_key="ck_test_secret", base_url="https://api.example.test")
        route = respx.get("https://api.example.test/v1/agents").respond(
            200, json={"data": [], "has_more": False, "next_cursor": None}
        )
        list(client.agents.list())
        assert route.calls[0].request.headers["x-api-key"] == "ck_test_secret"
        client.close()

    @respx.mock
    def test_bearer_token_when_no_api_key(self) -> None:
        client = Checkrd(bearer_token="jwt_xyz", base_url="https://api.example.test")
        route = respx.get("https://api.example.test/v1/agents").respond(
            200, json={"data": [], "has_more": False, "next_cursor": None}
        )
        list(client.agents.list())
        assert route.calls[0].request.headers["authorization"] == "Bearer jwt_xyz"
        client.close()

    @respx.mock
    def test_checkrd_version_header_pinned(self) -> None:
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test")
        route = respx.get("https://api.example.test/v1/agents").respond(
            200, json={"data": [], "has_more": False, "next_cursor": None}
        )
        list(client.agents.list())
        assert route.calls[0].request.headers["checkrd-version"] == client.api_version
        client.close()


# ---------------------------------------------------------------------------
# Retry-After parsing + retry-loop honoring
# ---------------------------------------------------------------------------


class TestRetryAfter:
    def test_parse_integer_seconds(self) -> None:
        from checkrd_api._exceptions import parse_retry_after

        resp = httpx.Response(429, headers={"retry-after": "12"})
        assert parse_retry_after(resp) == 12.0

    def test_parse_http_date(self) -> None:
        import datetime as dt

        from checkrd_api._exceptions import parse_retry_after

        # ~30s in the future, formatted as an RFC 9110 HTTP-date.
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
        http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        resp = httpx.Response(429, headers={"retry-after": http_date})
        seconds = parse_retry_after(resp)
        assert seconds is not None
        assert 20.0 <= seconds <= 31.0

    def test_parse_absent_or_garbage_is_none(self) -> None:
        from checkrd_api._exceptions import parse_retry_after

        assert parse_retry_after(httpx.Response(429)) is None
        assert parse_retry_after(httpx.Response(429, headers={"retry-after": "soon"})) is None

    def test_delay_before_retry_honors_header_and_caps(self) -> None:
        # An absurd Retry-After is clamped to the hard cap so a hostile
        # header can't wedge the retry loop.
        resp = httpx.Response(429, headers={"retry-after": "999999"})
        delay = Checkrd._delay_before_retry(resp, attempt=1)
        assert delay == Checkrd._MAX_RETRY_AFTER_SECS

    def test_delay_before_retry_falls_back_to_backoff(self) -> None:
        # No Retry-After → jittered exponential backoff (positive, bounded).
        resp = httpx.Response(500)
        delay = Checkrd._delay_before_retry(resp, attempt=1)
        assert 0.0 < delay <= 8.0

    @respx.mock
    def test_retry_loop_waits_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # First 429 carries Retry-After: 5; the loop must sleep that long
        # (not the jittered ~0.5s), then succeed on the retry.
        slept: list[float] = []
        monkeypatch.setattr("checkrd_api._client.time.sleep", lambda s: slept.append(s))
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test", max_retries=1)
        respx.get("https://api.example.test/v1/agents").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={
                        "type": "https://checkrd.io/errors/rate_limit_exceeded",
                        "title": "Too many requests",
                        "status": 429,
                        "code": "rate_limit_exceeded",
                    },
                    headers={"retry-after": "5"},
                ),
                httpx.Response(200, json={"data": [], "has_more": False, "next_cursor": None}),
            ],
        )
        list(client.agents.list())
        assert slept == [5.0]
        client.close()
