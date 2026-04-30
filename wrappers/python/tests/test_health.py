"""Tests for checkrd.healthy() health check function."""

from __future__ import annotations

import httpx
import pytest

import checkrd
from checkrd._state import get_last_eval_at
from checkrd.testing import mock_wrap
from tests.conftest import requires_wasm


def _mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
    for var in ("CHECKRD_API_KEY", "CHECKRD_BASE_URL", "CHECKRD_AGENT_ID",
                "CHECKRD_ENFORCE", "CHECKRD_DISABLED", "CHECKRD_DEBUG"):
        monkeypatch.delenv(var, raising=False)
    checkrd.shutdown()
    # Reset last eval via the ContextVar directly.
    from checkrd import _state
    _state._LAST_EVAL_AT = None
    yield
    checkrd.uninstrument()
    checkrd.shutdown()
    _state._LAST_EVAL_AT = None


class TestHealthyBeforeInit:
    def test_returns_disabled_status(self) -> None:
        result = checkrd.healthy()
        assert result["status"] == "disabled"
        assert result["engine_loaded"] is False
        assert result["last_eval_at"] is None

    def test_all_expected_keys_present(self) -> None:
        # The disabled path now also populates ``degradation_reason``
        # (set to None) so the dict shape is uniform with the
        # degraded path. Dashboards that ``.get("degradation_reason")``
        # see ``None`` here and a stable token in the degraded case.
        result = checkrd.healthy()
        expected_keys = {
            "status", "engine_loaded", "control_plane_connected",
            "agent_id", "enforce", "last_eval_at", "degradation_reason",
        }
        assert expected_keys == set(result.keys())
        assert result["degradation_reason"] is None


@requires_wasm
class TestHealthyAfterInit:
    def test_healthy_status(self) -> None:
        checkrd.init(agent_id="test")
        result = checkrd.healthy()
        assert result["status"] == "healthy"
        assert result["engine_loaded"] is True
        assert result["agent_id"] == "test"

    def test_enforce_reflects_settings(self) -> None:
        checkrd.init(
            agent_id="test",
            policy={"agent": "test", "default": "allow", "rules": []},
        )
        result = checkrd.healthy()
        assert result["enforce"] is True

    def test_control_plane_none_when_not_configured(self) -> None:
        checkrd.init(agent_id="test")
        result = checkrd.healthy()
        assert result["control_plane_connected"] is None


class TestHealthyDegradedMode:
    def test_degraded_status(self) -> None:
        """Degraded state is only reachable via opt-in permissive mode now.
        Strict (the default) raises instead — see tests/test_security_mode.py."""
        from unittest.mock import patch
        from checkrd.exceptions import CheckrdInitError

        with patch("checkrd.engine._get_module", side_effect=CheckrdInitError("missing")):
            checkrd.init(agent_id="test", security_mode="permissive")

        result = checkrd.healthy()
        assert result["status"] == "degraded"
        assert result["engine_loaded"] is False


class TestHealthyDisabledMode:
    def test_disabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_DISABLED", "1")
        checkrd.init(agent_id="test")
        result = checkrd.healthy()
        assert result["status"] == "disabled"


class TestLastEvalAt:
    def test_none_before_any_request(self) -> None:
        assert get_last_eval_at() is None
        result = checkrd.healthy()
        assert result["last_eval_at"] is None

    def test_populated_after_mock_request(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow")
            client.get("https://api.example.com")

            ts = get_last_eval_at()
            assert ts is not None
            assert "T" in ts  # ISO format

            result = checkrd.healthy()
            assert result["last_eval_at"] == ts
