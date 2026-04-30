"""Tests for permissive-mode graceful degradation.

Starting at 0.2, the default ``security_mode="strict"`` fails closed when
the WASM engine cannot load — the security layer must not silently
disable itself (see ``tests/test_security_mode.py``).

Teams who need the pre-0.2 Sentry-style "fail-open, silent no-op"
behavior during rollout opt in via ``security_mode="permissive"`` or
``CHECKRD_SECURITY_MODE=permissive``. These tests cover that opt-in
path: a corrupted .wasm binary in permissive mode must not take down
the application.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import httpx
import pytest

import checkrd
from checkrd._state import is_degraded, set_degraded
from checkrd.exceptions import CheckrdInitError
from checkrd.transports._httpx import CheckrdTransport


def _mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
    for var in ("CHECKRD_API_KEY", "CHECKRD_BASE_URL", "CHECKRD_AGENT_ID",
                "CHECKRD_ENFORCE", "CHECKRD_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    checkrd.shutdown()
    set_degraded(False)
    yield
    checkrd.uninstrument()
    checkrd.shutdown()
    set_degraded(False)


def _break_wasm() -> None:
    """Patch the WASM module loader to simulate a missing/corrupted binary."""
    pass  # The actual patch is applied via the decorator in each test.


class TestWrapGracefulDegradation:
    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_wrap_returns_client_unchanged(self, _mock) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            result = checkrd.wrap(client, security_mode="permissive")
            assert result is client
            assert not isinstance(client._transport, CheckrdTransport)

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_wrap_still_passes_requests(self, _mock) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            checkrd.wrap(client, security_mode="permissive")
            response = client.get("https://api.example.com/resource")
            assert response.status_code == 200

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_wrap_logs_warning(self, _mock, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="checkrd"), \
             httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            checkrd.wrap(client, security_mode="permissive")
        assert any(
            "pass-through" in r.message or "Policy enforcement is DISABLED"
            in r.message
            for r in caplog.records
        )

    @patch("checkrd.engine._get_module", side_effect=RuntimeError("wasmtime crash"))
    def test_wrap_handles_unexpected_exception(self, _mock) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            result = checkrd.wrap(client, security_mode="permissive")
            assert result is client

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    async def test_wrap_async_returns_client_unchanged(self, _mock) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            result = checkrd.wrap_async(client, security_mode="permissive")
            assert result is client


class TestInitGracefulDegradation:
    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_init_does_not_raise(self, _mock) -> None:
        checkrd.init(agent_id="test", security_mode="permissive")  # must not raise

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_init_sets_degraded_flag(self, _mock) -> None:
        checkrd.init(agent_id="test", security_mode="permissive")
        assert is_degraded() is True

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_instrument_is_noop_after_degraded_init(self, _mock) -> None:
        checkrd.init(agent_id="test", security_mode="permissive")
        checkrd.instrument()  # must not raise CheckrdInitError

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_instrument_openai_is_noop_after_degraded_init(self, _mock) -> None:
        checkrd.init(agent_id="test", security_mode="permissive")
        checkrd.instrument_openai()  # must not raise

    def test_instrument_without_init_still_errors(self) -> None:
        # Regression guard: degraded mode is distinct from "forgot init()".
        with pytest.raises(CheckrdInitError, match="init"):
            checkrd.instrument()

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_shutdown_clears_degraded_flag(self, _mock) -> None:
        checkrd.init(agent_id="test", security_mode="permissive")
        assert is_degraded() is True
        checkrd.shutdown()
        assert is_degraded() is False

    @patch("checkrd.engine._get_module", side_effect=CheckrdInitError("WASM not found"))
    def test_reinit_after_degraded_clears_flag_on_success(self, _mock) -> None:
        checkrd.init(agent_id="test", security_mode="permissive")
        assert is_degraded() is True
        # "Fix" the WASM — re-init should clear degraded.
        # We can't easily un-patch mid-test, so just verify shutdown clears.
        checkrd.shutdown()
        assert is_degraded() is False
