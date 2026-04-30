"""Tests for checkrd._state and the checkrd.init()/shutdown() lifecycle.

These tests exercise the global context lifecycle in isolation from
httpx transports and integration instrumentors. End-to-end behavior
(init() + instrument() + real httpx traffic) is covered in
``tests/integrations/test_toplevel.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

import checkrd
from checkrd._state import (
    _GlobalContext,
    get_context,
    has_context,
    set_context,
)
from checkrd.exceptions import CheckrdInitError
from tests.conftest import requires_wasm


@pytest.fixture(autouse=True)
def _reset_global_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Clear the global Checkrd context around every test in this module.

    Without this, tests that leak a populated context would break tests
    that assume a fresh interpreter. ``checkrd.shutdown()`` is the
    supported way to clear state; call it before AND after every test so
    neighbor tests aren't affected even if the body raises.
    """
    # Isolate the default key path so auto-gen doesn't touch ~/.checkrd.
    monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
    # Clear env vars that might leak in from the shell.
    for var in (
        "CHECKRD_API_KEY",
        "CHECKRD_BASE_URL",
        "CHECKRD_AGENT_ID",
        "CHECKRD_ENFORCE",
        "CHECKRD_DISABLED",
    ):
        monkeypatch.delenv(var, raising=False)

    checkrd.shutdown()
    yield
    checkrd.shutdown()


# ============================================================
# get_context() / has_context() / set_context()
# ============================================================


class TestGlobalStateSemantics:
    """Verify _state uses module-level globals (not ContextVar).

    SDK configuration is process-global — ContextVar has per-context
    semantics that cause policy enforcement to silently vanish in async
    tasks or threads running with a fresh ``contextvars.Context``. The
    correct pattern (matching Sentry, Datadog) is true module globals
    for the client/config, with ContextVar reserved for per-request
    state like the current span or scope.
    """

    def test_global_context_is_not_contextvar(self) -> None:
        """The global context must be a plain module-level variable."""
        import contextvars
        from checkrd import _state
        assert not isinstance(_state._GLOBAL_CONTEXT, contextvars.ContextVar)

    def test_degraded_flag_is_not_contextvar(self) -> None:
        import contextvars
        from checkrd import _state
        assert not isinstance(_state._DEGRADED, contextvars.ContextVar)

    def test_last_eval_at_is_not_contextvar(self) -> None:
        import contextvars
        from checkrd import _state
        assert not isinstance(_state._LAST_EVAL_AT, contextvars.ContextVar)

    def test_set_and_get_round_trip(self) -> None:
        from checkrd._state import set_last_eval_at, get_last_eval_at
        set_last_eval_at("2026-04-13T00:00:00Z")
        assert get_last_eval_at() == "2026-04-13T00:00:00Z"

    def test_degraded_set_and_get(self) -> None:
        from checkrd._state import set_degraded, is_degraded
        set_degraded(True)
        assert is_degraded() is True
        set_degraded(False)
        assert is_degraded() is False


class TestPyTypedMarker:
    """PEP 561: py.typed marker file must exist for mypy/pyright consumers."""

    def test_py_typed_exists(self) -> None:
        from pathlib import Path
        marker = Path(__file__).parent.parent / "src" / "checkrd" / "py.typed"
        assert marker.exists(), f"py.typed marker not found at {marker}"


class TestDuplicateModulesRemoved:
    """_config.py and _exceptions.py were dead code duplicating config.py and exceptions.py."""

    def test_config_underscore_removed(self) -> None:
        with pytest.raises(ImportError):
            import checkrd._config  # noqa: F401

    def test_exceptions_underscore_removed(self) -> None:
        with pytest.raises(ImportError):
            import checkrd._exceptions  # noqa: F401


