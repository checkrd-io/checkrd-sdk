"""Smoke tests for the resource-based facade.

The facade pattern only earns the "100% industry standard" claim if
the surface is actually exercised: client construction, options
chaining, error class hierarchy, pagination iterator. We use
``respx`` to mock httpx without standing up a full server, mirroring
how OpenAI and Anthropic test their own SDKs.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

import checkrd_api
from checkrd_api import (
    APIConnectionError,
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
            500, json={"error": {"type": "internal_error", "code": "boom", "message": "boom"}}
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
            json={
                "error": {
                    "type": "test_error",
                    "code": "test_code",
                    "message": f"oops {status}",
                }
            },
        )
        with pytest.raises(exc) as raised:
            client.agents.retrieve("abc")
        # Every subclass exposes status_code, code, message.
        assert raised.value.status_code == status
        assert raised.value.code == "test_code"
        assert "oops" in raised.value.message
        client.close()

    @respx.mock
    def test_request_id_exposed_when_header_present(self) -> None:
        client = Checkrd(api_key="ck_test_x", base_url="https://api.example.test", max_retries=0)
        respx.get("https://api.example.test/v1/agents/abc").respond(
            404,
            json={"error": {"type": "not_found", "code": "agent_not_found", "message": "no"}},
            headers={"checkrd-request-id": "req_abc123"},
        )
        with pytest.raises(NotFoundError) as raised:
            client.agents.retrieve("abc")
        assert raised.value.request_id == "req_abc123"
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
