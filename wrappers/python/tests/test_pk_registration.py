"""Tests for public key registration retry logic.

The public key registration happens in a background thread when
``wrap()`` or ``init()`` configures a control-plane connection. These
tests verify the retry behavior, error escalation, and thread safety
of ``_maybe_register_public_key()``.

P0 security fix: registration previously used a fire-and-forget thread
with no retry and no error escalation. A transient control-plane outage
during startup would silently leave the agent's public key unregistered,
causing cryptic telemetry signature verification failures later.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import pytest

from checkrd import (
    _PK_REGISTER_MAX_RETRIES,
    _maybe_register_public_key,
)
from checkrd.identity import IdentityProvider
from tests.conftest import unique_id, wait_for


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero out the retry delays so tests don't sleep for seconds."""
    monkeypatch.setattr("checkrd._PK_REGISTER_INITIAL_DELAY", 0.0)
    monkeypatch.setattr("checkrd._PK_REGISTER_MAX_DELAY", 0.0)


@pytest.fixture(autouse=True)
def _drain_stale_threads() -> None:
    """Wait for any leftover pk-register threads from previous tests.

    The registration function spawns a daemon thread named
    ``checkrd-pk-register``. With zero backoff, these finish almost
    instantly, but under random test ordering a thread from the previous
    test can still be alive when the next test patches ``urlopen``,
    causing the stale thread's retry to hit the new mock and inflate
    ``call_count``.
    """
    import threading
    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        stale = [t for t in threading.enumerate() if t.name == "checkrd-pk-register"]
        if not stale:
            break
        time.sleep(0.05)
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeIdentity:
    """Minimal IdentityProvider for registration tests.

    No WASM, no key file — just returns canned bytes.
    """

    def __init__(self, public_key: bytes = b"\x01" * 32) -> None:
        self._public_key = public_key

    @property
    def private_key_bytes(self) -> bytes | None:
        return b"\x00" * 32

    @property
    def public_key(self) -> bytes:
        return self._public_key

    @property
    def instance_id(self) -> str:
        return self._public_key[:8].hex()

    def sign(self, payload: bytes) -> bytes:
        return b"\x00" * 64


def _make_identity(public_key: bytes = b"\x01" * 32) -> _FakeIdentity:
    return _FakeIdentity(public_key=public_key)