class TestRateLimitFilter:
    """Datadog-style log rate limiter: 1 message per N seconds per call site."""

    def test_first_message_always_passes(self) -> None:
        from checkrd._logging import RateLimitFilter

        f = RateLimitFilter(rate_limit_secs=60)
        record = logging.LogRecord("checkrd", logging.WARNING, "a.py", 10, "msg", (), None)
        assert f.filter(record) is True

    def test_second_message_within_window_suppressed(self) -> None:
        from checkrd._logging import RateLimitFilter

        f = RateLimitFilter(rate_limit_secs=60)
        r1 = logging.LogRecord("checkrd", logging.WARNING, "a.py", 10, "msg", (), None)
        r2 = logging.LogRecord("checkrd", logging.WARNING, "a.py", 10, "msg", (), None)
        assert f.filter(r1) is True
        assert f.filter(r2) is False  # suppressed

    def test_different_call_sites_independent(self) -> None:
        from checkrd._logging import RateLimitFilter

        f = RateLimitFilter(rate_limit_secs=60)
        r1 = logging.LogRecord("checkrd", logging.WARNING, "a.py", 10, "msg1", (), None)
        r2 = logging.LogRecord("checkrd", logging.WARNING, "b.py", 20, "msg2", (), None)
        assert f.filter(r1) is True
        assert f.filter(r2) is True  # different site

    def test_skip_count_appended_after_window(self) -> None:
        from checkrd._logging import RateLimitFilter
        import time

        f = RateLimitFilter(rate_limit_secs=0.05)  # 50ms for test speed
        r1 = logging.LogRecord("checkrd", logging.WARNING, "a.py", 10, "msg", (), None)
        assert f.filter(r1) is True

        # Suppress 3 messages
        for _ in range(3):
            r = logging.LogRecord("checkrd", logging.WARNING, "a.py", 10, "msg", (), None)
            assert f.filter(r) is False

        time.sleep(0.2)  # wait for window to expire (4x margin over 50ms window)

        r_next = logging.LogRecord("checkrd", logging.WARNING, "a.py", 10, "msg", (), None)
        assert f.filter(r_next) is True
        assert "[3 skipped]" in r_next.msg


class TestInitContextManager:
    """checkrd.init() returns a context manager for automatic cleanup."""

    @requires_wasm
    def test_context_manager_calls_shutdown(self) -> None:
        with checkrd.init(agent_id="cm-test"):
            assert has_context() is True
        # After exiting the context manager, shutdown was called
        assert has_context() is False

    @requires_wasm
    def test_context_manager_exception_still_shuts_down(self) -> None:
        with pytest.raises(ValueError, match="test"):
            with checkrd.init(agent_id="cm-exc"):
                assert has_context() is True
                raise ValueError("test")
        assert has_context() is False

    def test_init_return_value_usable_without_with(self) -> None:
        # init() still works as a plain call — the return value is ignored
        # (backward compatibility)
        result = checkrd.init(agent_id="plain-call")
        assert result is not None  # returns _InitContextManager
        checkrd.shutdown()


class TestNoThrowDecorator:
    """Public SDK methods must never crash the host application."""

    def test_shutdown_swallows_unexpected_errors(self) -> None:
        # Inject a broken context that raises on shutdown
        from checkrd._state import set_context, _GlobalContext
        from unittest.mock import MagicMock

        broken_ctx = MagicMock(spec=_GlobalContext)
        broken_ctx.shutdown.side_effect = RuntimeError("boom")
        set_context(broken_ctx)

        # shutdown() should not raise — @no_throw catches it
        checkrd.shutdown()

    def test_healthy_returns_error_dict_on_unexpected_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Monkey-patch is_degraded at the import site (checkrd.__init__) to raise
        def explode() -> bool:
            raise RuntimeError("unexpected")

        monkeypatch.setattr("checkrd.is_degraded", explode)
        result = checkrd.healthy()
        assert result["status"] == "error"
        assert result["engine_loaded"] is False

    def test_policy_denied_not_swallowed(self) -> None:
        """CheckrdPolicyDenied is a deliberate signal — it must propagate."""
        from checkrd.exceptions import CheckrdPolicyDenied

        @checkrd._no_throw()
        def raise_denied() -> None:
            raise CheckrdPolicyDenied(
                reason="test", request_id="req-1",
            )

        with pytest.raises(CheckrdPolicyDenied):
            raise_denied()

    def test_init_error_not_swallowed(self) -> None:
        """CheckrdInitError is a deliberate user-facing signal — it must propagate."""
        @checkrd._no_throw()
        def raise_init_error() -> None:
            raise CheckrdInitError("test init error")

        with pytest.raises(CheckrdInitError):
            raise_init_error()

    def test_runtime_error_is_swallowed(self) -> None:
        """Unexpected RuntimeError should be caught and return default."""
        @checkrd._no_throw(default="safe")
        def raise_runtime() -> str:
            raise RuntimeError("unexpected boom")

        assert raise_runtime() == "safe"


