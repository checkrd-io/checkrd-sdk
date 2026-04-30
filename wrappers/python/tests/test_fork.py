"""Tests for ``checkrd._fork`` — the fork-safety registration helper.

The end-to-end integration test (parent forks → child handler resets
batcher state) lives in ``test_p1_fixes.py``. These tests cover the
helper's contract in isolation so regressions in the registration
logic are caught without paying the cost of a real fork.
"""

from __future__ import annotations

import os
import weakref
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class _Resettable:
    """Test fixture: holds a counter that the handler increments."""

    def __init__(self) -> None:
        self.reset_calls = 0
        self.last_pid: int | None = None

    def _reinit_after_fork(self) -> None:
        self.reset_calls += 1
        self.last_pid = os.getpid()


class TestRegisterForkHandler:
    """Behavioral contract for ``register_fork_handler``."""

    def test_returns_true_when_register_at_fork_is_available(self) -> None:
        from checkrd._fork import register_fork_handler

        registry: weakref.WeakSet[Any] = weakref.WeakSet()
        # Inject a no-op replacement so we don't actually register a
        # handler that survives the test.
        with patch("os.register_at_fork") as mock_reg:
            ok = register_fork_handler(registry, "_reinit_after_fork", "test")
        assert ok is True
        mock_reg.assert_called_once()

    def test_returns_false_when_register_at_fork_is_missing(self) -> None:
        # Simulates Windows, where ``os.register_at_fork`` does not exist.
        # Multiprocessing on Windows uses ``spawn``, which gives the
        # child a fresh module load; no fork handler is needed there.
        from checkrd import _fork

        registry: weakref.WeakSet[Any] = weakref.WeakSet()
        with patch.object(os, "register_at_fork", create=False) as _:
            # Remove the attribute via getattr fallback path.
            with patch.object(
                _fork, "os", MagicMock(spec=["getpid"]),  # has getpid, not register_at_fork
            ):
                ok = _fork.register_fork_handler(
                    registry, "_reinit_after_fork", "test",
                )
        assert ok is False

    def test_handler_invokes_reset_on_every_live_instance(self) -> None:
        """When the handler fires, it must walk the WeakSet and call
        ``reset_method`` on every entry. Multiple instances added to
        the same registry all get reset."""
        from checkrd._fork import register_fork_handler

        registry: weakref.WeakSet[Any] = weakref.WeakSet()
        a = _Resettable()
        b = _Resettable()
        c = _Resettable()
        registry.add(a)
        registry.add(b)
        registry.add(c)

        captured_handler = []
        with patch("os.register_at_fork") as mock_reg:
            mock_reg.side_effect = lambda after_in_child: captured_handler.append(
                after_in_child
            )
            register_fork_handler(registry, "_reinit_after_fork", "test")

        assert len(captured_handler) == 1
        # Invoke the captured handler — same effect as a real fork.
        captured_handler[0]()
        assert a.reset_calls == 1
        assert b.reset_calls == 1
        assert c.reset_calls == 1

    def test_handler_continues_after_one_instance_raises(self) -> None:
        """A reset that raises must not block the other instances from
        running. The child process is about to run user code; partial
        recovery is better than none."""
        from checkrd._fork import register_fork_handler

        class Angry:
            def _reinit_after_fork(self) -> None:
                raise RuntimeError("boom")

        registry: weakref.WeakSet[Any] = weakref.WeakSet()
        good = _Resettable()
        angry = Angry()
        # Order matters: place the angry one BEFORE the good one so a
        # naive "break on first exception" implementation would skip
        # the good one.
        registry.add(angry)
        registry.add(good)

        captured = []
        with patch("os.register_at_fork") as mock_reg:
            mock_reg.side_effect = lambda after_in_child: captured.append(
                after_in_child
            )
            register_fork_handler(registry, "_reinit_after_fork", "test")

        captured[0]()
        # The good instance got its reset despite the angry one's throw.
        assert good.reset_calls == 1

    def test_handler_logs_when_reset_method_is_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If an instance somehow lacks the named method, log it
        rather than raising. The fork-safety contract is best-effort —
        a malformed instance must not crash the child process."""
        import logging

        from checkrd._fork import register_fork_handler

        class NoMethod:
            pass

        registry: weakref.WeakSet[Any] = weakref.WeakSet()
        # Hold a strong reference so the instance survives until we
        # invoke the captured handler — WeakSet alone would let GC
        # reap it before the assertion runs.
        instance = NoMethod()
        registry.add(instance)

        captured = []
        with patch("os.register_at_fork") as mock_reg:
            mock_reg.side_effect = lambda after_in_child: captured.append(
                after_in_child
            )
            register_fork_handler(registry, "missing_method", "test")

        with caplog.at_level(logging.ERROR, logger="checkrd"):
            captured[0]()
        # Error-logged but did not raise. Use ``getMessage()`` (not
        # ``.message``) — the latter is only populated after the record
        # is formatted, which doesn't happen for caplog-attached records
        # under the parallel xdist workers.
        assert any(
            "missing_method" in r.getMessage() for r in caplog.records
        )

    def test_weakset_does_not_keep_instances_alive(self) -> None:
        """The registry holds instances weakly so a closed batcher
        doesn't leak memory or block GC. Once strong refs disappear,
        the entry leaves the set automatically."""
        from checkrd._fork import register_fork_handler  # noqa: F401

        registry: weakref.WeakSet[Any] = weakref.WeakSet()
        instance: _Resettable | None = _Resettable()
        registry.add(instance)
        assert len(registry) == 1
        instance = None  # drop the strong ref
        # GC may need to run explicitly under PyPy / older CPython; the
        # CPython default refcount behavior reaps immediately.
        import gc
        gc.collect()
        assert len(registry) == 0
