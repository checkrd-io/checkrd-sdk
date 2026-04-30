"""Tests for the one-time PII-risk banner fired when debug logging is on.

When ``CHECKRD_DEBUG=1`` or ``debug=True`` is passed to any Checkrd
entry point, the SDK emits a loud stderr banner the first time per
process. The banner exists because Checkrd sits in the request path
for LLM agent traffic — debug logs here can contain prompt payloads
(customer data), which most teams don't expect to find in their log
aggregator.

The banner must:
  - fire once per process (no spam on repeated init/wrap calls)
  - go to stderr, not the checkrd logger (the logger may be routed
    to a destination the operator isn't actively watching)
  - include the literal strings ``CHECKRD_DEBUG`` and ``production``
    so an operator searching logs for either can find it
  - NOT fire when debug is disabled

Parallel to the JS SDK's ``tests/debug_pii_warning.test.ts``.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr

import pytest

from checkrd._logging import (
    _reset_debug_warning_for_testing,
    warn_debug_pii_risk,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    """Reset the one-shot guard between tests so each test is isolated."""
    _reset_debug_warning_for_testing()


class TestWarnDebugPiiRisk:
    def test_writes_to_stderr(self) -> None:
        """The banner must go to stderr, not stdout, not the logger."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            warn_debug_pii_risk()
        output = buf.getvalue()
        assert output != ""

    def test_mentions_checkrd_debug_env_var(self) -> None:
        """Banner names the env var operators might need to unset."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            warn_debug_pii_risk()
        assert "CHECKRD_DEBUG" in buf.getvalue()

    def test_mentions_production(self) -> None:
        """Banner explicitly warns about production use."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            warn_debug_pii_risk()
        assert "production" in buf.getvalue().lower()

    def test_fires_once_per_process_by_default(self) -> None:
        """Repeated calls must not spam — operator should see it once."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            warn_debug_pii_risk()
            warn_debug_pii_risk()
            warn_debug_pii_risk()
        # Count occurrences of the banner's signature opening line.
        output = buf.getvalue()
        assert output.count("DEBUG logging is enabled") == 1

    def test_once_false_bypasses_guard(self) -> None:
        """`once=False` is a testing escape hatch — each call fires."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            warn_debug_pii_risk(once=False)
            warn_debug_pii_risk(once=False)
        output = buf.getvalue()
        assert output.count("DEBUG logging is enabled") == 2

    def test_reset_helper_re_arms_the_guard(self) -> None:
        """`_reset_debug_warning_for_testing` re-arms the one-shot.

        Test-only hook — lets tests verify fire-once semantics without
        relying on test ordering or inter-test state leakage.
        """
        buf = io.StringIO()
        with redirect_stderr(buf):
            warn_debug_pii_risk()  # fire
            warn_debug_pii_risk()  # suppressed (guard engaged)
            _reset_debug_warning_for_testing()
            warn_debug_pii_risk()  # fires again (guard cleared)
        output = buf.getvalue()
        assert output.count("DEBUG logging is enabled") == 2


class TestBuildRuntimeFiresWarning:
    """Integration test — `_build_runtime` fires the banner when debug=True."""

    def test_debug_true_fires_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A wrap() / init() call with debug=True emits the banner once.

        We exercise `_build_runtime` directly because it's the single
        point where both paths converge. The test isolates the banner
        behavior without needing a full engine / httpx.Client stack.
        """
        from checkrd import _build_runtime

        # Ensure no env vars influence the test.
        monkeypatch.delenv("CHECKRD_DEBUG", raising=False)
        monkeypatch.delenv("CHECKRD_DISABLED", raising=False)
        monkeypatch.setenv("CHECKRD_DISABLED", "1")  # short-circuit, avoid WASM

        _build_runtime(
            agent_id="test-agent",
            policy=None,
            identity=None,
            enforce="auto",
            control_plane_url=None,
            api_key=None,
            telemetry_sink=None,
            debug=True,
        )
        captured = capsys.readouterr()
        assert "DEBUG logging is enabled" in captured.err

    def test_debug_false_does_not_fire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Without debug, the banner stays silent."""
        from checkrd import _build_runtime

        monkeypatch.delenv("CHECKRD_DEBUG", raising=False)
        monkeypatch.setenv("CHECKRD_DISABLED", "1")

        _build_runtime(
            agent_id="test-agent",
            policy=None,
            identity=None,
            enforce="auto",
            control_plane_url=None,
            api_key=None,
            telemetry_sink=None,
            debug=False,
        )
        captured = capsys.readouterr()
        assert "DEBUG logging is enabled" not in captured.err

    def test_checkrd_debug_env_var_fires_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`CHECKRD_DEBUG=1` env var is equivalent to `debug=True`."""
        from checkrd import _build_runtime

        monkeypatch.setenv("CHECKRD_DEBUG", "1")
        monkeypatch.setenv("CHECKRD_DISABLED", "1")

        _build_runtime(
            agent_id="test-agent",
            policy=None,
            identity=None,
            enforce="auto",
            control_plane_url=None,
            api_key=None,
            telemetry_sink=None,
            debug=False,  # env wins
        )
        captured = capsys.readouterr()
        assert "DEBUG logging is enabled" in captured.err