class TestGetContext:
    def test_raises_when_uninitialized(self) -> None:
        with pytest.raises(CheckrdInitError, match="init"):
            get_context()

    def test_has_context_false_when_uninitialized(self) -> None:
        assert has_context() is False

    def test_set_context_allows_get(self) -> None:
        ctx = MagicMock(spec=_GlobalContext)
        set_context(ctx)
        assert get_context() is ctx
        assert has_context() is True

    def test_set_none_clears_state(self) -> None:
        set_context(MagicMock(spec=_GlobalContext))
        set_context(None)
        assert has_context() is False
        with pytest.raises(CheckrdInitError):
            get_context()

    def test_error_message_is_actionable(self) -> None:
        # Users who forget init() should see a concrete next step, not a
        # bare stack trace. The message must mention init() AND wrap()
        # so both entry points are discoverable.
        try:
            get_context()
        except CheckrdInitError as exc:
            message = str(exc)
            assert "init()" in message
            assert "wrap(" in message
        else:
            pytest.fail("expected CheckrdInitError")


# ============================================================
# _GlobalContext.shutdown() contract
# ============================================================


class TestGlobalContextShutdown:
    """Every component shutdown is best-effort: a failure in one component
    must not prevent the others from being stopped. This matches the
    ``atexit`` contract where users need shutdown to be robust."""

    def _make_context(
        self,
        *,
        receiver=None,
        watchers=None,
        sink=None,
    ) -> _GlobalContext:
        return _GlobalContext(
            engine=MagicMock(),
            identity=MagicMock(),
            sink=sink,
            enforce=False,
            settings=MagicMock(),
            watchers=watchers or [],
            control_receiver=receiver,
        )

    def test_stops_control_receiver(self) -> None:
        receiver = MagicMock()
        ctx = self._make_context(receiver=receiver)
        ctx.shutdown()
        receiver.stop.assert_called_once()
        assert ctx.control_receiver is None

    def test_stops_watchers(self) -> None:
        w1, w2 = MagicMock(), MagicMock()
        ctx = self._make_context(watchers=[w1, w2])
        ctx.shutdown()
        w1.stop.assert_called_once()
        w2.stop.assert_called_once()
        assert ctx.watchers == []

    def test_stops_sink(self) -> None:
        sink = MagicMock()
        ctx = self._make_context(sink=sink)
        ctx.shutdown()
        sink.stop.assert_called_once()
        assert ctx.sink is None

    def test_shutdown_is_idempotent(self) -> None:
        receiver = MagicMock()
        sink = MagicMock()
        ctx = self._make_context(receiver=receiver, sink=sink)
        ctx.shutdown()
        ctx.shutdown()  # no-op second call
        # Each component stopped exactly once.
        receiver.stop.assert_called_once()
        sink.stop.assert_called_once()

    def test_receiver_failure_does_not_prevent_watcher_shutdown(self) -> None:
        # The critical resilience property: a raising component must not
        # leak the ones after it. atexit handlers can't retry, so partial
        # shutdown is much worse than a logged warning.
        bad_receiver = MagicMock()
        bad_receiver.stop.side_effect = RuntimeError("boom")
        good_watcher = MagicMock()
        ctx = self._make_context(
            receiver=bad_receiver, watchers=[good_watcher]
        )
        ctx.shutdown()
        good_watcher.stop.assert_called_once()

    def test_watcher_failure_does_not_prevent_sink_shutdown(self) -> None:
        bad_watcher = MagicMock()
        bad_watcher.stop.side_effect = RuntimeError("boom")
        good_sink = MagicMock()
        ctx = self._make_context(
            watchers=[bad_watcher], sink=good_sink
        )
        ctx.shutdown()
        good_sink.stop.assert_called_once()


# ============================================================
# checkrd.init() / checkrd.shutdown() end-to-end
# ============================================================