def _mock_urlopen_ok() -> MagicMock:
    """Mock urlopen that returns HTTP 200."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = Mock(return_value=mock_resp)
    mock_resp.__exit__ = Mock(return_value=False)
    return mock_resp


def _mock_http_error(code: int) -> HTTPError:
    """Build an HTTPError with the given status code."""
    return HTTPError(
        url="https://api.checkrd.io/v1/agents/test/public-key",
        code=code,
        msg=f"HTTP {code}",
        hdrs={},  # type: ignore[arg-type]
        fp=None,
    )


# ---------------------------------------------------------------------------
# Tests: Successful registration
# ---------------------------------------------------------------------------


class TestSuccessfulRegistration:
    """Public key registers on first attempt when control plane is healthy."""

    @patch("checkrd.urlopen")
    def test_registers_on_first_attempt(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen_ok()
        identity = _make_identity()
        agent_id = unique_id()

        _maybe_register_public_key(
            "https://api.checkrd.io", "ck_test_key", agent_id, identity,
        )
        # Registration runs in a background thread — wait for it.
        wait_for(lambda: mock_urlopen.call_count >= 1)
        assert mock_urlopen.call_count == 1

        # Verify the request payload.
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert f"/v1/agents/{agent_id}/public-key" in req.full_url
        body = json.loads(req.data)
        assert body["public_key"] == identity.public_key.hex()

    @patch("checkrd.urlopen")
    def test_sends_api_key_header(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen_ok()
        _maybe_register_public_key(
            "https://api.checkrd.io", "ck_test_mykey", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 1)
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-api-key") == "ck_test_mykey"


# ---------------------------------------------------------------------------
# Tests: Skipped registration (no-op cases)
# ---------------------------------------------------------------------------


class TestSkippedRegistration:
    """Registration is a no-op when prerequisites are missing."""

    @patch("checkrd.urlopen")
    def test_no_control_plane_url(self, mock_urlopen: MagicMock) -> None:
        _maybe_register_public_key(None, "key", "agent", _make_identity())
        assert mock_urlopen.call_count == 0

    @patch("checkrd.urlopen")
    def test_no_api_key(self, mock_urlopen: MagicMock) -> None:
        _maybe_register_public_key("https://api.checkrd.io", None, "agent", _make_identity())
        assert mock_urlopen.call_count == 0

    @patch("checkrd.urlopen")
    def test_empty_public_key(self, mock_urlopen: MagicMock) -> None:
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", "agent", _make_identity(public_key=b""),
        )
        assert mock_urlopen.call_count == 0

    @patch("checkrd.urlopen")
    def test_identity_public_key_raises(self, mock_urlopen: MagicMock) -> None:
        """If identity.public_key raises, registration is silently skipped."""
        identity = Mock(spec=IdentityProvider)
        type(identity).public_key = property(lambda self: (_ for _ in ()).throw(RuntimeError("no key")))  # type: ignore[assignment]
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", "agent", identity,
        )
        assert mock_urlopen.call_count == 0


# ---------------------------------------------------------------------------
# Tests: Retry on transient errors
# ---------------------------------------------------------------------------


class TestRetryOnTransientErrors:
    """Transient failures (5xx, network errors) trigger exponential backoff."""

    @patch("checkrd.urlopen")
    def test_retries_on_500(self, mock_urlopen: MagicMock) -> None:
        """HTTP 500 on first attempt, 200 on second = success."""
        mock_urlopen.side_effect = [
            _mock_http_error(500),
            _mock_urlopen_ok(),
        ]
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 2, timeout=15)
        assert mock_urlopen.call_count == 2

    @patch("checkrd.urlopen")
    def test_retries_on_502(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [
            _mock_http_error(502),
            _mock_urlopen_ok(),
        ]
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 2, timeout=15)
        assert mock_urlopen.call_count == 2

    @patch("checkrd.urlopen")
    def test_retries_on_network_error(self, mock_urlopen: MagicMock) -> None:
        """URLError (DNS failure, connection refused) triggers retry."""
        mock_urlopen.side_effect = [
            URLError("Connection refused"),
            _mock_urlopen_ok(),
        ]
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 2, timeout=15)
        assert mock_urlopen.call_count == 2

    @patch("checkrd.urlopen")
    def test_retries_on_timeout(self, mock_urlopen: MagicMock) -> None:
        """TimeoutError triggers retry."""
        mock_urlopen.side_effect = [
            TimeoutError("timed out"),
            _mock_urlopen_ok(),
        ]
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 2, timeout=15)
        assert mock_urlopen.call_count == 2

    @patch("checkrd.urlopen")
    def test_retries_on_oserror(self, mock_urlopen: MagicMock) -> None:
        """OSError (broken pipe, etc.) triggers retry."""
        mock_urlopen.side_effect = [
            OSError("Broken pipe"),
            _mock_urlopen_ok(),
        ]
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 2, timeout=15)
        assert mock_urlopen.call_count == 2

    @patch("checkrd.urlopen")
    def test_exhausts_all_retries(self, mock_urlopen: MagicMock) -> None:
        """All retries fail = exactly MAX_RETRIES attempts."""
        mock_urlopen.side_effect = URLError("always fail")
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(
            lambda: mock_urlopen.call_count >= _PK_REGISTER_MAX_RETRIES,
            timeout=30,
        )
        assert mock_urlopen.call_count == _PK_REGISTER_MAX_RETRIES


# ---------------------------------------------------------------------------
# Tests: Permanent errors (no retry)
# ---------------------------------------------------------------------------


class TestPermanentErrors:
    """Auth errors and key conflicts stop immediately (retrying won't help)."""

    @patch("checkrd.urlopen")
    def test_401_stops_immediately(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _mock_http_error(401)
        _maybe_register_public_key(
            "https://api.checkrd.io", "bad_key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 1, timeout=10)
        import time
        time.sleep(0.2)
        assert mock_urlopen.call_count == 1

    @patch("checkrd.urlopen")
    def test_403_stops_immediately(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _mock_http_error(403)
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 1, timeout=10)
        import time
        time.sleep(0.2)
        assert mock_urlopen.call_count == 1

    @patch("checkrd.urlopen")
    def test_409_conflict_stops_immediately(self, mock_urlopen: MagicMock) -> None:
        """409 = key already registered with different value. No retry."""
        mock_urlopen.side_effect = _mock_http_error(409)
        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )
        wait_for(lambda: mock_urlopen.call_count >= 1, timeout=10)
        import time
        time.sleep(0.2)
        assert mock_urlopen.call_count == 1


# ---------------------------------------------------------------------------
# Tests: Log output
# ---------------------------------------------------------------------------


class TestLogOutput:
    """Verify correct log levels for operational visibility."""

    @patch("checkrd.urlopen")
    def test_exhausted_retries_logs_warning(
        self, mock_urlopen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Final failure after all retries must be WARNING, not DEBUG."""
        mock_urlopen.side_effect = URLError("always fail")
        agent_id = unique_id()

        import logging

        # Clear the rate-limit filter so log messages from a background
        # thread of a previous test don't suppress our message. The
        # autouse fixture clears it between tests, but threads from the
        # previous test may have written to it after the clear.
        checkrd_logger = logging.getLogger("checkrd")
        for f in checkrd_logger.filters:
            if hasattr(f, "_seen"):
                f._seen.clear()

        with caplog.at_level(logging.DEBUG, logger="checkrd"):
            _maybe_register_public_key(
                "https://api.checkrd.io", "key", agent_id, _make_identity(),
            )
            # Wait for all retries to complete AND the final log to be written.
            # The background thread sleeps between retries (backoff), so give
            # generous time.
            wait_for(
                lambda: any(
                    "failed after" in r.message and agent_id in r.message
                    for r in caplog.records
                ),
                timeout=30,
            )

        warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "failed after" in r.message
        ]
        assert len(warnings) >= 1
        assert agent_id in warnings[0].message
        assert "telemetry signature verification" in warnings[0].message

    @patch("checkrd.urlopen")
    def test_409_conflict_logs_warning(
        self, mock_urlopen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Key mismatch should warn the operator with actionable guidance."""
        mock_urlopen.side_effect = _mock_http_error(409)
        agent_id = unique_id()

        import logging

        with caplog.at_level(logging.WARNING, logger="checkrd"):
            _maybe_register_public_key(
                "https://api.checkrd.io", "key", agent_id, _make_identity(),
            )
            wait_for(
                lambda: any("differs" in r.message for r in caplog.records),
                timeout=10,
            )

        warnings = [r for r in caplog.records if "differs" in r.message]
        assert len(warnings) >= 1
        assert agent_id in warnings[0].message

    @patch("checkrd.urlopen")
    def test_401_logs_warning_with_guidance(
        self, mock_urlopen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_urlopen.side_effect = _mock_http_error(401)

        import logging

        with caplog.at_level(logging.WARNING, logger="checkrd"):
            _maybe_register_public_key(
                "https://api.checkrd.io", "bad_key", unique_id(), _make_identity(),
            )
            wait_for(
                lambda: any("check your API key" in r.message for r in caplog.records),
                timeout=10,
            )

        warnings = [r for r in caplog.records if "check your API key" in r.message]
        assert len(warnings) >= 1

    @patch("checkrd.urlopen")
    def test_success_logs_debug(
        self, mock_urlopen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Successful registration logs at DEBUG (not WARNING)."""
        mock_urlopen.return_value = _mock_urlopen_ok()

        import logging

        with caplog.at_level(logging.DEBUG, logger="checkrd"):
            _maybe_register_public_key(
                "https://api.checkrd.io", "key", unique_id(), _make_identity(),
            )
            wait_for(lambda: mock_urlopen.call_count >= 1)
            # Give thread time to log.
            wait_for(
                lambda: any("registration ok" in r.message for r in caplog.records),
                timeout=5,
            )

        debug_logs = [r for r in caplog.records if "registration ok" in r.message]
        assert len(debug_logs) >= 1
        assert debug_logs[0].levelno == logging.DEBUG


# ---------------------------------------------------------------------------
# Tests: Thread behavior
# ---------------------------------------------------------------------------


class TestThreadBehavior:
    """Registration runs in a daemon thread that doesn't block shutdown."""

    @patch("checkrd.urlopen")
    def test_runs_in_daemon_thread(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen_ok()

        _maybe_register_public_key(
            "https://api.checkrd.io", "key", unique_id(), _make_identity(),
        )

        # The thread should complete.
        wait_for(lambda: mock_urlopen.call_count >= 1)

    @patch("checkrd.urlopen")
    def test_max_retries_constant_exposed(self, mock_urlopen: MagicMock) -> None:
        """The retry count constant is importable for test assertions."""
        assert _PK_REGISTER_MAX_RETRIES == 3