@requires_wasm
class TestInitShutdownLifecycle:
    """These tests need the real WASM engine because init() actually
    builds one. The :func:`_reset_global_state` fixture isolates them."""

    def test_init_populates_global_context(self) -> None:
        checkrd.init(agent_id="test-init")
        assert has_context() is True
        ctx = get_context()
        assert ctx.settings.agent_id == "test-init"
        assert ctx.engine is not None

    def test_init_default_enforces_engine_verdict(self) -> None:
        # Auto-mode trusts the engine: even without a constructor policy
        # the transport blocks on engine deny. The engine boots with a
        # default-allow observation policy, so this is safe — and once
        # a real policy is delivered (constructor / SSE / poll) the
        # engine's `mode` field handles dry-run-vs-enforce internally.
        # Mirrors OPA-PEP / Envoy ext_authz / Stripe Radar / AWS Config /
        # Cloudflare WAF: enforcement points trust their engine's verdict.
        checkrd.init(agent_id="auto-test")
        ctx = get_context()
        assert ctx.enforce is True

    def test_init_explicit_policy_enforces(self) -> None:
        checkrd.init(
            agent_id="enforce-test",
            policy={"agent": "enforce-test", "default": "allow", "rules": []},
        )
        ctx = get_context()
        assert ctx.enforce is True

    def test_init_explicit_enforce_false_observes(self) -> None:
        # Operator escape hatch: explicit `enforce=False` keeps the SDK in
        # observation mode regardless of policy presence. Useful for
        # phased rollouts where the operator wants to log denies for a
        # week before flipping to block.
        checkrd.init(
            agent_id="observe-test",
            policy={"agent": "observe-test", "default": "deny", "rules": []},
            enforce=False,
        )
        ctx = get_context()
        assert ctx.enforce is False

    def test_init_is_idempotent_swaps_context(self) -> None:
        # Second init() replaces the first context cleanly.
        checkrd.init(agent_id="first")
        first_ctx = get_context()
        checkrd.init(agent_id="second")
        second_ctx = get_context()
        assert first_ctx is not second_ctx
        assert second_ctx.settings.agent_id == "second"

    def test_init_respects_disabled_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHECKRD_DISABLED", "1")
        checkrd.init(agent_id="disabled")
        # Disabled init is a no-op: no context was set.
        assert has_context() is False

    def test_shutdown_clears_context(self) -> None:
        checkrd.init(agent_id="to-shutdown")
        assert has_context() is True
        checkrd.shutdown()
        assert has_context() is False

    def test_shutdown_without_init_is_noop(self) -> None:
        # Users call shutdown() in atexit / finally blocks; it must be
        # safe even if init() was never called.
        assert has_context() is False
        checkrd.shutdown()  # no exception
        assert has_context() is False

    def test_init_after_shutdown_works(self) -> None:
        checkrd.init(agent_id="first")
        checkrd.shutdown()
        checkrd.init(agent_id="second")
        ctx = get_context()
        assert ctx.settings.agent_id == "second"

    def test_get_context_post_shutdown_raises(self) -> None:
        checkrd.init(agent_id="temp")
        checkrd.shutdown()
        with pytest.raises(CheckrdInitError):
            get_context()

    def test_init_reads_env_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHECKRD_AGENT_ID", "from-env")
        checkrd.init()
        assert get_context().settings.agent_id == "from-env"

    def test_init_explicit_wins_over_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHECKRD_AGENT_ID", "from-env")
        checkrd.init(agent_id="explicit")
        assert get_context().settings.agent_id == "explicit"


@requires_wasm
class TestInitShutdownPreviousContextCleanup:
    """When init() is called with a live previous context, the previous
    context's ancillary resources must be stopped before the new one
    takes its place. Otherwise watcher threads and control receivers
    leak across re-initialization."""

    def test_re_init_stops_previous_watchers(
        self,
        tmp_path,
    ) -> None:
        # Start with a policy file watcher.
        policy_file = tmp_path / "p.yaml"
        policy_file.write_text("agent: a\ndefault: allow\nrules: []\n")
        checkrd.init(
            agent_id="first",
            policy=policy_file,
            policy_watch=True,
            policy_watch_interval_secs=0.1,
        )
        first_watchers = list(get_context().watchers)
        assert first_watchers, "expected a policy watcher on first init"

        # Re-init without watchers.
        checkrd.init(agent_id="second")

        # The first context's watcher should have been stopped.
        for w in first_watchers:
            assert w._stopped, "previous watcher should be stopped"
        # New context has no watchers.
        assert get_context().watchers == []
